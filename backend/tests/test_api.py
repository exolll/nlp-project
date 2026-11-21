"""API contract tests against real durable local storage, with no database."""
import hashlib
import io
from datetime import timedelta
from uuid import uuid4

import fitz
import pytest
from docx import Document as WordDocument
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.main import create_app
from app.persistence.input_state import InputState, utcnow
from app.services import processing
from app.services.parsing import ParseError, parse_content
from app.services.storage import LocalStorage, StorageError


@pytest.fixture
def api(monkeypatch, tmp_path):
    settings = Settings(environment='test', max_request_bytes=100000, write_requests_per_minute=100,
                        local_storage_directory=tmp_path, _env_file=None)
    storage = LocalStorage(tmp_path)
    app = create_app(settings, storage)
    monkeypatch.setattr(processing, 'parse_isolated', lambda data, filename, text, settings: parse_content(data, filename, text, settings.parser_limits()))
    with TestClient(app) as client:
        yield client, app, InputState(storage, settings), storage


def auth(client):
    response = client.post('/api/v1/sessions', json={})
    assert response.status_code == 201, response.text
    return {'Authorization': 'Bearer ' + response.json()['access_token']}


def owner(headers):
    return hashlib.sha256(headers['Authorization'].split()[1].encode()).hexdigest()


def task(client, headers):
    response = client.post('/api/v1/tasks', headers=headers, json={})
    assert response.status_code == 201
    return response.json()['task_id']


def submit(client, headers, task_id, text='正文', file=None, key=None):
    fields = [('text', (None, text))]
    if file:
        fields.append(('file', file))
    return client.post(f'/api/v1/tasks/{task_id}/input', headers={**headers, 'Idempotency-Key': key or str(uuid4())}, files=fields)


def sample_file(mode):
    if mode.startswith('pdf'):
        with fitz.open() as pdf:
            page = pdf.new_page()
            page.insert_text((50, 50), 'Assignment requirements')
            return ('assignment.pdf', pdf.tobytes(), 'application/pdf')
    stream = io.BytesIO()
    doc = WordDocument()
    doc.add_paragraph('Assignment requirements')
    doc.save(stream)
    return ('assignment.docx', stream.getvalue(), 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')


@pytest.mark.parametrize('mode', ['text', 'pdf', 'docx', 'pdf_text', 'docx_text'])
def test_five_modes_and_restart(api, mode):
    client, _, state, storage = api
    headers = auth(client)
    tid = task(client, headers)
    response = submit(client, headers, tid, text='测试正文' if mode == 'text' or mode.endswith('_text') else '', file=None if mode == 'text' else sample_file(mode))
    assert response.status_code == 202, response.text
    result = client.get(f'/api/v1/tasks/{tid}/input', headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()['input_mode'] == mode
    session = state.session(owner(headers))
    assert session['tasks'][tid]['document']['result'] == result.json()
    raw = storage.get(state.session_key(owner(headers)))
    assert headers['Authorization'].split()[1].encode() not in raw
    if mode != 'text':
        key = session['tasks'][tid]['document']['object_key']
        assert key.startswith('originals/') and 'assignment' not in key
        assert storage.get(key)
    with TestClient(create_app(state.settings, LocalStorage(storage.directory))) as restarted:
        assert restarted.get(f'/api/v1/tasks/{tid}/input', headers=headers).json() == result.json()
        assert restarted.get('/api/v1/tasks', headers=headers).json()['tasks'][0]['status'] == 'completed'


def test_ownership_idempotency(api):
    client, _, _, _ = api
    first, other = auth(client), auth(client)
    tid, key = task(client, first), str(uuid4())
    assert submit(client, first, tid, key=key).status_code == 202
    assert submit(client, first, tid, key=key).json()['status'] == 'completed'
    assert submit(client, first, tid, text='不同', key=key).status_code == 409
    assert submit(client, first, tid).status_code == 409
    for path in ['input', 'input/status']:
        assert client.get(f'/api/v1/tasks/{tid}/{path}', headers=other).status_code == 404
        assert client.get(f'/api/v1/tasks/{uuid4()}/{path}', headers=other).status_code == 404
    assert submit(client, other, tid).status_code == 404
    assert client.get('/api/v1/tasks', headers=other).json() == {'tasks': []}
    assert client.get('/api/v1/tasks').status_code == 401
    assert client.post('/api/v1/tasks', headers=first, json={'owner': owner(other)}).status_code == 422


def test_body_and_multipart_limits(api):
    client, _, _, _ = api
    headers = auth(client)
    tid = task(client, headers)
    url = f'/api/v1/tasks/{tid}/input'
    headers['Idempotency-Key'] = str(uuid4())
    response = client.post(url, headers=headers, content=b'x' * 100001)
    assert response.status_code == 413
    assert response.json()['request_id'] == response.headers['x-request-id']
    for fields in [[('file', ('a.pdf', b'a')), ('file', ('b.pdf', b'b'))],
                   [('other', (None, 'x'))], [('text', (None, 'a')), ('text', (None, 'b'))],
                   [('text', ('a.pdf', b'a'))]]:
        assert client.post(url, headers=headers, files=fields).status_code == 422
    assert client.post(url, headers={**headers, 'Idempotency-Key': 'bad'}, files={'text': (None, 'abc')}).status_code == 422


def test_timeout_recovery_and_concurrency_limit(api):
    client, app, state, _ = api
    app.state.processor = lambda *args: None
    headers = auth(client)
    ids = [task(client, headers) for _ in range(3)]
    assert submit(client, headers, ids[0]).status_code == 202
    assert submit(client, headers, ids[1]).status_code == 202
    assert submit(client, headers, ids[2]).status_code == 429
    def age(session):
        session['tasks'][ids[0]]['document']['created_at'] = (utcnow() - timedelta(minutes=5)).isoformat()
    state.session(owner(headers), age)
    with TestClient(create_app(state.settings)) as restarted:
        assert restarted.get(f'/api/v1/tasks/{ids[0]}/input/status', headers=headers).json()['status'] == 'failed'
        assert restarted.get(f'/api/v1/tasks/{ids[1]}/input/status', headers=headers).json()['status'] == 'processing'
    assert submit(client, headers, ids[2]).status_code == 202


def test_safe_parser_error(api):
    client, _, _, _ = api
    headers = auth(client)
    tid = task(client, headers)
    assert submit(client, headers, tid, file=('bad.pdf', b'bad')).status_code == 202
    result = client.get(f'/api/v1/tasks/{tid}/input/status', headers=headers).json()
    assert result['status'] == 'failed'
    assert result['error']['error_code'] == 'FILE_SIGNATURE_MISMATCH'


@pytest.mark.parametrize('commit', [False, True])
def test_unknown_claim_write_retains_original(api, commit):
    client, app, state, storage = api
    headers = auth(client)
    tid = task(client, headers)
    class Uncertain(LocalStorage):
        calls = 0
        def compare_and_swap(self, key, expected, data):
            if key.startswith('sessions/'):
                self.calls += 1
                if commit:
                    super().compare_and_swap(key, expected, data)
                raise StorageError('secret provider detail')
            return super().compare_and_swap(key, expected, data)
    broken = Uncertain(storage.directory)
    app.state.storage = broken
    response = submit(client, headers, tid, file=sample_file('pdf'))
    assert response.status_code == 503
    assert 'secret' not in response.text
    assert broken.calls == 1
    originals = [p for p in (storage.directory / 'originals').iterdir() if not p.name.endswith('.lock')]
    assert len(originals) == 1
    app.state.storage = storage
    current = state.session(owner(headers))['tasks'][tid]
    assert current['status'] == ('processing' if commit else 'empty')
    if commit:
        assert storage.get(current['document']['object_key'])


@pytest.mark.parametrize('changed', ['bytes', 'filename'])
def test_file_identity(api, changed):
    client, _, _, _ = api
    headers = auth(client)
    tid, key = task(client, headers), str(uuid4())
    assert submit(client, headers, tid, key=key, file=('a.pdf', b'first')).status_code == 202
    file = ('a.pdf', b'second') if changed == 'bytes' else ('a.docx', b'first')
    assert submit(client, headers, tid, key=key, file=file).status_code == 409


def test_expired_session_and_configurable_lifetime(api):
    _, _, state, storage = api
    settings = state.settings.model_copy(update={'session_hours': 2})
    with TestClient(create_app(settings, storage)) as client:
        headers = auth(client)
        session = state.session(owner(headers))
        from datetime import datetime
        assert datetime.fromisoformat(session['expires_at']) - datetime.fromisoformat(session['created_at']) == timedelta(hours=2)
        state.session(owner(headers), lambda value: value.update(expires_at=(utcnow() - timedelta(seconds=1)).isoformat()))
        assert client.get('/api/v1/tasks', headers=headers).status_code == 401


def test_persistent_rates_and_task_cap(api):
    _, _, state, storage = api
    settings = state.settings.model_copy(update={'write_requests_per_minute': 1, 'session_requests_per_minute': 1})
    with TestClient(create_app(settings, storage)) as client:
        headers = auth(client)
        task(client, headers)
    with TestClient(create_app(settings, LocalStorage(storage.directory))) as client:
        assert client.post('/api/v1/sessions', json={}).status_code == 429
        assert client.post('/api/v1/tasks', headers=headers, json={}).status_code == 429
    state.session(owner(headers), lambda value: value.update(rate={}))
    with TestClient(create_app(settings.model_copy(update={'max_session_tasks': 1}), storage)) as client:
        response = client.post('/api/v1/tasks', headers=headers, json={})
        assert response.status_code == 429 and response.json()['error_code'] == 'TASK_LIMIT'


def test_real_subprocess_large_result():
    result = processing.parse_isolated(None, None, '大文本' * 9000, Settings(parse_timeout_seconds=20, _env_file=None))
    assert result['character_count'] == 27000


def test_real_subprocess_timeout():
    with pytest.raises(ParseError, match='超时'):
        processing.parse_isolated(None, None, '正文', Settings(parse_timeout_seconds=0.000001, _env_file=None))

import io
import json
import multiprocessing as mp
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.main import create_app
from app.persistence.input_state import InputState, utcnow
from app.services.storage import CASConflict, LocalStorage, S3Storage, StorageError
from test_api import auth, task, submit, sample_file, owner


def increment(directory, count):
    state = InputState(LocalStorage(directory), Settings(_env_file=None))
    for _ in range(count):
        state.update('counter.json', lambda value: ({'count': (value or {}).get('count', 0) + 1}, None))


def test_cross_process_cas(tmp_path):
    context = mp.get_context('spawn')
    processes = [context.Process(target=increment, args=(tmp_path, 20)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
        process.close()
    assert json.loads(LocalStorage(tmp_path).get('counter.json'))['count'] == 40


def test_concurrent_ip_rate_is_atomic(tmp_path):
    from app.persistence.input_state import APIError
    settings = Settings(session_requests_per_minute=1, _env_file=None)
    barrier = Barrier(2)
    def attempt(_):
        store = InputState(LocalStorage(tmp_path), settings)
        barrier.wait(timeout=5)
        try:
            store.ip_rate('127.0.0.1')
            return 201
        except APIError as error:
            return error.status
    with ThreadPoolExecutor(2) as executor:
        assert sorted(executor.map(attempt, range(2))) == [201, 429]
    assert all('127.0.0.1' not in p.name for p in (tmp_path / 'rates').iterdir())


def test_local_replace_failure_preserves_previous(tmp_path, monkeypatch):
    storage = LocalStorage(tmp_path)
    storage.put('state.json', b'old', 'application/json')
    _, version = storage.read_version('state.json')
    def reject(*args):
        raise OSError('simulated failure')
    monkeypatch.setattr('app.services.storage.os.replace', reject)
    with pytest.raises(StorageError):
        storage.compare_and_swap('state.json', version, b'new')
    assert storage.get('state.json') == b'old'
    assert not list(tmp_path.glob('.pending-*'))
    with pytest.raises(CASConflict):
        storage.compare_and_swap('state.json', 'wrong', b'new')


@pytest.mark.parametrize('scenario', ['same', 'conflict', 'limit'])
def test_independent_adapters_claim_race(tmp_path, scenario):
    settings = Settings(local_storage_directory=tmp_path, write_requests_per_minute=100, max_processing_tasks=1, _env_file=None)
    barrier = Barrier(2)
    class RacingStorage(LocalStorage):
        def put(self, *args):
            super().put(*args)
            barrier.wait(timeout=10)
    apps = [create_app(settings, RacingStorage(tmp_path)) for _ in range(2)]
    for app in apps:
        app.state.processor = lambda *args: None
    with TestClient(apps[0]) as first, TestClient(apps[1]) as second:
        headers = auth(first)
        tid = task(first, headers)
        from uuid import uuid4
        key = str(uuid4())
        payload = sample_file('pdf')
        other_tid = task(first, headers) if scenario == 'limit' else tid
        with ThreadPoolExecutor(2) as executor:
            futures = [executor.submit(submit, first, headers, tid, key=key, file=payload),
                       executor.submit(submit, second, headers, other_tid, key=key, file=payload,
                                       text='不同输入' if scenario == 'conflict' else '正文')]
            responses = [future.result() for future in futures]
        assert sorted(r.status_code for r in responses) == ([202, 202] if scenario == 'same' else [202, 409 if scenario == 'conflict' else 429])
        if scenario == 'same':
            assert responses[0].json() == responses[1].json()
        originals = [p for p in (tmp_path / 'originals').iterdir() if not p.name.endswith('.lock')]
        assert len(originals) == 1
        state = InputState(LocalStorage(tmp_path), settings)
        tasks = state.session(owner(headers))['tasks'].values()
        docs = [t['document'] for t in tasks if t['document']]
        assert len(docs) == 1
        assert state.storage.get(docs[0]['object_key']) == payload[1]


def test_no_database_import_even_when_packages_blocked(tmp_path):
    script = '''
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'sqlalchemy', 'sqlite3', 'pymysql', 'alembic'}:
            raise AssertionError('Database import: ' + fullname)
sys.meta_path.insert(0, Block())
from app.main import app
from fastapi.testclient import TestClient
with TestClient(app) as client:
    assert client.get('/api/v1/config').status_code == 200
    assert client.post('/api/v1/sessions', json={}).status_code == 201
'''
    import os
    from pathlib import Path
    result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1],
                            env={**os.environ, 'ENVIRONMENT': 'development', 'STORAGE_BACKEND': 'local',
                                 'LOCAL_STORAGE_DIRECTORY': str(tmp_path), 'MYSQL_PORT': 'not-an-integer'},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def s3():
    return S3Storage(Settings(storage_backend='s3', s3_bucket='private-test-bucket',
                             s3_access_key_id='test', s3_secret_access_key='test', _env_file=None))


def test_s3_etag_and_conditional_writes():
    storage = s3()
    assert storage.client.meta.config.retries['total_max_attempts'] == 1
    with Stubber(storage.client) as stub:
        stub.add_response('get_object', {'Body': StreamingBody(io.BytesIO(b'{}'), 2), 'ETag': '"v1"'},
                          {'Bucket': storage.bucket, 'Key': 'state'})
        assert storage.read_version('state') == (b'{}', '"v1"')
        stub.add_response('put_object', {}, {'Bucket': storage.bucket, 'Key': 'state', 'Body': b'new',
                                           'ContentType': 'application/json', 'IfMatch': '"v1"'})
        storage.compare_and_swap('state', '"v1"', b'new')
        stub.add_response('put_object', {}, {'Bucket': storage.bucket, 'Key': 'new', 'Body': b'{}',
                                           'ContentType': 'application/json', 'IfNoneMatch': '*'})
        storage.compare_and_swap('new', None, b'{}')
        stub.assert_no_pending_responses()


@pytest.mark.parametrize('code,status,conflict', [('PreconditionFailed', 412, True),
    ('ConditionalRequestConflict', 409, True), ('OperationAborted', 409, False),
    ('AccessDenied', 403, False), ('NotImplemented', 501, False), ('InternalError', 500, False)])
def test_s3_only_explicit_conflicts(code, status, conflict):
    storage = s3()
    with Stubber(storage.client) as stub:
        stub.add_client_error('put_object', service_error_code=code, http_status_code=status)
        with pytest.raises(StorageError) as error:
            storage.compare_and_swap('state', None, b'{}')
        assert isinstance(error.value, CASConflict) == conflict
        stub.assert_no_pending_responses()


def test_s3_network_write_is_not_replayed(monkeypatch):
    from botocore.exceptions import EndpointConnectionError
    storage = s3()
    calls = []
    def unavailable(**kwargs):
        calls.append(kwargs)
        raise EndpointConnectionError(endpoint_url='https://private.invalid')
    monkeypatch.setattr(storage.client, 'put_object', unavailable)
    monkeypatch.setattr(storage, 'read_version', lambda key: (None, None))
    with pytest.raises(StorageError):
        InputState(storage, Settings(_env_file=None)).update('state', lambda value: ({'count': 1}, None))
    assert len(calls) == 1
    assert calls[0]['IfNoneMatch'] == '*'


def test_missing_s3_returns_safe_503():
    settings = Settings(storage_backend='s3', s3_bucket='', s3_access_key_id='', s3_secret_access_key='', _env_file=None)
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/v1/config').status_code == 200
        response = client.post('/api/v1/sessions', json={})
        assert response.status_code == 503
        assert response.json()['error_code'] == 'STORAGE_UNAVAILABLE'
        assert response.json()['request_id'] == response.headers['x-request-id']


def test_unknown_completion_not_overwritten(tmp_path):
    settings = Settings(_env_file=None)
    storage = LocalStorage(tmp_path)
    state = InputState(storage, settings)
    now = utcnow()
    state.create_session('owner', now, now + timedelta(hours=24))
    state.session('owner', lambda s: s['tasks'].update(t={'id': 't', 'status': 'processing',
                  'document': {'id': 'd', 'created_at': now.isoformat()}}))
    calls = []
    class Uncertain(LocalStorage):
        def compare_and_swap(self, *args):
            calls.append(1)
            super().compare_and_swap(*args)
            raise StorageError('unknown')
    with pytest.raises(StorageError):
        InputState(Uncertain(tmp_path), settings).complete('owner', 't', 'd', {'saved': True}, None)
    assert len(calls) == 1
    assert state.session('owner')['tasks']['t']['status'] == 'completed'

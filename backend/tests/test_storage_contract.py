from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import os

import app.services.storage as storage_module
from app.persistence.input_state import InputState

import pytest
from pydantic import ValidationError

from app.core.settings import Settings
from app.schemas.documents import NormalizedDocument
from app.services.parsing import parse_content
from app.services.storage import LocalStorage, StorageError, create_storage


def test_local_roundtrip_and_collision(tmp_path):
    storage = LocalStorage(tmp_path)
    key = f'{uuid4()}/{uuid4()}.pdf'
    storage.put(key, b'original', 'application/pdf')
    assert storage.get(key) == b'original'
    with pytest.raises(StorageError):
        storage.put(key, b'replacement', 'application/pdf')
    assert storage.get(key) == b'original'
    storage.delete(key)
    with pytest.raises(StorageError):
        storage.get(key)


def test_local_rejects_path_escape(tmp_path):
    with pytest.raises(StorageError):
        LocalStorage(tmp_path).put('../outside', b'text', 'text/plain')


def test_production_rejects_local_storage():
    with pytest.raises(ValidationError):
        Settings(environment='production', storage_backend='local', _env_file=None)


def test_missing_s3_config_does_not_fall_back():
    with pytest.raises(StorageError):
        create_storage(Settings(storage_backend='s3', s3_bucket='', s3_access_key_id='', s3_secret_access_key=''))


def test_s3_requests_and_safe_error():
    from botocore.stub import Stubber
    from app.services.storage import S3Storage

    storage = S3Storage(Settings(storage_backend='s3', s3_bucket='private-test-bucket', s3_access_key_id='test-only', s3_secret_access_key='test-only'))
    with Stubber(storage.client) as stub:
        stub.add_response('put_object', {}, {'Bucket': 'private-test-bucket', 'Key': 'random-key', 'Body': b'input', 'ContentType': 'text/plain', 'IfNoneMatch': '*'})
        storage.put('random-key', b'input', 'text/plain')
        stub.add_client_error('put_object', service_error_code='AccessDenied', service_message='provider-internal-detail')
        with pytest.raises(StorageError) as error:
            storage.put('another-key', b'input', 'text/plain')
        assert 'provider-internal-detail' not in str(error.value)
        stub.assert_no_pending_responses()


def test_parser_result_matches_document_contract():
    result = parse_content(None, None, '不能删除否定词。\n\nprint(123)', Settings().parser_limits())
    document = NormalizedDocument(**result, task_id=uuid4(), document_id=uuid4(), created_at=datetime.now(timezone.utc))
    assert document.file_size == 0
    assert document.mime_type == 'text/plain'
    assert document.normalized_text[document.source_segments[0].start_offset:] == document.source_segments[0].content


def test_windows_resolve_prefix_race_preserves_containment(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    if os.name != 'nt':
        pytest.skip('Windows realpath prefix regression')
    storage = LocalStorage(tmp_path)
    candidate = storage.directory / 'new-folder' / 'child.json'
    extended = Path('\\\\?\\' + str(candidate))
    # Reproduce non-strict resolve retaining the prefix during a concurrent mkdir.
    monkeypatch.setattr(Path, 'resolve', lambda self: extended)
    assert storage._path('new-folder/child.json') == extended
    with pytest.raises(StorageError):
        storage._path('../outside')
    monkeypatch.setattr(Path, 'resolve', lambda self: Path('\\\\?\\' + str(storage.directory.parent / 'outside')))
    with pytest.raises(StorageError):
        storage._path('new-folder/child.json')


@pytest.mark.skipif(os.name != 'nt', reason='Windows replacement denial regression')
@pytest.mark.parametrize('winerror', [5, 32, 33])
@pytest.mark.parametrize('existing', [False, True])
def test_transient_replace_denial_reuses_payload_without_callback_replay(tmp_path, monkeypatch, winerror, existing):
    storage = LocalStorage(tmp_path)
    if existing:
        storage.put('state.json', b'{"count":0}', 'application/json')
    replace = os.replace
    attempts, transitions, delays = [], [], []
    denial = PermissionError('transient replacement denial')
    denial.winerror = winerror

    def denied_then_replace(source, destination):
        attempts.append((source, Path(source).read_bytes()))
        if len(attempts) < 3:
            raise denial
        replace(source, destination)

    def increment(value):
        transitions.append(1)
        return {'count': (value or {'count': 0})['count'] + 1}, 'saved'

    monkeypatch.setattr(storage_module.os, 'replace', denied_then_replace)
    monkeypatch.setattr(storage_module.time, 'sleep', delays.append)
    state = InputState(storage, Settings(_env_file=None))
    assert state.update('state.json', increment) == 'saved'
    assert storage.get('state.json') == b'{"count":1}'
    assert len(transitions) == 1
    assert len(attempts) == 3 and len(set(attempts)) == 1
    assert delays == [0.01, 0.02]
    assert not list(tmp_path.glob('.pending-*'))


@pytest.mark.skipif(os.name != 'nt', reason='Windows replacement denial regression')
@pytest.mark.parametrize('winerror', [5, 32, 33])
def test_persistent_replace_denial_preserves_previous_data(tmp_path, monkeypatch, winerror):
    storage = LocalStorage(tmp_path)
    storage.put('state.json', b'previous', 'application/json')
    _, version = storage.read_version('state.json')
    attempts, delays = [], []
    denial = PermissionError('persistent replacement denial')
    denial.winerror = winerror

    def denied(source, destination):
        attempts.append(source)
        raise denial

    monkeypatch.setattr(storage_module.os, 'replace', denied)
    monkeypatch.setattr(storage_module.time, 'sleep', delays.append)
    with pytest.raises(StorageError) as error:
        storage.compare_and_swap('state.json', version, b'next')
    assert error.value.__cause__ is denial
    assert len(attempts) == 4 and len(set(attempts)) == 1
    assert delays == [0.01, 0.02, 0.04]
    assert storage.get('state.json') == b'previous'
    assert not list(tmp_path.glob('.pending-*'))


@pytest.mark.skipif(os.name != 'nt', reason='Windows replacement denial regression')
@pytest.mark.parametrize('mutation', ['source', 'destination', 'committed'])
def test_replace_denial_with_changed_state_is_not_replayed(tmp_path, monkeypatch, mutation):
    storage = LocalStorage(tmp_path)
    storage.put('state.json', b'previous', 'application/json')
    _, version = storage.read_version('state.json')
    replace, attempts = os.replace, []
    denial = PermissionError('unknown replacement outcome')
    denial.winerror = 5

    def uncertain(source, destination):
        attempts.append(source)
        if mutation == 'source':
            Path(source).write_bytes(b'changed')
        elif mutation == 'destination':
            Path(destination).write_bytes(b'changed')
        else:
            replace(source, destination)
        raise denial

    monkeypatch.setattr(storage_module.os, 'replace', uncertain)
    monkeypatch.setattr(storage_module.time, 'sleep', lambda delay: None)
    with pytest.raises(StorageError):
        storage.compare_and_swap('state.json', version, b'next')
    assert len(attempts) == 1
    assert storage.get('state.json') == {'source': b'previous', 'destination': b'changed', 'committed': b'next'}[mutation]
    assert not list(tmp_path.glob('.pending-*'))


def test_unrelated_replace_error_is_not_retried(tmp_path, monkeypatch):
    storage = LocalStorage(tmp_path)
    attempts, delays = [], []

    def failed(source, destination):
        attempts.append(source)
        raise OSError('unrelated failure')

    monkeypatch.setattr(storage_module.os, 'replace', failed)
    monkeypatch.setattr(storage_module.time, 'sleep', delays.append)
    with pytest.raises(StorageError):
        storage.put('state.json', b'next', 'application/json')
    assert len(attempts) == 1 and not delays
    assert storage.read_version('state.json') == (None, None)
    assert not list(tmp_path.glob('.pending-*'))

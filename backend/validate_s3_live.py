"""Live S3 integration: storage contract plus one real app flow, then anonymous-access refusal.

Requires S3_ENDPOINT_URL, S3_BUCKET, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY (and optional
S3_REGION). Nothing is faked except the model/GitHub, which this flow never touches.
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))


def settings_from_env():
    from app.core.settings import Settings

    required = ('S3_ENDPOINT_URL', 'S3_BUCKET', 'S3_ACCESS_KEY_ID', 'S3_SECRET_ACCESS_KEY')
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SystemExit('Missing environment: ' + ', '.join(missing))
    return Settings(
        environment='development', storage_backend='s3',
        s3_endpoint_url=os.environ['S3_ENDPOINT_URL'], s3_bucket=os.environ['S3_BUCKET'],
        s3_region=os.environ.get('S3_REGION', 'us-east-1'),
        s3_access_key_id=os.environ['S3_ACCESS_KEY_ID'],
        s3_secret_access_key=os.environ['S3_SECRET_ACCESS_KEY'], _env_file=None)


def contract(storage, prefix):
    from app.services.storage import CASConflict

    created = []
    checks = {}
    key = f'{prefix}/unicode 中文.json'
    body = json.dumps({'text': '删除否定词', 'n': 1}, ensure_ascii=False).encode()
    storage.put(key, body, 'application/json')
    created.append(key)
    checks['roundtrip_bytes'] = storage.get(key) == body
    data, version = storage.read_version(key)
    checks['version_present'] = data == body and isinstance(version, str) and bool(version)

    original = storage.get(key)
    try:
        storage.put(key, b'{"overwrite":true}', 'application/json')
        checks['create_is_exclusive'] = False
    except CASConflict:
        checks['create_is_exclusive'] = storage.get(key) == original

    storage.compare_and_swap(key, version, b'{"n":2}')
    _, next_version = storage.read_version(key)
    checks['cas_advances_version'] = next_version != version and storage.get(key) == b'{"n":2}'
    try:
        storage.compare_and_swap(key, version, b'{"stale":true}')
        checks['stale_cas_refused'] = False
    except CASConflict:
        checks['stale_cas_refused'] = storage.get(key) == b'{"n":2}'

    winner = storage
    _, base = storage.read_version(key)

    def contender(value):
        try:
            winner.compare_and_swap(key, base, value)
            return 'won'
        except CASConflict:
            return 'lost'

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(contender, [b'{"writer":1}', b'{"writer":2}']))
    checks['concurrent_cas_single_winner'] = results == ['lost', 'won'] and storage.get(key) in (
        b'{"writer":1}', b'{"writer":2}')

    big = os.urandom(1024 * 1024)
    storage.put(f'{prefix}/blob.bin', big, 'application/octet-stream')
    created.append(f'{prefix}/blob.bin')
    checks['one_megabyte_roundtrip'] = storage.get(f'{prefix}/blob.bin') == big

    storage.delete(key)
    created.remove(key)
    try:
        storage.get(key)
        checks['delete_removes_object'] = False
    except Exception:
        checks['delete_removes_object'] = True
    for extra in created:
        storage.delete(extra)
    return checks


def app_flow(settings, storage, prefix):
    from fastapi.testclient import TestClient

    from app.main import create_app

    checks, keys = {}, []
    client = TestClient(create_app(settings, storage=storage), base_url='http://projectpilot.test')
    with client:
        token = client.post('/api/v1/sessions', json={}).json()['access_token']
        headers = {'Authorization': f'Bearer {token}'}
        task_id = client.post('/api/v1/tasks', json={}, headers=headers).json()['task_id']
        import hashlib
        owner = hashlib.sha256(token.encode()).hexdigest()
        keys.append(f'sessions/{owner}.json')
        keys.append('rates/' + hashlib.sha256(b'testclient').hexdigest() + '.json')
        checks['session_object_in_bucket'] = storage.read_version(f'sessions/{owner}.json')[0] is not None

        text = '必须支持上传音频。\n\n不得使用付费云 API。'
        key = str(uuid4())
        response = client.post(f'/api/v1/tasks/{task_id}/input', headers={**headers, 'Idempotency-Key': key},
                               files={'text': (None, text)})
        checks['input_accepted'] = response.status_code == 202
        status = None
        for _ in range(50):
            status = client.get(f'/api/v1/tasks/{task_id}/input/status', headers=headers).json()
            if status['status'] in ('completed', 'failed'):
                break
            import time
            time.sleep(0.2)
        checks['input_processed'] = status is not None and status['status'] == 'completed'
        document = client.get(f'/api/v1/tasks/{task_id}/input', headers=headers).json()
        checks['document_roundtrip'] = '必须支持上传音频' in document['normalized_text'] and '不得使用付费云 API' in document['normalized_text']
        session, _ = storage.read_version(f'sessions/{owner}.json')
        checks['session_updated_by_cas'] = json.loads(session)['tasks'][task_id]['status'] == 'completed'
    for key in keys:
        storage.delete(key)
    return checks


def private_bucket(settings, storage, prefix):
    import httpx

    from app.services.storage import StorageError

    key = f'{prefix}/private.txt'
    storage.put(key, b'secret', 'text/plain')
    url = settings.s3_endpoint_url.rstrip('/') + f'/{settings.s3_bucket}/{key}'
    response = httpx.get(url, timeout=10, trust_env=False)
    storage.delete(key)
    return {'anonymous_read_refused': response.status_code in (403, 401)}, response.status_code


def main():
    from app.services.storage import create_storage

    settings = settings_from_env()
    storage = create_storage(settings)
    prefix = f's3-validation/{uuid4().hex}'
    checks = contract(storage, prefix)
    checks.update(app_flow(settings, storage, prefix))
    privacy, status = private_bucket(settings, storage, prefix)
    checks.update(privacy)
    failed = [name for name, ok in checks.items() if not ok]
    print(json.dumps({'endpoint': settings.s3_endpoint_url, 'bucket': settings.s3_bucket,
                      'checks': checks, 'anonymous_status': status, 'failed': failed},
                     ensure_ascii=False))
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()

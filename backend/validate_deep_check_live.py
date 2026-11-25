"""Explicitly authorized live acceptance for the stage-3 deep check, driven through the real HTTP routes.

`run` spends money (1 extraction + 1 planning model call, up to 15 GitHub requests, 1 deep-check call) and
needs --allow-paid. `verify` opens a fresh process with the model base URL black-holed, so a replayed paid
call would fail loudly instead of silently billing twice.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

REQUIREMENT_TEXT = (
    'Create an application that renders Markdown text into a single self-contained HTML file.\n'
    '- Users paste Markdown text and render it to HTML.\n'
    '- The rendered HTML is saved as a downloadable file.\n'
    '- The project uses a permissive license such as MIT, BSD or Apache.\n'
    '- The project does not require a paid cloud API to render Markdown.'
)
BUDGET = {'model_calls': 1, 'github_requests': 15, 'search_requests': 5,
          'readme_candidates': 5, 'per_page': 5, 'max_pages': 1}
BLACK_HOLE = 'http://127.0.0.1:9/v1'


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def directory(case):
    return Path(__file__).resolve().parents[1] / 'data' / 'deep-check-validation' / case


def load(case):
    return json.loads((directory(case) / 'control.json').read_bytes())


def save(case, control):
    path = directory(case) / 'control.json'
    temporary = path.with_suffix('.pending')
    temporary.write_bytes(encode(control))
    temporary.replace(path)


def post(client, headers, path, *, body=None, key=None, files=None):
    request = {'headers': {**headers, **({'Idempotency-Key': key} if key else {})}}
    if files is not None:
        request['files'] = files
    else:
        request['json'] = {} if body is None else body
    response = client.post(path, **request)
    if response.status_code >= 400:
        raise RuntimeError(f'POST {path} -> {response.status_code} {response.text[:500]}')
    return response.json()


def wait_for(read, name, attempts=40, delay=1.0):
    for _ in range(attempts):
        value = read()
        if value is not None:
            return value
        time.sleep(delay)
    raise RuntimeError(f'Timed out waiting for {name}')


def phase_run(case):
    from fastapi.testclient import TestClient
    from app.main import create_app
    import httpx

    root = directory(case)
    if root.exists():
        raise ValueError('Case already exists; refusing to spend twice')
    root.mkdir(parents=True)
    control = {'case': case, 'started_at': datetime.now(timezone.utc).isoformat()}
    client = TestClient(create_app(), base_url='http://projectpilot.test')
    client.timeout = httpx.Timeout(900.0)
    with client:
        token = post(client, {}, '/api/v1/sessions')['access_token']
        headers = {'Authorization': f'Bearer {token}'}
        task_id = post(client, headers, '/api/v1/tasks')['task_id']

        input_key = str(uuid4())
        post(client, headers, f'/api/v1/tasks/{task_id}/input', key=input_key,
             files={'text': (None, REQUIREMENT_TEXT)})
        document_id = wait_for(lambda: client.get(f'/api/v1/tasks/{task_id}/input/status',
                                                  headers=headers).json().get('document_id'),
                               'the document to be parsed')
        post(client, headers, f'/api/v1/documents/{document_id}/requirement-extractions', key=str(uuid4()))

        def draft():
            value = client.get(f'/api/v1/documents/{document_id}/requirements', headers=headers).json()
            job = value.get('extraction') or {}
            if job.get('status') == 'failed':
                raise RuntimeError('Real extraction ended without a draft: '
                                   + json.dumps({'error': job.get('error')}, ensure_ascii=False))
            return next(iter(value['revisions']), None)

        revision_draft = wait_for(draft, 'the real extraction draft', attempts=60, delay=2.0)
        revision_id = revision_draft['revision_id']
        if revision_draft['requirements']['uncertain_information']:
            raise RuntimeError('Real extraction left unresolved ambiguity, so nothing is auto-edited: '
                               + json.dumps(revision_draft['requirements']['uncertain_information'],
                                            ensure_ascii=False)[:800])
        confirmed = post(client, headers, f'/api/v1/requirement-revisions/{revision_id}/confirm')
        items = confirmed['requirement_items']

        run_id = post(client, headers, f'/api/v1/tasks/{task_id}/search-runs', key=(search_key := str(uuid4())),
                      body={'revision_id': revision_id, 'consent': True, 'budget': BUDGET})['run_id']
        url = f'/api/v1/tasks/{task_id}/search-runs/{run_id}'
        view = lambda path: (lambda r: r.json() if r.status_code == 200 else None)(
            client.get(path, headers=headers))
        run = wait_for(lambda: (value if (value := view(url)) and value.get('dispatch_closed') else None),
                       'the real search pipeline to close dispatch', attempts=90, delay=2.0)
        post(client, headers, f'{url}/evidence')
        evidence = wait_for(lambda: view(f'{url}/evidence'), 'the literal evidence report', attempts=30, delay=2.0)

        deep = post(client, headers, f'{url}/deep-check', key=(deep_key := str(uuid4())),
                    body={'revision_id': revision_id, 'consent': True})
        if deep['run_id'] != run_id:
            raise RuntimeError('Deep check answered a different run')
        report = wait_for(lambda: view(f'{url}/deep-check'), 'the published deep check report', attempts=45, delay=2.0)
        control.update({'token': token, 'task_id': task_id, 'document_id': document_id,
                        'revision_id': revision_id, 'run_id': run_id, 'search_key': search_key,
                        'deep_key': deep_key, 'deep_body': {'revision_id': revision_id, 'consent': True},
                        'report_digest': digest(report), 'report': report,
                        'extraction': {'item_count': len(items),
                                       'items': [{'text': i['text'], 'modality': i['modality'],
                                                  'operator': i['operator']} for i in items]},
                        'github': {'search_status': run.get('search_status'),
                                   'stop_reasons': run.get('search_stop_reasons'),
                                   'candidates': len(run.get('candidates', [])),
                                   'snapshots': sum(1 for c in run.get('candidates', [])
                                                    if c['collection_status'] == 'completed')},
                        'evidence': {'digest': digest(evidence), 'candidates': len(evidence['candidates'])},
                        'finished_at': datetime.now(timezone.utc).isoformat()})
    save(case, control)
    print(json.dumps({'phase': 'run', 'task_id': task_id, 'run_id': run_id,
                      'requirement_items': len(items), 'github': control['github'],
                      'labels': [c['recommendation']['label'] for c in report['candidates']],
                      'report_digest': control['report_digest'][:16]}, ensure_ascii=False))


def citation_lines(data):
    """Independent line table over the same LF-normalized text the report offsets are measured in."""
    offsets, position = [], 0
    for line in data.split(b'\n'):
        offsets.append(position)
        position += len(line) + 1
    return lambda index: max(i for i, offset in enumerate(offsets) if offset <= index) + 1


def phase_verify(case):
    os.environ['DEEPSEEK_BASE_URL'] = BLACK_HOLE
    from fastapi.testclient import TestClient
    from app.core.settings import Settings
    from app.main import create_app
    from app.persistence.run_manifest import RunManifestStore
    from app.persistence.search_database import SearchDatabaseSettings, create_search_engine
    from app.persistence.search_repository import SearchRepository
    from app.services.search_persistence import SearchPersistence, _digest as bound_digest, _encode as bound_encode
    from app.services.storage import create_storage

    control = load(case)
    checks = []
    engine = create_search_engine(SearchDatabaseSettings())
    client = TestClient(create_app(), base_url='http://projectpilot.test')
    import httpx
    client.timeout = httpx.Timeout(900.0)
    with client:
        headers = {'Authorization': f'Bearer {control['token']}'}
        url = f"/api/v1/tasks/{control['task_id']}/search-runs/{control['run_id']}"
        report = post_or_get(client, headers, url)
        checks.append(('deep report reads back byte-identical with the model URL black-holed',
                       digest(report) == control['report_digest']))
        checks.append(('evidence report unchanged',
                       digest(post_or_get(client, headers, f'{url}/evidence')) == control['evidence']['digest']))
        run = post_or_get(client, headers, url)
        checks.append(('run view still marks the deep check published and integrity clean',
                       run['deep_check']['published'] is True and not run['deep_check']['integrity_blocked']))
        replay = post(client, headers, f'{url}/deep-check', key=control['deep_key'], body=control['deep_body'])
        checks.append(('same key and payload re-accepted onto the same run', replay['run_id'] == control['run_id']))
        checks.append(('report still identical after the replayed authorization',
                       digest(post_or_get(client, headers, f'{url}/deep-check')) == control['report_digest']))
        conflict = client.post(f'{url}/deep-check', headers={**headers, 'Idempotency-Key': str(uuid4())},
                               json=control['deep_body'])
        checks.append(('a second paid authorization for the same run is refused with 409', conflict.status_code == 409))

        owner = hashlib.sha256(control['token'].encode()).hexdigest()
        storage = create_storage(Settings())
        persistence = SearchPersistence(storage, SearchRepository(engine))
        manifest = RunManifestStore(storage).read(owner, control['run_id'])
        operation = manifest['deep_check_operation']
        stored, _ = storage.read_version(operation['response_key'])
        revision = post_or_get(client, headers, f"/api/v1/documents/{control['document_id']}/requirements")['revisions'][-1]
        checks.append(('the raw model response is stored under its recorded hash',
                       hashlib.sha256(stored).hexdigest() == operation['response_hash']))
        checks.append(('the operation is bound to this confirmed revision and this published plan',
                       operation['revision_hash'] == bound_digest(bound_encode(revision))
                       and operation['plan_hash'] == manifest['plan']['plan_hash']))
        checks.append(('exactly one published call, recorded with a real model alias',
                       operation['published'] is True and operation['status'] == 'completed'
                       and operation['model_alias'].startswith('alias:')))

        failures, cited = [], 0
        repository = SearchRepository(engine)
        for candidate in report['candidates']:
            if not candidate['source']:
                continue
            source = candidate['source']
            raw = persistence.read_snapshot(owner, control['run_id'], source['snapshot_id'], read_only=True)
            snapshot = repository.get_snapshot_by_id(source['snapshot_id'])
            checks.append((f"snapshot {source['snapshot_id'][:8]} hashes match the persisted README bytes",
                           hashlib.sha256(raw).hexdigest() == snapshot['content_hash']
                           and hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
                           == snapshot['blob_sha']))
            normalized = raw.decode('utf-8', 'replace').replace('\r\n', '\n').replace('\r', '\n').encode('utf-8')
            line_of = citation_lines(normalized)
            for item in candidate['items']:
                for citation in item['citations']:
                    cited += 1
                    if normalized[citation['byte_start']:citation['byte_end']].decode('utf-8', 'replace') != citation['text']:
                        failures.append(f"{item['text']}: byte range does not reproduce the cited text")
                    elif (line_of(citation['byte_start']) != citation['line_start']
                          or line_of(max(citation['byte_start'], citation['byte_end'] - 1)) != citation['line_end']):
                        failures.append(f"{item['text']}: line span disagrees with the byte offsets")
        uncited = [f"{item['text']} ({item['verdict']})"
                   for candidate in report['candidates'] if candidate['source'] for item in candidate['items']
                   if item['basis'] == 'model' and not item['citations']]
        rejected = sum(len(item['rejections']) for candidate in report['candidates'] for item in candidate['items'])
        checks.append(('every model-based verdict is backed by at least one citation', not uncited))
        checks.append((f'all {cited} published citations reproduce from the real README bytes', not failures))
        control['verify'] = {'citations_checked': cited, 'citation_failures': failures[:8],
                             'provider_usage': operation['provider_usage'], 'model_alias': operation['model_alias'],
                             'passed': [name for name, ok in checks if ok],
                             'failed': [name for name, ok in checks if not ok]}
    save(case, control)
    print(json.dumps({'phase': 'verify', 'passed': len(control['verify']['passed']),
                      'failed': control['verify']['failed'], 'citations': cited,
                      'citation_failures': failures[:4],
                      'provider_usage': control['verify']['provider_usage']}, ensure_ascii=False))
    if control['verify']['failed']:
        sys.exit(1)


def post_or_get(client, headers, path):
    response = client.get(path, headers=headers)
    if response.status_code >= 400:
        raise RuntimeError(f'GET {path} -> {response.status_code} {response.text[:500]}')
    return response.json()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', required=True)
    parser.add_argument('--phase', choices=['run', 'verify'], required=True)
    parser.add_argument('--allow-paid', action='store_true')
    arguments = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9_]{1,24}', arguments.case):
        raise SystemExit('Safe case name required')
    if arguments.phase == 'run' and not arguments.allow_paid:
        raise SystemExit('The run phase makes real paid model calls and needs --allow-paid')
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    {'run': phase_run, 'verify': phase_verify}[arguments.phase](arguments.case)

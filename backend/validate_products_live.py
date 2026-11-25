"""Real end-to-end acceptance for 3a-UI: one PDF, real model/GitHub/embedding, two sessions.

Spends one extraction call, one planner call, up to 15 GitHub requests and embedding tokens.
Writes a control JSON under data/acceptance/3a-ui/<run_id>.json for the screenshot script.
"""
import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

BASE = 'http://127.0.0.1:8000/api/v1'
BUDGET = {'model_calls': 1, 'github_requests': 15, 'search_requests': 5,
          'readme_candidates': 5, 'per_page': 5, 'max_pages': 1}
ROOT = Path(__file__).resolve().parents[1]
STEPS = []


def step(name, **facts):
    STEPS.append({'step': name, 'at': datetime.now(timezone.utc).isoformat(), **facts})
    print(json.dumps({'step': name, **facts}, ensure_ascii=False), flush=True)


def call(client, method, path, token=None, **kwargs):
    headers = kwargs.pop('headers', {})
    if token:
        headers['Authorization'] = f'Bearer {token}'
    response = client.request(method, BASE + path, headers=headers, **kwargs)
    return response


def ok(response, expected=(200, 201, 202)):
    if response.status_code not in expected:
        raise RuntimeError(f'{response.request.method} {response.request.url.path} -> '
                           f'{response.status_code} {response.text[:600]}')
    return response.json() if response.content else None


def wait(client, path, token, done, timeout=420, interval=2.0, label=''):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = ok(call(client, 'GET', path, token), (200, 409))
        if done(last):
            return last
        time.sleep(interval)
    raise RuntimeError(f'Timed out waiting for {label}: {json.dumps(last, ensure_ascii=False)[:500]}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--file', default=str(ROOT / 'data' / 'browser-validation' / 'assignment.pdf'))
    parser.add_argument('--text', default='')
    arguments = parser.parse_args()
    client = httpx.Client(timeout=120, trust_env=False)
    token_a = ok(call(client, 'POST', '/sessions', json={}))['access_token']
    token_b = ok(call(client, 'POST', '/sessions', json={}))['access_token']
    step('sessions', a=bool(token_a), b=bool(token_b))

    task = ok(call(client, 'POST', '/tasks', token_a, json={}))['task_id']
    path = Path(arguments.file)
    with path.open('rb') as handle:
        payload = handle.read()
    data, filename, text = None, None, arguments.text
    response = call(client, 'POST', f'/tasks/{task}/input', token_a,
                    headers={'Idempotency-Key': str(uuid4())},
                    files={'file': (path.name, payload, 'application/pdf')},
                    data={'text': text} if text else {})
    ok(response, (202,))
    status = wait(client, f'/tasks/{task}/input/status', token_a,
                  lambda value: value and value['status'] in ('completed', 'failed'), 120, 1.0, 'parse')
    if status['status'] != 'completed':
        raise RuntimeError('PDF parsing failed: ' + json.dumps(status, ensure_ascii=False))
    document = ok(call(client, 'GET', f"/tasks/{task}/input", token_a))
    step('parsed', task=task, document=document['document_id'], characters=document['character_count'],
         pages=document['total_pages'], warnings=document['warnings'])

    ok(call(client, 'POST', f"/documents/{document['document_id']}/requirement-extractions", token_a,
            headers={'Idempotency-Key': str(uuid4())}, json={}), (202,))
    result = wait(client, f"/documents/{document['document_id']}/requirements", token_a,
                  lambda value: value and (value.get('extraction') or {}).get('status') in ('completed', 'failed'),
                  150, 2.0, 'extraction')
    if result['extraction']['status'] != 'completed':
        raise RuntimeError('Extraction failed: ' + json.dumps(result['extraction'], ensure_ascii=False)[:800])
    revision = result['revisions'][-1]
    step('extracted', revision=revision['revision_id'], items=len(revision['requirement_items']),
         uncertain=bool(revision['requirements']['uncertain_information']),
         extraction_usage=(result['extraction'].get('usage') or {}))
    if revision['requirements']['uncertain_information']:
        raise RuntimeError('Extraction left uncertainty; refusing to auto-confirm: '
                           + json.dumps(revision['requirements']['uncertain_information'], ensure_ascii=False)[:800])
    confirmed = ok(call(client, 'POST', f"/requirement-revisions/{revision['revision_id']}/confirm",
                        token_a, json={}))
    step('confirmed', revision=confirmed['revision_id'], items=len(confirmed['requirement_items']))

    run = ok(call(client, 'POST', f'/tasks/{task}/search-runs', token_a,
                  headers={'Idempotency-Key': str(uuid4())},
                  json={'revision_id': confirmed['revision_id'], 'consent': True, 'budget': BUDGET}), (202,))
    run_id = run['run_id']
    view = wait(client, f'/tasks/{task}/search-runs/{run_id}', token_a,
                lambda value: value and value.get('pipeline_status') in ('completed', 'failed', 'interrupted'),
                420, 2.0, 'search pipeline')
    step('search', run=run_id, pipeline=view['pipeline_status'], search=view.get('search_status'),
         stop_reasons=view.get('search_stop_reasons'), error=view.get('error_code'),
         candidates=len(view.get('candidates') or []),
         snapshots=sum(1 for row in (view.get('candidates') or []) if row.get('collection_status') == 'completed'),
         evidence=view.get('evidence_status'), retrieval=view.get('retrieval_status'))

    bundle = ok(call(client, 'GET', f'/tasks/{task}/search-runs/{run_id}/retrieval-evidence', token_a))
    chunk_count = sum(len(item['chunks']) for row in bundle['candidates'] for item in row['items'])
    step('bundle', status=bundle['status'], candidates=len(bundle['candidates']),
         unique_chunks=chunk_count, requirements=len(bundle['requirements']),
         empty_reason=bundle.get('empty_reason'), query_usage=bundle.get('query_usage'),
         readme_paths=[row.get('readme_path') for row in bundle['candidates']])

    # Manifest and stored bytes.
    from app.core.settings import Settings
    from app.services.storage import create_storage
    storage = create_storage(Settings())
    owner = hashlib.sha256(token_a.encode()).hexdigest()
    manifest = json.loads(storage.read_version(f'runs/{owner}/{run_id}.json')[0])
    chunk_op = manifest.get('chunk_index_operation') or {}
    bundle_op = manifest.get('evidence_bundle_operation') or {}
    stored, _ = storage.read_version(bundle_op['storage_key'])
    stored_hash = hashlib.sha256(stored).hexdigest()
    progress = {}
    if chunk_op.get('index_key'):
        raw, _ = storage.read_version(f"chunk-index/{chunk_op['index_key']}/progress.json")
        progress = json.loads(raw) if raw else {}
    embedding_tokens = sum(int(row.get('total_tokens') or 0) for row in (progress.get('usage') or {}).values())

    failures, offsets_checked = [], 0
    for row in bundle['candidates']:
        if not row.get('snapshot_id'):
            continue
        operation = next((op for op in manifest['readme_persistence_operations'].values()
                          if op['snapshot_id'] == row['snapshot_id']), None)
        if operation is None or operation['readme_path'] != row.get('readme_path'):
            failures.append(f"{row['repository_id']}: readme_path differs")
            continue
        raw, _ = storage.read_version(operation['storage_key'])
        normalized = raw.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n').encode()
        for item in row['items']:
            for chunk in item['chunks']:
                offsets_checked += 1
                text = normalized[chunk['byte_start']:chunk['byte_end']].decode('utf-8')
                line = normalized[:chunk['byte_start']].count(b'\n') + 1
                if text != chunk['text'] or line != chunk['line_start']:
                    failures.append(f"{row['repository_id']}: chunk offsets differ")
    step('manifest', chunk_status=chunk_op.get('status'), bundle_status=bundle_op.get('status'),
         bundle_hash_matches=stored_hash == bundle_op.get('bundle_hash'),
         embedding_tokens=embedding_tokens, batches=progress.get('batches'),
         offset_failures=failures, offsets_checked=offsets_checked)

    # Real HTTP permission matrix.
    task_b = ok(call(client, 'POST', '/tasks', token_b, json={}))['task_id']
    checks = {}
    checks['a_reads_own_run'] = call(client, 'GET', f'/tasks/{task}/search-runs/{run_id}', token_a).status_code == 200
    checks['a_reads_own_bundle'] = call(client, 'GET', f'/tasks/{task}/search-runs/{run_id}/retrieval-evidence', token_a).status_code == 200
    checks['b_cannot_read_a_run'] = call(client, 'GET', f'/tasks/{task}/search-runs/{run_id}', token_b).status_code == 404
    checks['b_cannot_read_a_bundle'] = call(client, 'GET', f'/tasks/{task}/search-runs/{run_id}/retrieval-evidence', token_b).status_code == 404
    checks['b_cannot_reuse_run_under_own_task'] = call(client, 'GET', f'/tasks/{task_b}/search-runs/{run_id}', token_b).status_code == 404
    checks['a_wrong_run_id_is_404'] = call(client, 'GET', f'/tasks/{task}/search-runs/{uuid4()}', token_a).status_code == 404
    checks['anonymous_is_401'] = call(client, 'GET', f'/tasks/{task}/search-runs/{run_id}/retrieval-evidence').status_code == 401
    repository_id = bundle['candidates'][0]['repository_id'] if bundle['candidates'] else None
    requirement_id = bundle['requirements'][0]['requirement_item_id']
    checks['repo_filter_isolated'] = (repository_id is None or
        all(row['repository_id'] == repository_id for row in ok(call(
            client, 'GET', f'/tasks/{task}/search-runs/{run_id}/retrieval-evidence',
            token_a, params={'repository_id': repository_id}))['candidates']))
    checks['unknown_repo_is_404'] = call(
        client, 'GET', f'/tasks/{task}/search-runs/{run_id}/retrieval-evidence', token_a,
        params={'repository_id': '999999999'}).status_code == 404
    checks['requirement_filter_isolated'] = all(
        row['requirement_item_id'] == requirement_id for row in ok(call(
            client, 'GET', f'/tasks/{task}/search-runs/{run_id}/retrieval-evidence', token_a,
            params={'requirement_item_id': requirement_id}))['requirements'])
    checks['unknown_requirement_is_404'] = call(
        client, 'GET', f'/tasks/{task}/search-runs/{run_id}/retrieval-evidence', token_a,
        params={'requirement_item_id': str(uuid4())}).status_code == 404
    step('permissions', **checks)

    control = {
        'created_at': datetime.now(timezone.utc).isoformat(), 'token_a': token_a,
        'task_id': task, 'document_id': document['document_id'],
        'revision_id': confirmed['revision_id'], 'run_id': run_id,
        'requirements': [{'requirement_item_id': item['requirement_item_id'], 'field': item.get('field'),
                          'text': item['text'], 'modality': item['modality']}
                         for item in confirmed['requirement_items']],
        'view': {key: view.get(key) for key in ('pipeline_status', 'search_status', 'retrieval_status',
                                                'evidence_status', 'error_code', 'search_stop_reasons')},
        'bundle': {'status': bundle['status'], 'candidates': len(bundle['candidates']),
                   'unique_chunk_hits': chunk_count, 'query_usage': bundle.get('query_usage')},
        'embedding_tokens': embedding_tokens, 'manifest': {'chunk_status': chunk_op.get('status'),
                                                           'bundle_status': bundle_op.get('status')},
        'permissions': checks, 'offset_failures': failures,
        'steps': STEPS,
    }
    target = ROOT / 'data' / 'acceptance' / '3a-ui' / f'{run_id}.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(control, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'control': str(target), 'ok': not failures and all(checks.values())}, ensure_ascii=False))


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()

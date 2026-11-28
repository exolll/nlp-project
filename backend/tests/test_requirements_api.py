import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
from threading import Lock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.settings import ExtractionSettings, Settings
from app.main import create_app
from app.persistence.input_state import InputState, utcnow
from app.services import processing, requirements as service
from app.services.parsing import parse_content
from app.services.storage import LocalStorage, StorageError
from test_api import auth, owner, submit, task
from test_extraction_provider import success
from test_requirement_validation import extraction


@pytest.fixture
def api_factory(tmp_path, monkeypatch):
    stack, calls, behavior, mutex, auth_headers = ExitStack(), [], {}, Lock(), []
    settings = Settings(environment='test', local_storage_directory=tmp_path, write_requests_per_minute=1000,
                        session_requests_per_minute=100, _env_file=None)
    provider = ExtractionSettings(deepseek_api_key='test-only', _env_file=None)
    monkeypatch.setattr(processing, 'parse_isolated', lambda data, filename, text, settings: parse_content(data, filename, text, settings.parser_limits()))
    def handler(request):
        with mutex:
            calls.append(1)
            auth_headers.append(request.headers.get('authorization'))
        if behavior.get('timeout'):
            raise httpx.ReadTimeout('private-provider-detail')
        if 'status' in behavior:
            return httpx.Response(behavior['status'], text='private-provider-detail')
        doc = {'source_segments': json.loads(json.loads(request.content)['messages'][1]['content'])['UNTRUSTED_SOURCE_SEGMENTS']}
        configured = behavior.get('raw')
        raw = configured(doc) if callable(configured) else (
            configured if configured is not None else extraction(doc, behavior.get('specs')))
        return httpx.Response(200, json=success(json.dumps(raw, ensure_ascii=False), behavior.get('usage')))
    def factory(config=None, storage=None, injected=True):
        config = config or settings
        storage = storage or LocalStorage(tmp_path)
        app = create_app(config, storage, extraction_settings=provider,
                         extraction_transport=httpx.MockTransport(handler) if injected else None)
        client = stack.enter_context(TestClient(app))
        return client, app, InputState(storage, config)
    factory.settings, factory.calls, factory.behavior = settings, calls, behavior
    factory.auth_headers = auth_headers
    factory.storage = LocalStorage(tmp_path)
    yield factory
    stack.close()


def prepare(client, headers=None, text='实现检索。支持导出。'):
    headers = headers or auth(client)
    tid = task(client, headers)
    result = submit(client, headers, tid, text=text)
    assert result.status_code == 202, result.text
    return headers, tid, result.json()['document_id']


def start(client, headers, did, key=None):
    return client.post(f'/api/v1/documents/{did}/requirement-extractions',
                       headers={**headers, 'Idempotency-Key': key or str(uuid4())}, json={})


def read(client, headers, did):
    result = client.get(f'/api/v1/documents/{did}/requirements', headers=headers)
    assert result.status_code == 200, result.text
    return result.json()


def extracted(client, headers, did):
    result = start(client, headers, did)
    assert result.status_code == 202, result.text
    result = read(client, headers, did)
    assert result['extraction']['status'] == 'completed', result
    return result['revisions'][0]


def body_for(revision):
    return {'requirements': deepcopy(revision['requirements']), 'requirement_items': deepcopy(revision['requirement_items']),
            'change_reason': '明确修改需求'}


def edit(client, headers, parent, body):
    return client.post(f'/api/v1/requirement-revisions/{parent}/revisions', headers=headers, json=body)


def confirm(client, headers, rid):
    return client.post(f'/api/v1/requirement-revisions/{rid}/confirm', headers=headers, json={})


@pytest.mark.parametrize('invalid', ['quote', 'schema', 'negation'])
def test_private_validation_diagnostic_redacts_model_content(api_factory, invalid):
    client, _, state = api_factory()
    headers, tid, did = prepare(client)
    raw = extraction()
    for evidence in raw['requirements']['source_evidence']:
        evidence['source_location'] = '用户输入正文'
    secret = 'private-model-content-and-credential'
    if invalid == 'quote':
        raw['requirements']['source_evidence'][0]['original_text'] = secret
    elif invalid == 'schema':
        raw['requirements'][secret] = secret
    else:
        raw['requirements']['required_features'] = None
        raw['requirements']['preferred_features'] = ['禁止实现检索']
        raw['requirements']['source_evidence'] = raw['requirements']['source_evidence'][:1]
        raw['requirements']['source_evidence'][0]['field'] = 'preferred_features'
        raw['requirement_items'] = raw['requirement_items'][:1]
        raw['requirement_items'][0].update(field='preferred_features', value_path='preferred_features[0]',
                                           text='禁止实现检索', modality='preferred')
    api_factory.behavior['raw'] = raw
    assert start(client, headers, did).status_code == 202
    public = read(client, headers, did)
    assert public['extraction']['error']['error_code'] == 'EXTRACTION_INVALID'
    assert not public['revisions']
    stored = json.loads(state.storage.get(state.session_key(owner(headers))))
    record = stored['tasks'][str(tid)]['requirements']['extractions'][0]
    diagnostic = record['validation_diagnostic']
    assert diagnostic['validator'] == {'quote': 'resolve_spans', 'schema': 'validate_extraction', 'negation': 'validate_coverage'}[invalid]
    assert isinstance(diagnostic['line'], int)
    assert secret not in json.dumps(diagnostic)
    assert secret not in json.dumps(stored)
    assert 'validation_diagnostic' not in json.dumps(public)
    assert len(api_factory.calls) == 1
    restarted, _, _ = api_factory()
    assert read(restarted, headers, did) == public


def test_frozen_shapes_idempotency_usage_and_restart(api_factory):
    client, _, state = api_factory()
    headers, _, did = prepare(client)
    assert read(client, headers, did) == {'extraction': None, 'revisions': [], 'latest_revision_id': None}
    key = str(uuid4())
    api_factory.behavior['usage'] = {'prompt_tokens': 123, 'completion_tokens': 10}
    initial = start(client, headers, did, key)
    assert initial.status_code == 202
    assert set(initial.json()) == {'extraction_id', 'status', 'error', 'model', 'prompt_version', 'usage', 'created_at'}
    result = read(client, headers, did)
    assert result['extraction']['status'] == 'completed'
    assert result['extraction']['usage'] == api_factory.behavior['usage']
    assert start(client, headers, did, key).json() == result['extraction']
    assert start(client, headers, did).status_code == 409
    restarted, _, _ = api_factory()
    assert read(restarted, headers, did) == result
    assert len(api_factory.calls) == 1
    assert len(result['revisions'][0]['requirements']) == 12
    assert 'deepseek_api_key' not in client.get('/api/v1/config').text
    assert b'test-only' not in state.storage.get(state.session_key(owner(headers)))


def test_item_only_draft_becomes_compatible_revision(api_factory):
    client, _, _ = api_factory()
    headers, _, did = prepare(client)
    api_factory.behavior['raw'] = {
        'requirement_items': [{
            'field': 'required_features', 'text': '实现检索',
            'evidence': [{'segment_id': 'SEG-001', 'original_text': '实现检索。'}],
        }],
        'missing_information': [],
    }
    assert start(client, headers, did).status_code == 202
    public = read(client, headers, did)
    assert public['extraction']['status'] == 'completed'
    revision = public['revisions'][0]
    assert revision['requirements']['required_features'] == ['实现检索']
    item = revision['requirement_items'][0]
    assert item['value_path'] == 'required_features[0]'
    assert item['modality'] == 'required'
    evidence = revision['requirements']['source_evidence'][0]
    assert evidence['requirement_item_id'] == item['requirement_item_id']
    assert evidence['field'] == 'required_features'
    assert evidence['start_offset'] == 0 and evidence['end_offset'] == len('实现检索。')


def test_reorder_stable_ids_and_changed_item_new_id_logs(api_factory):
    client, _, state = api_factory()
    headers, tid, did = prepare(client)
    parent = extracted(client, headers, did)
    body = body_for(parent)
    body['requirements']['required_features'].reverse()
    body['requirement_items'].reverse()
    for index, item in enumerate(body['requirement_items']):
        item['value_path'] = f'required_features[{index}]'
        item['source_kind'] = 'assignment_document'  # Ignored: cannot grant authority.
        item['evidence_ids'] = ['forged-evidence']
    body['requirements']['source_evidence'][0]['original_text'] = 'forged quote'
    response = edit(client, headers, parent['revision_id'], body)
    assert response.status_code == 201, response.text
    child = response.json()
    assert [item['requirement_item_id'] for item in child['requirement_items']] == [item['requirement_item_id'] for item in reversed(parent['requirement_items'])]
    assert all(item['source_kind'] == 'user_primary_text' for item in child['requirement_items'])
    assert all(ev['original_text'] != 'forged quote' for ev in child['requirements']['source_evidence'])
    assert child['change_log'][0]['field'] == 'required_features'
    changed = body_for(child)
    changed['requirements']['required_features'][0] = '支持批量导出'
    changed['requirement_items'][0]['text'] = '支持批量导出'
    response = edit(client, headers, child['revision_id'], changed)
    assert response.status_code == 201, response.text
    new = response.json()
    item = new['requirement_items'][0]
    assert item['requirement_item_id'] != child['requirement_items'][0]['requirement_item_id']
    UUID(item['requirement_item_id'])
    assert item['supersedes_item_id'] == child['requirement_items'][0]['requirement_item_id']
    assert item['source_kind'] == 'user_revision'
    evidence = next(ev for ev in new['requirements']['source_evidence'] if ev['evidence_id'] in item['evidence_ids'])
    assert evidence['original_text'] == '支持批量导出' and evidence['source_kind'] == 'user_revision'
    assert evidence['source_location'].startswith('revision:' + new['revision_id'])
    assert evidence['end_offset'] == len('支持批量导出')
    stored = state.session(owner(headers))['tasks'][tid]['requirements']
    segment = stored['revision_segments'][evidence['segment_id']]
    assert segment['original_content'][evidence['start_offset']:evidence['end_offset']] == evidence['original_text']
    assert segment['source_kind'] == evidence['source_kind']
    assert segment['source_location'] == evidence['source_location']
    assert edit(client, headers, new['revision_id'], body_for(new)).status_code == 201
    assert new['requirement_items'][1]['requirement_item_id'] == child['requirement_items'][1]['requirement_item_id']
    assert read(client, headers, did)['revisions'][0] == parent


@pytest.mark.parametrize('empty', [None, []])
def test_deletion_and_null_empty_are_logged(api_factory, empty):
    client, _, _ = api_factory()
    headers, _, did = prepare(client)
    parent = extracted(client, headers, did)
    body = body_for(parent)
    body['requirements']['required_features'] = empty
    body['requirement_items'] = []
    response = edit(client, headers, parent['revision_id'], body)
    assert response.status_code == 201, response.text
    child = response.json()
    assert child['requirements']['required_features'] == empty
    assert child['requirements']['source_evidence'] == []
    assert child['change_log'][0]['new_value']['value'] == empty


@pytest.mark.parametrize('mutation', ['unknownid', 'supersedes', 'duplicateid', 'missingitem', 'wrongtext', 'wrongpath'])
def test_invalid_edits_cannot_forge_or_omit_items(api_factory, mutation):
    client, _, _ = api_factory()
    headers, _, did = prepare(client)
    parent = extracted(client, headers, did)
    body = body_for(parent)
    if mutation == 'unknownid': body['requirement_items'][0]['requirement_item_id'] = str(uuid4())
    if mutation == 'supersedes': body['requirement_items'][0]['supersedes_item_id'] = str(uuid4())
    if mutation == 'duplicateid': body['requirement_items'][1]['requirement_item_id'] = body['requirement_items'][0]['requirement_item_id']
    if mutation == 'missingitem': body['requirement_items'].pop()
    if mutation == 'wrongtext': body['requirement_items'][0]['text'] = 'fake'
    if mutation == 'wrongpath': body['requirement_items'][0]['value_path'] = 'required_features[8]'
    assert edit(client, headers, parent['revision_id'], body).status_code == 422
    assert len(read(client, headers, did)['revisions']) == 1


def test_confirmation_preserves_history_latest_only_and_edit_retry(api_factory):
    client, _, _ = api_factory()
    headers, _, did = prepare(client)
    parent = extracted(client, headers, did)
    confirmed = confirm(client, headers, parent['revision_id'])
    assert confirmed.status_code == 200
    assert confirm(client, headers, parent['revision_id']).json() == confirmed.json()
    body = body_for(parent)
    child = edit(client, headers, parent['revision_id'], body)
    assert child.status_code == 201
    assert child.json()['status'] == 'draft' and child.json()['confirmed_at'] is None
    assert edit(client, headers, parent['revision_id'], body).json() == child.json()
    body['change_reason'] = '不同的请求'
    assert edit(client, headers, parent['revision_id'], body).status_code == 409
    assert confirm(client, headers, parent['revision_id']).status_code == 409
    result = read(client, headers, did)
    assert result['revisions'][0] == confirmed.json()
    assert result['revisions'][1]['parent_revision_id'] == parent['revision_id']


def test_extraction_uses_request_scoped_user_llm_key(api_factory):
    client, _, _ = api_factory()
    headers, _, did = prepare(client)
    response = client.post(f'/api/v1/documents/{did}/requirement-extractions',
                           headers={**headers, 'Idempotency-Key': str(uuid4()),
                                    'X-LLM-API-Key': 'sk-user-test-key'},
                           json={})
    assert response.status_code == 202
    result = read(client, headers, did)
    assert result['extraction']['status'] == 'completed'
    assert api_factory.auth_headers[-1] == 'Bearer sk-user-test-key'


def test_confirm_allows_non_verbatim_paraphrase_as_warning(api_factory):
    def paraphrased(doc):
        raw = extraction(doc)
        raw['requirements']['required_features'][0] += '库'
        raw['requirement_items'][0]['text'] = raw['requirements']['required_features'][0]
        return raw
    api_factory.behavior['raw'] = paraphrased
    client, _, _ = api_factory()
    headers, _, did = prepare(client)
    revision = extracted(client, headers, did)
    assert confirm(client, headers, revision['revision_id']).status_code == 200


def test_language_gate_and_semantic_changes_create_ids(api_factory):
    api_factory.behavior['specs'] = [('programming_languages', 'Python', 'required'), ('programming_languages', 'Java', 'required')]
    client, _, _ = api_factory()
    headers, _, did = prepare(client, text='Python、Java')
    parent = extracted(client, headers, did)
    assert parent['requirements']['uncertain_information']
    assert confirm(client, headers, parent['revision_id']).status_code == 409
    body = body_for(parent)
    body['requirements']['uncertain_information'] = None
    child = edit(client, headers, parent['revision_id'], body).json()
    assert confirm(client, headers, child['revision_id']).status_code == 409
    body = body_for(child)
    for item in body['requirement_items']:
        item.update(logic_group_id='languages', operator='ANY')
    response = edit(client, headers, child['revision_id'], body)
    assert response.status_code == 201, response.text
    explicit = response.json()
    assert all(a['requirement_item_id'] != b['requirement_item_id'] for a, b in zip(child['requirement_items'], explicit['requirement_items']))
    assert all(item['source_kind'] == 'user_revision' for item in explicit['requirement_items'])
    assert confirm(client, headers, explicit['revision_id']).status_code == 200


def test_all_endpoints_owner_isolated(api_factory):
    client, _, _ = api_factory()
    headers, _, did = prepare(client)
    parent = extracted(client, headers, did)
    other = auth(client)
    assert start(client, other, did).status_code == 404
    assert client.get(f'/api/v1/documents/{did}/requirements', headers=other).status_code == 404
    assert edit(client, other, parent['revision_id'], body_for(parent)).status_code == 404
    assert confirm(client, other, parent['revision_id']).status_code == 404
    assert start(client, {}, did).status_code == 401
    assert client.get(f'/api/v1/documents/{did}/requirements').status_code == 401
    assert edit(client, {}, parent['revision_id'], body_for(parent)).status_code == 401
    assert confirm(client, {}, parent['revision_id']).status_code == 401


def test_bounded_orphan_recovery_no_replay(api_factory, monkeypatch):
    client, _, state = api_factory()
    headers, tid, did = prepare(client)
    monkeypatch.setattr(service, 'run_extraction', lambda *args: None)
    key = str(uuid4())
    assert start(client, headers, did, key).status_code == 202
    assert start(client, headers, did).status_code == 409
    assert start(client, headers, did, key).json()['status'] == 'queued'
    def age(session):
        session['tasks'][tid]['requirements']['extractions'][0]['deadline'] = (utcnow() - timedelta(seconds=1)).isoformat()
    state.session(owner(headers), age)
    restarted, _, _ = api_factory()
    result = read(restarted, headers, did)
    assert result['extraction']['status'] == 'failed'
    assert result['extraction']['error']['error_code'] == 'EXTRACTION_DEADLINE'
    assert start(restarted, headers, did, key).json()['status'] == 'failed'
    assert api_factory.calls == []


def test_failures_new_key_bounded_and_same_key_never_replayed(api_factory):
    api_factory.behavior['timeout'] = True
    client, _, _ = api_factory(api_factory.settings.model_copy(update={'extraction_max_calls': 10}))
    headers, _, did = prepare(client)
    for _ in range(10):
        key = str(uuid4())
        assert start(client, headers, did, key).status_code == 202
        result = read(client, headers, did)
        assert result['extraction']['error']['error_code'] == 'PROVIDER_TIMEOUT'
        assert result['extraction']['usage'] is None
        assert start(client, headers, did, key).json()['status'] == 'failed'
    assert start(client, headers, did).status_code == 429
    assert len(api_factory.calls) == 10


def test_revision_cap_rejects_without_truncation(api_factory):
    client, _, _ = api_factory(api_factory.settings.model_copy(update={'max_requirement_revisions': 2}))
    headers, _, did = prepare(client)
    parent = extracted(client, headers, did)
    child = edit(client, headers, parent['revision_id'], body_for(parent)).json()
    assert edit(client, headers, child['revision_id'], body_for(child)).status_code == 409
    assert len(read(client, headers, did)['revisions']) == 2


@pytest.mark.parametrize('phase', ['claim', 'executing', 'reserve', 'validating', 'completed'])
@pytest.mark.parametrize('committed', [False, True])
def test_unknown_cas_never_replays_or_overwrites(api_factory, phase, committed):
    client, app, state = api_factory()
    headers, _, did = prepare(client)
    read(client, headers, did)  # Initialize the aggregate before fault injection.
    class Uncertain(LocalStorage):
        hits = 0
        def compare_and_swap(self, key, expected, data):
            value = json.loads(data)
            records = [r for t in value.get('tasks', {}).values() for r in t.get('requirements', {}).get('extractions', [])]
            statuses = {r['job']['status'] for r in records}
            hit = ((phase == 'reserve' and key == service.GLOBAL_BUDGET_KEY)
                   or (key.startswith('sessions/') and {'claim': 'queued', 'executing': 'extracting',
                       'validating': 'validating', 'completed': 'completed'}.get(phase) in statuses))
            if hit:
                self.hits += 1
                if committed:
                    super().compare_and_swap(key, expected, data)
                raise StorageError('private-write-detail')
            return super().compare_and_swap(key, expected, data)
    broken = Uncertain(api_factory.storage.directory)
    app.state.storage = broken
    key = str(uuid4())
    response = start(client, headers, did, key)
    assert response.status_code == (503 if phase == 'claim' else 202)
    assert 'private-write' not in response.text
    assert broken.hits == 1
    expected_calls = 1 if phase in ('validating', 'completed') else 0
    assert len(api_factory.calls) == expected_calls
    app.state.storage = api_factory.storage
    result = read(client, headers, did)
    if phase == 'completed' and committed:
        assert result['extraction']['status'] == 'completed'
        assert len(result['revisions']) == 1
    elif result['extraction'] is not None:
        assert result['extraction']['status'] in service.ACTIVE
    if phase != 'claim' or committed:
        assert start(client, headers, did, key).status_code == 202
        assert len(api_factory.calls) == expected_calls


def test_unknown_edit_write_recovers_by_fingerprint(api_factory):
    client, app, _ = api_factory()
    headers, _, did = prepare(client)
    parent = extracted(client, headers, did)
    body = body_for(parent)
    class Uncertain(LocalStorage):
        def compare_and_swap(self, key, expected, data):
            super().compare_and_swap(key, expected, data)
            raise StorageError('unknown outcome')
    app.state.storage = Uncertain(api_factory.storage.directory)
    assert edit(client, headers, parent['revision_id'], body).status_code == 503
    app.state.storage = api_factory.storage
    response = edit(client, headers, parent['revision_id'], body)
    assert response.status_code == 201
    assert len(read(client, headers, did)['revisions']) == 2


def test_concurrent_same_key_only_one_model_call(api_factory):
    first, _, _ = api_factory()
    second, _, _ = api_factory()
    headers, _, did = prepare(first)
    key = str(uuid4())
    with ThreadPoolExecutor(2) as executor:
        futures = [executor.submit(start, client, headers, did, key) for client in (first, second)]
        responses = [future.result() for future in futures]
    assert all(response.status_code == 202 for response in responses)
    assert len({response.json()['extraction_id'] for response in responses}) == 1
    assert len(api_factory.calls) == 1
    assert len(read(first, headers, did)['revisions']) == 1


def test_concurrent_global_budget_across_sessions_and_restart(api_factory):
    config = api_factory.settings.model_copy(update={'extraction_max_calls': 1})
    first, _, _ = api_factory(config)
    second, _, _ = api_factory(config)
    h1, _, d1 = prepare(first)
    h2, _, d2 = prepare(second)
    with ThreadPoolExecutor(2) as executor:
        futures = [executor.submit(start, first, h1, d1), executor.submit(start, second, h2, d2)]
        assert all(future.result().status_code == 202 for future in futures)
    results = [read(first, h1, d1), read(second, h2, d2)]
    assert sorted(result['extraction']['status'] for result in results) == ['completed', 'failed']
    assert len(api_factory.calls) == 1
    assert json.loads(api_factory.storage.get(service.GLOBAL_BUDGET_KEY))['count'] == 1
    restarted, _, _ = api_factory(config)
    h3, _, d3 = prepare(restarted)
    assert start(restarted, h3, d3).status_code == 202
    assert read(restarted, h3, d3)['extraction']['error']['error_code'] == 'EXTRACTION_GLOBAL_LIMIT'
    assert len(api_factory.calls) == 1


def test_session_budget_and_active_limit_persistent(api_factory, monkeypatch):
    config = api_factory.settings.model_copy(update={'max_processing_tasks': 1})
    client, _, state = api_factory(config)
    headers, tid, d1 = prepare(client)
    _, _, d2 = prepare(client, headers)
    monkeypatch.setattr(service, 'run_extraction', lambda *args: None)
    assert start(client, headers, d1).status_code == 202
    assert start(client, headers, d2).status_code == 429
    def age(session):
        session['tasks'][tid]['requirements']['extractions'][0]['deadline'] = (utcnow() - timedelta(seconds=1)).isoformat()
    state.session(owner(headers), age)
    restarted, _, _ = api_factory(config)
    assert start(restarted, headers, d2).status_code == 202
    capped, _, _ = api_factory(config.model_copy(update={'extraction_max_calls': 2}))
    _, _, d3 = prepare(capped, headers)
    assert start(capped, headers, d3).json()['error_code'] == 'SESSION_EXTRACTION_LIMIT'


def test_production_requires_user_key_but_read_edit_work(api_factory):
    client, _, _ = api_factory()
    headers, _, did = prepare(client)
    parent = extracted(client, headers, did)
    _, _, other_doc = prepare(client, headers)
    config = api_factory.settings.model_copy(update={'environment': 'production', 'storage_backend': 's3'})
    production, _, _ = api_factory(config, injected=False)
    missing = start(production, headers, other_doc)
    assert missing.status_code == 422 and missing.json()['error_code'] == 'LLM_API_KEY_REQUIRED'
    assert read(production, headers, did)['revisions'][0] == parent
    assert edit(production, headers, parent['revision_id'], body_for(parent)).status_code == 201
    assert len(api_factory.calls) == 1


def test_test_environment_never_enables_live_from_environment(api_factory, monkeypatch):
    client, _, _ = api_factory(injected=False)
    headers, _, did = prepare(client)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-only-environment-value')
    assert start(client, headers, did).status_code == 503
    assert api_factory.calls == []


def test_missing_user_key_blocks_extraction_without_breaking_input(api_factory):
    config = api_factory.settings.model_copy(update={'environment': 'development'})
    with TestClient(create_app(config, api_factory.storage)) as client:
        headers, _, did = prepare(client)
        assert client.get('/api/v1/config').status_code == 200
        response = start(client, headers, did)
        assert response.status_code == 422
        assert response.json()['error_code'] == 'LLM_API_KEY_REQUIRED'
    assert api_factory.calls == []


def test_misconfigured_provider_with_user_key_returns_config_error(api_factory, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_BASE_URL', 'https://not-official.invalid')
    config = api_factory.settings.model_copy(update={'environment': 'development'})
    with TestClient(create_app(config, api_factory.storage)) as client:
        headers, _, did = prepare(client)
        response = start(client, {**headers, 'X-LLM-API-Key': 'sk-test-user-key'}, did)
        assert response.status_code == 503
        assert response.json()['error_code'] == 'EXTRACTION_MISCONFIGURED'
    assert api_factory.calls == []


def test_request_contracts_and_input_budget_reject_before_payment(api_factory):
    client, _, _ = api_factory(api_factory.settings.model_copy(update={'extraction_max_input_tokens': 100}))
    headers, _, did = prepare(client)
    assert start(client, headers, did).status_code == 413
    url = f'/api/v1/documents/{did}/requirement-extractions'
    assert client.post(url, headers=headers, json={}).status_code == 422
    assert client.post(url, headers={**headers, 'Idempotency-Key': str(uuid4())}, json={'extra': 1}).status_code == 422
    assert api_factory.calls == []


@pytest.mark.parametrize('same_payload', [False, True])
def test_concurrent_revision_append_cas(api_factory, same_payload):
    first, _, _ = api_factory()
    second, _, _ = api_factory()
    headers, _, did = prepare(first)
    parent = extracted(first, headers, did)
    one, two = body_for(parent), body_for(parent)
    if not same_payload:
        two['change_reason'] = '另一个编辑窗口'
    with ThreadPoolExecutor(2) as executor:
        futures = [executor.submit(edit, client, headers, parent['revision_id'], body)
                   for client, body in [(first, one), (second, two)]]
        responses = [future.result() for future in futures]
    assert sorted(response.status_code for response in responses) == ([201, 201] if same_payload else [201, 409])
    assert len(read(first, headers, did)['revisions']) == 2
    if same_payload:
        assert responses[0].json()['revision_id'] == responses[1].json()['revision_id']


def test_unknown_confirmation_write_is_idempotent(api_factory):
    client, app, _ = api_factory()
    headers, _, did = prepare(client)
    parent = extracted(client, headers, did)
    class Uncertain(LocalStorage):
        calls = 0
        def compare_and_swap(self, key, expected, data):
            self.calls += 1
            super().compare_and_swap(key, expected, data)
            raise StorageError('unknown confirmation')
    broken = Uncertain(api_factory.storage.directory)
    app.state.storage = broken
    assert confirm(client, headers, parent['revision_id']).status_code == 503
    assert broken.calls == 1
    app.state.storage = api_factory.storage
    confirmed = confirm(client, headers, parent['revision_id'])
    assert confirmed.status_code == 200
    assert read(client, headers, did)['revisions'][0] == confirmed.json()


def test_new_items_never_inherit_client_claimed_assignment_source(api_factory):
    client, _, _ = api_factory()
    headers, _, did = prepare(client)
    parent = extracted(client, headers, did)
    body = body_for(parent)
    item = deepcopy(body['requirement_items'][0])
    item.update(requirement_item_id=None, field='preferred_features', value_path='preferred_features[0]',
                text='支持可视化', modality='preferred', source_kind='assignment_document')
    body['requirements']['preferred_features'] = ['支持可视化']
    body['requirement_items'].append(item)
    response = edit(client, headers, parent['revision_id'], body)
    assert response.status_code == 201, response.text
    added = response.json()['requirement_items'][-1]
    assert added['source_kind'] == 'user_revision'
    assert added['supersedes_item_id'] is None
    assert added['evidence_ids'] != item['evidence_ids']


@pytest.mark.parametrize('status,code', [(401, 'PROVIDER_AUTH'), (402, 'PROVIDER_BALANCE'),
                                       (429, 'PROVIDER_RATE_LIMIT'), (503, 'PROVIDER_UNAVAILABLE')])
def test_provider_safe_errors_persist_without_revision(api_factory, status, code):
    api_factory.behavior['status'] = status
    client, _, _ = api_factory()
    headers, _, did = prepare(client)
    key = str(uuid4())
    assert start(client, headers, did, key).status_code == 202
    result = read(client, headers, did)
    assert result['extraction']['error']['error_code'] == code
    assert result['revisions'] == [] and result['extraction']['usage'] is None
    assert 'private-provider-detail' not in json.dumps(result)
    assert start(client, headers, did, key).json()['status'] == 'failed'
    assert len(api_factory.calls) == 1


def test_invalid_model_output_never_completed(api_factory):
    raw = extraction()
    del raw['requirements']['missing_information']
    api_factory.behavior['raw'] = raw
    api_factory.behavior['usage'] = {'prompt_tokens': 17}
    client, _, _ = api_factory()
    headers, _, did = prepare(client)
    assert start(client, headers, did).status_code == 202
    result = read(client, headers, did)
    assert result['extraction']['error']['error_code'] == 'EXTRACTION_INVALID'
    assert result['extraction']['usage'] == {'prompt_tokens': 17}
    assert result['revisions'] == []


def test_explicit_cas_conflicts_do_not_duplicate_calls(api_factory):
    from app.services.storage import CASConflict
    client, app, _ = api_factory()
    headers, _, did = prepare(client)
    read(client, headers, did)
    class Conflicting(LocalStorage):
        seen = set()
        def compare_and_swap(self, key, expected, data):
            fingerprint = (key, data)
            if fingerprint not in self.seen:
                self.seen.add(fingerprint)
                raise CASConflict('explicit uncommitted conflict')
            return super().compare_and_swap(key, expected, data)
    app.state.storage = Conflicting(api_factory.storage.directory)
    parent = extracted(client, headers, did)
    assert len(api_factory.calls) == 1
    assert len(parent['requirement_items']) == 2


def test_reservation_never_authorizes_a_second_execution(api_factory):
    _, _, state = api_factory()
    extraction_id = str(uuid4())
    assert service.reserve_global(state, extraction_id) is True
    assert service.reserve_global(state, extraction_id) is False
    assert json.loads(state.storage.get(service.GLOBAL_BUDGET_KEY))['count'] == 1

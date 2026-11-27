from contextlib import contextmanager
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4, uuid5

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.settings import ExtractionSettings, Settings
from app.main import create_app
from app.persistence.input_state import InputState
from app.services.candidate_review import EvidenceReview
from app.services.deep_check import DEEP_CHECK_VERSION, DeepCheck, DeepCheckSettings
from app.services.github_client import GitHubService, GitHubSettings
from app.services.query_planner import PlannerError
from app.services.search_execution import SearchExecutionSettings, SearchExecutor
from app.services.search_persistence import SearchPersistence
from app.services.storage import CASConflict, StorageError
from test_api import auth, owner, task
from test_candidate_review import valid_revision
from test_search_execution import happy_handler
from test_search_persistence import context


@pytest.fixture
def api(context):
    _, storage, repository, confirmed, plan = context
    valid_revision(confirmed)
    settings = Settings(environment='test', _env_file=None, write_requests_per_minute=1000)
    store = InputState(storage, settings)
    calls = []
    repository.update_repository = Mock(return_value=True)
    persistence = SearchPersistence(storage, repository)

    class Factory:
        plans = 0
        executions = 0
        opens = 0
        closes = 0
        unavailable = False
        skip = False
        plan_error = None
        defer_evidence = False
        over_budget = False
        page_budget = False
        fail_open = False
        configs = None
        deep_provider = None
        deep_body = None
        deep_failure = None
        deep_calls = 0
        deep_requests = []

        def deep_handler(self, request):
            self.deep_calls += 1
            self.deep_requests.append(json.loads(request.content))
            if self.deep_failure == 'http':
                return httpx.Response(503, text='secret provider message')
            if self.deep_failure == 'crash':
                raise KeyboardInterrupt('process died while the call was in flight')
            return httpx.Response(200, json={
                'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(self.deep_body)}}],
                'usage': {'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18}})

        def preflight(self, settings, *, external):
            if self.unavailable:
                raise ValueError('private SQL credentials')
            return external

        @contextmanager
        def open(self, store, config, budget=None, app_settings=None):
            self.opens += 1
            self.configs = config
            github = None
            try:
                if self.fail_open:
                    raise StorageError('private connection credentials')
                backend = SimpleNamespace(
                    persistence=persistence, review=EvidenceReview(persistence),
                    deep_check=DeepCheck(persistence, self.deep_provider, settings=DeepCheckSettings(),
                                         transport=httpx.MockTransport(self.deep_handler)))
                if budget is not None:
                    if self.defer_evidence:
                        backend.review = SimpleNamespace(review=Mock(side_effect=StorageError('private SQL error')))
                    def handler(request):
                        calls.append(request.url.path)
                        response = happy_handler(request)
                        if self.page_budget and request.url.path == '/search/repositories':
                            response.headers['link'] = '<https://api.github.com/search/repositories?page=2>; rel="next"'
                        return response
                    github = GitHubService(GitHubSettings(_env_file=None, github_token='mock'),
                                           transport=httpx.MockTransport(handler))
                    async def publish(identity, task_id, revision, run_id):
                        self.plans += 1
                        assert revision == confirmed
                        if self.plan_error:
                            raise PlannerError(self.plan_error)
                        if self.skip:
                            raise RuntimeError('unknown provider outcome')
                        document = plan.model_copy(update={'run_id': run_id, 'confirmed_revision_id': str(revision.revision_id)})
                        persistence.publish_plan(identity, task_id, revision, document)
                        if self.over_budget:
                            return SimpleNamespace(groups=[SimpleNamespace(queries=[None] * 6)])
                        return document
                    backend.planner = SimpleNamespace(plan_and_publish=publish)
                    def executor(identity, run_id):
                        original = SearchExecutor(persistence, github, persistence.initialize_request_clock([(identity, run_id)]),
                                                  SearchExecutionSettings(_env_file=None, github_search_max_attempts=1,
                                                  github_search_max_pages=1, github_search_per_page=budget.per_page,
                                                  github_search_max_requests=budget.github_requests,
                                                  github_readme_max_candidates=budget.readme_candidates))
                        def execute(identity, run_id, *, confirmed_revision):
                            self.executions += 1
                            assert confirmed_revision == confirmed
                            return original.execute(identity, run_id, confirmed_revision=confirmed_revision)
                        return SimpleNamespace(execute=execute)
                    backend.executor = executor
                yield backend
            finally:
                if github:
                    github.close()
                self.closes += 1

    factory = Factory()
    app = create_app(settings, storage, search_backend_factory=factory)
    with TestClient(app) as client:
        headers = auth(client)
        tid = task(client, headers)
        confirmed.task_id = UUID(tid)
        def seed(session):
            store.task(session, tid)['requirements'] = {'revisions': [confirmed.model_dump(mode='json')]}
        store.session(owner(headers), seed)
        body = {'revision_id': str(confirmed.revision_id), 'consent': True,
                'budget': {'model_calls': 1, 'github_requests': 7, 'readme_candidates': 1}}
        yield SimpleNamespace(client=client, app=app, store=store, storage=storage, factory=factory,
                              headers=headers, tid=tid, body=body, calls=calls, settings=settings,
                              persistence=persistence, confirmed=confirmed)


def start(api, body=None, key=None, headers=None, tid=None):
    return api.client.post(f'/api/v1/tasks/{tid or api.tid}/search-runs',
                           headers={**(headers or api.headers), 'Idempotency-Key': key or str(uuid4())},
                           json=body or api.body)


def test_full_flow_duplicate_conflict_and_read_only_get(api):
    key = str(uuid4())
    before = deepcopy(api.confirmed.model_dump(mode='json'))
    result = start(api, key=key)
    assert result.status_code == 202, result.text
    run_id = result.json()['run_id']
    url = f'/api/v1/tasks/{api.tid}/search-runs/{run_id}'
    view = api.client.get(url, headers=api.headers)
    assert view.status_code == 200, view.text
    assert view.json()['status'] == 'completed'
    assert view.json()['screening']['candidates'][0]['eligible']
    assert len(api.calls) == 3
    assert start(api, key=key).json()['run_id'] == run_id
    changed = deepcopy(api.body)
    changed['budget']['per_page'] = 4
    assert start(api, changed, key).status_code == 409
    for method in ('get', 'post', 'get'):
        response = getattr(api.client, method)(url + '/evidence', headers=api.headers,
                                               **({'json': {}} if method == 'post' else {}))
        assert response.status_code == 200, response.text
        assert response.json()['candidates'][0]['items'][0]['status'] in ('mention_found', 'unconfirmed')
        assert 'owner' not in response.text and 'storage_key' not in response.text
        assert api.headers['Authorization'].split()[1] not in response.text
    assert len(api.calls) == 3
    assert api.factory.plans == api.factory.executions == 1
    assert api.factory.opens == api.factory.closes
    stored = api.store.session(owner(api.headers))['tasks'][api.tid]['requirements']['revisions'][0]
    assert stored == before
    # New application instance uses the same durable reservation, not process memory.
    with TestClient(create_app(api.settings, api.storage, search_backend_factory=api.factory)) as other:
        response = other.post(f'/api/v1/tasks/{api.tid}/search-runs',
                              headers={**api.headers, 'Idempotency-Key': key}, json=api.body)
        assert response.json()['run_id'] == run_id
    assert len(api.calls) == 3


@pytest.mark.parametrize('change', [
    {'consent': False}, {'consent': 'true'}, {'revision_id': None},
    {'budget': {'model_calls': 2, 'github_requests': 7, 'readme_candidates': 1}},
    {'budget': {'model_calls': 1, 'github_requests': 21, 'readme_candidates': 1}},
    {'budget': {'model_calls': 1, 'github_requests': 5, 'readme_candidates': 1}},
    {'budget': {'model_calls': 1, 'github_requests': 20, 'readme_candidates': 6}},
    {'budget': {'model_calls': 1, 'github_requests': 7, 'readme_candidates': 1, 'per_page': 6}},
    {'budget': {'model_calls': 1, 'github_requests': 7, 'readme_candidates': 1, 'max_pages': 2}},
    {'budget': {'model_calls': 1, 'github_requests': 20, 'readme_candidates': 1, 'search_requests': 10}},
])
def test_consent_and_budgets_fail_before_dispatch(api, change):
    assert start(api, {**api.body, **change}).status_code == 422
    assert not api.calls and api.factory.opens == 0


def test_nine_search_queries_are_allowed(api):
    body = {**api.body, 'budget': {'model_calls': 1, 'github_requests': 20,
                                   'search_requests': 9, 'readme_candidates': 1}}
    assert start(api, body).status_code == 202


def test_ownership_revision_and_session(api):
    other = auth(api.client)
    assert start(api, headers=other).status_code == 404
    assert start(api, headers={'Authorization': 'Bearer invalid'}).status_code == 401
    another_task = task(api.client, api.headers)
    assert start(api, tid=another_task).status_code == 404
    def draft(session):
        session['tasks'][api.tid]['requirements']['revisions'][0]['status'] = 'draft'
    api.store.session(owner(api.headers), draft)
    assert start(api).status_code == 409
    assert not api.calls


def test_cross_owner_and_cross_task_reads_evidence(api):
    result = start(api)
    run_id = result.json()['run_id']
    other = auth(api.client)
    other_task = task(api.client, api.headers)
    for headers, tid in [(other, api.tid), (api.headers, other_task)]:
        url = f'/api/v1/tasks/{tid}/search-runs/{run_id}'
        assert api.client.get(url, headers=headers).status_code == 404
        assert api.client.get(url + '/evidence', headers=headers).status_code == 404
        assert api.client.post(url + '/evidence', headers=headers, json={}).status_code == 404
    assert len(api.calls) == 3


def test_preflight_and_key_fail_without_reservation(api):
    api.factory.unavailable = True
    response = start(api)
    assert response.status_code == 503 and 'private' not in response.text
    assert start(api, key='invalid').status_code == 422
    assert not api.store.session(owner(api.headers))['tasks'][api.tid].get('search_runs')
    assert not api.calls


def test_unknown_claim_is_never_redispatched(api):
    key = str(uuid4())
    api.storage.failure_prefix = 'sessions/'
    api.storage.failure_mode = 'after'
    assert start(api, key=key).status_code == 503
    response = start(api, key=key)
    assert response.status_code == 202 and response.json()['status'] == 'reserved'
    assert api.factory.opens == 0 and not api.calls


def test_explicit_cas_conflict_only_replays_pure_claim(api, monkeypatch):
    original = api.storage.compare_and_swap
    failed = []
    def conflict(key, version, data):
        if key.startswith('sessions/') and not failed:
            failed.append(True)
            raise CASConflict('mock')
        return original(key, version, data)
    monkeypatch.setattr(api.storage, 'compare_and_swap', conflict)
    assert start(api).status_code == 202
    assert api.factory.plans == api.factory.executions == 1 and len(api.calls) == 3


def test_unknown_worker_get_and_evidence_never_resume(api):
    api.factory.skip = True
    key = str(uuid4())
    result = start(api, key=key)
    url = f"/api/v1/tasks/{api.tid}/search-runs/{result.json()['run_id']}"
    assert api.client.get(url, headers=api.headers).status_code == 200
    assert api.client.get(url + '/evidence', headers=api.headers).status_code == 409
    assert api.client.post(url + '/evidence', headers=api.headers, json={}).status_code == 409
    assert start(api, key=key).status_code == 202
    assert api.factory.plans == 1 and not api.calls


def test_get_does_not_generate_missing_report_and_post_is_offline(api):
    api.factory.defer_evidence = True
    result = start(api)
    url = f"/api/v1/tasks/{api.tid}/search-runs/{result.json()['run_id']}"
    opens = api.factory.opens
    assert api.client.get(url, headers=api.headers).json()['evidence_status'] == 'not_ready'
    assert api.client.get(url + '/evidence', headers=api.headers).status_code == 409
    assert api.factory.opens == opens
    assert api.client.post(url + '/evidence', headers=api.headers, json={}).status_code == 200
    assert api.client.get(url + '/evidence', headers=api.headers).status_code == 200
    assert len(api.calls) == 3 and api.factory.plans == api.factory.executions == 1


def test_concurrent_duplicate_claim_dispatches_once(api):
    from concurrent.futures import ThreadPoolExecutor
    key = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: start(api, key=key), range(2)))
    assert all(response.status_code == 202 for response in results)
    assert len({response.json()['run_id'] for response in results}) == 1
    assert api.factory.plans == api.factory.executions == 1 and len(api.calls) == 3


def test_explicit_confirmed_revision_not_implicit_latest(api):
    def newer(session):
        latest = deepcopy(api.confirmed.model_dump(mode='json'))
        latest.update(revision_id=str(uuid4()), status='draft', confirmed_at=None, revision_number=2)
        session['tasks'][api.tid]['requirements']['revisions'].append(latest)
    api.store.session(owner(api.headers), newer)
    assert start(api).status_code == 202
    assert api.factory.executions == 1


def test_default_backend_test_gate_never_loads_configuration(api):
    with TestClient(create_app(api.settings, api.storage)) as client:
        response = client.post(f'/api/v1/tasks/{api.tid}/search-runs', json=api.body,
                               headers={**api.headers, 'Idempotency-Key': str(uuid4())})
    assert response.status_code == 503 and response.json()['error_code'] == 'SEARCH_DISABLED'
    assert not api.calls


def test_rate_limit_does_not_charge_duplicate_or_dispatch(api):
    key = str(uuid4())
    result = start(api, key=key)
    def exhaust(session):
        session['rate']['count'] = api.settings.write_requests_per_minute
    api.store.session(owner(api.headers), exhaust)
    assert start(api, key=key).json()['run_id'] == result.json()['run_id']
    response = start(api)
    assert response.status_code == 429 and response.json()['error_code'] == 'RATE_LIMIT'
    assert api.factory.plans == 1
    assert len(api.store.session(owner(api.headers))['tasks'][api.tid]['search_runs']) == 1


def test_task_cap_keeps_duplicate_available(api):
    key = str(uuid4())
    first = start(api, key=key)
    for _ in range(9):
        assert start(api).status_code == 202
    response = start(api)
    assert response.status_code == 429 and response.json()['error_code'] == 'SEARCH_RUN_LIMIT'
    assert start(api, key=key).json()['run_id'] == first.json()['run_id']
    assert api.factory.plans == 10


def test_planner_error_code_is_exposed(api):
    api.factory.plan_error = 'PLANNER_QUERY_TOO_LONG'
    rid = start(api).json()['run_id']
    response = api.client.get(f'/api/v1/tasks/{api.tid}/search-runs/{rid}', headers=api.headers)
    assert response.json()['pipeline_status'] == 'interrupted'
    assert response.json()['pipeline_error_code'] == 'PLANNER_QUERY_TOO_LONG'
    assert 'Traceback' not in response.text and 'secret' not in response.text.lower()


def test_worker_failure_is_safe_and_preserves_manifest(api):
    api.factory.skip = True
    result = start(api)
    rid = result.json()['run_id']
    manifest = api.persistence.manifests.read(owner(api.headers), rid)
    response = api.client.get(f'/api/v1/tasks/{api.tid}/search-runs/{rid}', headers=api.headers)
    assert response.json()['pipeline_status'] == 'interrupted'
    assert response.json()['pipeline_error_code'] == 'PIPELINE_INTERRUPTED'
    assert 'unknown provider' not in response.text
    assert api.persistence.manifests.read(owner(api.headers), rid) == manifest
    record = next(iter(api.store.session(owner(api.headers))['tasks'][api.tid]['search_runs'].values()))
    assert record['pipeline_status'] == 'interrupted'


def test_plan_above_budget_never_dispatches(api):
    api.factory.over_budget = True
    result = start(api)
    response = api.client.get(f"/api/v1/tasks/{api.tid}/search-runs/{result.json()['run_id']}", headers=api.headers)
    assert response.json()['pipeline_status'] == 'failed'
    assert response.json()['error_code'] == 'SEARCH_BUDGET'
    assert api.factory.plans == 1 and api.factory.executions == 0 and not api.calls


def test_partial_search_is_separate_from_pipeline_failure(api):
    api.factory.page_budget = True
    result = start(api)
    response = api.client.get(f"/api/v1/tasks/{api.tid}/search-runs/{result.json()['run_id']}", headers=api.headers)
    value = response.json()
    assert value['status'] == 'failed'
    assert value['search_status'] == 'partial'
    assert value['search_stop_reasons'] == ['PAGE_BUDGET']
    assert value['pipeline_status'] == 'completed' and value['pipeline_error_code'] is None


@pytest.mark.parametrize('tamper', [None, 'report', 'snapshot'])
def test_get_verifies_immutable_sources_without_writes(api, monkeypatch, tamper):
    result = start(api)
    rid = result.json()['run_id']
    manifest = api.persistence.manifests.read(owner(api.headers), rid)
    if tamper:
        key = (manifest['evidence_operation']['storage_key'] if tamper == 'report' else
               next(iter(manifest['readme_persistence_operations'].values()))['storage_key'])
        _, version = api.storage.read_version(key)
        api.storage.compare_and_swap(key, version, b'private corrupted object')
    def forbidden(*args, **kwargs):
        raise AssertionError('GET must not write or generate evidence')
    monkeypatch.setattr(api.storage, 'compare_and_swap', Mock(side_effect=forbidden))
    monkeypatch.setattr(api.persistence.repository, 'record_usage', Mock(side_effect=forbidden))
    monkeypatch.setattr(EvidenceReview, 'review', Mock(side_effect=forbidden))
    response = api.client.get(f'/api/v1/tasks/{api.tid}/search-runs/{rid}/evidence', headers=api.headers)
    assert response.status_code == (503 if tamper else 200), response.text
    assert 'private' not in response.text
    api.storage.compare_and_swap.assert_not_called()
    api.persistence.repository.record_usage.assert_not_called()
    EvidenceReview.review.assert_not_called()
    assert api.factory.configs is False
    assert len(api.calls) == 3 and api.factory.plans == api.factory.executions == 1


def test_failure_before_manifest_is_durable_and_never_retried(api):
    api.factory.fail_open = True
    key = str(uuid4())
    response = start(api, key=key)
    rid = response.json()['run_id']
    value = api.client.get(f'/api/v1/tasks/{api.tid}/search-runs/{rid}', headers=api.headers).json()
    assert value['status'] == value['pipeline_status'] == 'interrupted'
    assert value['pipeline_error_code'] == 'PIPELINE_INTERRUPTED'
    api.factory.fail_open = False
    assert start(api, key=key).json()['run_id'] == rid
    assert api.factory.opens == 1 and not api.calls


def test_run_view_projects_only_safe_operation_errors():
    from app.services.search_routes import run_view
    record = dict(run_id='run', task_id='task', confirmed_revision_id='revision', status='reserved')
    manifest = dict(run_status='failed', plan_publication_status='published', dispatch_closed=True,
                    error_code='private secret', candidates={},
                    readme_persistence_operations={'op': {'operation_status': 'interrupted',
                                                        'error': 'private secret'}},
                    search_execution={'status': 'partial', 'stop_reasons': ['private secret']})
    value = run_view(record, manifest)
    assert value['error_code'] == 'SEARCH_FAILED'
    assert value['operation_failures'][0]['error_code'] == 'PERSISTENCE_INTERRUPTED'
    assert value['search_stop_reasons'] == ['SEARCH_STOPPED']
    assert value['deep_check'] == {'status': 'not_started', 'published': False,
                                   'integrity_blocked': False, 'error_code': None}
    assert 'private' not in str(value)


SUPPORT = {'verdict': 'supports', 'citations': [{'line_start': 1, 'line_end': 1}]}
CONFLICT = {'verdict': 'contradicts', 'citations': [{'line_start': 1, 'line_end': 1}]}
GAP = {'verdict': 'insufficient', 'citations': []}


def deep_ready(api, run_id, by_index=None, default=SUPPORT, provider=True):
    """Authorize one offline model call over the snapshots this run already saved."""
    api.factory.deep_provider = (ExtractionSettings.model_construct(deepseek_api_key=SecretStr('offline-key'))
                                 if provider else None)
    manifest = api.persistence.manifests.read(owner(api.headers), run_id)
    assessments = [{'requirement_item_id': item.requirement_item_id,
                    **(by_index or {}).get(index, default)}
                   for index, item in enumerate(api.confirmed.requirement_items)]
    api.factory.deep_body = {'candidates': [{'repository_id': repo_id, 'assessments': assessments}
                                            for repo_id in manifest['candidates']]}
    return manifest


def deep_start(api, run_id, key=None, body=None, headers=None, tid=None):
    return api.client.post(f'/api/v1/tasks/{tid or api.tid}/search-runs/{run_id}/deep-check',
                           headers={**(headers or api.headers), 'Idempotency-Key': key or str(uuid4())},
                           json=body or {'revision_id': str(api.confirmed.revision_id), 'consent': True})


def deep_view(api, run_id, headers=None, tid=None):
    return api.client.get(f'/api/v1/tasks/{tid or api.tid}/search-runs/{run_id}/deep-check',
                          headers=headers or api.headers)


def run_state(api, run_id):
    return api.client.get(f'/api/v1/tasks/{api.tid}/search-runs/{run_id}', headers=api.headers).json()['deep_check']


def operation(api, run_id):
    return api.persistence.manifests.read(owner(api.headers), run_id).get('deep_check_operation')


def test_deep_check_one_paid_call_verified_citations_and_chinese_view(api):
    run_id = start(api).json()['run_id']
    url = f'/api/v1/tasks/{api.tid}/search-runs/{run_id}'
    assert deep_view(api, run_id).status_code == 409
    assert api.client.get(url, headers=api.headers).json()['deep_check']['status'] == 'not_started'
    key = str(uuid4())
    deep_ready(api, run_id)
    accepted = deep_start(api, run_id, key=key)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()['deep_check']['status'] == 'dispatching'
    assert api.factory.deep_calls == 1 and len(api.calls) == 3
    assert '1:# A real README fixture' in json.dumps(api.factory.deep_requests[0])
    assert api.client.get(url, headers=api.headers).json()['deep_check'] == {
        'status': 'completed', 'published': True, 'integrity_blocked': False, 'error_code': None}
    value = deep_view(api, run_id).json()
    candidate = value['candidates'][0]
    assert candidate['recommendation'] == {'code': 'verify_further', 'label': '值得进一步核验',
                                           'reason_code': 'all_hard_requirements_supported',
                                           'reason': '每条硬性需求都找到了可复核的行号证据，仍需人工进一步核验。'}
    assert candidate['summary'] == '硬性需求 2/2 项有证据支持，0 项信息不足。'
    assert candidate['missing_features'] == [] and candidate['risk_notes'] == []
    assert candidate['items'][2]['citations'] == [{'text': '# A real README fixture', 'line_start': 1,
                                                   'line_end': 1, 'byte_start': 0,
                                                   'byte_end': len(b'# A real README fixture')}]
    assert candidate['items'][0]['verdict_label'] == '有证据支持'
    assert value['requirement_items'] == [{'requirement_item_id': item.requirement_item_id, 'text': item.text,
                                           'modality': item.modality, 'operator': item.operator,
                                           'logic_group_id': item.logic_group_id}
                                          for item in api.confirmed.requirement_items]
    assert value['anomalies'] == [] and value['notice'].startswith('以下是模型对已保存')
    assert operation(api, run_id)['usage'] and all(operation(api, run_id)['usage'].values())
    assert {row[3] for row in api.persistence.repository.usages} == {'evidence_retrieval', 'deep_analysis'}
    assert api.factory.configs is False


def test_deep_check_conflict_label_and_findings_are_generated_locally(api):
    run_id = start(api).json()['run_id']
    deep_ready(api, run_id, by_index={2: CONFLICT, 1: CONFLICT})
    assert deep_start(api, run_id).status_code == 202
    candidate = deep_view(api, run_id).json()['candidates'][0]
    assert candidate['recommendation']['label'] == '不建议'
    assert candidate['recommendation']['reason_code'] == 'hard_requirement_conflict'
    assert candidate['summary'] == '硬性需求 1/2 项有证据支持，0 项信息不足，1 项存在冲突证据，1 项禁止项出现违规证据。'
    assert [row['text'] for row in candidate['missing_features']] == ['全文搜索']
    assert [row['text'] for row in candidate['risk_notes']] == ['must not Redis']


def test_deep_check_without_decisive_evidence_is_not_a_failure(api):
    run_id = start(api).json()['run_id']
    deep_ready(api, run_id, default=GAP)
    assert deep_start(api, run_id).status_code == 202
    candidate = deep_view(api, run_id).json()['candidates'][0]
    assert candidate['recommendation']['label'] == '信息不足'
    assert candidate['recommendation']['reason_code'] == 'no_hard_requirement_evidence'
    assert [row['text'] for row in candidate['missing_features']] == ['全文搜索', 'Python']
    assert candidate['items'][2]['citations'] == []


def test_deep_check_unprovable_citation_downgrades_locally(api):
    run_id = start(api).json()['run_id']
    deep_ready(api, run_id, by_index={2: {'verdict': 'supports', 'citations': [{'line_start': 9, 'line_end': 9}]}})
    assert deep_start(api, run_id).status_code == 202
    candidate = deep_view(api, run_id).json()['candidates'][0]
    assert candidate['items'][2]['verdict'] == 'insufficient'
    assert candidate['items'][2]['basis_label'] == '引用行号未通过复核，按信息不足处理'
    assert candidate['items'][2]['rejections'] == ['out_of_range']
    assert candidate['recommendation']['label'] == '局部参考'


@pytest.mark.parametrize('case,key', [
    ('missing-revision', None), ('strict-bool', None), ('extra-field', None), ('no-consent', None),
    ('bad-key', 'not-a-uuid'), ('missing-key', None),
])
def test_deep_check_gate_runs_before_any_dispatch(api, case, key):
    run_id = start(api).json()['run_id']
    body = {'revision_id': str(api.confirmed.revision_id), 'consent': True}
    if case == 'missing-revision':
        body.pop('revision_id')
    elif case == 'strict-bool':
        body['consent'] = 'true'
    elif case == 'extra-field':
        body['model'] = 'deepseek-chat'
    elif case == 'no-consent':
        body['consent'] = False
    headers = api.headers if key is None else {**api.headers, 'Idempotency-Key': key}
    response = api.client.post(f'/api/v1/tasks/{api.tid}/search-runs/{run_id}/deep-check',
                               headers=headers, json=body)
    assert response.status_code == 422, response.text
    assert api.factory.deep_calls == 0
    assert operation(api, run_id) is None
    record = next(iter(api.store.session(owner(api.headers))['tasks'][api.tid]['search_runs'].values()))
    assert 'deep_check' not in record


def test_deep_check_needs_stable_snapshots_and_the_bound_revision(api):
    api.factory.skip = True
    run_id = start(api).json()['run_id']
    assert deep_start(api, run_id).status_code == 409
    assert deep_view(api, run_id).status_code == 409
    assert api.factory.deep_calls == 0 and api.factory.opens == 1
    record = next(iter(api.store.session(owner(api.headers))['tasks'][api.tid]['search_runs'].values()))
    assert 'deep_check' not in record
    api.factory.skip = False
    other = start(api).json()['run_id']
    def draft(session):
        latest = deepcopy(api.confirmed.model_dump(mode='json'))
        latest.update(revision_id=str(uuid4()), status='draft', confirmed_at=None, revision_number=2)
        session['tasks'][api.tid]['requirements']['revisions'].append(latest)
    api.store.session(owner(api.headers), draft)
    stored = api.store.session(owner(api.headers))['tasks'][api.tid]['requirements']['revisions'][1]['revision_id']
    assert deep_start(api, other, body={'revision_id': stored, 'consent': True}).status_code == 409
    assert deep_start(api, other, body={'revision_id': str(uuid4()), 'consent': True}).status_code == 404


def test_deep_check_single_authorization_per_run(api):
    run_id = start(api).json()['run_id']
    deep_ready(api, run_id)
    key = str(uuid4())
    assert deep_start(api, run_id, key=key).status_code == 202
    assert api.factory.deep_calls == 1
    assert deep_start(api, run_id, key=str(uuid4())).status_code == 409
    assert deep_start(api, run_id, key=key).status_code == 202
    assert api.factory.deep_calls == 1 and deep_view(api, run_id).status_code == 200
    def poison(session):
        for value in session['tasks'][api.tid]['search_runs'].values():
            if value['run_id'] == run_id:
                value['deep_check']['request_hash'] = 'other'
    api.store.session(owner(api.headers), poison)
    assert deep_start(api, run_id, key=key).status_code == 409
    assert api.factory.deep_calls == 1
    assert operation(api, run_id)['operation_id'] == str(uuid5(UUID(run_id), DEEP_CHECK_VERSION))


def test_unconfigured_model_never_claims_the_paid_call(api):
    run_id = start(api).json()['run_id']
    key = str(uuid4())
    assert deep_start(api, run_id, key=key).status_code == 202
    assert api.factory.deep_calls == 0 and operation(api, run_id) is None
    assert run_state(api, run_id) == {'status': 'failed', 'published': False, 'integrity_blocked': False,
                                      'error_code': 'DEEP_CHECK_UNAVAILABLE'}
    deep_ready(api, run_id)
    assert deep_start(api, run_id, key=key).status_code == 202
    assert api.factory.deep_calls == 1 and deep_view(api, run_id).status_code == 200


def test_deep_check_interrupted_open_is_resumable_with_the_same_key(api):
    run_id = start(api).json()['run_id']
    deep_ready(api, run_id)
    key = str(uuid4())
    api.factory.fail_open = True
    assert deep_start(api, run_id, key=key).status_code == 202
    value = api.client.get(f'/api/v1/tasks/{api.tid}/search-runs/{run_id}', headers=api.headers)
    assert 'private' not in value.text
    assert value.json()['deep_check'] == {'status': 'interrupted', 'published': False,
                                          'integrity_blocked': False, 'error_code': 'DEEP_CHECK_INTERRUPTED'}
    assert operation(api, run_id) is None and api.factory.deep_calls == 0
    api.factory.fail_open = False
    assert deep_start(api, run_id, key=key).status_code == 202
    assert run_state(api, run_id) == {'status': 'completed', 'published': True, 'integrity_blocked': False,
                                      'error_code': None}
    assert api.factory.deep_calls == 1


def test_deep_check_provider_failure_is_terminal_and_never_retried(api):
    run_id = start(api).json()['run_id']
    key = str(uuid4())
    deep_ready(api, run_id)
    api.factory.deep_failure = 'http'
    assert deep_start(api, run_id, key=key).status_code == 202
    assert run_state(api, run_id) == {'status': 'failed', 'published': False, 'integrity_blocked': False,
                                      'error_code': 'DEEP_CHECK_PROVIDER_FAILED'}
    assert 'secret' not in api.client.get(f'/api/v1/tasks/{api.tid}/search-runs/{run_id}',
                                          headers=api.headers).text
    api.factory.deep_failure = None
    assert deep_start(api, run_id, key=key).status_code == 202
    assert api.factory.deep_calls == 1 and deep_view(api, run_id).status_code == 409
    assert operation(api, run_id)['provider_usage'] == {}


def test_deep_check_crash_after_dispatch_never_replays_the_call(api):
    run_id = start(api).json()['run_id']
    deep_ready(api, run_id)
    api.factory.deep_failure = 'crash'
    crashed = DeepCheck(api.persistence, api.factory.deep_provider, settings=DeepCheckSettings(),
                        transport=httpx.MockTransport(api.factory.deep_handler))
    with pytest.raises(KeyboardInterrupt):
        anyio.run(crashed.check, owner(api.headers), api.tid, run_id, api.confirmed)
    assert api.factory.deep_calls == 1 and operation(api, run_id)['status'] == 'dispatched'
    api.factory.deep_failure = None
    assert deep_start(api, run_id, key=str(uuid4())).status_code == 202
    assert api.factory.deep_calls == 1
    assert run_state(api, run_id) == {'status': 'failed', 'published': False, 'integrity_blocked': False,
                                      'error_code': 'DEEP_CHECK_OUTCOME_UNKNOWN'}
    assert deep_view(api, run_id).status_code == 409


@pytest.mark.parametrize('tamper', [None, 'report', 'response'])
def test_deep_check_get_verifies_bytes_without_side_effects(api, monkeypatch, tamper):
    run_id = start(api).json()['run_id']
    deep_ready(api, run_id)
    deep_start(api, run_id, key=str(uuid4()))
    reference = operation(api, run_id)
    if tamper:
        key = reference['report_key' if tamper == 'report' else 'response_key']
        _, version = api.storage.read_version(key)
        api.storage.compare_and_swap(key, version, b'private corrupted object')
    def forbidden(*args, **kwargs):
        raise AssertionError('GET must not write or call the model')
    monkeypatch.setattr(api.storage, 'compare_and_swap', Mock(side_effect=forbidden))
    monkeypatch.setattr(api.persistence.repository, 'record_usage', Mock(side_effect=forbidden))
    monkeypatch.setattr(DeepCheck, 'check', Mock(side_effect=forbidden))
    response = deep_view(api, run_id)
    assert response.status_code == (503 if tamper else 200), response.text
    assert 'private' not in response.text
    api.storage.compare_and_swap.assert_not_called()
    api.persistence.repository.record_usage.assert_not_called()
    DeepCheck.check.assert_not_called()
    assert not api.persistence.manifests.read(owner(api.headers), run_id)['integrity_blocked']
    assert api.factory.deep_calls == 1 and len(api.calls) == 3 and api.factory.configs is False


def test_deep_check_post_blocks_the_run_on_tampered_bytes(api):
    run_id = start(api).json()['run_id']
    deep_ready(api, run_id)
    key = str(uuid4())
    deep_start(api, run_id, key=key)
    reference = operation(api, run_id)
    _, version = api.storage.read_version(reference['response_key'])
    api.storage.compare_and_swap(reference['response_key'], version, b'{"candidates":[]}')
    assert deep_start(api, run_id, key=key).status_code == 202
    manifest = api.persistence.manifests.read(owner(api.headers), run_id)
    assert manifest['integrity_blocked'] and manifest['error_code'] == 'DEEP_CHECK_INTEGRITY'
    assert api.factory.deep_calls == 1
    value = api.client.get(f'/api/v1/tasks/{api.tid}/search-runs/{run_id}', headers=api.headers).json()
    assert value['error_code'] == 'SEARCH_FAILED'
    assert value['deep_check'] == {'status': 'completed', 'published': True, 'integrity_blocked': True,
                                   'error_code': None}
    assert deep_view(api, run_id).status_code == 409
    assert api.client.post(f'/api/v1/tasks/{api.tid}/search-runs/{run_id}/evidence',
                           headers=api.headers, json={}).status_code == 409


def test_deep_check_cross_owner_and_cross_task(api):
    run_id = start(api).json()['run_id']
    deep_ready(api, run_id)
    deep_start(api, run_id)
    other, other_task = auth(api.client), task(api.client, api.headers)
    for headers, tid in [(other, api.tid), (api.headers, other_task)]:
        assert deep_view(api, run_id, headers=headers, tid=tid).status_code == 404
        assert deep_start(api, run_id, headers=headers, tid=tid).status_code == 404
    assert api.factory.deep_calls == 1 and len(api.calls) == 3


def test_deep_check_default_backend_test_gate_never_loads_configuration(api):
    run_id = start(api).json()['run_id']
    with TestClient(create_app(api.settings, api.storage)) as client:
        response = client.post(f'/api/v1/tasks/{api.tid}/search-runs/{run_id}/deep-check',
                               headers={**api.headers, 'Idempotency-Key': str(uuid4())},
                               json={'revision_id': str(api.confirmed.revision_id), 'consent': True})
    assert response.status_code == 503 and response.json()['error_code'] == 'SEARCH_DISABLED'
    assert api.factory.deep_calls == 0

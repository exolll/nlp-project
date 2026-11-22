import base64
from copy import deepcopy
import hashlib
import json
from unittest.mock import Mock

import httpx
import pytest

from app.schemas.search import SearchIntegrityError
from app.services.github_client import GitHubService, GitHubSettings
from app.services.search_execution import SearchExecutionSettings, SearchExecutor
from app.services.search_persistence import SearchPersistence
from app.services.storage import StorageError
from test_search_persistence import OWNER, context, publish


RAW = b'# A real README fixture\n'
BLOB = hashlib.sha1(b'blob ' + str(len(RAW)).encode() + b'\0' + RAW).hexdigest()
COMMIT = 'c' * 40


def repo(number=1):
    return {'id': number, 'name': f'project{number}', 'full_name': f'owner/project{number}',
            'html_url': f'https://github.com/owner/project{number}', 'default_branch': 'main',
            'description': None, 'stargazers_count': 10}


def readme():
    return {'type': 'file', 'size': len(RAW), 'path': 'README.md', 'sha': BLOB,
            'encoding': 'base64', 'content': base64.b64encode(RAW).decode()}


@pytest.fixture
def execution(context):
    service, storage, repository, revision, plan = publish(context)
    repository.metadata = {}
    def update(repository_id, patch, issued):
        current = repository.metadata.get(repository_id)
        if current and current[1] > issued:
            return False
        repository.metadata[repository_id] = deepcopy(patch), issued
        return True
    repository.update_repository = Mock(side_effect=update)
    repository.clients = []
    yield service, storage, repository, plan
    for client in repository.clients:
        client.close()


def runner(execution, handler, **budget):
    service, storage, repository, plan = execution
    settings = GitHubSettings(_env_file=None, github_token='test-token')
    client = GitHubService(settings, transport=httpx.MockTransport(handler))
    repository.clients.append(client)
    clock = service.initialize_request_clock([(OWNER, plan.run_id)])
    return SearchExecutor(service, client, clock, SearchExecutionSettings(_env_file=None, **budget), wait=lambda _: None)


def happy_handler(request):
    if request.url.path == '/search/repositories':
        return httpx.Response(200, json={'total_count': 1, 'incomplete_results': False, 'items': [repo()]})
    if '/commits/' in request.url.path:
        return httpx.Response(200, json={'sha': COMMIT})
    assert request.url.params['ref'] == COMMIT
    return httpx.Response(200, json=readme())


def test_search_to_snapshot_and_repeat_never_dispatches(execution):
    service, storage, repository, plan = execution
    calls = []
    def handler(request):
        manifest = service.manifests.read(OWNER, plan.run_id)
        attempts = manifest['search_execution']['attempts']
        assert len(attempts) == len(calls) + 1
        assert all(request_id in manifest['requests'] for request_id in attempts)
        calls.append(request.url.path)
        return happy_handler(request)
    executor = runner(execution, handler)
    result = executor.execute(OWNER, plan.run_id)
    assert result['run_status'] == 'completed'
    assert result['search_execution']['status'] == 'completed'
    candidate = result['candidates']['1']
    assert candidate['collection_status'] == 'completed'
    assert candidate['hits'][0]['page'] == candidate['hits'][0]['position'] == 1
    assert service.read_snapshot(OWNER, plan.run_id, candidate['snapshot_id']) == RAW
    assert len(repository.snapshots) == 1
    assert repository.usages == {}
    assert len(calls) == 3
    assert executor.execute(OWNER, plan.run_id) == result
    assert len(calls) == 3


def test_pagination_deduplicates_and_preserves_every_hit(execution):
    service, storage, repository, plan = execution
    pages = []
    def handler(request):
        page = int(request.url.params['page'])
        pages.append(page)
        return httpx.Response(200, json={'total_count': 3, 'incomplete_results': False,
                                        'items': [repo(1), repo(page + 1)]},
                             headers={'Link': '<https://evil.invalid/steal>; rel="next"'} if page == 1 else {})
    executor = runner(execution, handler, github_readme_max_candidates=0, github_search_per_page=2)
    result = executor.execute(OWNER, plan.run_id)
    assert pages == [1, 2]
    assert set(result['candidates']) == {'1', '2', '3'}
    assert len(result['candidates']['1']['hits']) == 2
    assert len(result['search_execution']['attempts']) == 2
    assert result['run_status'] == 'completed'


def test_page_candidate_and_incomplete_budgets_are_explicit(execution):
    def handler(request):
        return httpx.Response(200, json={'total_count': 999, 'incomplete_results': True,
                                        'items': [repo(1), repo(2)]}, headers={'Link': '<ignored>; rel=next'})
    executor = runner(execution, handler, github_search_max_pages=1,
                      github_search_max_candidates=1, github_readme_max_candidates=0)
    result = executor.execute(OWNER, execution[3].run_id)
    assert result['run_status'] == 'failed'
    assert result['search_execution']['status'] == 'partial'
    assert set(result['search_execution']['stop_reasons']) == {'PAGE_BUDGET', 'CANDIDATE_BUDGET', 'INCOMPLETE_RESULTS'}
    assert len(result['candidates']) == 1
    attempt = next(iter(result['search_execution']['attempts'].values()))
    payload = json.loads(execution[1].get(attempt['response_key']))
    assert len(payload['result']['data']['items']) == 2


def test_missing_readme_keeps_candidate_without_fake_snapshot(execution):
    def handler(request):
        if request.url.path.endswith('/readme'):
            return httpx.Response(404, json={'message': 'not found'})
        return happy_handler(request)
    result = runner(execution, handler).execute(OWNER, execution[3].run_id)
    assert result['candidates']['1']['collection_status'] == 'NO_README'
    assert not execution[2].snapshots
    assert result['search_execution']['status'] == 'partial'


def test_bounded_retry_keeps_each_attempt_and_original_issue_time(execution):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        return happy_handler(request)
    executor = runner(execution, handler, github_readme_max_candidates=0)
    result = executor.execute(OWNER, execution[3].run_id)
    assert len(calls) == 2
    assert result['run_status'] == 'completed'
    attempts = sorted(result['search_execution']['attempts'].values(), key=lambda attempt: attempt['issued_at'])
    assert attempts[0]['error_code'] == 'SERVER_ERROR'
    assert attempts[1]['issued_at'] > attempts[0]['issued_at']
    assert execution[2].metadata[1][1].isoformat() == attempts[1]['issued_at']


def test_rate_limit_does_not_sleep_past_budget(execution):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={'retry-after': '120', 'x-ratelimit-resource': 'search'})
    executor = runner(execution, handler)
    executor.wait = Mock(side_effect=AssertionError('must not wait'))
    result = executor.execute(OWNER, execution[3].run_id)
    assert len(calls) == 1
    assert 'RETRY_BUDGET' in result['search_execution']['stop_reasons']
    assert result['run_status'] == 'failed'


def test_authentication_failure_stops_dispatch(execution):
    result = runner(execution, lambda _: httpx.Response(401)).execute(OWNER, execution[3].run_id)
    assert len(result['search_execution']['attempts']) == 1
    assert 'AUTHENTICATION' in result['search_execution']['stop_reasons']


def test_request_budget_cannot_be_bypassed_by_readme(execution):
    result = runner(execution, happy_handler, github_search_max_requests=1).execute(OWNER, execution[3].run_id)
    assert len(result['search_execution']['attempts']) == 1
    assert result['candidates']['1']['collection_status'] == 'REQUEST_BUDGET'


@pytest.mark.parametrize('failure_mode', ['before', 'after'])
def test_durable_response_gap_does_not_reissue_http(execution, failure_mode):
    service, storage, repository, plan = execution
    calls = []
    def handler(request):
        calls.append(request)
        storage.failure_prefix = 'github-responses/'
        storage.failure_mode = failure_mode
        return happy_handler(request)
    executor = runner(execution, handler, github_readme_max_candidates=0)
    if failure_mode == 'before':
        with pytest.raises(StorageError):
            executor.execute(OWNER, plan.run_id)
        executor.github.search = Mock(side_effect=AssertionError('recovery cannot use network'))
        result = executor.recover(OWNER, plan.run_id)
        assert result['search_execution']['status'] == 'partial'
        assert 'REQUEST_INTERRUPTED' in result['search_execution']['stop_reasons']
        assert not result['candidates']
    else:
        result = executor.execute(OWNER, plan.run_id)
        assert result['run_status'] == 'completed'
    assert len(calls) == 1


def test_sql_failure_recovers_original_response_without_network(execution):
    service, storage, repository, plan = execution
    executor = runner(execution, happy_handler)
    original = repository.update_repository.side_effect
    repository.update_repository.side_effect = StorageError('SQL unavailable')
    with pytest.raises(StorageError):
        executor.execute(OWNER, plan.run_id)
    repository.update_repository.side_effect = original
    executor.github.search = Mock(side_effect=AssertionError('no search retry'))
    executor.github.commit = Mock(side_effect=AssertionError('no new commit lookup'))
    result = executor.recover(OWNER, plan.run_id)
    assert result['search_execution']['status'] == 'partial'
    assert result['candidates']['1']['metadata']['full_name'] == 'owner/project1'
    assert len(result['candidates']['1']['hits']) == 1
    assert executor.recover(OWNER, plan.run_id) == result


def test_readme_sql_unknown_commit_recovers_without_fetch(execution):
    service, storage, repository, plan = execution
    repository.fail_snapshot = 'after'
    executor = runner(execution, happy_handler)
    with pytest.raises(StorageError):
        executor.execute(OWNER, plan.run_id)
    executor.github = Mock()
    executor.github.readme.side_effect = AssertionError('no refetch')
    result = executor.recover(OWNER, plan.run_id)
    assert result['candidates']['1']['collection_status'] == 'completed'
    assert len(repository.snapshots) == 1
    assert result['run_status'] == 'failed'
    executor.github.readme.assert_not_called()


def test_response_tampering_blocks_recovery(execution):
    service, storage, repository, plan = execution
    repository.update_repository.side_effect = StorageError('SQL unavailable')
    executor = runner(execution, happy_handler)
    with pytest.raises(StorageError):
        executor.execute(OWNER, plan.run_id)
    attempt = next(iter(service.manifests.read(OWNER, plan.run_id)['search_execution']['attempts'].values()))
    storage._path(attempt['response_key']).write_bytes(b'{}')
    with pytest.raises(SearchIntegrityError):
        executor.recover(OWNER, plan.run_id)
    assert service.manifests.read(OWNER, plan.run_id)['integrity_blocked']


def test_no_network_during_expired_deadline(execution):
    executor = runner(execution, lambda _: pytest.fail('unexpected request'))
    ticks = iter([0, 121])
    executor.timer = lambda: next(ticks)
    result = executor.execute(OWNER, execution[3].run_id)
    assert 'DEADLINE' in result['search_execution']['stop_reasons']
    assert not result['requests']


@pytest.mark.parametrize('http_status,expected', [(429, 'RATE_LIMIT'), (401, 'AUTHENTICATION')])
def test_collection_limit_or_auth_stops_other_candidates(execution, http_status, expected):
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path == '/search/repositories':
            return httpx.Response(200, json={'total_count': 2, 'incomplete_results': False,
                                            'items': [repo(1), repo(2)]})
        return httpx.Response(http_status, headers={'retry-after': '120'})
    result = runner(execution, handler).execute(OWNER, execution[3].run_id)
    assert len(calls) == 2
    assert expected in result['search_execution']['stop_reasons']
    assert not execution[2].snapshots


def test_unknown_http_result_is_not_retried(execution):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('response may have been sent')
    result = runner(execution, handler).execute(OWNER, execution[3].run_id)
    assert len(calls) == 1
    assert result['search_execution']['status'] == 'partial'
    assert next(iter(result['search_execution']['attempts'].values()))['error_code'] == 'TIMEOUT'


def test_deadline_rechecked_after_request_journal_write(execution):
    executor = runner(execution, lambda _: pytest.fail('deadline passed before HTTP'))
    ticks = iter([0, 0, 121])
    executor.timer = lambda: next(ticks)
    result = executor.execute(OWNER, execution[3].run_id)
    assert next(iter(result['search_execution']['attempts'].values()))['status'] == 'not_sent'
    assert result['run_status'] == 'failed'


def test_response_persisted_before_manifest_ack_is_recoverable(execution, monkeypatch):
    service, storage, repository, plan = execution
    executor = runner(execution, happy_handler, github_readme_max_candidates=0)
    attach = executor._attach_response
    monkeypatch.setattr(executor, '_attach_response', Mock(side_effect=StorageError('Manifest write unknown')))
    with pytest.raises(StorageError):
        executor.execute(OWNER, plan.run_id)
    monkeypatch.setattr(executor, '_attach_response', attach)
    executor.github.search = Mock(side_effect=AssertionError('cannot replay HTTP'))
    result = executor.recover(OWNER, plan.run_id)
    assert len(result['candidates']) == 1
    assert len(result['candidates']['1']['hits']) == 1


def test_configured_missing_token_does_not_break_existing_input_settings():
    from app.core.settings import Settings
    settings = Settings(_env_file=None)
    assert settings.storage_backend == 'local'
    assert GitHubSettings(_env_file=None).github_token.get_secret_value() == ''

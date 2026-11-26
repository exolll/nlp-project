import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.core.settings import ExtractionSettings
from app.schemas.requirements import RequirementItem
from app.services.query_planner import PlannerError, PlannerSettings, QueryPlanner
from app.services.storage import StorageError
from test_search_persistence import OWNER, context


def items(revision):
    result = []
    for field in ('project_topic', 'project_description', 'project_type', 'domain',
                  'required_features', 'preferred_features', 'programming_languages',
                  'required_technologies', 'preferred_technologies'):
        value = getattr(revision.requirements, field)
        for index, text in enumerate(value if isinstance(value, list) else [value] if value else []):
            result.append(RequirementItem(
                requirement_item_id=str(uuid4()), field=field,
                value_path=f'{field}[{index}]' if isinstance(value, list) else field,
                text=text, modality='informational' if not isinstance(value, list) else
                'preferred' if field.startswith('preferred_') else 'required',
                source_kind='user_primary_text', evidence_ids=[], logic_group_id=None,
                operator='not_applicable', supersedes_item_id=None))
    revision.requirement_items = result


def draft():
    return {'groups': [
        {'name': name, 'status': 'ready' if name == 'A' else 'skipped',
         'sources': ['/project_topic'] if name == 'A' else [],
         'skip_reason': None if name == 'A' else '暂不用于发现',
         'queries': [{'terms': [{'text': '文本搜索', 'source': '/project_topic'}],
                      'language_filter': None, 'qualifiers': {}}] if name == 'A' else []}
        for name in 'ABC']}


class Harness:
    def __init__(self, context):
        self.persistence, self.storage, self.repo, self.revision, self.plan = context
        items(self.revision)
        self.body = draft()
        self.calls = 0
        self.failure = None
        self.requests = []
        self.provider = ExtractionSettings.model_construct(deepseek_api_key=SecretStr('offline-key'))
        self.planner = self.make()

    def make(self, settings=None):
        return QueryPlanner(self.persistence, self.provider, settings=settings,
                            transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        self.calls += 1
        self.requests.append(json.loads(request.content))
        if self.failure == 'http':
            return httpx.Response(503, text='secret provider message')
        if self.failure == 'timeout':
            raise httpx.ReadTimeout('secret provider message')
        content = '{bad' if self.failure == 'json' else json.dumps(self.body)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]})

    def run(self, planner=None, owner=OWNER):
        return asyncio.run((planner or self.planner).plan_and_publish(
            owner, str(self.revision.task_id), self.revision, self.plan.run_id))

    def recover(self):
        return self.planner.recover(OWNER, str(self.revision.task_id), self.revision, self.plan.run_id)

    def manifest(self):
        return self.persistence.manifests.read(OWNER, self.plan.run_id)


@pytest.fixture
def h(context):
    return Harness(context)


@pytest.mark.parametrize('pointer', ['project_topic', 'required_features[0]',
                                     'required_features', '/required_features',
                                     '/requirements/project_topic', '/required_features/01'])
@pytest.mark.parametrize('location', ['term', 'group'])
def test_draft_schema_rejects_noncanonical_sources(pointer, location):
    from pydantic import ValidationError
    from app.services.query_planner import PlannerDraft
    body = draft()
    group = body['groups'][0]
    if location == 'term':
        group['queries'][0]['terms'][0]['source'] = pointer
    else:
        group['sources'] = [pointer]
    with pytest.raises(ValidationError):
        PlannerDraft.model_validate(body)


def test_payload_catalog_uses_exact_pointers_and_preserves_constraints(h):
    h.revision.requirements.required_features = ['Do not use cloud transcription']
    h.revision.requirements.preferred_features = ['Export subtitles']
    items(h.revision)
    h.revision.requirement_items[1].modality = 'prohibited'
    before = h.revision.model_dump(mode='json')
    payload = h.planner._payload(h.revision)
    envelope = json.loads(payload['messages'][1]['content'])
    catalog = {entry['pointer']: entry for entry in envelope['UNTRUSTED_SOURCE_CATALOG']}
    assert set(catalog) == {'/project_topic', '/required_features/0', '/preferred_features/0'}
    assert catalog['/required_features/0']['positive'] is False
    assert catalog['/required_features/0']['modality'] == 'prohibited'
    assert catalog['/preferred_features/0']['modality'] == 'preferred'
    assert envelope['UNTRUSTED_REQUIREMENT_REVISION'] == before
    assert h.revision.model_dump(mode='json') == before
    assert 'never copy requirement_items.value_path' in payload['messages'][0]['content']
    assert 'pattern' in payload['messages'][0]['content']
    assert h.calls == 0


@pytest.mark.parametrize('invalid_sources', [True, False])
def test_audio_transcription_source_contract(h, invalid_sources):
    h.revision.requirements.project_topic = 'Audio Transcription with Timestamps'
    h.revision.requirements.project_description = 'Convert uploaded audio recordings into readable transcripts.'
    h.revision.requirements.required_features = ['Upload audio through a web interface',
                                               'Generate timestamped transcript segments',
                                               'Download the transcript as a text file']
    items(h.revision)
    h.body['groups'][0].update(
        sources=['/project_topic', '/project_description'],
        queries=[{'terms': [{'text': 'audio transcription', 'source': '/project_topic'},
                           {'text': 'timestamps', 'source': '/project_topic'}],
                  'language_filter': None, 'qualifiers': {}},
                 {'terms': [{'text': 'speech to text', 'source': '/project_description'}],
                  'language_filter': None, 'qualifiers': {}}])
    h.body['groups'][1].update(
        status='ready', skip_reason=None,
        sources=[f'/required_features/{i}' for i in range(3)],
        queries=[{'terms': [{'text': text, 'source': f'/required_features/{i}'}],
                  'language_filter': None, 'qualifiers': {}}
                 for i, text in enumerate(['audio upload web',
                                           'transcription timestamps',
                                           'transcript export'])])
    if invalid_sources:
        for group in h.body['groups'][:2]:
            for query in group['queries']:
                for term in query['terms']:
                    term['source'] = term['source'].removeprefix('/')
                    if term['source'].startswith('required_features/'):
                        term['source'] = 'required_features[' + term['source'].split('/')[1] + ']'
        h.body['groups'][0]['sources'] = ['project_topic', 'project_description']
        h.body['groups'][1]['sources'] = ['required_features']
        with pytest.raises(PlannerError, match='PLANNER_INVALID_DRAFT'):
            h.run()
        assert h.manifest()['plan'] is None
        assert h.manifest()['planner_operation']['status'] == 'failed'
        with pytest.raises(PlannerError, match='PLANNER_ALREADY_DISPATCHED'):
            h.run()
    else:
        plan = h.run()
        assert sum(len(group.queries) for group in plan.groups) == 5
        assert plan.groups[1].source_requirement_pointers == [f'/required_features/{i}' for i in range(3)]
        assert plan.groups[2].group_status == 'skipped'
        assert all(query.language_filter is None for group in plan.groups for query in group.queries)
        assert h.persistence.read_plan(OWNER, plan.run_id) == plan
        from app.services.github_client import GitHubService, GitHubSettings
        from app.services.search_execution import SearchExecutor, SearchExecutionSettings
        requests = []
        def search(request):
            requests.append(request)
            assert request.url.path == '/search/repositories'
            assert request.url.params['page'] == '1'
            assert request.url.params['per_page'] == '5'
            return httpx.Response(200, json={'total_count': 0, 'incomplete_results': False, 'items': []})
        with GitHubService(GitHubSettings(_env_file=None, github_token='offline'),
                           transport=httpx.MockTransport(search)) as client:
            executor = SearchExecutor(
                h.persistence, client, h.persistence.initialize_request_clock([(OWNER, plan.run_id)]),
                SearchExecutionSettings(_env_file=None, github_search_per_page=5,
                                        github_search_max_pages=1, github_search_max_requests=5,
                                        github_search_max_attempts=1, github_readme_max_candidates=0))
            manifest = executor.execute(OWNER, plan.run_id)
            executor.execute(OWNER, plan.run_id)
        assert manifest['run_status'] == 'completed'
        assert len(requests) == 5
        assert {r.url.params['q'] for r in requests} == {
            'audio timestamps transcription', 'speech text to', 'audio upload web',
            'timestamps transcription', 'export transcript'}
    assert h.calls == 1


@pytest.mark.parametrize('text', ['audio OR video', 'audio and video', 'audio NOT video',
                                  '-audio', '+audio', 'audio | video', 'audio (video)',
                                  'audio*', 'audio repo:owner/name', 'audio "video"',
                                  'audio\\video', 'audio\nvideo'])
def test_unquoted_keywords_cannot_inject_search_syntax(h, text):
    h.body['groups'][0]['queries'][0]['terms'][0]['text'] = text
    with pytest.raises(PlannerError):
        h.run()
    assert h.manifest()['plan'] is None
    assert h.calls == 1


@pytest.mark.parametrize('terms,code', [
    (['audio', 'transcription', 'timestamps'], 'PLANNER_INVALID_DRAFT'),
    (['one two three four five six seven eight nine'], 'PLANNER_QUERY_TOO_LONG'),
    (['download transcript', 'one two three four five six seven'], 'PLANNER_QUERY_TOO_LONG'),
])
def test_query_concept_and_keyword_bounds(h, terms, code):
    h.body['groups'][0]['queries'][0]['terms'] = [
        {'text': text, 'source': '/project_topic'} for text in terms]
    with pytest.raises(PlannerError, match=code):
        h.run()
    assert h.manifest()['plan'] is None
    assert h.calls == 1


@pytest.mark.parametrize('words,ok', [(0, False), (1, True), (4, True),
                                      (5, True), (8, True), (9, False)])
def test_query_keyword_boundary(words, ok):
    from app.services.query_planner import _query_text
    text = ' '.join(f'w{index}' for index in range(words))
    terms = [SimpleNamespace(text=text)]
    if ok:
        assert _query_text(terms, False) == ' '.join(sorted({f'w{index}' for index in range(words)}))
    else:
        with pytest.raises(PlannerError, match='PLANNER_QUERY_TOO_LONG'):
            _query_text(terms, False)


def test_first_real_run_draft_queries_compile(h):
    h.body['groups'][0].update(
        status='ready', sources=['/project_topic'], skip_reason=None,
        queries=[{'terms': [{'text': text, 'source': '/project_topic'}],
                  'language_filter': None, 'qualifiers': {}}
                 for text in ('audio transcription timestamps', 'audio recordings transcripts')])
    h.body['groups'][1].update(
        status='ready', sources=['/required_features/0'], skip_reason=None,
        queries=[{'terms': [{'text': text, 'source': '/required_features/0'}],
                  'language_filter': None, 'qualifiers': {}}
                 for text in ('audio file upload web interface', 'transcript timestamps segments',
                              'transcript text file export')])
    plan = h.run()
    assert h.calls == 1
    assert sorted(query.query_text for group in plan.groups for query in group.queries) == sorted([
        'audio timestamps transcription', 'audio recordings transcripts',
        'audio file interface upload web', 'segments timestamps transcript',
        'export file text transcript'])


def test_keywords_are_unquoted_deduplicated_and_hashed(h):
    from app.services.search_contracts import query_hash
    group = h.body['groups'][0]
    group['queries'][0]['terms'] = [
        {'text': 'audio transcription', 'source': '/project_topic'},
        {'text': 'audio timestamps', 'source': '/project_topic'}]
    other = copy.deepcopy(group['queries'][0])
    other['terms'] = [{'text': 'timestamps audio transcription', 'source': '/project_topic'}]
    group['queries'].append(other)
    query, = h.run().groups[0].queries
    assert query.query_text == 'audio timestamps transcription'
    assert query.query_hash == query_hash(query.query_text, qualifiers={})
    assert query.source_requirement_pointers == ['/project_topic']


@pytest.mark.parametrize('technology,expected', [('C++', 'C++'), ('C#', 'C#'),
    ('Objective-C', 'Objective-C'), ('Node.js', 'Node.js'),
    ('Microsoft SQL Server', '"Microsoft SQL Server"'), ('OR', '"OR"'),
    ('-custom', '"-custom"')])
def test_technology_names_keep_exact_spelling_and_literal_safety(h, technology, expected):
    h.revision.requirements.required_technologies = [technology]
    items(h.revision)
    h.body['groups'][2].update(
        status='ready', skip_reason=None, sources=['/required_technologies/0'],
        queries=[{'terms': [{'text': technology, 'source': '/required_technologies/0'}],
                  'language_filter': None, 'qualifiers': {}}])
    query = h.run().groups[2].queries[0]
    assert query.query_text == expected
    assert query.language_filter is None


def test_prompt_requests_short_separate_discovery_queries(h):
    payload = h.planner._payload(h.revision)
    prompt = payload['messages'][0]['content']
    assert '1..2 core concepts' in prompt
    assert 'at most 4 whitespace-separated keywords' in prompt
    assert 'separate B queries' in prompt
    assert 'do not add automatic fallback searches' in prompt
    assert h.calls == 0


def test_generated_plan_connects_to_search_executor(h):
    from unittest.mock import Mock
    from app.services.github_client import GitHubService, GitHubSettings
    from app.services.search_execution import SearchExecutor, SearchExecutionSettings
    plan = h.run()
    h.repo.update_repository = Mock(return_value=True)
    calls = []
    def search(request):
        calls.append(request)
        assert request.url.params['q'] == plan.groups[0].queries[0].query_text
        return httpx.Response(200, json={'total_count': 1, 'incomplete_results': False, 'items': [{
            'id': 17, 'name': 'search', 'full_name': 'fixture/search',
            'html_url': 'https://github.com/fixture/search', 'default_branch': 'main'}]})
    with GitHubService(GitHubSettings(_env_file=None, github_token='offline'),
                       transport=httpx.MockTransport(search)) as client:
        executor = SearchExecutor(h.persistence, client,
                                  h.persistence.initialize_request_clock([(OWNER, plan.run_id)]),
                                  SearchExecutionSettings(_env_file=None, github_readme_max_candidates=0))
        manifest = executor.execute(OWNER, plan.run_id)
    assert manifest['run_status'] == 'completed'
    assert manifest['candidates']['17']['hits'][0]['query_id'] == plan.groups[0].queries[0].query_id
    assert len(calls) == h.calls == 1
    h.repo.update_repository.assert_called_once()


def test_publish_and_repeat(h):
    result = h.run()
    assert [g.group_status for g in result.groups] == ['ready', 'skipped', 'skipped']
    assert result.groups[0].queries[0].query_text == '文本搜索'
    assert result.model_version == 'alias:deepseek-flash'
    assert h.run() == result == h.recover()
    assert h.calls == 1
    payload = h.requests[0]
    envelope = json.loads(payload['messages'][1]['content'])['UNTRUSTED_REQUIREMENT_REVISION']
    assert envelope['requirement_items'] == h.revision.model_dump(mode='json')['requirement_items']
    assert 'additionalProperties' in payload['messages'][0]['content']
    assert h.manifest()['planner_operation']['status'] == 'draft_registered'


@pytest.mark.parametrize('change', ['owner', 'unconfirmed', 'items', 'mismatch', 'key', 'size'])
def test_preflight_never_calls(h, change):
    owner = OWNER
    if change == 'owner':
        owner = 'b' * 64
    elif change == 'unconfirmed':
        h.revision.status = 'draft'
    elif change == 'items':
        h.revision.requirement_items = []
    elif change == 'mismatch':
        h.revision.requirement_items[0].text = 'wrong'
    elif change == 'key':
        h.provider.deepseek_api_key = SecretStr('')
        h.planner = h.make()
    else:
        h.planner = h.make(PlannerSettings(extraction_max_input_tokens=1024))
    with pytest.raises(PlannerError):
        h.run(owner=owner)
    assert h.calls == 0
    assert 'planner_operation' not in h.manifest()


@pytest.mark.parametrize('change', ['extra', 'wrong_group', 'out_of_bounds',
                                  'skip_query', 'budget', 'missing_group', 'term_invented',
                                  'term_wrong_source', 'strict', 'syntax'])
def test_invalid_drafts(h, change):
    g = h.body['groups'][0]
    q = g['queries'][0]
    if change == 'extra':
        q['query_id'] = str(uuid4())
    elif change == 'wrong_group':
        g['sources'] = ['/required_features/0']
    elif change == 'out_of_bounds':
        g['sources'] = ['/project_topic/9']
    elif change == 'skip_query':
        g['status'] = 'skipped'
    elif change == 'budget':
        g['queries'] *= 4
    elif change == 'missing_group':
        h.body['groups'][2]['name'] = 'A'
    elif change == 'term_invented':
        language_case(h, ['Python'])
        h.body['groups'][2]['queries'][0]['terms'][0]['text'] = 'Rust'
    elif change == 'term_wrong_source':
        q['terms'][0]['source'] = '/required_features/0'
    elif change == 'strict':
        q['terms'][0]['text'] = 42
    else:
        h.revision.requirements.project_topic = 'topic:security'
        items(h.revision)
        q['terms'][0]['text'] = 'topic:security'
    with pytest.raises(PlannerError):
        h.run()
    with pytest.raises(PlannerError):
        h.run()
    assert h.calls == 1
    assert not h.repo.plans
    assert h.manifest()['planner_operation']['status'] == 'failed'
    assert h.manifest()['planner_operation']['error_code'] == 'PLANNER_INVALID_DRAFT'
    with pytest.raises(PlannerError, match='PLANNER_RECOVERY_UNAVAILABLE'):
        h.recover()


def test_duplicates_merge_sources_and_canonical_qualifiers(h):
    h.revision.requirements.domain = '文本搜索'
    items(h.revision)
    g = h.body['groups'][0]
    g['sources'].append('/domain')
    q = g['queries'][0]
    q['qualifiers'] = {'in': ['readme,name', 'description', 'name']}
    other = copy.deepcopy(q)
    other['terms'][0]['source'] = '/domain'
    other['qualifiers'] = {'in': ['description,name,readme']}
    g['queries'].append(other)
    queries = h.run().groups[0].queries
    assert len(queries) == 1
    assert queries[0].source_requirement_pointers == ['/domain', '/project_topic']
    assert queries[0].qualifiers == {'in': ['description,name,readme']}


@pytest.mark.parametrize('key', ['stars', 'forks', 'license', 'archived', 'topic', 'unknown'])
def test_hard_filters_rejected(h, key):
    h.body['groups'][0]['queries'][0]['qualifiers'] = {key: ['true']}
    with pytest.raises(PlannerError, match='PLANNER_FILTER_UNSUPPORTED'):
        h.run()


def language_case(h, values):
    h.revision.requirements.programming_languages = values
    items(h.revision)
    h.body['groups'][2] = {
        'name': 'C', 'status': 'ready', 'sources': ['/programming_languages/0'], 'skip_reason': None,
        'queries': [{'terms': [{'text': values[0], 'source': '/programming_languages/0'}],
                     'language_filter': values[0], 'qualifiers': {}}]}


def test_exact_required_language(h):
    language_case(h, ['Python'])
    assert h.run().groups[2].queries[0].language_filter == 'Python'


@pytest.mark.parametrize('case', ['preferred', 'prohibited', 'ANY', 'ALL', 'multiple', 'wrong', 'no_source'])
def test_language_not_promoted(h, case):
    language_case(h, ['Python', 'Java'] if case == 'multiple' else ['Python'])
    item = h.revision.requirement_items[-1]
    if case in ('preferred', 'prohibited'):
        item.modality = case
    elif case in ('ANY', 'ALL'):
        item.operator, item.logic_group_id = case, 'languages'
    elif case == 'wrong':
        h.body['groups'][2]['queries'][0]['language_filter'] = 'Java'
    elif case == 'no_source':
        h.body['groups'][0]['queries'][0]['language_filter'] = 'Python'
    with pytest.raises(PlannerError):
        h.run()


def test_complex_negation_preserved_but_not_discovered(h):
    h.revision.requirements.required_features = ['禁止删除记录，除非管理员批准']
    items(h.revision)
    h.revision.requirement_items[-1].modality = 'prohibited'
    result = h.run()
    assert result.groups[1].skip_reason == '没有可用正向来源'
    envelope = json.loads(h.requests[0]['messages'][1]['content'])['UNTRUSTED_REQUIREMENT_REVISION']
    assert envelope['requirement_items'] == h.revision.model_dump(mode='json')['requirement_items']
    assert h.calls == 1


@pytest.mark.parametrize('failure', ['json', 'http', 'timeout'])
def test_single_attempt_failure_safe(h, failure):
    h.failure = failure
    with pytest.raises(PlannerError, match='^PLANNER_PROVIDER_FAILED$'):
        h.run()
    with pytest.raises(PlannerError):
        h.run()
    with pytest.raises(PlannerError):
        h.recover()
    assert h.calls == 1
    assert 'secret' not in json.dumps(h.manifest())


def test_sql_failure_recovery_stable(h):
    h.repo.fail_plan = True
    with pytest.raises(PlannerError):
        h.run()
    old = copy.deepcopy(h.manifest()['plan'])
    with pytest.raises(PlannerError, match='PLANNER_ALREADY_DISPATCHED'):
        h.run()
    result = h.recover()
    assert result.plan_id == old['plan_id']
    assert h.manifest()['plan'] == old
    assert h.calls == 1


@pytest.mark.parametrize('committed', [False, True])
def test_unknown_draft_write_no_redispatch(h, monkeypatch, committed):
    original = h.persistence._put_immutable
    def fail(key, data, content_type):
        if key.startswith('planner-drafts/'):
            if committed:
                original(key, data, content_type)
            raise StorageError('unknown object outcome')
        return original(key, data, content_type)
    monkeypatch.setattr(h.persistence, '_put_immutable', fail)
    with pytest.raises(PlannerError):
        h.run()
    monkeypatch.setattr(h.persistence, '_put_immutable', original)
    if committed:
        assert h.recover().plan_id == h.manifest()['planner_operation']['plan_id']
    else:
        with pytest.raises(PlannerError, match='PLANNER_DRAFT_UNAVAILABLE'):
            h.recover()
        with pytest.raises(PlannerError):
            h.run()
    assert h.calls == 1


def test_dispatch_not_durable_never_calls(h):
    h.storage.failure_prefix = 'runs/'
    h.storage.failure_mode = 'before'
    with pytest.raises(PlannerError):
        h.run()
    assert h.calls == 0


def test_concurrent_claim(h):
    barrier = Barrier(2)
    def work():
        planner = h.make()
        barrier.wait()
        try:
            return h.run(planner=planner)
        except PlannerError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: work(), range(2)))
    assert any(results)
    assert h.calls == 1
    assert h.recover().plan_id == h.manifest()['planner_operation']['plan_id']


def test_no_sources_all_skipped(h):
    h.revision.requirements.project_topic = None
    h.revision.requirements.required_features = []
    items(h.revision)
    h.body['groups'][0].update(status='skipped', sources=[], queries=[])
    with pytest.raises(PlannerError, match='PLANNER_NO_USABLE_SOURCES'):
        h.run()
    assert h.calls == 0


def test_composite_language_is_not_exact_filter(h):
    language_case(h, ['Python or Java'])
    with pytest.raises(PlannerError, match='PLANNER_LANGUAGE_UNSUPPORTED'):
        h.run()


def test_preference_is_discovery_only(h):
    h.revision.requirements.preferred_features = ['导出报告']
    items(h.revision)
    h.body['groups'][1] = {
        'name': 'B', 'status': 'ready', 'sources': ['/preferred_features/0'], 'skip_reason': None,
        'queries': [{'terms': [{'text': '导出报告', 'source': '/preferred_features/0'}],
                     'language_filter': None, 'qualifiers': {}}]}
    query = h.run().groups[1].queries[0]
    assert query.query_text == '导出报告' and query.language_filter is None


def test_corrupt_draft_cannot_recover(h, monkeypatch):
    original_put = h.persistence._put_immutable
    def fail(key, data, content_type):
        original_put(key, data, content_type)
        raise StorageError('unknown outcome')
    monkeypatch.setattr(h.persistence, '_put_immutable', fail)
    with pytest.raises(PlannerError):
        h.run()
    key = h.manifest()['planner_operation']['draft_key']
    original = h.storage.read_version
    monkeypatch.setattr(h.storage, 'read_version', lambda k: (b'{}', 'fake') if k == key else original(k))
    with pytest.raises(PlannerError, match='PLANNER_DRAFT_UNAVAILABLE'):
        h.recover()
    assert h.calls == 1


def test_stored_plan_recovery_does_not_require_draft(h, monkeypatch):
    h.repo.fail_plan = True
    with pytest.raises(PlannerError):
        h.run()
    key = h.manifest()['planner_operation']['draft_key']
    original = h.storage.read_version
    monkeypatch.setattr(h.storage, 'read_version', lambda k: (None, None) if k == key else original(k))
    assert h.recover().plan_id == h.manifest()['planner_operation']['plan_id']
    assert h.calls == 1


def test_durable_dispatched_without_response_never_replayed(h, monkeypatch):
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr('app.services.extraction_provider.call_provider', cancelled)
    with pytest.raises(asyncio.CancelledError):
        h.run()
    assert h.manifest()['planner_operation']['status'] == 'dispatched'
    with pytest.raises(PlannerError, match='PLANNER_ALREADY_DISPATCHED'):
        h.run()
    with pytest.raises(PlannerError, match='PLANNER_RECOVERY_UNAVAILABLE'):
        h.recover()


def test_translation_keeps_pointer(h):
    h.body['groups'][0]['queries'][0]['terms'] = [
        {'text': 'text search', 'source': '/project_topic'},
        {'text': 'information retrieval', 'source': '/project_topic'}]
    result = h.run()
    query = result.groups[0].queries[0]
    assert query.query_text == 'information retrieval search text'
    assert query.source_requirement_pointers == ['/project_topic']
    assert result.planner_version == result.prompt_version == 'planner-v3'
    op = h.manifest()['planner_operation']
    assert op['draft_key'] == f"planner-drafts/{op['plan_id']}.json"


def test_declared_sources_are_trimmed_to_executed_queries(h):
    h.revision.requirements.project_description = '补充描述'
    items(h.revision)
    group = h.body['groups'][0]
    group['sources'] = ['/project_topic', '/project_description']
    plan = h.run()
    assert plan.groups[0].source_requirement_pointers == ['/project_topic']
    operation = h.manifest()['planner_operation']
    assert operation['source_normalization'] == [{
        'group': 'A',
        'declared': ['/project_description', '/project_topic'],
        'used': ['/project_topic']}]


def test_empty_declared_sources_are_derived_from_executed_queries(h):
    h.body['groups'][0]['sources'] = []
    plan = h.run()
    assert plan.groups[0].source_requirement_pointers == ['/project_topic']


def test_missing_declared_source_is_added_from_executed_queries(h):
    h.revision.requirements.project_description = '补充描述'
    items(h.revision)
    group = h.body['groups'][0]
    group['sources'] = ['/project_topic']
    group['queries'][0]['terms'][0]['source'] = '/project_description'
    plan = h.run()
    assert plan.groups[0].source_requirement_pointers == ['/project_description']
    operation = h.manifest()['planner_operation']
    assert operation['source_normalization'] == [{
        'group': 'A',
        'declared': ['/project_topic'],
        'used': ['/project_description']}]


@pytest.mark.parametrize('text', ['bad\nterm', 'bad\tterm', 'bad\x00term', 'bad\x7fterm',
                                  'bad"term', "bad'term", 'bad\\term', 'repo:secret', '   '])
def test_term_syntax_rejected(h, text):
    h.body['groups'][0]['queries'][0]['terms'][0]['text'] = text
    with pytest.raises(PlannerError, match='PLANNER_INVALID_DRAFT'):
        h.run()


@pytest.mark.parametrize('pointer', [True, False])
def test_prohibition_cannot_be_positive(h, pointer):
    h.revision.requirements.required_features = ['禁止Redis']
    items(h.revision)
    h.revision.requirement_items[-1].modality = 'prohibited'
    if pointer:
        h.body['groups'][1].update(status='ready', skip_reason=None,
            sources=['/required_features/0'], queries=[{'terms': [
                {'text': 'Redis', 'source': '/required_features/0'}],
                'language_filter': None, 'qualifiers': {}}])
    else:
        h.body['groups'][0]['queries'][0]['terms'][0]['text'] = 'redis search'
    with pytest.raises(PlannerError):
        h.run()
    assert h.calls == 1


def test_negated_topic_no_positive_sources(h):
    h.revision.requirements.project_topic = '禁止Redis'
    h.revision.requirements.required_features = []
    items(h.revision)
    with pytest.raises(PlannerError, match='PLANNER_NO_USABLE_SOURCES'):
        h.run()
    assert h.calls == 0


def test_all_skipped_rejected(h):
    for group in h.body['groups']:
        group.update(status='skipped', sources=[], queries=[], skip_reason='暂不发现')
    with pytest.raises(PlannerError):
        h.run()


def test_explicit_skip_reason_retained(h):
    h.body['groups'][1]['skip_reason'] = '功能描述不适合发现关键词'
    assert h.run().groups[1].skip_reason == '功能描述不适合发现关键词'


@pytest.mark.parametrize('operator', ['ANY', 'ALL'])
@pytest.mark.parametrize('combined', [True, False])
def test_language_discovery_logic(h, operator, combined):
    language_case(h, ['Python', 'Java'])
    for item in h.revision.requirement_items:
        if item.field == 'programming_languages':
            item.operator, item.logic_group_id = operator, 'languages'
    group = h.body['groups'][2]
    group['sources'].append('/programming_languages/1')
    query = group['queries'][0]
    query['language_filter'] = None
    term = {'text': 'Java', 'source': '/programming_languages/1'}
    if combined:
        query['terms'].append(term)
    else:
        group['queries'].append({'terms': [term], 'language_filter': None, 'qualifiers': {}})
    if combined and operator == 'ANY':
        with pytest.raises(PlannerError):
            h.run()
    else:
        assert all(q.language_filter is None for q in h.run().groups[2].queries)


@pytest.mark.parametrize('combined', [True, False])
def test_preference_does_not_narrow_required(h, combined):
    h.revision.requirements.required_technologies = ['FastAPI', 'SQLite']
    h.revision.requirements.preferred_technologies = ['Redis']
    items(h.revision)
    for item in h.revision.requirement_items:
        if item.field == 'required_technologies':
            item.operator, item.logic_group_id = 'ALL', 'technologies'
    terms = [{'text': text, 'source': source} for text, source in [
        ('FastAPI', '/required_technologies/0'), ('SQLite', '/required_technologies/1'),
        ('Redis', '/preferred_technologies/0')]]
    h.body['groups'][2] = dict(name='C', status='ready', skip_reason=None,
        sources=[t['source'] for t in terms], queries=[
            dict(terms=ts, language_filter=None, qualifiers={})
            for ts in ([terms] if combined else [[t] for t in terms])])
    if combined:
        with pytest.raises(PlannerError):
            h.run()
    else:
        assert len(h.run().groups[2].queries) == 3


@pytest.mark.parametrize('field', ['required_technologies', 'preferred_technologies'])
def test_technology_expansion_rejected(h, field):
    setattr(h.revision.requirements, field, ['SQLite'])
    items(h.revision)
    pointer = '/' + field + '/0'
    h.body['groups'][2].update(status='ready', skip_reason=None, sources=[pointer],
        queries=[dict(terms=[dict(text='PostgreSQL', source=pointer)],
                      language_filter=None, qualifiers={})])
    with pytest.raises(PlannerError):
        h.run()


@pytest.mark.parametrize('change', ['missing_reason', 'blank_reason', 'skip_sources'])
def test_group_source_and_skip_contract(h, change):
    group = h.body['groups'][1]
    if change == 'missing_reason':
        del group['skip_reason']
    elif change == 'blank_reason':
        group['skip_reason'] = '  '
    elif change == 'skip_sources':
        group['sources'] = ['/required_features/0']
    with pytest.raises(PlannerError):
        h.run()


def test_preferred_language_discovery_without_filter(h):
    language_case(h, ['Python'])
    h.revision.requirement_items[-1].modality = 'preferred'
    h.body['groups'][2]['queries'][0]['language_filter'] = None
    assert h.run().groups[2].queries[0].query_text == 'Python'


def test_mixed_positive_and_negative_group(h):
    h.revision.requirements.required_features = ['禁止Redis', '文本查询']
    items(h.revision)
    for item in h.revision.requirement_items:
        if item.text == '禁止Redis':
            item.modality = 'prohibited'
    pointer = '/required_features/1'
    h.body['groups'][1].update(status='ready', skip_reason=None, sources=[pointer],
        queries=[dict(terms=[dict(text='text query', source=pointer)],
                      language_filter=None, qualifiers={})])
    result = h.run()
    assert result.groups[1].group_status == 'ready'
    assert result.groups[1].source_requirement_pointers == [pointer]


def test_usage_whitelist(h, monkeypatch):
    async def response(*args):
        return h.body, {'prompt_tokens': 12, 'completion_tokens': 5, 'total_tokens': 17,
                        'prompt_cache_hit_tokens': 0, 'prompt_cache_miss_tokens': -1,
                        'secret': 'do not persist', 'details': {'secret': 'hidden'}}
    monkeypatch.setattr('app.services.extraction_provider.call_provider', response)
    h.run()
    assert h.manifest()['planner_operation']['usage'] == {
        'prompt_tokens': 12, 'completion_tokens': 5, 'total_tokens': 17,
        'prompt_cache_hit_tokens': 0}


def test_changed_confirmation_rejected_after_publish(h):
    h.run()
    h.revision.requirements.project_topic = 'replacement'
    with pytest.raises(PlannerError, match='PLANNER_REVISION_CHANGED'):
        h.run()
    assert h.calls == 1

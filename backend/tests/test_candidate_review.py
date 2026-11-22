from uuid import uuid4
from unittest.mock import Mock

import httpx
import pytest

from app.schemas.requirements import RequirementItem
from app.schemas.search import SearchIntegrityError, SearchStateError
from app.services.candidate_review import CandidateReview, EvidenceReview
from app.services.storage import StorageError
from test_search_persistence import OWNER, context, publish, readme_arguments
from test_search_execution import runner, repo, happy_handler


def valid_revision(revision):
    revision.requirements.project_topic = 'search'
    revision.requirements.required_features = ['must not Redis', '全文搜索']
    revision.requirements.preferred_features = ['export']
    revision.requirements.programming_languages = ['Python']
    values = [('project_topic', 'search', 'informational'),
              ('required_features[0]', 'must not Redis', 'prohibited'),
              ('required_features[1]', '全文搜索', 'required'),
              ('preferred_features[0]', 'export', 'preferred'),
              ('programming_languages[0]', 'Python', 'required')]
    revision.requirement_items = [RequirementItem(
        requirement_item_id=str(uuid4()), field=path.split('[')[0], value_path=path,
        text=text, modality=modality, source_kind='user_primary_text', evidence_ids=[],
        logic_group_id='language' if text == 'Python' else None,
        operator='ANY' if text == 'Python' else 'not_applicable', supersedes_item_id=None,
    ) for path, text, modality in values]
    return revision


@pytest.fixture
def reviewed(context):
    service, storage, repository, revision, plan = context
    valid_revision(revision)
    publish(context)
    service.manifests.update(OWNER, plan.run_id, lambda m: m['candidates'].update({
        '10': {'metadata': {'description': 'search Redis', 'language': None}},
        '20': {'metadata': {}},
    }))
    return context


def close(context, raw='标题\r\nRedis is not supported.\r\n全文搜索 export Python\n'.encode()):
    service, storage, repository, revision, plan = context
    if raw is not None:
        service.collect_readme(OWNER, plan.run_id, *readme_arguments(data=raw))
    service.manifests.seal_dispatch(OWNER, plan.run_id)
    service.manifests.finish(OWNER, plan.run_id)
    return EvidenceReview(service).review(OWNER, str(revision.task_id), plan.run_id, revision)


def test_screening_keeps_unknown_preferred_negative_and_language_absence(reviewed):
    service, _, _, revision, plan = reviewed
    result = CandidateReview(service).screen(OWNER, str(revision.task_id), plan.run_id, revision)
    assert all(row['eligible'] for row in result['candidates'])
    assert result['candidates'][0]['metadata_mentions'] == [revision.requirement_items[0].requirement_item_id]
    assert result['candidates'][1]['reason'] == 'unknown_retained'
    with pytest.raises(SearchIntegrityError):
        service.manifests.update(OWNER, plan.run_id, lambda m: m['candidate_screening'].update(warning='changed'))


def test_all_items_quotes_and_no_satisfaction(reviewed):
    service, _, repository, revision, plan = reviewed
    raw = '标题\r\nRedis is not supported.\r\n全文搜索 export Python\n'.encode()
    report = close(reviewed, raw)
    assert len(report['candidates']) == 2
    first, missing = report['candidates']
    assert first['availability'] == 'available'
    assert missing['availability'] == 'no_snapshot' and missing['source'] is None
    assert len(repository.snapshots) == len(repository.usages) == 1
    for row in report['candidates']:
        assert len(row['items']) == len(revision.requirement_items)
        for entry, original in zip(row['items'], revision.requirement_items):
            assert entry['modality'] == original.modality
            assert entry['operator'] == original.operator
            assert entry['logic_group_id'] == original.logic_group_id
            assert entry['status'] in ('mention_found', 'unconfirmed')
            for quote in entry['quotes']:
                assert raw[quote['byte_start']:quote['byte_end']].decode() == quote['text']
                assert raw[:quote['byte_start']].count(b'\n') + 1 == quote['line_start']
    assert first['items'][1]['status'] == 'mention_found'
    assert first['items'][1]['quotes'][0]['text'] == 'Redis is not supported.'
    assert 'Negation' in report['warning']
    before = service.manifests.read(OWNER, plan.run_id)
    assert EvidenceReview(service).review(OWNER, str(revision.task_id), plan.run_id, revision) == report
    assert service.manifests.read(OWNER, plan.run_id) == before
    assert before['run_status'] == 'completed'


@pytest.mark.parametrize('raw,status', [(None, 'no_snapshot'), (b'\xffRedis', 'non_utf8')])
def test_unavailable_is_not_fake_snapshot(reviewed, raw, status):
    report = close(reviewed, raw)
    assert report['candidates'][0]['availability'] == status
    assert all(item['status'] == 'unconfirmed' for item in report['candidates'][0]['items'])


def test_binding_coverage_and_mutation(reviewed):
    service, _, _, revision, plan = reviewed
    review = EvidenceReview(service)
    for owner, task, changed in [('b' * 64, str(revision.task_id), revision),
                                  (OWNER, str(uuid4()), revision),
                                  (OWNER, str(revision.task_id), revision.model_copy(update={'revision_id': uuid4()})),
                                  (OWNER, str(revision.task_id), revision.model_copy(update={'status': 'draft'}))]:
        with pytest.raises((SearchStateError, SearchIntegrityError)):
            review.review(owner, task, plan.run_id, changed)
    with pytest.raises(ValueError):
        CandidateReview(service).screen(OWNER, str(revision.task_id), plan.run_id,
                                        revision.model_copy(update={'requirement_items': []}))
    CandidateReview(service).screen(OWNER, str(revision.task_id), plan.run_id, revision)
    changed = revision.model_copy(deep=True)
    changed.revision_number += 1
    with pytest.raises(SearchIntegrityError):
        review.review(OWNER, str(revision.task_id), plan.run_id, changed)
    with pytest.raises(SearchStateError):
        review.review(OWNER, str(revision.task_id), plan.run_id, revision)


@pytest.mark.parametrize('mode', ['before', 'after'])
def test_object_unknown_write_recovery(reviewed, mode):
    service, storage, _, revision, plan = reviewed
    storage.failure_prefix, storage.failure_mode = 'evidence-reviews/', mode
    if mode == 'before':
        with pytest.raises(StorageError):
            close(reviewed)
    else:
        close(reviewed)
    prior = service.manifests.read(OWNER, plan.run_id)['evidence_operation']
    service.collect_readme = Mock(side_effect=AssertionError('No new collection'))
    EvidenceReview(service).review(OWNER, str(revision.task_id), plan.run_id, revision)
    after = service.manifests.read(OWNER, plan.run_id)['evidence_operation']
    assert prior['operation_id'] == after['operation_id']
    assert prior['report'] == after['report']
    assert after['published']
    service.collect_readme.assert_not_called()


def test_usage_unknown_commit_recovery_and_monotonicity(reviewed):
    service, _, repository, revision, plan = reviewed
    record = repository.record_usage
    def unknown(usage):
        record(usage)
        raise StorageError('unknown commit')
    repository.record_usage = unknown
    with pytest.raises(StorageError):
        close(reviewed)
    before = service.manifests.read(OWNER, plan.run_id)['evidence_operation']
    assert not before['published']
    repository.record_usage = record
    EvidenceReview(service).review(OWNER, str(revision.task_id), plan.run_id, revision)
    assert len(repository.usages) == 1
    for mutate in [lambda m: m['evidence_operation'].update(published=False),
                   lambda m: m['evidence_operation'].update(operation_id=str(uuid4())),
                   lambda m: m['evidence_operation']['usage'].update({next(iter(before['usage'])): False})]:
        with pytest.raises(SearchIntegrityError):
            service.manifests.update(OWNER, plan.run_id, mutate)


def test_report_tampering_and_integrity_block(reviewed):
    service, storage, _, revision, plan = reviewed
    close(reviewed)
    op = service.manifests.read(OWNER, plan.run_id)['evidence_operation']
    _, version = storage.read_version(op['storage_key'])
    storage.compare_and_swap(op['storage_key'], version, b'altered')
    with pytest.raises(SearchIntegrityError):
        EvidenceReview(service).review(OWNER, str(revision.task_id), plan.run_id, revision)
    assert service.manifests.read(OWNER, plan.run_id)['integrity_blocked']


def test_snapshot_tampering_blocks_published_review(reviewed):
    service, storage, _, revision, plan = reviewed
    close(reviewed)
    m = service.manifests.read(OWNER, plan.run_id)
    source = next(iter(m['readme_persistence_operations'].values()))
    _, version = storage.read_version(source['storage_key'])
    storage.compare_and_swap(source['storage_key'], version, b'changed snapshot')
    with pytest.raises(SearchIntegrityError):
        EvidenceReview(service).review(OWNER, str(revision.task_id), plan.run_id, revision)
    assert service.manifests.read(OWNER, plan.run_id)['integrity_blocked']


def test_failed_run_not_reopened_and_report_immutable(reviewed):
    service, _, _, revision, plan = reviewed
    service.manifests.seal_dispatch(OWNER, plan.run_id)
    service.manifests.fail(OWNER, plan.run_id, 'SEARCH_PARTIAL')
    report = EvidenceReview(service).review(OWNER, str(revision.task_id), plan.run_id, revision)
    assert all(row['availability'] == 'no_snapshot' for row in report['candidates'])
    assert service.manifests.read(OWNER, plan.run_id)['run_status'] == 'failed'
    with pytest.raises(SearchIntegrityError):
        service.manifests.update(OWNER, plan.run_id, lambda m: m['candidates'].update({'30': {}}))
    with pytest.raises(SearchIntegrityError):
        service.manifests.update(OWNER, plan.run_id, lambda m: m['evidence_operation']['report']['candidates'][0]['items'].pop())


def test_quotes_capped_and_boundaries_exact(reviewed):
    from app.services.candidate_review import _mentions
    item = reviewed[3].requirement_items[0].model_copy(update={'text': 'one two three four five'})
    text = '前缀\r\nstone one two three\r\n中文 four\nfive café\none again\n'
    quotes = _mentions(text, item, 10)
    assert len(quotes) == 3
    assert [quote['text'] for quote in quotes] == ['stone one two three', '中文 four', 'five café']
    for number, quote in enumerate(quotes, 2):
        assert text.encode()[quote['byte_start']:quote['byte_end']].decode() == quote['text']
        assert quote['line_start'] == quote['line_end'] == number
    assert not _mentions('stone', item.model_copy(update={'text': 'one'}))
    assert not _mentions('one', item, 0)
    assert not _mentions('one ' + '长' * 2000, item)


@pytest.mark.parametrize('requirement,text,found', [
    ('the application must support Redis', 'the application must support a feature', False),
    ('the must a', 'the must a', False),
    ('Python', 'python is not supported.', True),
    ('python', 'PYTHON', True),
    ('Python', 'CPython', False),
    ('C++', 'C', False), ('C#', 'C', False),
    ('C', 'C++ C#', False), ('C++', 'c++', True), ('C#', 'c#', True),
    ('.NET', 'net', False), ('.NET', '.net', True),
    ('Node.js', 'node', False), ('Node.js', 'NODE.JS', True),
    ('全文搜索', '全文搜索 is not supported.', True),
])
def test_meaningful_literal_mentions(reviewed, requirement, text, found):
    from app.services.candidate_review import _mentions
    item = reviewed[3].requirement_items[0].model_copy(update={'text': requirement})
    quotes = _mentions(text, item)
    assert bool(quotes) == found
    if found:
        assert quotes[0]['text'] == text


def test_core_metadata_outweighs_secondary_hints(reviewed):
    service, _, _, revision, plan = reviewed
    service.manifests.update(OWNER, plan.run_id, lambda m: m['candidates'].update({
        '10': {'metadata': {'description': 'export', 'language': 'python'}},
        '20': {'metadata': {'description': 'search'}},
        '30': {'metadata': {'description': '全文搜索'}},
        '40': {'metadata': {'description': 'Redis the must a'}},
        '50': {'metadata': {}},
    }))
    result = CandidateReview(service).screen(OWNER, str(revision.task_id), plan.run_id, revision)
    rows = result['candidates']
    assert [row['repository_id'] for row in rows] == ['20', '30', '10', '40', '50']
    assert all(row['eligible'] for row in rows)
    assert rows[2]['priority'] > 0
    assert all(row['priority'] == 0 and row['reason'] == 'unknown_retained' for row in rows[3:])


def test_planner_hash_binding(reviewed):
    service, _, _, revision, plan = reviewed
    service.manifests.update(OWNER, plan.run_id, lambda m: m.update(planner_operation={'revision_hash': '0' * 64}))
    with pytest.raises(SearchIntegrityError):
        CandidateReview(service).screen(OWNER, str(revision.task_id), plan.run_id, revision)


def test_selection_screened_before_dispatch_with_budget(context):
    service, storage, repository, revision, plan = context
    valid_revision(revision)
    publish(context)
    repository.metadata = {}
    repository.update_repository = Mock()
    repository.clients = []
    fixture = service, storage, repository, plan
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if request.url.path == '/search/repositories':
            preferred = repo(2)
            preferred['description'] = 'search export'
            return httpx.Response(200, json={'total_count': 2, 'incomplete_results': False, 'items': [repo(1), preferred]})
        m = service.manifests.read(OWNER, plan.run_id)
        assert 'candidate_screening' in m
        assert m['search_execution']['readme_targets'] == ['2']
        return happy_handler(request)
    executor = runner(fixture, handler, github_readme_max_candidates=1, github_search_max_requests=3)
    try:
        result = executor.execute(OWNER, plan.run_id, confirmed_revision=revision)
        assert len(calls) == 3 and len(repository.snapshots) == 1
        assert result['candidates']['1']['collection_status'] == 'not_selected'
        executor.execute(OWNER, plan.run_id, confirmed_revision=revision)
        assert len(calls) == 3
    finally:
        for client in repository.clients:
            client.close()

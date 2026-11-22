"""Offline mock units and MySQL SQL compilation, NOT MySQL concurrency tests."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import OperationalError, IntegrityError

from app.persistence import search_repository as module
from app.persistence.search_models import (
    Repository, RepositorySnapshot, SearchGroup, SearchPlan, SearchQuery, SearchSnapshot,
)
from app.persistence.search_repository import RequestClock, SearchRepository
from app.schemas.search import SearchIntegrityError, SearchStateError
from app.services.search_contracts import query_hash, snapshot_key
from app.services.storage import StorageError

UTC = timezone.utc
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
SQL_NOW = NOW.replace(tzinfo=None)


def test_github_patch_preserves_missing_and_explicit_null_fields():
    repository_id, patch = SearchRepository.github_patch({
        'id': 7, 'description': None, 'stargazers_count': 0,
        'license': {'spdx_id': 'MIT'}, 'archived': False,
        'pushed_at': '2026-01-01T13:00:00+01:00',
    })
    assert repository_id == 7
    assert patch == {'description': None, 'stars': 0, 'license': 'MIT',
                     'is_archived': False, 'pushed_at': '2026-01-01T12:00:00+00:00'}
    assert SearchRepository.github_patch({'id': 7, 'license': None}) == (7, {'license': None})


@pytest.mark.parametrize('response', [None, {}, {'id': True}, {'id': 7, 'license': {}},
                                     {'id': 7, 'license': 'MIT'}, {'id': 7, 'archived': 1}])
def test_github_patch_rejects_invalid_metadata(response):
    with pytest.raises(SearchIntegrityError):
        SearchRepository.github_patch(response)


def test_plan_query_rejects_sql_text_byte_overflow():
    plan = plan_document()
    query = plan['groups'][0]['queries'][0]
    query['query_text'] = '汉' * 22000
    query['query_hash'] = query_hash(query['query_text'], qualifiers={})
    with pytest.raises(SearchIntegrityError):
        SearchRepository._plan_values(plan, 'search-plans/test.json', 'a' * 64)


def uid(number):
    return str(UUID(int=number))


def plan_document():
    query = {'query_id': uid(10), 'query_text': 'search', 'query_type': 'primary',
             'language_filter': None, 'qualifiers': {},
             'source_requirement_pointers': ['/project_topic'],
             'query_hash': query_hash('search', qualifiers={}), 'created_at': NOW.isoformat()}
    return {'plan_id': uid(1), 'run_id': uid(2), 'confirmed_revision_id': uid(3),
            'planner_version': 'v1', 'model_version': 'test', 'prompt_version': 'v1',
            'query_hash_algorithm': 'projectpilot-query-v1', 'created_at': NOW.isoformat(),
            'groups': [
                {'group_id': uid(20 + index), 'group_name': name, 'group_purpose': 'purpose',
                 'source_requirement_pointers': ['/project_topic'] if index == 0 else [],
                 'group_status': 'ready' if index == 0 else 'skipped',
                 'skip_reason': None if index == 0 else 'not needed',
                 'created_at': NOW.isoformat(), 'queries': [query] if index == 0 else []}
                for index, name in enumerate('ABC')]}


def snapshot_document():
    return {'snapshot_id': uid(30), 'repository_id': 2**53 + 1,
            'commit_sha': 'a' * 40, 'blob_sha': 'b' * 40, 'readme_path': '文档/README.md ',
            'content_hash': 'c' * 64, 'storage_key': 'snapshots/readme.raw',
            'snapshot_key': snapshot_key(2**53 + 1, 'a' * 40, '文档/README.md ', 'b' * 40),
            'collected_at': NOW.isoformat()}


def usage_document():
    return {'id': uid(40), 'run_id': uid(2), 'plan_id': uid(1),
            'repository_id': 2**53 + 1, 'snapshot_id': uid(30),
            'usage_type': 'candidate_review', 'used_at': NOW.isoformat()}


def mock_session():
    session = MagicMock()
    session.__enter__.return_value = session
    session.scalar.return_value = None
    session.get.return_value = None
    session.scalars.return_value.all.return_value = []
    return session


@pytest.fixture
def setup(monkeypatch):
    # No engine is constructed: any missed mock would receive an invalid object,
    # never a DSN or credentials. Neither .env nor a real database is read.
    sessions = []
    factory = MagicMock(side_effect=lambda *args, **kwargs: sessions.pop(0))
    monkeypatch.setattr(module, 'Session', factory)
    monkeypatch.setattr(module, 'sleep', MagicMock())
    return SearchRepository(object()), sessions, factory


def plan_rows(state='ready'):
    parent, groups, queries = SearchRepository._plan_values(plan_document(), 'plans/1.json', 'd' * 64)
    return (SearchPlan(**parent, plan_status=state),
            [SearchGroup(**g) for g in groups], [SearchQuery(**q) for q in queries])


def projection_results(session, groups, queries):
    session.scalars.side_effect = [MagicMock(all=MagicMock(return_value=groups)),
                                   MagicMock(all=MagicMock(return_value=queries))]


def test_pending_maps_only_explicit_sql_columns(setup):
    repo, sessions, factory = setup
    session = mock_session()
    sessions.append(session)
    repo.ensure_pending(plan_document(), 'plans/1.json', 'd' * 64)
    row = session.add.call_args.args[0]
    assert row.created_at == SQL_NOW and row.plan_status == 'pending'
    assert not hasattr(row, 'groups') and not hasattr(row, 'query_hash_algorithm')
    assert 'FOR UPDATE' in str(session.scalar.call_args.args[0].compile(dialect=mysql.dialect()))
    session.commit.assert_called_once()
    assert factory.call_count == 1


def test_publish_all_children_and_ready_share_one_commit(setup):
    repo, sessions, _ = setup
    session = mock_session()
    sessions.append(session)
    parent, groups, queries = plan_rows('pending')
    session.scalar.return_value = parent
    session.scalars.side_effect = [MagicMock(all=MagicMock(return_value=value))
                                   for value in ([], [], groups, queries)]
    repo.publish_plan(plan_document(), 'plans/1.json', 'd' * 64)
    assert parent.plan_status == 'ready'
    assert session.add_all.call_count == 2
    added_groups, added_queries = [call.args[0] for call in session.add_all.call_args_list]
    assert len(added_groups) == 3 and len(added_queries) == 1
    assert all(g.plan_id == uid(1) for g in added_groups)
    assert added_queries[0].group_id == uid(20)
    assert added_queries[0].created_at == SQL_NOW
    session.commit.assert_called_once()


@pytest.mark.parametrize('operation', ['ensure_pending', 'publish_plan', 'plan_is_ready'])
def test_ready_exact_replay_does_not_overwrite(setup, operation):
    repo, sessions, _ = setup
    session = mock_session()
    sessions.append(session)
    parent, groups, queries = plan_rows()
    session.scalar.return_value = parent
    projection_results(session, groups, queries)
    result = getattr(repo, operation)(plan_document(), 'plans/1.json', 'd' * 64)
    assert result is (True if operation == 'plan_is_ready' else None)
    session.add.assert_not_called()
    session.add_all.assert_not_called()


@pytest.mark.parametrize('corruption', ['parent', 'group', 'query', 'extra', 'missing'])
def test_ready_projection_corruption_is_rejected(setup, corruption):
    repo, sessions, _ = setup
    session = mock_session()
    sessions.append(session)
    parent, groups, queries = plan_rows()
    if corruption == 'parent':
        parent.run_id = uid(100)
    elif corruption == 'group':
        groups[0].group_purpose = 'changed'
    elif corruption == 'query':
        queries[0].qualifiers = {'archived': ['true']}
    elif corruption == 'extra':
        queries.append(SearchQuery(query_id=uid(999)))
    else:
        groups.pop()
    session.scalar.return_value = parent
    projection_results(session, groups, queries)
    with pytest.raises(SearchIntegrityError):
        repo.plan_is_ready(plan_document(), 'plans/1.json', 'd' * 64)


@pytest.mark.parametrize('state,expected', [('missing', False), ('pending', False), ('failed', None)])
def test_readiness_states(setup, state, expected):
    repo, sessions, _ = setup
    session = mock_session()
    sessions.append(session)
    session.scalar.return_value = None if state == 'missing' else plan_rows(state)[0]
    if state == 'failed':
        with pytest.raises(SearchStateError):
            repo.plan_is_ready(plan_document(), 'plans/1.json', 'd' * 64)
    else:
        assert repo.plan_is_ready(plan_document(), 'plans/1.json', 'd' * 64) is expected


def test_publish_query_failure_never_sets_ready(setup):
    repo, sessions, _ = setup
    session = mock_session()
    sessions.append(session)
    parent = plan_rows('pending')[0]
    session.scalar.return_value = parent
    session.flush.side_effect = [None, OperationalError('secret SQL', {}, Exception(2006, 'secret data'))]
    with pytest.raises(StorageError) as error:
        repo.publish_plan(plan_document(), 'plans/1.json', 'd' * 64)
    assert parent.plan_status == 'pending'
    session.rollback.assert_called_once()
    session.commit.assert_not_called()
    assert 'secret' not in str(error.value) and error.value.__suppress_context__


def test_snapshot_insert_and_utc_output(setup):
    repo, sessions, _ = setup
    session = mock_session()
    sessions.append(session)
    document = snapshot_document()
    document['collected_at'] = NOW.astimezone(timezone(timedelta(hours=8))).isoformat()
    result = repo.ensure_snapshot(document)
    assert result['collected_at'] == '2026-01-01T12:00:00Z'
    assert result['readme_path'].endswith(' ')
    assert session.add.call_args.args[0].collected_at == SQL_NOW


def test_snapshot_reuses_original_id_and_collection_time(setup):
    repo, sessions, _ = setup
    session = mock_session()
    sessions.append(session)
    document = snapshot_document()
    existing = RepositorySnapshot(**{**document, 'snapshot_id': uid(31), 'collected_at': SQL_NOW})
    session.scalar.return_value = existing
    document['collected_at'] = (NOW + timedelta(days=1)).isoformat()
    result = repo.ensure_snapshot(document)
    assert result['snapshot_id'] == uid(31)
    assert result['collected_at'] == '2026-01-01T12:00:00Z'
    session.add.assert_not_called()


@pytest.mark.parametrize('field', ['repository_id', 'commit_sha', 'blob_sha', 'readme_path', 'content_hash', 'storage_key'])
def test_snapshot_collision_never_overwrites(setup, field):
    repo, sessions, _ = setup
    session = mock_session()
    sessions.append(session)
    document = snapshot_document()
    existing = RepositorySnapshot(**{**document, 'collected_at': SQL_NOW})
    setattr(existing, field, 77 if field == 'repository_id' else 'different')
    session.scalar.return_value = existing
    with pytest.raises(SearchIntegrityError):
        repo.ensure_snapshot(document)
    session.add.assert_not_called()
    session.commit.assert_not_called()


def prepare_usage_session(session):
    session.scalar.side_effect = [plan_rows()[0], None]
    session.get.side_effect = [RepositorySnapshot(repository_id=2**53 + 1), None]


def test_usage_validates_relationship_and_inserts(setup):
    repo, sessions, _ = setup
    session = mock_session()
    sessions.append(session)
    prepare_usage_session(session)
    result = repo.record_usage(usage_document())
    assert result['used_at'] == '2026-01-01T12:00:00Z'
    session.commit.assert_called_once()


@pytest.mark.parametrize('failure', ['pending', 'run', 'repository', 'duplicate_id'])
def test_usage_rejects_wrong_relationships(setup, failure):
    repo, sessions, _ = setup
    session = mock_session()
    sessions.append(session)
    plan = plan_rows('pending' if failure == 'pending' else 'ready')[0]
    if failure == 'run':
        plan.run_id = uid(100)
    session.scalar.side_effect = [plan, None]
    collision = SearchSnapshot(**{**usage_document(), 'snapshot_id': uid(100)}) if failure == 'duplicate_id' else None
    session.get.side_effect = [RepositorySnapshot(repository_id=77 if failure == 'repository' else 2**53 + 1), collision]
    with pytest.raises(SearchStateError if failure == 'pending' else SearchIntegrityError):
        repo.record_usage(usage_document())
    session.add.assert_not_called()


def test_usage_four_field_replay_returns_original(setup):
    repo, sessions, _ = setup
    session = mock_session()
    sessions.append(session)
    usage = usage_document()
    existing = SearchSnapshot(**{**usage, 'id': uid(41), 'used_at': SQL_NOW})
    session.scalar.side_effect = [plan_rows()[0], existing]
    session.get.side_effect = [RepositorySnapshot(repository_id=2**53 + 1), None]
    usage['used_at'] = (NOW + timedelta(days=1)).isoformat()
    result = repo.record_usage(usage)
    assert result['id'] == uid(41) and result['used_at'] == '2026-01-01T12:00:00Z'
    session.add.assert_not_called()


@pytest.mark.parametrize('patch', [
    {'unknown': 1}, {'repository_id': 1}, {'fetched_at': NOW}, {'full_name': None},
    {'html_url': ''}, {'full_name': 'x' * 256}, {'html_url': 'x' * 2049},
    {'stars': True}, {'stars': -1}, {'forks': 2**64}, {'forks': 1.0},
    {'topics': {}}, {'topics': [1]}, {'is_archived': 1}, {'language': 'x' * 65},
    {'license': 'x' * 129}, {'default_branch': 'x' * 256},
    {'description': '中' * 22000}, {'pushed_at': '2026-01-01T00:00:00'},
])
def test_metadata_validation_happens_before_session(setup, patch):
    repo, _, factory = setup
    with pytest.raises(SearchIntegrityError):
        repo.update_repository(1, patch, NOW)
    factory.assert_not_called()


def test_metadata_newer_sparse_null_and_missing(setup):
    repo, sessions, _ = setup
    session = mock_session()
    sessions.append(session)
    row = Repository(repository_id=1, full_name='old/name', html_url='https://github.com/old/name',
                     description='old', stars=42, topics=['a'], fetched_at=SQL_NOW - timedelta(seconds=1))
    session.scalar.return_value = row
    assert repo.update_repository(1, {'description': None, 'topics': [], 'pushed_at': NOW.isoformat()}, NOW)
    assert row.description is None and row.topics == [] and row.stars == 42
    assert row.full_name == 'old/name' and row.pushed_at == SQL_NOW and row.fetched_at == SQL_NOW
    assert 'FOR UPDATE' in str(session.scalar.call_args.args[0].compile(dialect=mysql.dialect()))


@pytest.mark.parametrize('delta,patch,raises', [
    (-1, {'stars': 100}, False), (0, {'stars': 42}, False),
    (0, {}, False), (0, {'stars': 100}, True),
])
def test_metadata_old_and_equal_watermarks(setup, delta, patch, raises):
    repo, sessions, _ = setup
    session = mock_session()
    sessions.append(session)
    row = Repository(repository_id=1, stars=42, fetched_at=SQL_NOW)
    session.scalar.return_value = row
    if raises:
        with pytest.raises(SearchIntegrityError):
            repo.update_repository(1, patch, NOW)
    else:
        assert repo.update_repository(1, patch, NOW + timedelta(seconds=delta)) is False
    assert row.stars == 42 and row.fetched_at == SQL_NOW
    session.flush.assert_not_called()


def test_initial_repository_requires_both_fields(setup):
    repo, sessions, _ = setup
    session = mock_session()
    sessions.append(session)
    with pytest.raises(SearchIntegrityError):
        repo.update_repository(1, {'stars': 1}, NOW)
    session.add.assert_not_called()


def db_error(code):
    cls = IntegrityError if code == 1062 else OperationalError
    return cls('secret SQL', {'secret': 'payload'}, Exception(code, 'secret driver message'))


def test_first_insert_duplicate_rolls_back_then_relocks(setup):
    repo, sessions, factory = setup
    first, second = mock_session(), mock_session()
    sessions.extend([first, second])
    first.flush.side_effect = db_error(1062)
    second.scalar.return_value = Repository(repository_id=1, full_name='a/b', html_url='url', fetched_at=SQL_NOW)
    assert repo.update_repository(1, {'full_name': 'a/b', 'html_url': 'url'}, NOW) is False
    first.rollback.assert_called_once()
    first.commit.assert_not_called()
    second.add.assert_not_called()
    assert factory.call_count == 2


@pytest.mark.parametrize('code', [1205, 1213])
def test_known_rollback_has_three_attempt_budget(setup, code):
    repo, sessions, factory = setup
    attempted = [mock_session() for _ in range(3)]
    sessions.extend(attempted)
    for session in attempted:
        session.scalar.side_effect = db_error(code)
    with pytest.raises(StorageError):
        repo.update_repository(1, {'full_name': 'a/b', 'html_url': 'url'}, NOW)
    assert factory.call_count == 3
    assert module.sleep.call_count == 2
    for session in attempted:
        session.rollback.assert_called_once()


def test_rollback_failure_prohibits_replay(setup):
    repo, sessions, factory = setup
    session = mock_session()
    sessions.append(session)
    session.scalar.side_effect = db_error(1213)
    session.rollback.side_effect = db_error(2006)
    with pytest.raises(StorageError, match='rollback unconfirmed'):
        repo.update_repository(1, {'stars': 5}, NOW)
    assert factory.call_count == 1


def test_unknown_commit_outcome_is_redacted_and_not_replayed(setup):
    repo, sessions, factory = setup
    session = mock_session()
    sessions.append(session)
    session.commit.side_effect = db_error(2013)
    with pytest.raises(StorageError) as error:
        repo.update_repository(1, {'full_name': 'a/b', 'html_url': 'url'}, NOW)
    assert factory.call_count == 1
    assert 'secret' not in str(error.value) and error.value.__suppress_context__
    module.sleep.assert_not_called()


def test_read_apis_and_persisted_watermark(setup):
    repo, sessions, factory = setup
    one, two, three, four = [mock_session() for _ in range(4)]
    sessions.extend([one, two, three, four])
    one.scalar.return_value = RepositorySnapshot(**{**snapshot_document(), 'collected_at': SQL_NOW})
    two.get.return_value = one.scalar.return_value
    three.get.return_value = Repository(repository_id=1, fetched_at=SQL_NOW)
    four.scalar.return_value = SQL_NOW
    assert repo.get_snapshot(snapshot_document()['snapshot_key'])['snapshot_id'] == uid(30)
    assert repo.get_snapshot_by_id(uid(30))['repository_id'] == 2**53 + 1
    assert repo.get_repository(1)['fetched_at'] == '2026-01-01T12:00:00Z'
    assert repo.latest_fetched_at() == NOW
    assert factory.call_count == 4
    for session in (one, two, three, four):
        session.commit.assert_not_called()
        session.__exit__.assert_called_once()


def test_clock_merges_watermarks_and_thread_safe_monotonicity():
    future = datetime(2099, 1, 1, tzinfo=UTC)
    clock = RequestClock([NOW, future.astimezone(timezone(timedelta(hours=8)))])
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: clock.issue(), range(200)))
    assert len(set(values)) == 200
    assert min(values) == future + timedelta(microseconds=1)
    assert max(values) == future + timedelta(microseconds=200)
    assert all(value.tzinfo == UTC for value in values)


def test_naive_clock_seed_rejected():
    with pytest.raises(SearchIntegrityError):
        RequestClock([SQL_NOW])


@pytest.mark.parametrize('value', [SQL_NOW, datetime(999, 1, 1, tzinfo=UTC), 'invalid'])
def test_request_time_validation_precedes_sql(setup, value):
    repo, _, factory = setup
    with pytest.raises(SearchIntegrityError):
        repo.update_repository(1, {}, value)
    factory.assert_not_called()


@pytest.mark.parametrize('value', [True, 0, -1, 2**64, 1.5, '123'])
def test_repository_id_is_exact_uint64(setup, value):
    repo, _, factory = setup
    with pytest.raises(SearchIntegrityError):
        repo.get_repository(value)
    factory.assert_not_called()


def test_snapshot_duplicate_insert_reloads_winner(setup):
    repo, sessions, factory = setup
    first, second = mock_session(), mock_session()
    sessions.extend([first, second])
    first.flush.side_effect = db_error(1062)
    second.scalar.return_value = RepositorySnapshot(**{
        **snapshot_document(), 'snapshot_id': uid(99), 'collected_at': SQL_NOW})
    assert repo.ensure_snapshot(snapshot_document())['snapshot_id'] == uid(99)
    assert factory.call_count == 2
    first.rollback.assert_called_once()
    second.add.assert_not_called()


def test_reused_snapshot_id_cannot_bind_another_identity(setup):
    repo, sessions, _ = setup
    session = mock_session()
    sessions.append(session)
    session.get.return_value = RepositorySnapshot(**{
        **snapshot_document(), 'repository_id': 77, 'collected_at': SQL_NOW})
    with pytest.raises(SearchIntegrityError):
        repo.ensure_snapshot(snapshot_document())
    session.add.assert_not_called()


def test_pending_with_children_is_not_repaired_by_overwrite(setup):
    repo, sessions, _ = setup
    session = mock_session()
    sessions.append(session)
    parent, groups, queries = plan_rows('pending')
    session.scalar.return_value = parent
    projection_results(session, groups, queries)
    with pytest.raises(SearchIntegrityError):
        repo.publish_plan(plan_document(), 'plans/1.json', 'd' * 64)
    session.add_all.assert_not_called()
    session.commit.assert_not_called()
    assert parent.plan_status == 'pending'


def test_missing_reads_and_no_watermark(setup):
    repo, sessions, _ = setup
    sessions.extend([mock_session() for _ in range(4)])
    assert repo.get_snapshot('a' * 64) is None
    assert repo.get_snapshot_by_id(uid(30)) is None
    assert repo.get_repository(1) is None
    assert repo.latest_fetched_at() is None


def test_read_errors_are_redacted(setup):
    repo, sessions, factory = setup
    session = mock_session()
    sessions.append(session)
    session.get.side_effect = db_error(2013)
    with pytest.raises(StorageError) as error:
        repo.get_repository(1)
    assert 'secret' not in str(error.value) and error.value.__suppress_context__
    assert factory.call_count == 1

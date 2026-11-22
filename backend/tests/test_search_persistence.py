import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.schemas.requirements import RequirementJSON, RequirementRevision
from app.schemas.search import SearchIntegrityError, SearchPlanDocument, SearchStateError
from app.services.search_contracts import query_hash, snapshot_key
from app.services.search_persistence import SearchPersistence
from app.services.storage import LocalStorage, StorageError


OWNER = 'a' * 64


class RepositoryStub:
    def __init__(self):
        self.plans = {}
        self.snapshots = {}
        self.usages = {}
        self.fail_plan = False
        self.fail_snapshot = None

    def ensure_pending(self, plan, key, digest):
        value = (copy.deepcopy(plan), key, digest)
        if plan['plan_id'] in self.plans:
            assert self.plans[plan['plan_id']]['value'] == value
        else:
            self.plans[plan['plan_id']] = {'value': value, 'ready': False}

    def publish_plan(self, plan, key, digest):
        if self.fail_plan:
            self.fail_plan = False
            raise StorageError('simulated rolled back projection')
        self.plans[plan['plan_id']]['ready'] = True

    def plan_is_ready(self, plan, key, digest):
        row = self.plans.get(plan['plan_id'])
        if row is None:
            return False
        if row['value'] != (plan, key, digest):
            raise SearchIntegrityError('projection mismatch')
        return row['ready']

    def ensure_snapshot(self, snapshot):
        failure, self.fail_snapshot = self.fail_snapshot, None
        if failure == 'before':
            raise StorageError('simulated SQL connection failure')
        existing = self.snapshots.get(snapshot['snapshot_key'])
        if existing is not None:
            for key in ('repository_id', 'commit_sha', 'readme_path', 'blob_sha', 'content_hash', 'storage_key'):
                if existing[key] != snapshot[key]:
                    raise SearchIntegrityError('snapshot collision')
        else:
            self.snapshots[snapshot['snapshot_key']] = copy.deepcopy(snapshot)
        if failure == 'after':
            raise StorageError('simulated unknown SQL commit')
        return copy.deepcopy(self.snapshots[snapshot['snapshot_key']])

    def get_snapshot_by_id(self, snapshot_id):
        return next((copy.deepcopy(row) for row in self.snapshots.values() if row['snapshot_id'] == snapshot_id), None)

    def record_usage(self, usage):
        if usage['usage_type'] not in {'candidate_review', 'evidence_retrieval', 'deep_analysis'}:
            raise SearchStateError('invalid usage')
        key = tuple(usage[k] for k in ('run_id', 'repository_id', 'snapshot_id', 'usage_type'))
        self.usages.setdefault(key, copy.deepcopy(usage))
        return copy.deepcopy(self.usages[key])

    def latest_fetched_at(self):
        return None


class FaultStorage(LocalStorage):
    failure_prefix = None
    failure_mode = None

    def compare_and_swap(self, key, expected, data):
        if self.failure_prefix and key.startswith(self.failure_prefix):
            mode = self.failure_mode
            self.failure_prefix = None
            if mode == 'before':
                raise StorageError('simulated unavailable write')
            super().compare_and_swap(key, expected, data)
            raise StorageError('simulated unknown committed write')
        super().compare_and_swap(key, expected, data)


def build_context(tmp_path):
    task_id, revision_id, run_id = str(uuid4()), str(uuid4()), str(uuid4())
    now = datetime.now(timezone.utc)
    revision = RequirementRevision(
        revision_id=revision_id, task_id=task_id, revision_number=1, parent_revision_id=None,
        status='confirmed', requirements=RequirementJSON(
            project_topic='文本搜索', project_description=None, project_type=None, domain=None,
            required_features=['全文搜索'], preferred_features=None, programming_languages=None,
            required_technologies=None, preferred_technologies=None, missing_information=[],
            uncertain_information=None, source_evidence=[],
        ), requirement_items=[], change_log=[], created_at=now, confirmed_at=now,
    )
    query = dict(query_id=str(uuid4()), query_text='text search', query_type='primary', language_filter=None,
                 qualifiers={}, query_hash=query_hash('text search', qualifiers={}),
                 source_requirement_pointers=['/project_topic'], created_at=now)
    groups = [dict(group_id=str(uuid4()), group_name=name, group_purpose='查找主题项目',
                   group_status='ready' if name == 'A' else 'skipped',
                   source_requirement_pointers=['/project_topic'] if name == 'A' else [],
                   skip_reason=None if name == 'A' else '本组无来源', created_at=now,
                   queries=[query] if name == 'A' else []) for name in 'ABC']
    plan = SearchPlanDocument(plan_id=str(uuid4()), run_id=run_id, confirmed_revision_id=revision_id,
                              planner_version='v1', model_version='test-fixture', prompt_version='v1',
                              created_at=now, groups=groups)
    storage, repository = FaultStorage(tmp_path), RepositoryStub()
    service = SearchPersistence(storage, repository)
    service.create_run(OWNER, task_id, revision, run_id)
    return service, storage, repository, revision, plan


@pytest.fixture
def context(tmp_path):
    return build_context(tmp_path)


def publish(context):
    service, storage, repository, revision, plan = context
    service.publish_plan(OWNER, str(revision.task_id), revision, plan)
    service.manifests.start(OWNER, plan.run_id)
    return service, storage, repository, revision, plan


def readme_arguments(repo=10, data=b'# README\nreal test bytes\n'):
    return repo, 'a' * 40, 'README.md', hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest(), data


def test_publish_immutable_original_and_ready_gate(context):
    service, storage, repository, revision, plan = context
    with pytest.raises(SearchStateError):
        service.read_plan(OWNER, plan.run_id)
    result = service.publish_plan(OWNER, str(revision.task_id), revision, plan)
    assert result['plan_publication_status'] == 'published'
    raw = storage.get(result['plan']['plan_storage_key'])
    assert hashlib.sha256(raw).hexdigest() == result['plan']['plan_hash']
    assert 'run_status' not in json.loads(raw)
    assert 'plan_status' not in json.loads(raw)
    assert service.read_plan(OWNER, plan.run_id) == plan
    service.publish_plan(OWNER, str(revision.task_id), revision, plan)
    assert storage.get(result['plan']['plan_storage_key']) == raw
    assert len(repository.plans) == 1


def test_projection_failure_recovers_from_original_only(context):
    service, storage, repository, revision, plan = context
    repository.fail_plan = True
    with pytest.raises(StorageError):
        service.publish_plan(OWNER, str(revision.task_id), revision, plan)
    assert service.manifests.read(OWNER, plan.run_id)['plan_publication_status'] == 'preparing'
    with pytest.raises(SearchStateError):
        service.read_plan(OWNER, plan.run_id)
    assert service.recover_plan(OWNER, plan.run_id, revision)['plan_publication_status'] == 'published'


def test_missing_original_does_not_regenerate(context):
    service, storage, repository, revision, plan = context
    storage.failure_prefix, storage.failure_mode = 'search-plans/', 'before'
    with pytest.raises(StorageError):
        service.publish_plan(OWNER, str(revision.task_id), revision, plan)
    with pytest.raises(StorageError):
        service.recover_plan(OWNER, plan.run_id, revision)
    assert not repository.plans


def test_unknown_plan_write_is_confirmed_without_replaying(context):
    service, storage, repository, revision, plan = context
    storage.failure_prefix, storage.failure_mode = 'search-plans/', 'after'
    service.publish_plan(OWNER, str(revision.task_id), revision, plan)
    assert service.read_plan(OWNER, plan.run_id) == plan


def test_corrupt_plan_blocks_reads_without_overwrite(context):
    service, storage, repository, revision, plan = publish(context)
    key = service.manifests.read(OWNER, plan.run_id)['plan']['plan_storage_key']
    raw, version = storage.read_version(key)
    storage.compare_and_swap(key, version, raw + b' ')
    with pytest.raises(SearchIntegrityError):
        service.read_plan(OWNER, plan.run_id)
    assert service.manifests.read(OWNER, plan.run_id)['integrity_blocked']
    assert storage.get(key) == raw + b' '


def test_revision_task_and_sources_cannot_be_substituted(context):
    service, storage, repository, revision, plan = context
    with pytest.raises(ValueError):
        service.publish_plan(OWNER, str(uuid4()), revision, plan)
    wrong = plan.model_dump(mode='json')
    wrong['groups'][0]['source_requirement_pointers'] = ['/project_overview/project_topic']
    wrong['groups'][0]['queries'][0]['source_requirement_pointers'] = ['/project_overview/project_topic']
    with pytest.raises(ValueError):
        service.publish_plan(OWNER, str(revision.task_id), revision, wrong)
    assert not repository.plans


def test_readme_snapshot_and_usage_are_separate(context):
    service, storage, repository, revision, plan = publish(context)
    args = readme_arguments()
    snapshot = service.collect_readme(OWNER, plan.run_id, *args)
    manifest = service.manifests.read(OWNER, plan.run_id)
    assert len(repository.snapshots) == 1 and not repository.usages
    assert manifest['candidates']['10']['snapshot_id'] == snapshot['snapshot_id']
    assert service.read_snapshot(OWNER, plan.run_id, snapshot['snapshot_id']) == args[-1]
    first = service.record_usage(OWNER, plan.run_id, snapshot['snapshot_id'], 'candidate_review')
    assert service.record_usage(OWNER, plan.run_id, snapshot['snapshot_id'], 'candidate_review') == first
    assert len(repository.usages) == 1
    assert service.collect_readme(OWNER, plan.run_id, *args)['snapshot_id'] == snapshot['snapshot_id']
    assert len(manifest['readme_persistence_operations']) == 1


@pytest.mark.parametrize('prefix,mode', [
    ('staging/readmes/', 'before'), ('staging/readmes/', 'after'),
    ('repository-snapshots/', 'before'), ('repository-snapshots/', 'after'),
])
def test_readme_storage_failure_recovery(context, prefix, mode):
    service, storage, repository, revision, plan = publish(context)
    storage.failure_prefix, storage.failure_mode = prefix, mode
    if mode == 'before':
        with pytest.raises(StorageError):
            service.collect_readme(OWNER, plan.run_id, *readme_arguments())
    else:
        service.collect_readme(OWNER, plan.run_id, *readme_arguments())
    operation = next(iter(service.manifests.read(OWNER, plan.run_id)['readme_persistence_operations'].values()))
    if prefix == 'staging/readmes/' and mode == 'before':
        assert service.resume_readme(OWNER, plan.run_id, operation['operation_id']) is None
        assert not repository.snapshots
        assert service.manifests.read(OWNER, plan.run_id)['readme_persistence_operations'][operation['operation_id']]['operation_status'] == 'interrupted'
    else:
        assert service.resume_readme(OWNER, plan.run_id, operation['operation_id'])['snapshot_key'] == operation['snapshot_key']
        assert len(repository.snapshots) == 1


@pytest.mark.parametrize('failure', ['before', 'after'])
def test_snapshot_commit_failure_recovers_idempotently(context, failure):
    service, storage, repository, revision, plan = publish(context)
    repository.fail_snapshot = failure
    with pytest.raises(StorageError):
        service.collect_readme(OWNER, plan.run_id, *readme_arguments())
    results = service.recover_readmes(OWNER, plan.run_id)
    assert len(results) == 1 and len(repository.snapshots) == 1
    operation = next(iter(service.manifests.read(OWNER, plan.run_id)['readme_persistence_operations'].values()))
    assert operation['operation_status'] == 'completed'
    assert results[operation['operation_id']]['snapshot_id'] == operation['snapshot_id']


def test_corrupt_readme_is_never_replaced(context):
    service, storage, repository, revision, plan = publish(context)
    snapshot = service.collect_readme(OWNER, plan.run_id, *readme_arguments())
    raw, version = storage.read_version(snapshot['storage_key'])
    storage.compare_and_swap(snapshot['storage_key'], version, b'changed')
    with pytest.raises(SearchIntegrityError):
        service.read_snapshot(OWNER, plan.run_id, snapshot['snapshot_id'])
    assert storage.get(snapshot['storage_key']) == b'changed'
    assert service.manifests.read(OWNER, plan.run_id)['integrity_blocked']


def test_changed_content_creates_immutable_history(context):
    service, storage, repository, revision, plan = publish(context)
    original = service.collect_readme(OWNER, plan.run_id, *readme_arguments())
    args = list(readme_arguments(data=b'new bytes'))
    args[1] = 'b' * 40
    changed = service.collect_readme(OWNER, plan.run_id, *args)
    assert original['snapshot_id'] != changed['snapshot_id']
    assert len(repository.snapshots) == 2
    assert service.read_snapshot(OWNER, plan.run_id, original['snapshot_id']) == readme_arguments()[-1]


def test_concurrent_readmes_keep_all_results(context):
    service, storage, repository, revision, plan = publish(context)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: service.collect_readme(OWNER, plan.run_id, *readme_arguments(i)), range(20, 28)))
    manifest = service.manifests.read(OWNER, plan.run_id)
    assert len(manifest['readme_persistence_operations']) == 8
    assert all(op['operation_status'] == 'completed' for op in manifest['readme_persistence_operations'].values())
    assert {row['snapshot_id'] for row in results} == {row['snapshot_id'] for row in manifest['candidates'].values()}
    service.manifests.seal_dispatch(OWNER, plan.run_id)
    assert service.manifests.finish(OWNER, plan.run_id)['run_status'] == 'completed'


def test_staging_cleanup_requires_retention_and_verified_final(context):
    service, storage, repository, revision, plan = publish(context)
    snapshot = service.collect_readme(OWNER, plan.run_id, *readme_arguments())
    operation = next(iter(service.manifests.read(OWNER, plan.run_id)['readme_persistence_operations'].values()))
    assert service.cleanup_staging(OWNER, plan.run_id, operation['operation_id']) is False
    later = datetime.fromisoformat(operation['updated_at']) + timedelta(days=2)
    assert service.cleanup_staging(OWNER, plan.run_id, operation['operation_id'], now=later)
    assert storage.read_version(operation['staging_key'])[0] is None
    assert storage.get(snapshot['storage_key']) == readme_arguments()[-1]


def test_failed_run_recovery_does_not_restart_run(context):
    service, storage, repository, revision, plan = publish(context)
    repository.fail_snapshot = 'after'
    with pytest.raises(StorageError):
        service.collect_readme(OWNER, plan.run_id, *readme_arguments())
    service.manifests.fail(OWNER, plan.run_id, 'RUN_INTERRUPTED')
    assert len(service.recover_readmes(OWNER, plan.run_id)) == 1
    assert service.manifests.read(OWNER, plan.run_id)['run_status'] == 'failed'


def test_request_clock_reserves_before_dispatch_and_reuses_identity(context):
    service, storage, repository, revision, plan = publish(context)
    clock = service.initialize_request_clock([(OWNER, plan.run_id)])
    request_id = str(uuid4())
    issued = service.reserve_request(OWNER, plan.run_id, request_id, clock)
    assert service.reserve_request(OWNER, plan.run_id, request_id, clock) == issued
    manifest = service.manifests.read(OWNER, plan.run_id)
    assert manifest['requests'][request_id] == issued.isoformat()
    future = issued + timedelta(days=2)
    service.manifests.reserve_request(OWNER, plan.run_id, str(uuid4()), future)
    restarted_service = SearchPersistence(storage, repository)
    restarted = restarted_service.initialize_request_clock([(OWNER, plan.run_id)])
    assert restarted_service.reserve_request(OWNER, plan.run_id, request_id, restarted) == issued
    assert restarted_service.reserve_request(OWNER, plan.run_id, str(uuid4()), restarted) > future


def test_blob_identity_and_size_checked_before_storage(context):
    service, storage, repository, revision, plan = publish(context)
    args = list(readme_arguments())
    args[3] = 'c' * 40
    with pytest.raises(SearchIntegrityError):
        service.collect_readme(OWNER, plan.run_id, *args)
    with pytest.raises(SearchStateError):
        service.collect_readme(OWNER, plan.run_id, *readme_arguments(data=b'x' * (1024 * 1024 + 1)))
    assert not service.manifests.read(OWNER, plan.run_id)['readme_persistence_operations']


def test_unowned_snapshot_is_not_readable(context):
    service, storage, repository, revision, plan = publish(context)
    snapshot = service.collect_readme(OWNER, plan.run_id, *readme_arguments())
    with pytest.raises((SearchStateError, StorageError)):
        service.read_snapshot('b' * 64, plan.run_id, snapshot['snapshot_id'])
    with pytest.raises(SearchStateError):
        service.read_snapshot(OWNER, plan.run_id, str(uuid4()))

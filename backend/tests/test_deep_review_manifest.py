"""Offline CAS coverage for the singular deep-review Manifest operation."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.persistence.run_manifest import RunManifestStore
from app.schemas.search import SearchIntegrityError, SearchStateError
from app.services.search_contracts import SNAPSHOT_KEY_ALGORITHM, snapshot_key
from app.services.storage import LocalStorage

OWNER = 'a' * 64
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def uid():
    return str(uuid4())


def retrieval_operation():
    return {'operation_id': uid(), 'version': 'evidence-bundle-v2', 'chunk_index_key': uid(),
            'revision_hash': 'a' * 64, 'plan_hash': 'b' * 64, 'config_hash': 'c' * 64,
            'bundle_hash': 'd' * 64, 'model': 'BAAI/bge-m3', 'dimension': 1024,
            'storage_key': None, 'status': 'ready', 'error_code': None,
            'created_at': NOW.isoformat(), 'updated_at': NOW.isoformat()}


def review_operation(revision_id, retrieval):
    return {'operation_id': uid(), 'status': 'running', 'revision_id': revision_id,
            'revision_hash': retrieval['revision_hash'], 'bundle_hash': retrieval['bundle_hash'],
            'model': 'deepseek-flash', 'prompt_version': 'deep-review-v1', 'consent': True,
            'authorized_at': NOW.isoformat(),
            'deadline': (NOW + timedelta(seconds=570)).isoformat(),
            'report_key': None, 'report_hash': None, 'error_code': None}


@pytest.fixture
def env(tmp_path):
    storage = LocalStorage(tmp_path)
    store = RunManifestStore(storage)
    run_id, task_id, revision_id = uid(), uid(), uid()
    store.create(OWNER, run_id, task_id, revision_id)
    store.start(OWNER, run_id)
    plan_id = uid()
    plan = {'plan_id': plan_id, 'plan_storage_key': f'search-plans/{plan_id}.json',
            'plan_hash': 'e' * 64, 'confirmed_revision_id': revision_id}
    store.bind_plan(OWNER, run_id, plan)
    store.publish_plan(OWNER, run_id, plan)
    op_id = uid()
    key = snapshot_key(1, 'b' * 40, 'README.md', 'c' * 40)
    readme = {'operation_id': op_id, 'repository_id': 1, 'commit_sha': 'b' * 40,
              'readme_path': 'README.md', 'blob_sha': 'c' * 40, 'content_hash': 'f' * 64,
              'snapshot_key': key, 'storage_key': f'repository-snapshots/{key}/readme.raw',
              'operation_status': 'prepared', 'snapshot_id': uid(),
              'staging_key': f'staging/readmes/{op_id}.raw', 'byte_length': 12,
              'created_at': NOW.isoformat(), 'updated_at': NOW.isoformat(),
              'collected_at': NOW.isoformat(), 'snapshot_key_algorithm': SNAPSHOT_KEY_ALGORITHM}
    store.register_operation(OWNER, run_id, readme)
    for stage in ('staged', 'object_verified', 'snapshot_committed', 'completed'):
        store.advance_operation(OWNER, run_id, op_id, stage, uid())
    store.seal_dispatch(OWNER, run_id)
    index_key = uid()
    chunk = {'operation_id': uid(), 'version': 'chunk-index-v1', 'index_key': index_key,
             'revision_hash': 'a' * 64, 'plan_hash': plan['plan_hash'], 'model': 'BAAI/bge-m3',
             'dimension': 1024, 'preprocess_version': 'raw-v1', 'config_hash': 'c' * 64,
             'status': 'ready', 'chunk_manifest_key': f'chunk-index/{index_key}/manifest.json',
             'chunk_manifest_hash': '9' * 64, 'usage': {readme['snapshot_id']: True}, 'error_code': None,
             'failed_operation_ids': [], 'created_at': NOW.isoformat(), 'updated_at': NOW.isoformat()}
    pending = {**chunk, 'status': 'pending',
               'chunk_manifest_key': None, 'chunk_manifest_hash': None, 'usage': {}}
    store.update(OWNER, run_id, lambda m: m.update(chunk_index_operation=pending))
    store.update(OWNER, run_id, lambda m: m.update(chunk_index_operation=chunk))
    retrieval = retrieval_operation()
    retrieval.update(chunk_index_key=index_key,
                     storage_key=f"retrieval-bundles/{retrieval['operation_id']}.json")
    store.update(OWNER, run_id, lambda m: m.update(evidence_bundle_operation=retrieval))
    return store, storage, run_id, revision_id, retrieval


def test_create_once_preserves_existing_manifest_and_input(env):
    store, _, run_id, revision_id, retrieval = env
    before = store.read(OWNER, run_id)
    operation = review_operation(revision_id, retrieval)
    original = deepcopy(operation)
    created, flag = store.create_deep_review(OWNER, run_id, operation)
    assert flag and created == operation and operation == original
    after = store.read(OWNER, run_id)
    assert after.pop('deep_review_operation') == operation
    assert after == before
    again, flag = store.create_deep_review(OWNER, run_id, review_operation(revision_id, retrieval))
    assert not flag and again == operation


def test_concurrent_create_has_one_winner(env):
    store, _, run_id, revision_id, retrieval = env
    operations = [review_operation(revision_id, retrieval) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda op: store.create_deep_review(OWNER, run_id, op), operations))
    assert sum(created for _, created in results) == 1
    assert len({op['operation_id'] for op, _ in results}) == 1


def test_repeated_create_rejects_different_revision(env):
    store, _, run_id, revision_id, retrieval = env
    store.create_deep_review(OWNER, run_id, review_operation(revision_id, retrieval))
    with pytest.raises(SearchStateError):
        store.create_deep_review(OWNER, run_id, review_operation(uid(), retrieval))


@pytest.mark.parametrize('field,value', [
    ('consent', False), ('prompt_version', 'deep-review-v2'), ('model', ''),
    ('status', 'queued'), ('deadline', NOW.isoformat()), ('report_hash', '0' * 64),
    ('error_code', 'FAILED'), ('revision_hash', 'f' * 64), ('bundle_hash', 'f' * 64),
])
def test_create_rejects_invalid_authorization(env, field, value):
    store, _, run_id, revision_id, retrieval = env
    operation = review_operation(revision_id, retrieval)
    operation[field] = value
    with pytest.raises(SearchIntegrityError):
        store.create_deep_review(OWNER, run_id, operation)
    assert 'deep_review_operation' not in store.read(OWNER, run_id)


def test_identity_and_sources_are_immutable(env):
    store, _, run_id, revision_id, retrieval = env
    store.create_deep_review(OWNER, run_id, review_operation(revision_id, retrieval))
    with pytest.raises(SearchIntegrityError):
        store.update(OWNER, run_id, lambda m: m['deep_review_operation'].update(consent=False))
    with pytest.raises(SearchIntegrityError):
        store.update(OWNER, run_id, lambda m: m['deep_review_operation'].update(model='other'))
    with pytest.raises(SearchIntegrityError):
        store.update(OWNER, run_id, lambda m: m['candidates'].update({'1': {'sources': ['new']}}))
    with pytest.raises(SearchIntegrityError):
        store.update(OWNER, run_id, lambda m: m['evidence_bundle_operation'].update(bundle_hash='f' * 64))


@pytest.mark.parametrize('status', ['completed', 'partial'])
def test_successful_terminal_requires_immutable_report(env, status):
    store, _, run_id, revision_id, retrieval = env
    operation, _ = store.create_deep_review(OWNER, run_id, review_operation(revision_id, retrieval))
    report_key = f"deep-reviews/{operation['operation_id']}/report.json"
    report_hash = '8' * 64
    with pytest.raises(SearchIntegrityError):
        store.complete_deep_review(OWNER, run_id, operation['operation_id'], status)
    manifest = store.complete_deep_review(OWNER, run_id, operation['operation_id'], status,
                                          report_key=report_key, report_hash=report_hash)
    assert manifest['deep_review_operation']['status'] == status
    store.complete_deep_review(OWNER, run_id, operation['operation_id'], status,
                               report_key=report_key, report_hash=report_hash)
    with pytest.raises(SearchIntegrityError):
        store.complete_deep_review(OWNER, run_id, operation['operation_id'], status,
                                   report_key=report_key, report_hash='7' * 64)
    with pytest.raises(SearchIntegrityError):
        store.update(OWNER, run_id, lambda m: m['deep_review_operation'].update(status='running'))


@pytest.mark.parametrize('status,kwargs', [
    ('failed', {'error_code': 'PROVIDER_AUTH'}),
    ('unknown', {'error_code': 'PROVIDER_TIMEOUT'}),
    ('failed', {}),
    ('unknown', {}),
])
def test_terminal_without_report_requires_error(env, status, kwargs):
    store, _, run_id, revision_id, retrieval = env
    operation, _ = store.create_deep_review(OWNER, run_id, review_operation(revision_id, retrieval))
    if kwargs:
        result = store.complete_deep_review(OWNER, run_id, operation['operation_id'], status, **kwargs)
        assert result['deep_review_operation']['status'] == status
    else:
        with pytest.raises(SearchIntegrityError):
            store.complete_deep_review(OWNER, run_id, operation['operation_id'], status)


def test_cannot_create_without_ready_stable_bundle(tmp_path):
    store = RunManifestStore(LocalStorage(tmp_path))
    run_id, revision_id = uid(), uid()
    store.create(OWNER, run_id, uid(), revision_id)
    store.start(OWNER, run_id)
    store.seal_dispatch(OWNER, run_id)
    with pytest.raises(SearchStateError):
        store.create_deep_review(OWNER, run_id, review_operation(revision_id, retrieval_operation()))

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.core.settings import SiliconFlowSettings
from app.persistence.run_manifest import RunManifestStore
from app.schemas.search import SearchIntegrityError, SearchStateError
from app.services.chunk_index import DurableProgress, build_manifest
from app.services.chunking import normalize_readme
from app.services.search_contracts import SNAPSHOT_KEY_ALGORITHM, snapshot_key
from app.services.storage import LocalStorage

OWNER = 'a' * 64
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def uid():
    return str(uuid4())


def settings(**overrides):
    values = dict(siliconflow_api_key='secret', embedding_enabled=True, embedding_batch_size=16,
                  embedding_max_batches=32, embedding_max_chunks_per_run=512,
                  embedding_max_tokens_per_run=262144)
    values.update(overrides)
    return SiliconFlowSettings(_env_file=None, **values)


def snapshot(sid, repo, text):
    return {'snapshot_id': sid, 'repository_id': str(repo), 'commit_sha': 'b' * 40,
            'readme_path': 'README.md', 'content_hash': 'd' * 64,
            'normalized': normalize_readme(text.encode())}


def test_build_manifest_counts_and_offsets():
    report = build_manifest(OWNER, 'task', 'run', 'r' * 64, 'p' * 64, settings(),
                            [snapshot('s1', 1, '# A\n\n' + 'word ' * 400), snapshot('s2', 2, '## B\n\ntext')])
    assert report['chunk_count'] == sum(row['chunk_count'] for row in report['snapshots'])
    assert report['chunk_count'] >= 2
    for row in report['snapshots']:
        for chunk in row['chunks']:
            assert chunk['commit_sha'] == 'b' * 40 and chunk['readme_path'] == 'README.md'


def test_non_utf8_snapshot_is_recorded_not_chunked():
    row = snapshot('s1', 1, 'x')
    row['normalized'] = None
    report = build_manifest(OWNER, 'task', 'run', 'r' * 64, 'p' * 64, settings(), [row])
    assert report['snapshots'][0]['coverage'] == 'non_utf8'
    assert report['chunk_count'] == 0


def test_durable_progress_survives_reload(tmp_path):
    storage = LocalStorage(tmp_path)
    progress = DurableProgress.load(storage, 'index/progress.json')
    progress.record('batch-1', 'h1', {'prompt_tokens': 3, 'total_tokens': 3})
    reloaded = DurableProgress.load(storage, 'index/progress.json')
    assert reloaded.done('batch-1', 'h1') and reloaded.batches == 1 and reloaded.tokens == 3
    assert json.loads(storage.get('index/progress.json'))['completed'] == {'batch-1': 'h1'}


def _stable_manifest(tmp_path):
    storage = LocalStorage(tmp_path)
    store = RunManifestStore(storage)
    run, task, revision = uid(), uid(), uid()
    store.create(OWNER, run, task, revision)
    store.start(OWNER, run)
    plan_id = uid()
    plan = dict(plan_id=plan_id, plan_storage_key=f'search-plans/{plan_id}.json',
                plan_hash='e' * 64, confirmed_revision_id=revision)
    store.bind_plan(OWNER, run, plan)
    store.publish_plan(OWNER, run, plan)
    op_id = uid()
    key = snapshot_key(1, 'b' * 40, 'README.md', 'c' * 40)
    operation = dict(operation_id=op_id, repository_id=1, commit_sha='b' * 40, readme_path='README.md',
                     blob_sha='c' * 40, content_hash='d' * 64, snapshot_key=key,
                     storage_key=f'repository-snapshots/{key}/readme.raw', operation_status='prepared',
                     snapshot_id=uid(), staging_key=f'staging/readmes/{op_id}.raw', byte_length=12,
                     created_at=NOW.isoformat(), updated_at=NOW.isoformat(), collected_at=NOW.isoformat(),
                     snapshot_key_algorithm=SNAPSHOT_KEY_ALGORITHM)
    store.register_operation(OWNER, run, operation)
    store.update(OWNER, run, lambda m: m['candidates'].update({'1': {'sources': ['q']}}))
    for stage in ('staged', 'object_verified', 'snapshot_committed', 'completed'):
        result = store.advance_operation(OWNER, run, op_id, stage, uid())
    store.seal_dispatch(OWNER, run)
    return store, storage, run, plan, result['readme_persistence_operations'][op_id]['snapshot_id']


def _chunk_op(plan, snapshot_id, status='pending', **overrides):
    operation = {'operation_id': uid(), 'version': 'chunk-index-v1', 'index_key': uid(),
                 'revision_hash': 'f' * 64, 'plan_hash': plan['plan_hash'], 'model': 'BAAI/bge-m3',
                 'dimension': 1024, 'preprocess_version': 'raw-v1', 'config_hash': 'a' * 64,
                 'status': status, 'chunk_manifest_key': None, 'chunk_manifest_hash': None,
                 'usage': {}, 'error_code': None, 'failed_operation_ids': [],
                 'created_at': NOW.isoformat(), 'updated_at': NOW.isoformat()}
    operation.update(overrides)
    return operation


def _install_ready(store, run, plan, snapshot_id, index_key=None):
    pending = _chunk_op(plan, snapshot_id, index_key=index_key or uid())
    store.update(OWNER, run, lambda m: m.update(chunk_index_operation=pending))
    key = f"chunk-index/{pending['index_key']}/manifest.json"
    embedding = {**pending, 'status': 'embedding', 'chunk_manifest_key': key,
                 'chunk_manifest_hash': '1' * 64, 'usage': {snapshot_id: False}}
    store.update(OWNER, run, lambda m: m.update(chunk_index_operation=embedding))
    ready = {**embedding, 'status': 'ready', 'usage': {snapshot_id: True}}
    store.update(OWNER, run, lambda m: m.update(chunk_index_operation=ready))
    return ready


def test_manifest_accepts_pending_embedding_ready(tmp_path):
    store, storage, run, plan, snapshot_id = _stable_manifest(tmp_path)
    ready = _install_ready(store, run, plan, snapshot_id)
    assert store.read(OWNER, run)['chunk_index_operation'] == ready


def test_ready_chunk_index_cannot_change(tmp_path):
    store, storage, run, plan, snapshot_id = _stable_manifest(tmp_path)
    ready = _install_ready(store, run, plan, snapshot_id)
    with pytest.raises(SearchIntegrityError):
        store.update(OWNER, run, lambda m: m.update(
            chunk_index_operation={**ready, 'status': 'failed', 'error_code': 'X'}))


def test_failed_operation_can_only_be_superseded(tmp_path):
    store, storage, run, plan, snapshot_id = _stable_manifest(tmp_path)
    pending = _chunk_op(plan, snapshot_id)
    store.update(OWNER, run, lambda m: m.update(chunk_index_operation=pending))
    failed = {**pending, 'status': 'failed', 'error_code': 'EMBEDDING_TIMEOUT'}
    store.update(OWNER, run, lambda m: m.update(chunk_index_operation=failed))
    with pytest.raises(SearchIntegrityError):
        store.update(OWNER, run, lambda m: m.update(
            chunk_index_operation={**failed, 'status': 'embedding'}))
    retry = _chunk_op(plan, snapshot_id, status='pending', index_key=failed['index_key'],
                      failed_operation_ids=[failed['operation_id']])
    store.update(OWNER, run, lambda m: m.update(chunk_index_operation=retry))
    assert store.read(OWNER, run)['chunk_index_operation']['operation_id'] == retry['operation_id']


def _bundle_op(chunk, status='ready', version='evidence-bundle-v1', **overrides):
    operation = {'operation_id': uid(), 'version': version,
                 'revision_hash': chunk['revision_hash'], 'plan_hash': chunk['plan_hash'],
                 'chunk_index_key': chunk['index_key'], 'model': 'BAAI/bge-m3', 'dimension': 1024,
                 'config_hash': '2' * 64,
                 'storage_key': None, 'bundle_hash': '3' * 64, 'status': status,
                 'error_code': None, 'created_at': NOW.isoformat(), 'updated_at': NOW.isoformat()}
    operation['storage_key'] = f"retrieval-bundles/{operation['operation_id']}.json"
    operation.update(overrides)
    return operation


@pytest.mark.parametrize('version', ['evidence-bundle-v1', 'evidence-bundle-v2'])
def test_retrieval_bundle_requires_ready_chunk_index_and_is_immutable(tmp_path, version):
    store, storage, run, plan, snapshot_id = _stable_manifest(tmp_path)
    with pytest.raises(SearchIntegrityError):
        store.update(OWNER, run, lambda m: m.update(evidence_bundle_operation=_bundle_op(
            {'revision_hash': 'f' * 64, 'plan_hash': plan['plan_hash'], 'index_key': uid()},
            version=version)))
    chunk = _install_ready(store, run, plan, snapshot_id)
    operation = _bundle_op(chunk, version=version)
    store.update(OWNER, run, lambda m: m.update(evidence_bundle_operation=operation))
    assert store.read(OWNER, run)['evidence_bundle_operation'] == operation
    with pytest.raises(SearchIntegrityError):
        store.update(OWNER, run, lambda m: m.update(
            evidence_bundle_operation={**operation, 'status': 'partial'}))


def test_failed_retrieval_bundle_is_allowed_without_ready_index(tmp_path):
    store, storage, run, plan, snapshot_id = _stable_manifest(tmp_path)
    pending = _chunk_op(plan, snapshot_id)
    store.update(OWNER, run, lambda m: m.update(chunk_index_operation=pending))
    operation = _bundle_op(pending, status='failed', error_code='RETRIEVAL_NOT_READY')
    store.update(OWNER, run, lambda m: m.update(evidence_bundle_operation=operation))
    assert store.read(OWNER, run)['evidence_bundle_operation']['status'] == 'failed'


def test_chunk_index_rejects_invalid_usage_and_identity(tmp_path):
    store, storage, run, plan, snapshot_id = _stable_manifest(tmp_path)
    pending = _chunk_op(plan, snapshot_id)
    store.update(OWNER, run, lambda m: m.update(chunk_index_operation=pending))
    with pytest.raises(SearchIntegrityError):
        store.update(OWNER, run, lambda m: m.update(
            chunk_index_operation={**pending, 'status': 'embedding',
                                   'chunk_manifest_hash': '1' * 64,
                                   'usage': {'not-a-snapshot': True}}))
    with pytest.raises(SearchIntegrityError):
        store.update(OWNER, run, lambda m: m.update(
            chunk_index_operation={**pending, 'model': 'other/model'}))

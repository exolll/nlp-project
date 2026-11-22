"""Offline aggregate tests: real local CAS and explicit unknown-write faults."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from threading import Barrier, Lock, local
from uuid import uuid4

import pytest

from app.persistence.run_manifest import RunManifestStore
from app.schemas.search import SearchIntegrityError, SearchStateError
from app.services.search_contracts import SNAPSHOT_KEY_ALGORITHM, snapshot_key
from app.services.storage import CASConflict, LocalStorage, StorageError

OWNER = 'a' * 64
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def uid():
    return str(uuid4())


def operation(repo=1):
    op_id = uid()
    key = snapshot_key(repo, 'b' * 40, 'README.md', 'c' * 40)
    return dict(operation_id=op_id, repository_id=repo, commit_sha='b' * 40,
                readme_path='README.md', blob_sha='c' * 40, content_hash='d' * 64,
                snapshot_key=key, storage_key=f'repository-snapshots/{key}/readme.raw',
                operation_status='prepared', snapshot_id=uid(),
                staging_key=f'staging/readmes/{op_id}.raw', byte_length=12,
                created_at=NOW.isoformat(), updated_at=NOW.isoformat(),
                collected_at=NOW.isoformat(), snapshot_key_algorithm=SNAPSHOT_KEY_ALGORITHM)


@pytest.fixture
def env(tmp_path):
    storage = LocalStorage(tmp_path)
    store = RunManifestStore(storage)
    run, task, revision = uid(), uid(), uid()
    store.create(OWNER, run, task, revision)
    return store, storage, run, task, revision


def plan(env):
    p = uid()
    return dict(plan_id=p, plan_storage_key=f'search-plans/{p}.json',
                plan_hash='e' * 64, confirmed_revision_id=env[4])


def registered(env, repo=1):
    store, _, run, _, _ = env
    store.start(OWNER, run)
    op = operation(repo)
    store.register_operation(OWNER, run, op)
    return op


def advance(store, run, op, stages=('staged', 'object_verified', 'snapshot_committed', 'completed')):
    for stage in stages:
        result = store.advance_operation(OWNER, run, op['operation_id'], stage, uid())
    return result


def test_create_scoping_and_idempotence(env):
    store, storage, run, task, revision = env
    initial = store.read(OWNER, run)
    assert set(initial) == {'owner', 'run_id', 'task_id', 'confirmed_revision_id',
                            'run_status', 'plan_publication_status', 'plan', 'candidates',
                            'readme_persistence_operations', 'requests', 'dispatch_closed',
                            'integrity_blocked'}
    store.start(OWNER, run)
    assert store.create(OWNER, run, task, revision)['run_status'] == 'running'
    with pytest.raises(SearchIntegrityError):
        store.create(OWNER, run, uid(), revision)
    with pytest.raises(SearchStateError):
        store.read('f' * 64, run)
    for owner, identifier in [('../x', run), ('A' * 64, run), (OWNER, '../x'),
                               (OWNER, run.replace('-', '')), (OWNER, run.upper())]:
        with pytest.raises(SearchIntegrityError):
            store.read(owner, identifier)
    assert storage.get(f'runs/{OWNER}/{run}.json')


@pytest.mark.parametrize('field,value', [
    ('owner', 'f' * 64), ('run_id', str(uuid4())), ('task_id', 'not-a-uuid'),
    ('confirmed_revision_id', None), ('run_status', 'unknown'), ('candidates', []),
    ('dispatch_closed', 1), ('requests', []), ('plan_publication_status', 'ready'),
])
def test_read_checks_structure_and_identity(env, field, value):
    store, storage, run, _, _ = env
    key = f'runs/{OWNER}/{run}.json'
    m = store.read(OWNER, run)
    m[field] = value
    _, version = storage.read_version(key)
    storage.compare_and_swap(key, version, json.dumps(m).encode())
    with pytest.raises(SearchIntegrityError):
        store.read(OWNER, run)


def test_plan_binding_publication_and_finish(env):
    store, _, run, _, _ = env
    ref = plan(env)
    store.bind_plan(OWNER, run, ref)
    assert store.bind_plan(OWNER, run, ref)['plan'] == ref
    with pytest.raises(SearchIntegrityError):
        store.bind_plan(OWNER, run, plan(env))
    wrong = {**ref, 'confirmed_revision_id': uid()}
    with pytest.raises(SearchIntegrityError):
        store.publish_plan(OWNER, run, wrong)
    store.publish_plan(OWNER, run, ref)
    store.publish_plan(OWNER, run, ref)
    store.start(OWNER, run)
    with pytest.raises(SearchStateError):
        store.finish(OWNER, run)
    store.seal_dispatch(OWNER, run)
    assert store.finish(OWNER, run)['run_status'] == 'completed'
    assert store.fail(OWNER, run, 'LATE_ERROR')['run_status'] == 'completed'
    with pytest.raises(SearchStateError):
        store.start(OWNER, run)


@pytest.mark.parametrize('close', ['seal_dispatch', 'fail'])
def test_closed_run_rejects_new_plan(env, close):
    store, _, run, _, _ = env
    getattr(store, close)(OWNER, run, *(['FAILED'] if close == 'fail' else []))
    with pytest.raises(SearchStateError):
        store.bind_plan(OWNER, run, plan(env))


@pytest.mark.parametrize('field,value', [
    ('snapshot_key_algorithm', 'v2'), ('snapshot_key', 'f' * 64),
    ('storage_key', 'arbitrary/key'), ('staging_key', '../x'),
    ('byte_length', True), ('content_hash', 'X' * 64), ('snapshot_id', 'x'),
    ('repository_id', 1.0), ('updated_at', '2026-01-01'),
])
def test_registration_validates_all_facts(env, field, value):
    store, _, run, _, _ = env
    store.start(OWNER, run)
    op = operation()
    op[field] = value
    with pytest.raises(SearchIntegrityError):
        store.register_operation(OWNER, run, op)


def test_registration_required_fields(env):
    store, _, run, _, _ = env
    store.start(OWNER, run)
    original = operation()
    for field in original:
        missing = deepcopy(original)
        del missing[field]
        with pytest.raises(SearchIntegrityError):
            store.register_operation(OWNER, run, missing)


def test_dedup_identity_and_sealed_replays(env):
    store, _, run, _, _ = env
    op = registered(env)
    duplicate = operation()
    result = store.register_operation(OWNER, run, duplicate)
    assert list(result['readme_persistence_operations']) == [op['operation_id']]
    for bad in [{**op, 'content_hash': 'f' * 64}, {**duplicate, 'content_hash': 'f' * 64}]:
        with pytest.raises(SearchIntegrityError):
            store.register_operation(OWNER, run, bad)
    store.seal_dispatch(OWNER, run)
    store.register_operation(OWNER, run, op)
    store.register_operation(OWNER, run, duplicate)
    with pytest.raises(SearchStateError):
        store.register_operation(OWNER, run, operation(2))


def test_stages_reuse_snapshot_atomic_candidate_and_replay(env):
    store, storage, run, _, _ = env
    op = registered(env)
    store.update(OWNER, run, lambda m: m['candidates'].update({'1': {'sources': ['query-A']}}))
    with pytest.raises(SearchStateError):
        store.advance_operation(OWNER, run, op['operation_id'], 'object_verified', uid())
    advance(store, run, op, ('staged', 'object_verified'))
    actual = uid()
    transition = uid()
    store.advance_operation(OWNER, run, op['operation_id'], 'snapshot_committed', transition,
                            snapshot_id=actual)
    result = advance(store, run, op, ('completed',))
    assert result['candidates']['1'] == dict(sources=['query-A'], snapshot_id=actual,
                                            operation_id=op['operation_id'], collection_status='completed')
    raw = storage.get(f'runs/{OWNER}/{run}.json')
    store.advance_operation(OWNER, run, op['operation_id'], 'snapshot_committed', transition,
                            snapshot_id=actual)
    store.advance_operation(OWNER, run, op['operation_id'], 'staged', uid())
    assert storage.get(f'runs/{OWNER}/{run}.json') == raw
    with pytest.raises(SearchIntegrityError):
        store.advance_operation(OWNER, run, op['operation_id'], 'snapshot_committed', transition,
                                snapshot_id=uid())
    with pytest.raises(SearchIntegrityError):
        store.advance_operation(OWNER, run, op['operation_id'], 'completed', uid(), content_hash='f' * 64)
    store.register_operation(OWNER, run, op)  # preallocated ID may differ after SQL reuse


@pytest.mark.parametrize('error_class,status,blocked', [
    ('transient', 'prepared', False), ('outcome_unknown', 'prepared', False),
    ('terminal', 'interrupted', False), ('integrity', 'integrity_failed', True),
])
def test_error_classification(env, error_class, status, blocked):
    store, _, run, _, _ = env
    op = registered(env)
    result = store.record_error(OWNER, run, op['operation_id'], 'MISSING_BYTES', error_class)
    assert result['readme_persistence_operations'][op['operation_id']]['operation_status'] == status
    assert result['integrity_blocked'] is blocked
    if status != 'prepared':
        with pytest.raises(SearchStateError):
            advance(store, run, op, ('staged',))


def test_completed_facts_survive_late_integrity_error(env):
    store, _, run, _, _ = env
    op = registered(env)
    advance(store, run, op)
    ref = plan(env)
    store.bind_plan(OWNER, run, ref)
    store.publish_plan(OWNER, run, ref)
    store.seal_dispatch(OWNER, run)
    store.finish(OWNER, run)
    result = store.record_error(OWNER, run, op['operation_id'], 'HASH_MISMATCH', 'integrity')
    assert result['run_status'] == 'completed'
    assert result['readme_persistence_operations'][op['operation_id']]['operation_status'] == 'completed'
    assert result['integrity_blocked']


def test_failed_run_can_reconcile_but_never_restart(env):
    store, _, run, _, _ = env
    op = registered(env)
    store.fail(OWNER, run, 'RUN_INTERRUPTED')
    result = advance(store, run, op)
    assert result['run_status'] == 'failed'
    with pytest.raises(SearchStateError):
        store.register_operation(OWNER, run, operation(2))
    with pytest.raises(SearchStateError):
        store.finish(OWNER, run)


def test_request_reservation_utc_unique_and_running_gate(env):
    store, _, run, _, _ = env
    request = uid()
    with pytest.raises(SearchStateError):
        store.reserve_request(OWNER, run, request, NOW)
    store.start(OWNER, run)
    result = store.reserve_request(OWNER, run, request, NOW.astimezone(timezone(timedelta(hours=8))))
    assert result['requests'][request] == NOW.isoformat()
    store.fail(OWNER, run, 'STOPPED')
    store.reserve_request(OWNER, run, request, NOW)
    with pytest.raises(SearchIntegrityError):
        store.reserve_request(OWNER, run, request, NOW + timedelta(microseconds=1))
    with pytest.raises(SearchStateError):
        store.reserve_request(OWNER, run, uid(), NOW)
    with pytest.raises(SearchIntegrityError):
        store.reserve_request(OWNER, run, uid(), NOW.replace(tzinfo=None))


@pytest.mark.parametrize('field,value', [('task_id', str(uuid4())),
                                        ('confirmed_revision_id', str(uuid4())),
                                        ('run_status', 'queued'), ('dispatch_closed', False)])
def test_generic_update_cannot_bypass_invariants(env, field, value):
    store, _, run, _, _ = env
    store.start(OWNER, run)
    store.seal_dispatch(OWNER, run)
    with pytest.raises((SearchIntegrityError, SearchStateError)):
        store.update(OWNER, run, lambda m: m.update({field: value}))


def test_generic_update_cannot_change_operation_identity_or_jump(env):
    store, _, run, _, _ = env
    op = registered(env)
    def mutate(field, value):
        return lambda m: m['readme_persistence_operations'][op['operation_id']].update({field: value})
    for field, value in [('content_hash', 'f' * 64), ('snapshot_id', uid()),
                         ('operation_status', 'completed'), ('operation_status', 'nonsense')]:
        with pytest.raises((SearchIntegrityError, SearchStateError)):
            store.update(OWNER, run, mutate(field, value))
    with pytest.raises(SearchIntegrityError):
        store.update(OWNER, run, lambda m: m.update(extra=float('nan')))


def test_generic_plan_reference_and_publication_are_monotonic(env):
    store, _, run, _, _ = env
    ref = plan(env)
    store.bind_plan(OWNER, run, ref)
    store.publish_plan(OWNER, run, ref)
    for change in ({'plan': plan(env)}, {'plan': None},
                   {'plan_publication_status': 'preparing'}):
        with pytest.raises((SearchIntegrityError, SearchStateError)):
            store.update(OWNER, run, lambda m: m.update(change))


@pytest.mark.parametrize('terminal', ['terminal', 'integrity'])
def test_coordinator_refuses_failed_operations(env, terminal):
    store, _, run, _, _ = env
    op = registered(env)
    ref = plan(env)
    store.bind_plan(OWNER, run, ref)
    store.publish_plan(OWNER, run, ref)
    store.seal_dispatch(OWNER, run)
    store.record_error(OWNER, run, op['operation_id'], 'UNAVAILABLE', terminal)
    with pytest.raises(SearchStateError):
        store.finish(OWNER, run)
    with pytest.raises((SearchIntegrityError, SearchStateError)):
        store.update(OWNER, run, lambda m: m.update(run_status='completed'))


def test_completed_candidate_cannot_be_erased_by_generic_update(env):
    store, _, run, _, _ = env
    op = registered(env)
    advance(store, run, op)
    with pytest.raises(SearchIntegrityError):
        store.update(OWNER, run, lambda m: m['candidates'].clear())
    with pytest.raises(SearchIntegrityError):
        store.register_operation(OWNER, run, {**op, 'snapshot_id': uid()})


def test_read_revalidates_operation_and_duplicate_json_keys(env):
    store, storage, run, _, _ = env
    op = registered(env)
    key = f'runs/{OWNER}/{run}.json'
    original, version = storage.read_version(key)
    m = json.loads(original)
    m['readme_persistence_operations'][op['operation_id']]['readme_path'] = 'OTHER.md'
    storage.compare_and_swap(key, version, json.dumps(m).encode())
    with pytest.raises(SearchIntegrityError):
        store.read(OWNER, run)
    _, version = storage.read_version(key)
    storage.compare_and_swap(key, version, original[:-1] + b',"plan":null}')
    with pytest.raises(SearchIntegrityError):
        store.read(OWNER, run)


def test_conflict_reloads_and_merges_only_callback_fields(env):
    _, storage, run, _, _ = env
    calls = []
    class RacingStorage:
        raced = False

        def read_version(self, key):
            return storage.read_version(key)

        def compare_and_swap(self, key, version, payload):
            if not self.raced:
                self.raced = True
                raw, current = storage.read_version(key)
                m = json.loads(raw)
                m['candidates']['2'] = {'sources': ['concurrent']}
                storage.compare_and_swap(key, current, json.dumps(m).encode())
            storage.compare_and_swap(key, version, payload)
    def apply(m):
        calls.append(1)
        m['candidates']['1'] = {'sources': ['mine']}
    result = RunManifestStore(RacingStorage()).update(OWNER, run, apply)
    assert len(calls) == 2
    assert result['candidates'] == {'1': {'sources': ['mine']}, '2': {'sources': ['concurrent']}}


class FaultStorage:
    def __init__(self, inner, mode):
        self.inner, self.mode, self.writes = inner, mode, 0

    def read_version(self, key):
        if self.mode == 'read_failure' and self.writes:
            raise StorageError('Read unknown')
        return self.inner.read_version(key)

    def compare_and_swap(self, key, version, payload):
        self.writes += 1
        if self.mode == 'conflict':
            raise CASConflict('Explicit conflict')
        if self.mode in ('committed', 'different', 'read_failure'):
            self.inner.compare_and_swap(key, version, payload)
        if self.mode == 'different':
            raw, version = self.inner.read_version(key)
            m = json.loads(raw)
            m['concurrent_note'] = True
            self.inner.compare_and_swap(key, version, json.dumps(m).encode())
        raise StorageError('Write outcome unknown')


@pytest.mark.parametrize('mode', ['committed', 'different', 'not_committed', 'read_failure'])
def test_unknown_update_never_replays_callback(env, mode):
    _, storage, run, _, _ = env
    fault = FaultStorage(storage, mode)
    store = RunManifestStore(fault)
    calls = []
    def change(m):
        calls.append(1)
        m['run_status'] = 'running'
    if mode == 'committed':
        assert store.update(OWNER, run, change)['run_status'] == 'running'
    else:
        with pytest.raises(StorageError):
            store.update(OWNER, run, change)
    assert len(calls) == fault.writes == 1


@pytest.mark.parametrize('mode', ['committed', 'different', 'not_committed', 'read_failure'])
def test_unknown_create_readback_only(tmp_path, mode):
    fault = FaultStorage(LocalStorage(tmp_path), mode)
    store = RunManifestStore(fault)
    if mode == 'committed':
        assert store.create(OWNER, uid(), uid(), uid())['run_status'] == 'queued'
    else:
        with pytest.raises(StorageError):
            store.create(OWNER, uid(), uid(), uid())
    assert fault.writes == 1


def test_conflict_budget_is_bounded(env):
    _, storage, run, _, _ = env
    fault = FaultStorage(storage, 'conflict')
    store = RunManifestStore(fault)
    calls = []
    def change(m):
        calls.append(1)
        m['run_status'] = 'running'
    with pytest.raises(StorageError):
        store.update(OWNER, run, change)
    assert len(calls) == fault.writes == 64
    with pytest.raises(StorageError):
        store.create(OWNER, uid(), uid(), uid())
    assert fault.writes == 128


def test_local_storage_threads_merge_independent_completions(env):
    store, storage, run, _, _ = env
    operations = [registered(env, repo) for repo in range(1, 9)]
    for op in operations:
        advance(store, run, op, ('staged', 'object_verified', 'snapshot_committed'))
    barrier, state, lock = Barrier(len(operations)), local(), Lock()
    conflicts = []
    class SimultaneousStorage:
        def read_version(self, key):
            value = storage.read_version(key)
            if not getattr(state, 'read', False):
                state.read = True
                barrier.wait(timeout=15)
            return value

        def compare_and_swap(self, key, version, payload):
            try:
                storage.compare_and_swap(key, version, payload)
            except CASConflict:
                with lock:
                    conflicts.append(1)
                raise
    concurrent = RunManifestStore(SimultaneousStorage())
    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        results = list(pool.map(lambda op: advance(concurrent, run, op, ('completed',)), operations))
    assert len(results) == 8 and conflicts
    final = store.read(OWNER, run)
    assert len(final['candidates']) == 8
    for op in operations:
        assert final['readme_persistence_operations'][op['operation_id']]['operation_status'] == 'completed'
        assert final['candidates'][str(op['repository_id'])]['snapshot_id'] == op['snapshot_id']

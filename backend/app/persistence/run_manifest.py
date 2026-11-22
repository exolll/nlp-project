"""Callers authorize owners; pure transitions replay only after explicit CAS conflicts."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
from uuid import UUID

from app.schemas.search import SearchIntegrityError, SearchStateError
from app.services.search_contracts import SNAPSHOT_KEY_ALGORITHM, snapshot_key
from app.services.storage import CASConflict, StorageError

_STAGES = ('prepared', 'staged', 'object_verified', 'snapshot_committed', 'completed')
_TERMINALS = ('interrupted', 'integrity_failed')
_IDENTITIES = ('owner', 'run_id', 'task_id', 'confirmed_revision_id')
_OP_IDENTITY = ('operation_id', 'repository_id', 'commit_sha', 'readme_path',
                'blob_sha', 'content_hash', 'snapshot_key', 'storage_key',
                'staging_key', 'byte_length', 'created_at', 'collected_at',
                'snapshot_key_algorithm')
_ERRORS = ('integrity', 'terminal', 'transient', 'outcome_unknown')


def _require(condition, message, error=SearchIntegrityError):
    if not condition:
        raise error(message)


def _uuid(value):
    try:
        _require(type(value) is str and str(UUID(value)) == value, 'Noncanonical UUID')
    except (ValueError, TypeError, AttributeError) as exc:
        raise SearchIntegrityError('Noncanonical UUID') from exc


def _hex(value, length=64):
    _require(type(value) is str and re.fullmatch(r'[0-9a-f]{%d}' % length, value),
             'Invalid lowercase hexadecimal identity')


def _time(value):
    _require(type(value) is str, 'Timestamp must be an ISO string')
    try:
        dt = datetime.fromisoformat(value)
        _require(dt.utcoffset() is not None and dt.utcoffset().total_seconds() == 0,
                 'Timestamp must be UTC')
        return dt
    except (ValueError, TypeError) as exc:
        raise SearchIntegrityError('Invalid UTC timestamp') from exc


def _encode(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError) as exc:
        raise SearchIntegrityError('Manifest is not strict JSON') from exc


def _pairs(items):
    result = {}
    for key, value in items:
        _require(key not in result, 'Duplicate JSON key')
        result[key] = value
    return result


def _plan(ref, revision):
    _require(type(ref) is dict and set(ref) == {
        'plan_id', 'plan_storage_key', 'plan_hash', 'confirmed_revision_id'},
        'Invalid plan reference')
    _uuid(ref['plan_id'])
    _hex(ref['plan_hash'])
    _require(ref['confirmed_revision_id'] == revision, 'Plan revision mismatch')
    _require(ref['plan_storage_key'] == f"search-plans/{ref['plan_id']}.json",
             'Invalid plan object key')


def _operation(op):
    _require(type(op) is dict and set(_OP_IDENTITY).union({
        'snapshot_id', 'operation_status', 'updated_at'}).issubset(op),
        'Incomplete operation')
    _uuid(op['operation_id'])
    _uuid(op['snapshot_id'])
    for field in ('content_hash', 'snapshot_key'):
        _hex(op[field])
    for field in ('commit_sha', 'blob_sha'):
        _hex(op[field], 40)
    _require(op['snapshot_key_algorithm'] == SNAPSHOT_KEY_ALGORITHM,
             'Unsupported snapshot algorithm')
    try:
        expected = snapshot_key(op['repository_id'], op['commit_sha'],
                                op['readme_path'], op['blob_sha'])
    except ValueError as exc:
        raise SearchIntegrityError('Invalid snapshot identity') from exc
    _require(op['snapshot_key'] == expected, 'Snapshot identity hash mismatch')
    _require(op['storage_key'] == f'repository-snapshots/{expected}/readme.raw',
             'Invalid snapshot object key')
    _require(op['staging_key'] == f"staging/readmes/{op['operation_id']}.raw",
             'Invalid staging object key')
    _require(type(op['byte_length']) is int and op['byte_length'] >= 0,
             'Invalid byte length')
    for field in ('created_at', 'updated_at', 'collected_at'):
        _time(op[field])
    _require(op['operation_status'] in _STAGES + _TERMINALS, 'Invalid operation status')
    history = op.get('transitions', {})
    _require(type(history) is dict, 'Invalid transition history')
    ids = set()
    for stage, fact in history.items():
        _require(stage in _STAGES[1:] and type(fact) is dict and
                 set(fact) == {'transition_id', 'snapshot_id'}, 'Invalid transition record')
        _require(type(fact['transition_id']) is str and bool(fact['transition_id']),
                 'Missing transition ID')
        _require(fact['transition_id'] not in ids, 'Reused transition ID')
        ids.add(fact['transition_id'])
        _uuid(fact['snapshot_id'])
    rank = (_STAGES.index(op['operation_status']) if op['operation_status'] in _STAGES
            else len(history))
    _require(set(history) == set(_STAGES[1:rank + 1]), 'Incomplete transition history')
    _require(op['operation_status'] not in _TERMINALS or rank < 4,
             'Completed facts cannot become a failure terminal')
    if history:
        _require(op.get('transition_id') == history[_STAGES[rank]]['transition_id'],
                 'Latest transition ID mismatch')
    else:
        _require(op.get('transition_id') is None, 'Prepared operation has a transition ID')
    if 'object_verified' in history:
        _require(history['staged']['snapshot_id'] == history['object_verified']['snapshot_id'],
                 'Pre-commit snapshot identity changed')
    if 'snapshot_committed' not in history and history:
        _require(history['staged']['snapshot_id'] == op['snapshot_id'],
                 'Pre-commit snapshot identity mismatch')
    if 'snapshot_committed' in history:
        _require(history['snapshot_committed']['snapshot_id'] == op['snapshot_id'],
                 'Committed snapshot identity mismatch')
    if 'completed' in history:
        _require(history['completed']['snapshot_id'] == op['snapshot_id'],
                 'Completed snapshot identity mismatch')


def _validate(m, owner, run_id):
    _require(type(m) is dict and set(_IDENTITIES).union({
        'run_status', 'plan_publication_status', 'plan', 'candidates',
        'readme_persistence_operations', 'requests', 'dispatch_closed',
        'integrity_blocked'}).issubset(m), 'Incomplete manifest')
    _hex(m['owner'])
    for field in _IDENTITIES[1:]:
        _uuid(m[field])
    _require(m['owner'] == owner and m['run_id'] == run_id, 'Manifest ownership mismatch')
    _require(m['run_status'] in ('queued', 'running', 'failed', 'completed'), 'Invalid run status')
    _require(m['plan_publication_status'] in ('preparing', 'published', 'failed'),
             'Invalid publication status')
    for field in ('dispatch_closed', 'integrity_blocked'):
        _require(type(m[field]) is bool, 'Invalid manifest flag')
    for field in ('candidates', 'readme_persistence_operations', 'requests'):
        _require(type(m[field]) is dict, 'Invalid manifest mapping')
    if m['plan'] is not None:
        _plan(m['plan'], m['confirmed_revision_id'])
    _require(m['plan_publication_status'] != 'published' or m['plan'] is not None,
             'Published plan is missing')
    seen = set()
    for key, op in m['readme_persistence_operations'].items():
        _operation(op)
        _require(key == op['operation_id'], 'Operation key mismatch')
        _require(op['snapshot_key'] not in seen, 'Duplicate snapshot operation')
        seen.add(op['snapshot_key'])
        _require(op['operation_status'] != 'integrity_failed' or m['integrity_blocked'],
                 'Integrity failure must block the run')
    for key, issued_at in m['requests'].items():
        _uuid(key)
        _time(issued_at)
    for key, candidate in m['candidates'].items():
        _require(type(key) is str and re.fullmatch(r'[1-9][0-9]*', key) and
                 int(key) <= 2**64 - 1 and type(candidate) is dict, 'Invalid candidate')
        if candidate.get('collection_status') == 'completed':
            op = m['readme_persistence_operations'].get(candidate.get('operation_id'))
            _require(op is not None and op['operation_status'] == 'completed' and
                     str(op['repository_id']) == key and
                     op['snapshot_id'] == candidate.get('snapshot_id'), 'Invalid collected reference')
    for op in m['readme_persistence_operations'].values():
        if op['operation_status'] == 'completed':
            candidate = m['candidates'].get(str(op['repository_id']), {})
            _require(candidate.get('collection_status') == 'completed',
                     'Completed operation has no collected candidate')
    if m['run_status'] == 'completed':
        _require(m['dispatch_closed'] and m['plan_publication_status'] == 'published' and
                 all(op['operation_status'] == 'completed'
                     for op in m['readme_persistence_operations'].values()), 'Premature run completion')
    _encode(m)


def _invariants(old, new):
    for field in _IDENTITIES:
        _require(old[field] == new[field], 'Immutable manifest identity changed')
    if old['plan'] is not None:
        _require(old['plan'] == new['plan'], 'Immutable plan reference changed')
    elif new['plan'] is not None:
        _require(old['run_status'] in ('queued', 'running') and not old['dispatch_closed']
                 and old['plan_publication_status'] == 'preparing',
                 'Run cannot bind a new plan', SearchStateError)
    for field, allowed in (
        ('run_status', {'queued': ('running', 'failed'), 'running': ('completed', 'failed')}),
        ('plan_publication_status', {'preparing': ('published', 'failed')}),
    ):
        _require(new[field] == old[field] or new[field] in allowed.get(old[field], ()),
                 'Illegal state transition', SearchStateError)
    if old['plan_publication_status'] != 'published' and new['plan_publication_status'] == 'published':
        _require(not new['integrity_blocked'], 'Integrity blocks publication', SearchStateError)
    for field in ('dispatch_closed', 'integrity_blocked'):
        _require(not old[field] or new[field], 'Monotonic flag cleared', SearchStateError)
    if old['run_status'] != 'completed' and new['run_status'] == 'completed':
        _require(not new['integrity_blocked'], 'Integrity blocks completion', SearchStateError)
    for key, value in old['requests'].items():
        _require(new['requests'].get(key) == value, 'Request reservation changed')
    if new['requests'].keys() - old['requests'].keys():
        _require(old['run_status'] == 'running', 'Run is not running', SearchStateError)
    before, after = old['readme_persistence_operations'], new['readme_persistence_operations']
    _require(before.keys() <= after.keys(), 'Operation removed')
    for key, op in after.items():
        if key not in before:
            _require(old['run_status'] == 'running' and not old['dispatch_closed'],
                     'Dispatch is closed', SearchStateError)
            _require(op['operation_status'] == 'prepared', 'New operation must be prepared')
            continue
        previous = before[key]
        for field in _OP_IDENTITY:
            _require(op[field] == previous[field], 'Immutable operation identity changed')
        a, b = previous['operation_status'], op['operation_status']
        if a != b:
            allowed = () if a in _TERMINALS or a == 'completed' else (
                _STAGES[_STAGES.index(a) + 1], *_TERMINALS)
            _require(b in allowed, 'Illegal operation transition', SearchStateError)
        if op['snapshot_id'] != previous['snapshot_id']:
            _require(a == 'object_verified' and b == 'snapshot_committed',
                     'Snapshot ID can change only at SQL commit')
        history, updated = previous.get('transitions', {}), op.get('transitions', {})
        _require(all(updated.get(k) == v for k, v in history.items()), 'Transition history changed')
        if a == b or b in _TERMINALS:
            _require(history == updated, 'Transition history changed without advancement')
        else:
            _require(set(updated) - set(history) == {b} and
                     updated[b]['snapshot_id'] == op['snapshot_id'], 'Invalid advancement facts')
        if b == 'completed' and a != b:
            candidate = new['candidates'].get(str(op['repository_id']), {})
            _require(candidate.get('snapshot_id') == op['snapshot_id'] and
                     candidate.get('operation_id') == key and
                     candidate.get('collection_status') == 'completed', 'Completion is not atomic')
    for key, previous in old['candidates'].items():
        if previous.get('collection_status') != 'completed':
            continue
        candidate = new['candidates'].get(key, {})
        fields = ('snapshot_id', 'operation_id', 'collection_status')
        if any(candidate.get(k) != previous.get(k) for k in fields):
            replacement = candidate.get('operation_id')
            _require(replacement in before and replacement in after and
                     before[replacement]['operation_status'] == 'snapshot_committed' and
                     after[replacement]['operation_status'] == 'completed' and
                     str(after[replacement]['repository_id']) == key,
                     'Completed candidate reference changed without new completion')


class RunManifestStore:
    def __init__(self, storage):
        self.storage = storage

    @staticmethod
    def _key(owner, run_id):
        _hex(owner)
        _uuid(run_id)
        return f'runs/{owner}/{run_id}.json'

    def _load(self, owner, run_id):
        raw, version = self.storage.read_version(self._key(owner, run_id))
        if raw is None:
            return None, raw, version
        try:
            value = json.loads(raw, object_pairs_hook=_pairs,
                               parse_constant=lambda _: (_ for _ in ()).throw(
                                   SearchIntegrityError('Nonfinite JSON number')))
        except (ValueError, TypeError, UnicodeError) as exc:
            raise SearchIntegrityError('Corrupt manifest JSON') from exc
        _validate(value, owner, run_id)
        return value, raw, version

    def _write(self, key, version, payload):
        try:
            self.storage.compare_and_swap(key, version, payload)
        except CASConflict:
            raise
        except StorageError as exc:
            # Only exact expected bytes establish success. A later concurrent
            # write is not proof that this transition committed.
            try:
                actual, _ = self.storage.read_version(key)
            except StorageError:
                raise exc
            if actual != payload:
                raise exc

    def create(self, owner: str, run_id: str, task_id: str, confirmed_revision_id: str) -> dict:
        key = self._key(owner, run_id)
        _uuid(task_id)
        _uuid(confirmed_revision_id)
        initial = dict(owner=owner, run_id=run_id, task_id=task_id,
                       confirmed_revision_id=confirmed_revision_id, run_status='queued',
                       plan_publication_status='preparing', plan=None, candidates={},
                       readme_persistence_operations={}, requests={}, dispatch_closed=False,
                       integrity_blocked=False)
        for _ in range(64):
            existing, _, version = self._load(owner, run_id)
            if existing is not None:
                _require(all(existing[k] == initial[k] for k in _IDENTITIES),
                         'Run already belongs to a different identity')
                return existing
            try:
                self._write(key, version, _encode(initial))
                return initial
            except CASConflict:
                continue
        raise StorageError('Manifest CAS retry budget exhausted')

    def read(self, owner, run_id) -> dict:
        value, _, _ = self._load(owner, run_id)
        _require(value is not None, 'Run does not exist', SearchStateError)
        return value

    def update(self, owner, run_id, transition) -> dict:
        key = self._key(owner, run_id)
        for _ in range(64):
            value, raw, version = self._load(owner, run_id)
            _require(value is not None, 'Run does not exist', SearchStateError)
            before = deepcopy(value)
            transition(value)
            _validate(value, owner, run_id)
            _invariants(before, value)
            payload = _encode(value)
            if payload == raw:
                return value
            try:
                self._write(key, version, payload)
                return value
            except CASConflict:
                continue
        raise StorageError('Manifest CAS retry budget exhausted')

    def bind_plan(self, owner, run_id, plan_ref: dict):
        ref = deepcopy(plan_ref)
        def apply(m):
            _plan(ref, m['confirmed_revision_id'])
            _require(m['plan'] is None or m['plan'] == ref, 'Plan binding conflict')
            m['plan'] = deepcopy(ref)
        return self.update(owner, run_id, apply)

    def publish_plan(self, owner, run_id, plan_ref):
        ref = deepcopy(plan_ref)
        def apply(m):
            _plan(ref, m['confirmed_revision_id'])
            _require(m['plan'] == ref, 'Published reference differs from bound plan')
            _require(m['plan_publication_status'] in ('preparing', 'published'),
                     'Plan publication failed', SearchStateError)
            _require(not m['integrity_blocked'], 'Integrity blocks publication', SearchStateError)
            m['plan_publication_status'] = 'published'
        return self.update(owner, run_id, apply)

    def start(self, owner, run_id):
        def apply(m):
            _require(m['run_status'] in ('queued', 'running'), 'Run is terminal', SearchStateError)
            m['run_status'] = 'running'
        return self.update(owner, run_id, apply)

    def fail(self, owner, run_id, code, error_class='terminal'):
        self._error_fields(code, error_class, False)
        def apply(m):
            if m['run_status'] in ('queued', 'running'):
                m['run_status'] = 'failed'
            m.update(error_code=code, error_class=error_class)
            if error_class == 'integrity':
                m['integrity_blocked'] = True
        return self.update(owner, run_id, apply)

    def seal_dispatch(self, owner, run_id):
        return self.update(owner, run_id, lambda m: m.update(dispatch_closed=True))

    def finish(self, owner, run_id):
        def apply(m):
            _require(m['run_status'] in ('running', 'completed') and m['dispatch_closed']
                     and m['plan_publication_status'] == 'published' and not m['integrity_blocked']
                     and all(o['operation_status'] == 'completed'
                             for o in m['readme_persistence_operations'].values()),
                     'Run is not ready for completion', SearchStateError)
            m['run_status'] = 'completed'
        return self.update(owner, run_id, apply)

    def register_operation(self, owner, run_id, operation: dict) -> dict:
        incoming = deepcopy(operation)
        _operation(incoming)
        _require(incoming['operation_status'] == 'prepared', 'Expected prepared operation', SearchStateError)
        def apply(m):
            ops = m['readme_persistence_operations']
            prior = ops.get(incoming['operation_id'])
            if prior is not None:
                _require(all(prior[k] == incoming[k] for k in _OP_IDENTITY),
                         'Operation identity conflict')
                allocated = prior.get('transitions', {}).get('staged', {}).get(
                    'snapshot_id', prior['snapshot_id'])
                _require(allocated == incoming['snapshot_id'], 'Preallocated snapshot ID conflict')
                return
            for op in ops.values():
                if op['snapshot_key'] == incoming['snapshot_key']:
                    _require(all(op[k] == incoming[k] for k in (
                        'repository_id', 'commit_sha', 'readme_path', 'blob_sha',
                        'content_hash', 'storage_key', 'byte_length', 'snapshot_key_algorithm')),
                        'Snapshot identity conflict')
                    return
            _require(m['run_status'] == 'running' and not m['dispatch_closed'],
                     'Cannot register new work', SearchStateError)
            ops[incoming['operation_id']] = deepcopy(incoming)
        return self.update(owner, run_id, apply)

    def advance_operation(self, owner, run_id, operation_id, status, transition_id: str, **facts) -> dict:
        _uuid(operation_id)
        _require(status in _STAGES[1:], 'Invalid target stage', SearchStateError)
        _require(type(transition_id) is str and bool(transition_id), 'Missing transition ID')
        _require(set(facts) <= {'snapshot_id'}, 'Only snapshot_id facts are accepted')
        _require(not facts or status == 'snapshot_committed', 'Snapshot ID requires commit stage')
        if facts:
            _uuid(facts['snapshot_id'])
        now = datetime.now(timezone.utc).isoformat()
        def apply(m):
            op = m['readme_persistence_operations'].get(operation_id)
            _require(op is not None, 'Unknown operation', SearchStateError)
            current = op['operation_status']
            _require(current not in _TERMINALS, 'Operation is terminal', SearchStateError)
            history = op.setdefault('transitions', {})
            for stage, record in history.items():
                _require(record['transition_id'] != transition_id or stage == status,
                         'Transition ID belongs to another stage')
            if _STAGES.index(current) >= _STAGES.index(status):
                if facts:
                    _require(history[status]['snapshot_id'] == facts['snapshot_id'],
                             'Replay result differs from committed snapshot')
                return
            _require(_STAGES.index(status) == _STAGES.index(current) + 1,
                     'Operation stages cannot be skipped', SearchStateError)
            op.update(facts)
            op['operation_status'] = status
            op['updated_at'] = now
            history[status] = {'transition_id': transition_id, 'snapshot_id': op['snapshot_id']}
            op['transition_id'] = transition_id
            if status == 'completed':
                candidate = m['candidates'].setdefault(str(op['repository_id']), {})
                candidate.update(snapshot_id=op['snapshot_id'], operation_id=operation_id,
                                 collection_status='completed')
        return self.update(owner, run_id, apply)

    @staticmethod
    def _error_fields(code, error_class, retryable):
        _require(type(code) is str and re.fullmatch(r'[A-Za-z0-9_]{1,128}', code),
                 'Error code must be a safe identifier')
        _require(error_class in _ERRORS and type(retryable) is bool, 'Invalid error classification')

    def record_error(self, owner, run_id, operation_id, code, error_class, retryable=False) -> dict:
        _uuid(operation_id)
        self._error_fields(code, error_class, retryable)
        now = datetime.now(timezone.utc).isoformat()
        def apply(m):
            op = m['readme_persistence_operations'].get(operation_id)
            _require(op is not None, 'Unknown operation', SearchStateError)
            op.update(error_code=code, error_class=error_class, retryable=retryable,
                      last_error_at=now)
            if error_class == 'integrity':
                m['integrity_blocked'] = True
            if op['operation_status'] not in _TERMINALS + ('completed',):
                if error_class == 'integrity':
                    op['operation_status'] = 'integrity_failed'
                elif error_class == 'terminal':
                    op['operation_status'] = 'interrupted'
        return self.update(owner, run_id, apply)

    def reserve_request(self, owner, run_id, request_id: str, issued_at: datetime) -> dict:
        _uuid(request_id)
        _require(isinstance(issued_at, datetime) and issued_at.utcoffset() is not None,
                 'Request time must be timezone-aware')
        stamp = issued_at.astimezone(timezone.utc).isoformat()
        def apply(m):
            if request_id in m['requests']:
                _require(m['requests'][request_id] == stamp, 'Request timestamp conflict')
                return
            _require(m['run_status'] == 'running', 'Run is not running', SearchStateError)
            m['requests'][request_id] = stamp
        return self.update(owner, run_id, apply)

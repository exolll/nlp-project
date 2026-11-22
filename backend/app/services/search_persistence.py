import hashlib
import json
from datetime import datetime, timezone
from threading import RLock
from uuid import UUID, uuid4

from app.persistence.run_manifest import RunManifestStore
from app.persistence.search_repository import RequestClock
from app.schemas.search import SearchIntegrityError, SearchPlanDocument, SearchStateError
from app.services.search_contracts import SNAPSHOT_KEY_ALGORITHM, snapshot_key
from app.services.storage import CASConflict, StorageError


_LOCKS = tuple(RLock() for _ in range(64))
_STAGES = ('prepared', 'staged', 'object_verified', 'snapshot_committed', 'completed')
_MAX_README_BYTES = 1024 * 1024


def _lock(identity):
    return _LOCKS[int.from_bytes(hashlib.sha256(identity.encode('utf-8')).digest()[:4], 'big') % len(_LOCKS)]


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _verify_readme(data, operation):
    if (len(data) != operation['byte_length'] or len(data) > _MAX_README_BYTES
            or _digest(data) != operation['content_hash']
            or hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest() != operation['blob_sha']):
        raise SearchIntegrityError('README bytes do not match the registered identity')


class SearchPersistence:
    def __init__(self, storage, repository):
        self.storage = storage
        self.repository = repository
        self.manifests = RunManifestStore(storage)

    def create_run(self, owner, task_id, revision, run_id):
        if str(revision.task_id) != str(UUID(task_id)) or revision.status != 'confirmed':
            raise SearchStateError('A run requires the authorized confirmed revision')
        return self.manifests.create(owner, run_id, task_id, str(revision.revision_id))

    def _put_immutable(self, key, data, content_type):
        existing, _ = self.storage.read_version(key)
        if existing is not None:
            if existing != data:
                raise SearchIntegrityError('Immutable object content differs')
            return
        try:
            self.storage.put(key, data, content_type)
        except (CASConflict, StorageError):
            actual, _ = self.storage.read_version(key)
            if actual == data:
                return
            if actual is not None:
                raise SearchIntegrityError('Immutable object content differs') from None
            raise
        actual, _ = self.storage.read_version(key)
        if actual is None:
            raise StorageError('Object write could not be confirmed')
        if actual != data:
            raise SearchIntegrityError('Immutable object verification failed')

    def _block(self, owner, run_id, code):
        def mark(manifest):
            manifest['integrity_blocked'] = True
            manifest['error_class'] = 'integrity'
            manifest['error_code'] = code
            manifest['retryable'] = False
        self.manifests.update(owner, run_id, mark)

    def publish_plan(self, owner, task_id, revision, plan):
        document = SearchPlanDocument.model_validate(
            plan.model_dump(mode='json') if isinstance(plan, SearchPlanDocument) else plan,
        )
        document.validate_revision(revision, task_id)
        value = document.model_dump(mode='json')
        data = _encode(value)
        reference = {
            'plan_id': document.plan_id,
            'confirmed_revision_id': document.confirmed_revision_id,
            'plan_storage_key': 'search-plans/' + document.plan_id + '.json',
            'plan_hash': _digest(data),
        }
        with _lock(document.run_id):
            manifest = self.manifests.read(owner, document.run_id)
            if manifest['task_id'] != str(UUID(task_id)):
                raise SearchStateError('Plan belongs to another task')
            if manifest['integrity_blocked']:
                raise SearchIntegrityError('Run is blocked by an integrity failure')
            self.manifests.bind_plan(owner, document.run_id, reference)
            try:
                self._put_immutable(reference['plan_storage_key'], data, 'application/json')
                self.repository.ensure_pending(value, reference['plan_storage_key'], reference['plan_hash'])
                self.repository.publish_plan(value, reference['plan_storage_key'], reference['plan_hash'])
                if not self.repository.plan_is_ready(value, reference['plan_storage_key'], reference['plan_hash']):
                    raise SearchStateError('Plan projection is not ready')
                return self.manifests.publish_plan(owner, document.run_id, reference)
            except SearchIntegrityError:
                self._block(owner, document.run_id, 'PLAN_INTEGRITY')
                raise

    def _plan_original(self, manifest):
        reference = manifest['plan']
        if reference is None:
            raise SearchStateError('No plan has been registered')
        key = 'search-plans/' + str(UUID(reference['plan_id'])) + '.json'
        if reference['plan_storage_key'] != key:
            raise SearchIntegrityError('Invalid plan object identity')
        raw, _ = self.storage.read_version(key)
        if raw is None:
            raise StorageError('Original plan is unavailable; do not regenerate automatically')
        if _digest(raw) != reference['plan_hash']:
            raise SearchIntegrityError('Plan hash verification failed')
        try:
            plan = SearchPlanDocument.model_validate_json(raw)
        except ValueError:
            raise SearchIntegrityError('Stored plan schema is invalid') from None
        if (plan.plan_id != reference['plan_id'] or plan.run_id != manifest['run_id']
                or plan.confirmed_revision_id != manifest['confirmed_revision_id']):
            raise SearchIntegrityError('Plan does not match the bound run')
        return plan

    def recover_plan(self, owner, run_id, revision):
        with _lock(run_id):
            manifest = self.manifests.read(owner, run_id)
            if manifest['integrity_blocked']:
                raise SearchIntegrityError('Run is blocked by an integrity failure')
            try:
                plan = self._plan_original(manifest)
                plan.validate_revision(revision, manifest['task_id'])
                value, reference = plan.model_dump(mode='json'), manifest['plan']
                self.repository.ensure_pending(value, reference['plan_storage_key'], reference['plan_hash'])
                self.repository.publish_plan(value, reference['plan_storage_key'], reference['plan_hash'])
                if not self.repository.plan_is_ready(value, reference['plan_storage_key'], reference['plan_hash']):
                    raise SearchStateError('Plan projection is not ready')
                return self.manifests.publish_plan(owner, run_id, reference)
            except SearchIntegrityError:
                self._block(owner, run_id, 'PLAN_INTEGRITY')
                raise

    def read_plan(self, owner, run_id):
        manifest = self.manifests.read(owner, run_id)
        if manifest['integrity_blocked']:
            raise SearchIntegrityError('Run is blocked by an integrity failure')
        if manifest['plan_publication_status'] != 'published':
            raise SearchStateError('Plan has not been published')
        try:
            plan = self._plan_original(manifest)
            reference = manifest['plan']
            if not self.repository.plan_is_ready(plan.model_dump(mode='json'), reference['plan_storage_key'], reference['plan_hash']):
                raise SearchStateError('Plan projection is not ready')
            return plan
        except SearchIntegrityError:
            self._block(owner, run_id, 'PLAN_INTEGRITY')
            raise

    def initialize_request_clock(self, run_references):
        watermarks = []
        latest = self.repository.latest_fetched_at()
        if latest is not None:
            watermarks.append(latest)
        for owner, run_id in run_references:
            manifest = self.manifests.read(owner, run_id)
            for request in manifest['requests'].values():
                watermarks.append(datetime.fromisoformat(request))
        return RequestClock(watermarks)

    def reserve_request(self, owner, run_id, request_id, clock):
        with _lock(run_id):
            manifest = self.manifests.read(owner, run_id)
            request_id = str(UUID(request_id))
            if request_id in manifest['requests']:
                return datetime.fromisoformat(manifest['requests'][request_id])
            issued_at = clock.issue()
            self.manifests.reserve_request(owner, run_id, request_id, issued_at)
            return issued_at

    def collect_readme(self, owner, run_id, repository_id, commit_sha, readme_path, blob_sha, data):
        if type(data) is not bytes or len(data) > _MAX_README_BYTES:
            raise SearchStateError('README must be bytes within the collection limit')
        identity = snapshot_key(repository_id, commit_sha, readme_path, blob_sha)
        operation_id, timestamp = str(uuid4()), _now()
        operation = {
            'operation_id': operation_id, 'repository_id': int(repository_id),
            'commit_sha': commit_sha.lower(), 'readme_path': readme_path, 'blob_sha': blob_sha.lower(),
            'content_hash': _digest(data), 'snapshot_key': identity,
            'snapshot_key_algorithm': SNAPSHOT_KEY_ALGORITHM,
            'storage_key': 'repository-snapshots/' + identity + '/readme.raw',
            'staging_key': 'staging/readmes/' + operation_id + '.raw',
            'snapshot_id': str(uuid4()), 'byte_length': len(data),
            'operation_status': 'prepared', 'created_at': timestamp,
            'updated_at': timestamp, 'collected_at': timestamp,
        }
        _verify_readme(data, operation)
        with _lock(identity):
            self.read_plan(owner, run_id)
            manifest = self.manifests.register_operation(owner, run_id, operation)
            existing = next(op for op in manifest['readme_persistence_operations'].values() if op['snapshot_key'] == identity)
            return self._resume_readme(owner, run_id, existing['operation_id'], data)

    def resume_readme(self, owner, run_id, operation_id):
        manifest = self.manifests.read(owner, run_id)
        operation = manifest['readme_persistence_operations'].get(operation_id)
        if operation is None:
            raise SearchStateError('README operation does not exist')
        with _lock(operation['snapshot_key']):
            return self._resume_readme(owner, run_id, operation_id)

    def _advance(self, owner, run_id, operation_id, status, **facts):
        return self.manifests.advance_operation(owner, run_id, operation_id, status, str(uuid4()), **facts)

    def _resume_readme(self, owner, run_id, operation_id, supplied=None):
        manifest = self.manifests.read(owner, run_id)
        if manifest['integrity_blocked']:
            raise SearchIntegrityError('Run is blocked by an integrity failure')
        operation = manifest['readme_persistence_operations'][operation_id]
        status = operation['operation_status']
        if status not in _STAGES:
            raise SearchStateError('Terminal README operations cannot be reopened')
        try:
            staged, _ = self.storage.read_version(operation['staging_key'])
            final, _ = self.storage.read_version(operation['storage_key'])
            for data in (supplied, staged, final):
                if data is not None:
                    _verify_readme(data, operation)
            data = final if final is not None else staged if staged is not None else supplied
            if data is None:
                if status in ('snapshot_committed', 'completed'):
                    raise SearchIntegrityError('Persisted README evidence is missing')
                self.manifests.record_error(owner, run_id, operation_id, 'README_BYTES_UNAVAILABLE', 'terminal')
                return None
            if status == 'prepared':
                self._put_immutable(operation['staging_key'], data, 'application/octet-stream')
                self._advance(owner, run_id, operation_id, 'staged')
            self._put_immutable(operation['storage_key'], data, 'application/octet-stream')
            if _STAGES.index(status) < _STAGES.index('object_verified'):
                self._advance(owner, run_id, operation_id, 'object_verified')
            snapshot = self.repository.ensure_snapshot({
                key: operation[key] for key in (
                    'snapshot_id', 'repository_id', 'commit_sha', 'readme_path', 'blob_sha',
                    'content_hash', 'storage_key', 'snapshot_key', 'collected_at',
                )
            })
            if status in ('snapshot_committed', 'completed') and operation['snapshot_id'] != snapshot['snapshot_id']:
                raise SearchIntegrityError('Committed snapshot identity changed')
            if _STAGES.index(status) < _STAGES.index('snapshot_committed'):
                self._advance(owner, run_id, operation_id, 'snapshot_committed', snapshot_id=snapshot['snapshot_id'])
            self._advance(owner, run_id, operation_id, 'completed')
            return snapshot
        except SearchIntegrityError:
            self.manifests.record_error(owner, run_id, operation_id, 'README_INTEGRITY', 'integrity')
            raise
        except StorageError:
            try:
                self.manifests.record_error(owner, run_id, operation_id, 'PERSISTENCE_UNCONFIRMED', 'outcome_unknown')
            except StorageError:
                pass
            raise

    def read_snapshot(self, owner, run_id, snapshot_id):
        self.read_plan(owner, run_id)
        manifest = self.manifests.read(owner, run_id)
        operations = [op for op in manifest['readme_persistence_operations'].values()
                      if op['operation_status'] == 'completed' and op['snapshot_id'] == snapshot_id]
        if not operations:
            raise SearchStateError('Snapshot does not belong to the requested run')
        operation = operations[0]
        try:
            snapshot = self.repository.get_snapshot_by_id(snapshot_id)
            if snapshot is None or any(snapshot[key] != operation[key] for key in (
                    'repository_id', 'snapshot_key', 'content_hash', 'storage_key', 'commit_sha', 'blob_sha', 'readme_path')):
                raise SearchIntegrityError('Snapshot projection differs from its operation')
            raw, _ = self.storage.read_version(operation['storage_key'])
            if raw is None:
                raise SearchIntegrityError('Snapshot original bytes are missing')
            _verify_readme(raw, operation)
            return raw
        except SearchIntegrityError:
            self.manifests.record_error(owner, run_id, operation['operation_id'], 'README_INTEGRITY', 'integrity')
            raise

    def record_usage(self, owner, run_id, snapshot_id, usage_type):
        self.read_snapshot(owner, run_id, snapshot_id)
        manifest = self.manifests.read(owner, run_id)
        snapshot = self.repository.get_snapshot_by_id(snapshot_id)
        return self.repository.record_usage({
            'id': str(uuid4()), 'run_id': run_id, 'plan_id': manifest['plan']['plan_id'],
            'repository_id': snapshot['repository_id'], 'snapshot_id': snapshot_id,
            'usage_type': usage_type, 'used_at': _now(),
        })

    def recover_readmes(self, owner, run_id, max_operations=10):
        if type(max_operations) is not int or not 1 <= max_operations <= 100:
            raise SearchStateError('Recovery batch must contain 1 to 100 operations')
        manifest = self.manifests.read(owner, run_id)
        operations = [op for op in manifest['readme_persistence_operations'].values()
                      if op['operation_status'] in _STAGES[:-1]]
        results = {}
        for operation in operations[:max_operations]:
            results[operation['operation_id']] = self.resume_readme(owner, run_id, operation['operation_id'])
        return results

    def cleanup_staging(self, owner, run_id, operation_id, *, now=None, retention_seconds=86400):
        if type(retention_seconds) is not int or retention_seconds < 86400:
            raise SearchStateError('Staging retention must be at least one day')
        manifest = self.manifests.read(owner, run_id)
        operation = manifest['readme_persistence_operations'][operation_id]
        with _lock(operation['snapshot_key']):
            manifest = self.manifests.read(owner, run_id)
            operation = manifest['readme_persistence_operations'][operation_id]
            if operation['operation_status'] != 'completed' or manifest['integrity_blocked']:
                raise SearchStateError('Unconfirmed staging cannot be removed')
            now = now or datetime.now(timezone.utc)
            if now.tzinfo is None:
                raise SearchStateError('Cleanup time must be timezone-aware')
            if (now - datetime.fromisoformat(operation['updated_at'])).total_seconds() < retention_seconds:
                return False
            self.read_snapshot(owner, run_id, operation['snapshot_id'])
            self.storage.delete(operation['staging_key'])
            return True

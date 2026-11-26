"""Persist retrieval evidence bundles; reads never re-run retrieval or embeddings."""
import json
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID, uuid4, uuid5

from app.persistence.run_manifest import RunManifestStore
from app.schemas.search import SearchIntegrityError, SearchStateError
from app.services.candidate_review import _bound
from app.services.embedding_service import EmbeddingProgress
from app.services.retrieval import retrieve
from app.services.search_persistence import _digest, _encode, _lock

VERSION = 'evidence-bundle-v2'
SCHEMA_VERSION = 2
REPOSITORY_FIELDS = ('full_name', 'html_url', 'stars', 'description', 'language')
TRACE_FIELDS = ('commit_sha', 'blob_sha', 'content_hash', 'collected_at', 'readme_path')


class EvidenceBundleError(ValueError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _repository(manifest, repository_id):
    metadata = (manifest['candidates'].get(str(repository_id)) or {}).get('metadata') or {}
    return {key: metadata.get(key) for key in REPOSITORY_FIELDS}


def snapshot_traces(manifest, repository, report):
    """Read-only trace extraction: every field re-verified against Manifest and MySQL."""
    operations = {op['snapshot_id']: op
                  for op in manifest['readme_persistence_operations'].values()
                  if op['operation_status'] == 'completed'}
    snapshots = []
    for row in report['snapshots']:
        operation = operations.get(row['snapshot_id'])
        if operation is None:
            raise SearchIntegrityError('Chunk source is not a completed README operation')
        snapshot = repository.get_snapshot_by_id(row['snapshot_id'])
        if snapshot is None or any(snapshot.get(key) != operation[key] for key in
                                   ('commit_sha', 'blob_sha', 'content_hash', 'readme_path')):
            raise SearchIntegrityError('Snapshot projection differs from its operation')
        if any(row.get(key) != operation[key] for key in ('commit_sha', 'content_hash', 'readme_path')):
            raise SearchIntegrityError('Chunk source identity differs from the README operation')
        snapshots.append({'repository_id': row['repository_id'], 'snapshot_id': row['snapshot_id'],
                          'coverage': row['coverage'], 'chunks': row['chunks'],
                          'readme_path': operation['readme_path'],
                          'commit_sha': operation['commit_sha'],
                          'blob_sha': operation['blob_sha'],
                          'content_hash': operation['content_hash'],
                          'collected_at': operation['collected_at'],
                          'repository': _repository(manifest, row['repository_id'])})
    return snapshots


class EvidenceBundleStore:
    def __init__(self, storage):
        self.storage = storage
        self.manifests = RunManifestStore(storage)

    def read(self, owner, run_id, revision):
        manifest = self.manifests.read(owner, run_id)
        if (manifest['integrity_blocked'] or manifest['confirmed_revision_id'] != str(revision.revision_id)
                or manifest['task_id'] != str(revision.task_id)):
            raise SearchIntegrityError('Evidence bundle binding differs')
        operation = manifest.get('evidence_bundle_operation')
        if operation is None:
            raise EvidenceBundleError('RETRIEVAL_NOT_READY', '检索证据尚未生成。')
        if operation['status'] == 'failed':
            raise EvidenceBundleError(operation['error_code'] or 'RETRIEVAL_FAILED', '检索证据生成失败。')
        raw, _ = self.storage.read_version(operation['storage_key'])
        if raw is None or _digest(raw) != operation['bundle_hash']:
            raise SearchIntegrityError('Evidence bundle content differs')
        return json.loads(raw), deepcopy(operation)


class EvidenceBundleService:
    def __init__(self, persistence, indexer, embedding_settings):
        self.persistence, self.indexer, self.settings = persistence, indexer, embedding_settings
        self.storage = persistence.storage
        self.manifests = persistence.manifests
        self.store = EvidenceBundleStore(self.storage)

    def _stable(self, manifest):
        return (manifest['dispatch_closed'] and not manifest['integrity_blocked']
                and manifest['plan_publication_status'] == 'published'
                and all(op['operation_status'] in ('completed', 'interrupted')
                        for op in manifest['readme_persistence_operations'].values()))

    def _operation(self, manifest, digest, index_operation, bundle, status, error_code):
        operation_id = str(uuid4())
        key = f'retrieval-bundles/{operation_id}.json'
        return {'operation_id': operation_id, 'version': VERSION,
                'revision_hash': digest, 'plan_hash': manifest['plan']['plan_hash'],
                'chunk_index_key': index_operation['index_key'],
                'model': self.settings.embedding_model,
                'dimension': self.settings.embedding_dimension,
                'config_hash': _digest(_encode(bundle['config'])),
                'storage_key': key, 'bundle_hash': _digest(_encode(bundle)),
                'status': status, 'error_code': error_code, 'created_at': _now(),
                'updated_at': _now()}

    def _fail(self, owner, run_id, manifest, digest, error_code):
        index_operation = manifest.get('chunk_index_operation') or {
            'index_key': str(uuid5(UUID(manifest['run_id']), 'chunk-index:none'))}
        empty = {'version': VERSION, 'config': {}, 'model': self.settings.embedding_model,
                 'dimension': self.settings.embedding_dimension}
        operation = self._operation(manifest, digest, index_operation, empty, 'failed', error_code)
        data = _encode({'version': VERSION, 'schema_version': SCHEMA_VERSION, 'owner': owner,
                        'run_id': run_id, 'task_id': manifest['task_id'],
                        'confirmed_revision_id': manifest['confirmed_revision_id'],
                        'revision_hash': digest, 'plan_hash': manifest['plan']['plan_hash'],
                        'status': 'failed', 'error_code': error_code,
                        'created_at': operation['created_at'], 'warning': '', 'requirements': [],
                        'candidates': [], 'empty_reason': 'retrieval_failed'})
        operation['bundle_hash'] = _digest(data)
        self.persistence._put_immutable(operation['storage_key'], data, 'application/json')

        def apply(value):
            if 'evidence_bundle_operation' in value:
                raise SearchStateError('Evidence bundle already recorded')
            value['evidence_bundle_operation'] = operation
        self.manifests.update(owner, run_id, apply)
        raise EvidenceBundleError(error_code)

    def publish(self, owner, task_id, run_id, revision):
        with _lock(run_id + ':evidence-bundle'):
            manifest, digest = _bound(self.persistence, owner, task_id, run_id, revision)
            if not self._stable(manifest):
                raise SearchStateError('Formal snapshots are not stable')
            if 'evidence_bundle_operation' in manifest:
                bundle, _ = self.store.read(owner, run_id, revision)
                return bundle
            index_operation = manifest.get('chunk_index_operation')
            if index_operation is None or index_operation['status'] != 'ready':
                self._fail(owner, run_id, manifest, digest, 'RETRIEVAL_NOT_READY')
            try:
                view = self.indexer.read(owner, task_id, run_id, revision)
                report = view['chunk_manifest']
                snapshots = snapshot_traces(manifest, self.persistence.repository, report)
                progress = EmbeddingProgress()

                def embed(texts):
                    summary = self.indexer.embeddings.embed_sync(
                        texts, progress,
                        max_batches=max(1, int(getattr(self.settings, 'embedding_max_batches', 32))),
                        max_tokens=16384)
                    return [summary.vectors[text] for text in texts]

                chroma = self.indexer.chroma_factory()
                bundle = retrieve(revision.requirement_items, snapshots, embed=embed,
                                  dense_query=lambda vector, sid, rid, limit: chroma.query(
                                      vector, snapshot_id=sid, repository_id=rid, limit=limit))
                rows = bundle['candidates']
                failed = [row for row in rows if row['status'] == 'failed']
                partial = [row for row in rows if row['status'] == 'partial']
                if rows and len(failed) == len(rows):
                    status, error = 'failed', failed[0].get('error_code') or 'RETRIEVAL_FAILED'
                else:
                    status = 'partial' if partial or failed else 'ready'
                    error = None
                bundle.update(schema_version=SCHEMA_VERSION, owner=owner, task_id=str(task_id), run_id=run_id,
                              confirmed_revision_id=str(revision.revision_id),
                              revision_hash=digest, plan_hash=manifest['plan']['plan_hash'],
                              model=self.settings.embedding_model,
                              dimension=self.settings.embedding_dimension,
                              status=status, created_at=_now(),
                              empty_reason=None if rows else 'no_saved_snapshots',
                              query_usage={'tokens': progress.tokens, 'attempts': progress.attempts})
                operation = self._operation(manifest, digest, index_operation, bundle, status, error)
                data = _encode(bundle)
                operation['bundle_hash'] = _digest(data)
                self.persistence._put_immutable(operation['storage_key'], data, 'application/json')

                def apply(value):
                    if 'evidence_bundle_operation' in value:
                        raise SearchStateError('Evidence bundle already recorded')
                    value['evidence_bundle_operation'] = operation
                self.manifests.update(owner, run_id, apply)
                return bundle
            except EvidenceBundleError:
                raise
            except Exception as exc:
                self._fail(owner, run_id, manifest, digest,
                           'RETRIEVAL_FAILED' if not isinstance(exc, SearchIntegrityError) else 'RETRIEVAL_INTEGRITY')

"""Deterministic README chunk indexing over saved snapshots, with durable batch progress."""
import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4, uuid5

from app.persistence.run_manifest import RunManifestStore
from app.schemas.search import SearchIntegrityError, SearchStateError
from app.services.candidate_review import _bound
from app.services.chunking import MAX_CHUNKS_PER_RUN, chunk_readme, chunk_manifest_hash, normalize_readme
from app.services.chroma_store import ChromaStore, ChromaStoreError
from app.services.embedding_service import (EmbeddingProgress, EmbeddingService, EmbeddingServiceError,
                                            PREPROCESS_VERSION)
from app.services.search_persistence import _digest, _encode, _lock
from app.services.storage import CASConflict, StorageError

VERSION = 'chunk-index-v1'


class ChunkIndexError(ValueError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _config(manifest, revision_hash, settings):
    return {'version': VERSION, 'revision_hash': revision_hash,
            'plan_hash': manifest['plan']['plan_hash'], 'model': settings.embedding_model,
            'dimension': settings.embedding_dimension, 'preprocess_version': PREPROCESS_VERSION}


def _index_key(run_id, config_hash):
    return str(uuid5(UUID(run_id), 'chunk-index:' + config_hash))


class DurableProgress(EmbeddingProgress):
    """Batch bookkeeping persisted through CAS; every successful batch survives a crash."""

    def __init__(self, storage, key, value=None, version=None):
        super().__init__()
        self.storage, self.key, self.version = storage, key, version
        if value:
            self.completed = dict(value.get('completed') or {})
            self.usage = dict(value.get('usage') or {})
            self.batches = int(value.get('batches') or 0)
            self.attempts = int(value.get('attempts') or 0)
            self.tokens = int(value.get('tokens') or 0)

    @classmethod
    def load(cls, storage, key):
        raw, version = storage.read_version(key)
        if raw is None:
            return cls(storage, key)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise StorageError('Embedding 进度对象损坏。') from None
        return cls(storage, key, value, version)

    def record(self, batch, digest, usage):
        super().record(batch, digest, usage)
        data = _encode(self.snapshot())
        try:
            self.storage.compare_and_swap(self.key, self.version, data)
        except CASConflict:
            raw, version = self.storage.read_version(self.key)
            latest = json.loads(raw) if raw else {}
            merged = dict(latest.get('completed') or {})
            merged.update(self.completed)
            self.completed = merged
            self.batches = max(self.batches, int(latest.get('batches') or 0))
            self.tokens = max(self.tokens, int(latest.get('tokens') or 0))
            self.storage.compare_and_swap(self.key, version, _encode(self.snapshot()))
            self.version = version
            return
        self.version = self.storage.read_version(self.key)[1]


def build_manifest(owner, task_id, run_id, revision_hash, plan_hash, settings, snapshots):
    """snapshots: [{snapshot_id, repository_id, commit_sha, readme_path, content_hash, normalized}]"""
    rows, total, estimate = [], 0, 0
    for snapshot in snapshots:
        if snapshot.get('normalized') is None:
            rows.append({'snapshot_id': snapshot['snapshot_id'], 'repository_id': snapshot['repository_id'],
                         'commit_sha': snapshot['commit_sha'], 'readme_path': snapshot['readme_path'],
                         'content_hash': snapshot['content_hash'], 'coverage': 'non_utf8',
                         'chunk_count': 0, 'total_estimate_tokens': 0, 'chunks': []})
            continue
        result = chunk_readme(snapshot['normalized'], snapshot_id=snapshot['snapshot_id'],
                              repository_id=snapshot['repository_id'])
        chunks = result['chunks']
        for chunk in chunks:
            chunk['commit_sha'] = snapshot['commit_sha']
            chunk['readme_path'] = snapshot['readme_path']
        total += len(chunks)
        estimate += result['total_estimate_tokens']
        rows.append({'snapshot_id': snapshot['snapshot_id'], 'repository_id': snapshot['repository_id'],
                     'commit_sha': snapshot['commit_sha'], 'readme_path': snapshot['readme_path'],
                     'content_hash': snapshot['content_hash'], 'coverage': result['coverage'],
                     'chunk_count': len(chunks), 'total_estimate_tokens': result['total_estimate_tokens'],
                     'chunks': chunks})
    return {'version': VERSION, 'owner': owner, 'task_id': task_id, 'run_id': run_id,
            'revision_hash': revision_hash, 'plan_hash': plan_hash,
            'model': settings.embedding_model, 'dimension': settings.embedding_dimension,
            'preprocess_version': PREPROCESS_VERSION, 'snapshots': rows,
            'chunk_count': total, 'total_estimate_tokens': estimate}


class ChunkIndexer:
    def __init__(self, persistence, embedding_service, settings, *, chroma_factory=None):
        self.persistence, self.embeddings, self.settings = persistence, embedding_service, settings
        self.storage = persistence.storage
        self.manifests = persistence.manifests
        self.chroma_factory = chroma_factory or self._chroma

    def _stable(self, manifest):
        return (manifest['dispatch_closed'] and not manifest['integrity_blocked']
                and manifest['plan_publication_status'] == 'published'
                and all(op['operation_status'] in ('completed', 'interrupted')
                        for op in manifest['readme_persistence_operations'].values()))

    def _chroma(self):
        return ChromaStore.persistent(self.settings.chroma_path, model=self.settings.embedding_model,
                                      dimension=self.settings.embedding_dimension,
                                      version=self.settings.version())

    def _operation(self, manifest, digest, prior):
        config = _config(manifest, digest, self.settings)
        config_hash = _digest(_encode(config))
        index_key = _index_key(manifest['run_id'], config_hash)
        history = list(prior['failed_operation_ids']) if prior else []
        if prior:
            history.append(prior['operation_id'])
        return {'operation_id': str(uuid4()), 'version': VERSION, 'index_key': index_key,
                'revision_hash': digest, 'plan_hash': manifest['plan']['plan_hash'],
                'model': self.settings.embedding_model,
                'dimension': self.settings.embedding_dimension,
                'preprocess_version': PREPROCESS_VERSION, 'config_hash': config_hash,
                'status': 'pending', 'chunk_manifest_key': None, 'chunk_manifest_hash': None,
                'usage': {}, 'error_code': None, 'failed_operation_ids': history,
                'created_at': _now(), 'updated_at': _now()}

    def _register(self, owner, run_id, operation, prior):
        def apply(manifest):
            current = manifest.get('chunk_index_operation')
            if prior is None:
                if current is not None:
                    raise SearchStateError('Chunk index already registered')
            elif current != prior:
                raise SearchStateError('Chunk index operation changed')
            manifest['chunk_index_operation'] = operation
        self.manifests.update(owner, run_id, apply)

    def _update(self, owner, run_id, operation, **changes):
        def apply(manifest):
            if manifest.get('chunk_index_operation') != operation:
                raise SearchStateError('Chunk index operation changed')
            manifest['chunk_index_operation'] = {**operation, **changes, 'updated_at': _now()}
        return self.manifests.update(owner, run_id, apply)['chunk_index_operation']

    def _fail(self, owner, run_id, operation, code):
        try:
            self._update(owner, run_id, operation, status='failed', error_code=code)
        except Exception:
            pass

    def _snapshots(self, owner, run_id, manifest):
        rows = []
        for repo_id, candidate in sorted(manifest['candidates'].items(), key=lambda pair: int(pair[0])):
            if candidate.get('collection_status') != 'completed':
                continue
            op = manifest['readme_persistence_operations'][candidate['operation_id']]
            raw = self.persistence.read_snapshot(owner, run_id, op['snapshot_id'], read_only=True)
            try:
                normalized = normalize_readme(raw)
            except Exception:
                normalized = None
            rows.append({'snapshot_id': op['snapshot_id'], 'repository_id': str(op['repository_id']),
                         'commit_sha': op['commit_sha'], 'readme_path': op['readme_path'],
                         'content_hash': op['content_hash'], 'normalized': normalized})
        return rows

    def index(self, owner, task_id, run_id, revision):
        with _lock(run_id + ':chunk-index'):
            manifest, digest = _bound(self.persistence, owner, task_id, run_id, revision)
            if not self._stable(manifest):
                raise SearchStateError('Formal snapshots are not stable')
            operation = manifest.get('chunk_index_operation')
            if operation is not None and operation['status'] == 'ready':
                return self.read(owner, task_id, run_id, revision)
            prior = operation if operation is not None and operation['status'] == 'failed' else None
            if operation is not None and operation['status'] in ('pending', 'embedding') and prior is None:
                current = operation
            else:
                current = self._operation(manifest, digest, prior)
                self._register(owner, run_id, current, prior)
                manifest = self.manifests.read(owner, run_id)
            try:
                snapshots = self._snapshots(owner, run_id, manifest)
                report = build_manifest(owner, str(task_id), run_id, digest,
                                        manifest['plan']['plan_hash'], self.settings, snapshots)
                capacity = min(self.settings.embedding_max_chunks_per_run,
                               self.settings.embedding_batch_size * self.settings.embedding_max_batches,
                               MAX_CHUNKS_PER_RUN)
                if report['chunk_count'] > capacity:
                    raise ChunkIndexError('EMBEDDING_CAPACITY',
                                          f"块数 {report['chunk_count']} 超过有效容量 {capacity}；不会静默丢块。")
                if report['total_estimate_tokens'] > self.settings.embedding_max_tokens_per_run:
                    raise ChunkIndexError('EMBEDDING_BUDGET', '块 token 估算超过本次运行预算。')
                data = _encode(report)
                chunk_key = f"chunk-index/{current['index_key']}/manifest.json"
                usage = {row['snapshot_id']: row['chunk_count'] == 0 for row in report['snapshots']}
                usage.update(current['usage'])
                current = self._update(owner, run_id, current, status='embedding',
                                       chunk_manifest_key=chunk_key,
                                       chunk_manifest_hash=_digest(data), usage=usage)
                self.persistence._put_immutable(chunk_key, data, 'application/json')
                if not report['chunk_count']:
                    return self._finish_empty(owner, run_id, current, report)
                progress = DurableProgress.load(self.storage, f"chunk-index/{current['index_key']}/progress.json")
                summary = self.embeddings.embed_sync(
                    [chunk['text'] for row in report['snapshots'] for chunk in row['chunks']],
                    progress, max_batches=self.settings.embedding_max_batches,
                    max_tokens=self.settings.embedding_max_tokens_per_run)
                chroma = self.chroma_factory()
                for row in report['snapshots']:
                    chunks = row['chunks']
                    if chunks:
                        vectors = {chunk['chunk_id']: summary.vectors[chunk['text']] for chunk in chunks}
                        chroma.upsert(chunks, vectors)
                        verified = chroma.verify(row['snapshot_id'],
                                                 {chunk['chunk_id']: chunk['content_hash'] for chunk in chunks})
                        if not verified['ok']:
                            raise ChromaStoreError('向量写入校验失败。')
                    current = self._update(owner, run_id, current,
                                           usage={**current['usage'], row['snapshot_id']: True})
                current = self._update(owner, run_id, current, status='ready')
                return self._view(current, report, summary)
            except ChunkIndexError as exc:
                self._fail(owner, run_id, current, exc.code)
                raise
            except EmbeddingServiceError as exc:
                self._fail(owner, run_id, current, exc.code)
                raise ChunkIndexError(exc.code, str(exc)) from None
            except ChromaStoreError:
                self._fail(owner, run_id, current, 'CHUNK_INDEX_INTEGRITY')
                raise
            except StorageError:
                self._fail(owner, run_id, current, 'CHUNK_INDEX_STORAGE')
                raise
            except Exception:
                self._fail(owner, run_id, current, 'CHUNK_INDEX_INTERRUPTED')
                raise

    def _finish_empty(self, owner, run_id, operation, report):
        operation = self._update(owner, run_id, operation, status='ready')
        return self._view(operation, report, None)

    def _view(self, operation, report, summary):
        return {'operation_id': operation['operation_id'], 'index_key': operation['index_key'],
                'status': operation['status'], 'model': operation['model'],
                'dimension': operation['dimension'], 'chunk_count': report['chunk_count'],
                'total_estimate_tokens': report['total_estimate_tokens'],
                'snapshots': [{'snapshot_id': row['snapshot_id'], 'repository_id': row['repository_id'],
                               'coverage': row['coverage'], 'chunk_count': row['chunk_count'],
                               'total_estimate_tokens': row['total_estimate_tokens']}
                              for row in report['snapshots']],
                'chunk_manifest_key': operation['chunk_manifest_key'],
                'chunk_manifest_hash': operation['chunk_manifest_hash'],
                'embedding': None if summary is None else {
                    'cache_hits': summary.cache_hits, 'batches': summary.batches,
                    'attempts': summary.attempts}}

    def read(self, owner, task_id, run_id, revision):
        manifest, _ = _bound(self.persistence, owner, task_id, run_id, revision, read_only=True)
        operation = manifest.get('chunk_index_operation')
        if operation is None or operation['status'] != 'ready':
            raise SearchStateError('Chunk index is not ready')
        raw, _ = self.storage.read_version(operation['chunk_manifest_key'])
        if raw is None or _digest(raw) != operation['chunk_manifest_hash']:
            raise SearchIntegrityError('Chunk manifest content differs')
        report = json.loads(raw)
        return self._view(operation, report, None) | {
            'chunk_manifest': report, 'read_only': True}


def manifest_hash(report):
    return chunk_manifest_hash(report)

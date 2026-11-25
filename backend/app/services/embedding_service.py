"""Deduplicated, cached, budgeted embedding calls over a single provider."""
import hashlib
import json
import math
from dataclasses import dataclass, field

import anyio

from app.services.chunking import estimate_tokens
from app.services.siliconflow_client import EmbeddingClientError
from app.services.storage import CASConflict, StorageError

PREPROCESS_VERSION = 'raw-v1'


class EmbeddingServiceError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)

    def public(self):
        return {'error_code': self.code, 'message': self.message, 'retryable': False}


def _slug(model):
    return model.lower().replace('/', '_').replace('-', '_').replace('.', '_')


def text_key(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def batch_id(texts):
    return hashlib.sha256('\0'.join(texts).encode('utf-8')).hexdigest()[:16]


@dataclass
class EmbeddingProgress:
    """Per-run batch bookkeeping; a durable version is written by the chunk indexer."""
    completed: dict = field(default_factory=dict)  # batch_id -> input_hash
    usage: dict = field(default_factory=dict)      # batch_id -> usage dict
    batches: int = 0
    attempts: int = 0
    tokens: int = 0

    def done(self, batch, digest):
        return self.completed.get(batch) == digest

    def record(self, batch, digest, usage):
        self.completed[batch] = digest
        self.usage[batch] = usage
        self.batches += 1
        self.attempts += 1
        self.tokens += int(usage.get('total_tokens') or 0)

    def snapshot(self):
        return {'completed': dict(self.completed), 'usage': dict(self.usage),
                'batches': self.batches, 'attempts': self.attempts, 'tokens': self.tokens}


@dataclass(frozen=True)
class EmbeddingSummary:
    vectors: dict
    usage: dict
    cache_hits: int
    batches: int
    attempts: int


class EmbeddingService:
    def __init__(self, client, storage, settings):
        self.client, self.storage, self.settings = client, storage, settings

    def cache_key(self, text):
        return hashlib.sha256(
            f'{self.settings.version()}|{PREPROCESS_VERSION}|{text}'.encode('utf-8')).hexdigest()

    def cache_object(self, text):
        return f'embedding-cache/{_slug(self.settings.embedding_model)}/{self.cache_key(text)}.json'

    def _read_cache(self, text):
        raw, version = self.storage.read_version(self.cache_object(text))
        if raw is None:
            return None, None
        try:
            value = json.loads(raw)
            vector = value['vector']
            if (value.get('model') != self.settings.embedding_model
                    or value.get('dimension') != self.settings.embedding_dimension
                    or value.get('text_hash') != text_key(text)
                    or not isinstance(vector, list) or len(vector) != self.settings.embedding_dimension
                    or any(type(item) not in (int, float) or not math.isfinite(item) for item in vector)
                    or not any(item != 0 for item in vector)):
                return None, version
            return [float(item) for item in vector], version
        except (ValueError, TypeError, KeyError, UnicodeDecodeError):
            return None, version

    def _write_cache(self, text, vector, expected):
        data = json.dumps({'model': self.settings.embedding_model,
                           'dimension': self.settings.embedding_dimension,
                           'version': self.settings.version(), 'text_hash': text_key(text),
                           'vector': vector}, sort_keys=True, separators=(',', ':')).encode('utf-8')
        try:
            self.storage.compare_and_swap(self.cache_object(text), expected, data)
        except CASConflict:
            existing, _ = self.storage.read_version(self.cache_object(text))
            if existing != data:
                raise EmbeddingServiceError('EMBEDDING_CACHE_CONFLICT', 'Embedding 缓存键冲突。') from None
        except StorageError:
            # Cache is an optimization; the batch result is still valid.
            pass

    async def embed(self, texts, progress: EmbeddingProgress, *, max_batches=32, max_tokens=262144):
        unique = list(dict.fromkeys(texts))
        vectors, hits = {}, 0
        pending = []
        for text in unique:
            cached, version = self._read_cache(text)
            if cached is None:
                pending.append((text, version))
            else:
                vectors[text] = cached
                hits += 1
        size = self.settings.embedding_batch_size
        for offset in range(0, len(pending), size):
            slice_ = pending[offset:offset + size]
            group = [text for text, _ in slice_]
            versions = dict(slice_)
            digest = hashlib.sha256('\0'.join(group).encode('utf-8')).hexdigest()
            identifier = batch_id(group)
            if progress.done(identifier, digest):
                # Recovered batch: vectors must already be cached.
                for text in group:
                    cached, _ = self._read_cache(text)
                    if cached is None:
                        raise EmbeddingServiceError('EMBEDDING_CACHE_LOST', '已完成批次缺少缓存向量，无法安全续跑。')
                    vectors[text] = cached
                continue
            estimate = sum(estimate_tokens(text) for text in group)
            if progress.batches + 1 > max_batches:
                raise EmbeddingServiceError('EMBEDDING_BUDGET', 'Embedding 批次数超过本次运行上限。')
            if progress.tokens + estimate > max_tokens:
                raise EmbeddingServiceError('EMBEDDING_BUDGET', 'Embedding token 预算不足，已停止且不会跳过证据。')
            try:
                result, usage, attempts = await self.client.embed(group)
            except EmbeddingClientError as exc:
                raise EmbeddingServiceError(exc.code, str(exc)) from None
            if len(result) != len(group):
                raise EmbeddingServiceError('EMBEDDING_INVALID_RESPONSE', 'Embedding 返回条数与请求不一致。')
            for text, vector in zip(group, result):
                if len(vector) != self.settings.embedding_dimension:
                    raise EmbeddingServiceError('EMBEDDING_INVALID_RESPONSE', 'Embedding 维度与配置不一致。')
                vectors[text] = [float(item) for item in vector]
                self._write_cache(text, vector, versions[text])
            progress.record(identifier, digest, usage)
            progress.attempts += max(0, attempts - 1)
        missing = [text for text in unique if text not in vectors]
        if missing:
            raise EmbeddingServiceError('EMBEDDING_INCOMPLETE', 'Embedding 未返回全部输入。')
        return EmbeddingSummary(vectors=vectors, usage=dict(progress.usage), cache_hits=hits,
                                batches=progress.batches, attempts=progress.attempts)

    def embed_sync(self, texts, progress: EmbeddingProgress, **limits):
        async def runner():
            return await self.embed(list(texts), progress, **limits)
        return anyio.run(runner)

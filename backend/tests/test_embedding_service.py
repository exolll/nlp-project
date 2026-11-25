import asyncio
import hashlib

import pytest

from app.core.settings import SiliconFlowSettings
from app.services.embedding_service import (EmbeddingProgress, EmbeddingService, EmbeddingServiceError,
                                            PREPROCESS_VERSION, batch_id)
from app.services.siliconflow_client import EmbeddingClientError
from app.services.storage import LocalStorage

DIM = 1024


def settings(**overrides):
    values = dict(siliconflow_api_key='secret-key', embedding_enabled=True, embedding_batch_size=8,
                  embedding_dimension=DIM)
    values.update(overrides)
    return SiliconFlowSettings(_env_file=None, **values)


def vector(text):
    seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
    return [((seed + i) % 97 + 1) / 1000 for i in range(DIM)]


class FakeClient:
    def __init__(self, fail=None, dim=DIM):
        self.calls, self.fail, self.dim = [], fail, dim

    async def embed(self, texts):
        self.calls.append(list(texts))
        if self.fail is not None:
            raise self.fail
        return [vector(text)[:self.dim] for text in texts], {'prompt_tokens': 10, 'total_tokens': 10}, 1


def service(tmp_path, client, **overrides):
    return EmbeddingService(client, LocalStorage(tmp_path), settings(**overrides))


def embed(svc, texts, progress=None, **limits):
    return asyncio.run(svc.embed(texts, progress or EmbeddingProgress(), **limits))


def test_duplicates_are_sent_once(tmp_path):
    client = FakeClient()
    summary = embed(service(tmp_path, client), ['a', 'a', 'b'])
    assert client.calls == [['a', 'b']]
    assert set(summary.vectors) == {'a', 'b'}
    assert summary.batches == 1 and summary.cache_hits == 0


def test_second_run_is_fully_cached(tmp_path):
    client = FakeClient()
    embed(service(tmp_path, client), ['a', 'b'])
    client.calls.clear()
    summary = embed(service(tmp_path, client), ['a', 'b'])
    assert client.calls == [] and summary.cache_hits == 2


def test_corrupt_cache_is_recomputed(tmp_path):
    client = FakeClient()
    svc = service(tmp_path, client)
    svc.storage.put(svc.cache_object('a'), b'{broken', 'application/json')
    summary = embed(svc, ['a'])
    assert client.calls == [['a']] and summary.vectors['a'] == vector('a')


def test_batches_respect_batch_size(tmp_path):
    client = FakeClient()
    summary = embed(service(tmp_path, client), [f'text-{i}' for i in range(20)])
    assert [len(call) for call in client.calls] == [8, 8, 4]
    assert summary.batches == 3


def test_batch_budget_stops_before_any_call(tmp_path):
    client = FakeClient()
    progress = EmbeddingProgress(batches=1)
    with pytest.raises(EmbeddingServiceError) as error:
        embed(service(tmp_path, client), [f'text-{i}' for i in range(8)], progress, max_batches=1)
    assert error.value.code == 'EMBEDDING_BUDGET' and client.calls == []


def test_token_budget_stops_before_any_call(tmp_path):
    client = FakeClient()
    with pytest.raises(EmbeddingServiceError) as error:
        embed(service(tmp_path, client), ['必须支持上传音频'], EmbeddingProgress(), max_tokens=1)
    assert error.value.code == 'EMBEDDING_BUDGET' and client.calls == []


def test_failed_call_keeps_previous_batches_and_cache(tmp_path):
    client = FakeClient(fail=EmbeddingClientError('EMBEDDING_RATE_LIMIT', 'limited'))
    svc = service(tmp_path, client)
    progress = EmbeddingProgress()
    with pytest.raises(EmbeddingServiceError) as error:
        embed(svc, ['a', 'b'], progress)
    assert error.value.code == 'EMBEDDING_RATE_LIMIT'
    assert progress.batches == 0
    assert svc._read_cache('a')[0] is None


def test_recovered_batch_without_cache_is_refused(tmp_path):
    client = FakeClient()
    svc = service(tmp_path, client)
    embed(svc, ['a'])
    digest = hashlib.sha256(b'a').hexdigest()
    progress = EmbeddingProgress(completed={batch_id(['a']): digest})
    svc.storage.delete(svc.cache_object('a'))
    with pytest.raises(EmbeddingServiceError) as error:
        embed(svc, ['a'], progress)
    assert error.value.code == 'EMBEDDING_CACHE_LOST'


def test_wrong_dimension_from_client_is_rejected(tmp_path):
    client = FakeClient(dim=DIM - 1)
    with pytest.raises(EmbeddingServiceError) as error:
        embed(service(tmp_path, client), ['a'])
    assert error.value.code == 'EMBEDDING_INVALID_RESPONSE'


def test_cache_key_includes_preprocess_version(tmp_path):
    svc = service(tmp_path, FakeClient())
    key = svc.cache_key('a')
    assert PREPROCESS_VERSION in f'{svc.settings.version()}|{PREPROCESS_VERSION}|a'
    assert len(key) == 64

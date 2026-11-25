import asyncio
import json

import httpx
import pytest

from app.core.settings import SiliconFlowSettings
from app.services.siliconflow_client import EmbeddingClientError, SiliconFlowClient

DIM = 1024


def settings(**overrides):
    values = dict(siliconflow_api_key='secret-key', embedding_max_retries=1,
                  embedding_timeout_seconds=5, embedding_batch_size=16, embedding_enabled=True)
    values.update(overrides)
    return SiliconFlowSettings(_env_file=None, **values)


def vector(index):
    return [0.001 * (index + 1)] * DIM


def response(texts, *, reverse=False, dim=DIM, zero=False):
    rows = [{'object': 'embedding', 'index': index,
             'embedding': [0.0] * dim if zero else vector(index)} for index in range(len(texts))]
    if reverse:
        rows.reverse()
    return {'object': 'list', 'model': 'BAAI/bge-m3', 'data': rows,
            'usage': {'prompt_tokens': 7, 'completion_tokens': 0, 'total_tokens': 7}}


def run(client, texts):
    return asyncio.run(client.embed(texts))


def test_payload_has_no_dimensions_and_uses_bearer_key():
    seen = {}

    def handler(request):
        seen['body'] = json.loads(request.content)
        seen['auth'] = request.headers.get('authorization')
        seen['url'] = str(request.url)
        return httpx.Response(200, json=response(seen['body']['input']))

    client = SiliconFlowClient(settings(), transport=httpx.MockTransport(handler))
    vectors, usage, attempts = run(client, ['你好', 'hello world'])
    assert seen['url'] == 'https://api.siliconflow.cn/v1/embeddings'
    assert seen['body'] == {'model': 'BAAI/bge-m3', 'input': ['你好', 'hello world'], 'encoding_format': 'float'}
    assert 'dimensions' not in seen['body']
    assert seen['auth'] == 'Bearer secret-key'
    assert len(vectors) == 2 and all(len(row) == DIM for row in vectors)
    assert usage == {'prompt_tokens': 7, 'completion_tokens': 0, 'total_tokens': 7}
    assert attempts == 1


def test_rows_are_reordered_by_index():
    def handler(request):
        return httpx.Response(200, json=response(['a', 'b', 'c'], reverse=True))

    vectors, _, _ = run(SiliconFlowClient(settings(), transport=httpx.MockTransport(handler)), ['a', 'b', 'c'])
    assert vectors == [vector(0), vector(1), vector(2)]


@pytest.mark.parametrize('payload', [
    {'object': 'list', 'data': [], 'usage': {}},
    {'object': 'list', 'data': [{'object': 'embedding', 'index': 0, 'embedding': [0.1] * (DIM - 1)}]},
    {'object': 'list', 'data': [{'object': 'embedding', 'index': 0, 'embedding': [0.0] * DIM}]},
    {'object': 'list', 'data': [{'object': 'embedding', 'index': 1, 'embedding': [0.1] * DIM}]},
])
def test_invalid_vectors_are_rejected(payload):
    def handler(request):
        return httpx.Response(200, json=payload)

    with pytest.raises(EmbeddingClientError) as error:
        run(SiliconFlowClient(settings(), transport=httpx.MockTransport(handler)), ['x'])
    assert error.value.code == 'EMBEDDING_INVALID_RESPONSE'


def test_auth_and_bad_request_are_not_retried():
    calls = []

    def unauthorized(request):
        calls.append(1)
        return httpx.Response(401, json={'message': 'Invalid token'})

    with pytest.raises(EmbeddingClientError) as error:
        run(SiliconFlowClient(settings(), transport=httpx.MockTransport(unauthorized)), ['x'])
    assert error.value.code == 'EMBEDDING_AUTH' and len(calls) == 1
    assert 'secret-key' not in str(error.value)

    calls.clear()

    def bad(request):
        calls.append(1)
        return httpx.Response(400, json={'code': 20012, 'message': 'bad'})

    with pytest.raises(EmbeddingClientError) as error:
        run(SiliconFlowClient(settings(), transport=httpx.MockTransport(bad)), ['x'])
    assert error.value.code == 'EMBEDDING_HTTP_ERROR' and len(calls) == 1


def test_rate_limit_retries_once_then_fails():
    delays = []

    def handler(request):
        return httpx.Response(429, json={'message': 'TPM limit reached'})

    async def fake_sleep(seconds):
        delays.append(seconds)

    client = SiliconFlowClient(settings(), transport=httpx.MockTransport(handler), sleep=fake_sleep)
    with pytest.raises(EmbeddingClientError) as error:
        run(client, ['x'])
    assert error.value.code == 'EMBEDDING_RATE_LIMIT'
    assert error.value.attempts == 2
    assert delays == [0.5]


def test_server_error_retries_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, json={'message': 'overloaded'})
        return httpx.Response(200, json=response(['x']))

    client = SiliconFlowClient(settings(), transport=httpx.MockTransport(handler), sleep=_no_sleep)
    vectors, usage, attempts = run(client, ['x'])
    assert attempts == 2 and len(calls) == 2 and vectors[0] == vector(0)


def test_timeout_is_retried_and_reported():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectTimeout('slow')

    client = SiliconFlowClient(settings(), transport=httpx.MockTransport(handler), sleep=_no_sleep)
    with pytest.raises(EmbeddingClientError) as error:
        run(client, ['x'])
    assert error.value.code == 'EMBEDDING_TIMEOUT' and error.value.attempts == 2 and len(calls) == 2


def test_invalid_json_is_rejected_without_leaking_body():
    def handler(request):
        return httpx.Response(200, content=b'{"object":"list","data":[{"embedding":NaN}]}')

    with pytest.raises(EmbeddingClientError) as error:
        run(SiliconFlowClient(settings(), transport=httpx.MockTransport(handler)), ['x'])
    assert error.value.code == 'EMBEDDING_INVALID_RESPONSE'
    assert 'NaN' not in str(error.value)


def test_empty_or_oversized_batches_are_refused_without_http():
    def handler(request):
        raise AssertionError('must not call')

    client = SiliconFlowClient(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(EmbeddingClientError):
        run(client, [])
    with pytest.raises(EmbeddingClientError):
        run(client, ['x'] * 17)
    with pytest.raises(EmbeddingClientError):
        run(client, ['   '])


async def _no_sleep(_seconds):
    return None

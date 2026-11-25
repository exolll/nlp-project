"""SiliconFlow embeddings transport: bounded, validated, never logs content or the key."""
import asyncio
import json
import math

import httpx

from app.core.settings import SiliconFlowSettings

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_USAGE_KEYS = ('prompt_tokens', 'completion_tokens', 'total_tokens')


class EmbeddingClientError(Exception):
    def __init__(self, code, message, *, retryable=False, usage=None, attempts=1):
        self.code, self.message = code, message
        self.retryable, self.usage, self.attempts = retryable, usage, attempts
        super().__init__(message)

    def public(self):
        return {'error_code': self.code, 'message': self.message, 'retryable': self.retryable}


def strict_json(text):
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result

    def constant(_):
        raise ValueError('invalid JSON constant')

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def _clean_usage(value):
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in _USAGE_KEYS
            if type(value.get(key)) is int and value[key] >= 0}


class SiliconFlowClient:
    def __init__(self, settings: SiliconFlowSettings, *, transport=None, sleep=None):
        self.settings = settings
        self.transport = transport
        self.sleep = sleep or asyncio.sleep

    def payload(self, texts):
        # No `dimensions`: documented as Qwen3-only; bge-m3 always returns 1024.
        return {'model': self.settings.embedding_model, 'input': list(texts), 'encoding_format': 'float'}

    async def embed(self, texts):
        if not isinstance(texts, (list, tuple)) or not texts:
            raise EmbeddingClientError('EMBEDDING_INVALID_REQUEST', 'Embedding 输入不能为空。')
        if len(texts) > min(32, self.settings.embedding_batch_size):
            raise EmbeddingClientError('EMBEDDING_INVALID_REQUEST', 'Embedding 批次超过允许条数。')
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise EmbeddingClientError('EMBEDDING_INVALID_REQUEST', 'Embedding 输入包含空文本。')
        attempts = 0
        while True:
            attempts += 1
            try:
                return await self._once(texts, attempts)
            except EmbeddingClientError as exc:
                exc.attempts = attempts
                if not exc.retryable or attempts > self.settings.embedding_max_retries:
                    raise
                await self.sleep(0.5)

    async def _once(self, texts, attempts):
        try:
            async with asyncio.timeout(self.settings.embedding_timeout_seconds):
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self.settings.embedding_timeout_seconds),
                    follow_redirects=False, trust_env=False,
                    transport=self.transport if self.transport is not None else httpx.AsyncHTTPTransport(retries=0),
                ) as client:
                    async with client.stream(
                        'POST', self.settings.siliconflow_base_url.rstrip('/') + '/embeddings',
                        json=self.payload(texts),
                        headers={'Authorization': 'Bearer ' + self.settings.siliconflow_api_key.get_secret_value()},
                    ) as response:
                        if response.status_code in (401, 403):
                            raise EmbeddingClientError('EMBEDDING_AUTH', 'Embedding 服务认证失败，请检查服务端配置。')
                        if response.status_code == 429:
                            raise EmbeddingClientError('EMBEDDING_RATE_LIMIT', 'Embedding 服务限流，本次将有限重试。', retryable=True)
                        if response.status_code >= 500:
                            raise EmbeddingClientError('EMBEDDING_UNAVAILABLE', 'Embedding 服务暂时不可用。', retryable=True)
                        if response.status_code != 200:
                            raise EmbeddingClientError('EMBEDDING_HTTP_ERROR', 'Embedding 服务返回非成功响应。')
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise EmbeddingClientError('EMBEDDING_RESPONSE_LIMIT', 'Embedding 响应超过允许上限。')
                            body.extend(chunk)
        except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException):
            # This attempt may already be billed upstream; the caller counts every attempt.
            raise EmbeddingClientError('EMBEDDING_TIMEOUT', 'Embedding 请求超时，本次将有限重试。', retryable=True) from None
        except httpx.HTTPError:
            raise EmbeddingClientError('EMBEDDING_UNAVAILABLE', 'Embedding 连接失败。', retryable=True) from None
        try:
            result = strict_json(bytes(body))
        except (ValueError, TypeError, UnicodeDecodeError, RecursionError):
            raise EmbeddingClientError('EMBEDDING_INVALID_RESPONSE', 'Embedding 响应不是有效 JSON。') from None
        return self._validate(result, len(texts), attempts)

    def _validate(self, result, count, attempts):
        dimension = self.settings.embedding_dimension
        try:
            if not isinstance(result, dict) or result.get('object') != 'list':
                raise ValueError()
            rows = result.get('data')
            if not isinstance(rows, list) or len(rows) != count:
                raise ValueError()
            indexes = []
            ordered = {}
            for row in rows:
                if not isinstance(row, dict) or row.get('object') != 'embedding':
                    raise ValueError()
                index, vector = row.get('index'), row.get('embedding')
                if type(index) is not int or not 0 <= index < count or index in ordered:
                    raise ValueError()
                if not isinstance(vector, list) or len(vector) != dimension:
                    raise ValueError()
                if any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
                    raise ValueError()
                if not any(value != 0 for value in vector):
                    raise ValueError()
                indexes.append(index)
                ordered[index] = [float(value) for value in vector]
        except ValueError:
            raise EmbeddingClientError('EMBEDDING_INVALID_RESPONSE', 'Embedding 向量格式或维度无效。', attempts=attempts) from None
        if sorted(indexes) != list(range(count)):
            raise EmbeddingClientError('EMBEDDING_INVALID_RESPONSE', 'Embedding 返回顺序不完整。', attempts=attempts)
        return [ordered[index] for index in range(count)], _clean_usage(result.get('usage')), attempts

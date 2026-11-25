"""Provider execution certainty: classify one HTTP attempt without retries or guessing.

Existing `extraction_provider.call_provider` is unchanged. This adapter is additive and
reuses the same payload/settings/transport conventions.
"""
import asyncio
import json
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, model_validator

from app.services import extraction_provider

SUCCESS = 'success'
FAILED = 'failed'
OUTCOME_UNKNOWN = 'outcome_unknown'

UNAVAILABLE = 'PROVIDER_UNAVAILABLE'
AUTH = 'PROVIDER_AUTH'
BALANCE = 'PROVIDER_BALANCE'
REJECTED = 'PROVIDER_REJECTED'
UNKNOWN = 'OUTCOME_UNKNOWN'
INVALID_OUTPUT = 'INVALID_OUTPUT'
OUTPUT_TRUNCATED = 'OUTPUT_TRUNCATED'


class ProviderExecutionResult(BaseModel):
    """Strict, closed result of exactly one provider attempt."""
    model_config = ConfigDict(extra='forbid', strict=True)

    execution_status: Literal['success', 'failed', 'outcome_unknown']
    error_code: str | None
    request_sent: bool
    execution_confirmed: bool
    retryable: bool
    requires_acknowledgement: bool
    usage_known: bool
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    http_status: int | None = None
    response_complete: bool
    attempt_id: str | None = None

    @model_validator(mode='after')
    def consistent(self):
        if self.execution_status == 'success':
            if self.error_code is not None or not self.execution_confirmed or self.retryable:
                raise ValueError('invalid success result')
        else:
            if not self.error_code:
                raise ValueError('failure requires error_code')
            if self.execution_status == 'outcome_unknown':
                if self.execution_confirmed or not self.retryable or not self.requires_acknowledgement:
                    raise ValueError('unknown result must be unconfirmed and acknowledged')
            else:
                if not self.execution_confirmed or self.requires_acknowledgement:
                    raise ValueError('failed result cannot require acknowledgement')
        if self.execution_status == 'outcome_unknown' and self.usage_known:
            raise ValueError('unknown execution cannot claim known usage')
        tokens = (self.prompt_tokens, self.completion_tokens, self.total_tokens)
        if self.usage_known:
            if any(type(value) is not int or value < 0 for value in tokens):
                raise ValueError('known usage requires non-negative token counts')
            if self.total_tokens != self.prompt_tokens + self.completion_tokens:
                raise ValueError('usage total mismatch')
        elif any(value is not None for value in tokens):
            raise ValueError('unknown usage must not carry token counts')
        return self


def _usage_known(usage):
    if not isinstance(usage, dict):
        return None
    keys = ('prompt_tokens', 'completion_tokens', 'total_tokens')
    if any(type(usage.get(key)) is not int or usage[key] < 0 for key in keys):
        return None
    if usage['total_tokens'] != usage['prompt_tokens'] + usage['completion_tokens']:
        return None
    return usage


def _result(status, code, *, request_sent, confirmed, retryable, ack, usage=None,
            http_status=None, complete, attempt_id=None):
    known = usage if usage is not None else None
    return ProviderExecutionResult(
        execution_status=status, error_code=code, request_sent=request_sent,
        execution_confirmed=confirmed, retryable=retryable, requires_acknowledgement=ack,
        usage_known=known is not None,
        prompt_tokens=None if known is None else known['prompt_tokens'],
        completion_tokens=None if known is None else known['completion_tokens'],
        total_tokens=None if known is None else known['total_tokens'],
        http_status=http_status, response_complete=complete, attempt_id=attempt_id)


def classify_exception(exc, attempt_id=None):
    zero = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return _result('failed', UNAVAILABLE, request_sent=False, confirmed=True,
                       retryable=True, ack=False, usage=zero, complete=False,
                       attempt_id=attempt_id)
    if isinstance(exc, (httpx.WriteTimeout, httpx.WriteError)):
        return _result('outcome_unknown', UNKNOWN, request_sent=True, confirmed=False,
                       retryable=True, ack=True, complete=False, attempt_id=attempt_id)
    if isinstance(exc, (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError,
                        httpx.DecodingError)):
        return _result('outcome_unknown', UNKNOWN, request_sent=True, confirmed=False,
                       retryable=True, ack=True, complete=False, attempt_id=attempt_id)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)):
        return _result('outcome_unknown', UNKNOWN, request_sent=True, confirmed=False,
                       retryable=True, ack=True, complete=False, attempt_id=attempt_id)
    if isinstance(exc, httpx.HTTPError):
        # Transport errors after connect cannot prove the request was not executed.
        return _result('outcome_unknown', UNKNOWN, request_sent=True, confirmed=False,
                       retryable=True, ack=True, complete=False, attempt_id=attempt_id)
    # Unknown exception types cannot prove the request was not sent.
    return _result('outcome_unknown', UNKNOWN, request_sent=True, confirmed=False,
                   retryable=True, ack=True, complete=False, attempt_id=attempt_id)


def classify_response(status, *, body=None, parsed=None, explicit_429_rejection=False,
                      attempt_id=None):
    """Classify one completed HTTP response. 5xx/429 default to conservative unknown."""
    zero = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
    if status == 401:
        return _result('failed', AUTH, request_sent=True, confirmed=True, retryable=False,
                       ack=False, usage=zero, http_status=status, complete=True, attempt_id=attempt_id), None
    if status == 402:
        return _result('failed', BALANCE, request_sent=True, confirmed=True, retryable=False,
                       ack=False, usage=zero, http_status=status, complete=True, attempt_id=attempt_id), None
    if status == 429:
        if explicit_429_rejection:
            return _result('failed', REJECTED, request_sent=True, confirmed=True, retryable=True,
                           ack=False, usage=zero, http_status=status, complete=True,
                           attempt_id=attempt_id), None
        return _result('outcome_unknown', UNKNOWN, request_sent=True, confirmed=False, retryable=True,
                       ack=True, http_status=status, complete=True, attempt_id=attempt_id), None
    if status >= 500:
        return _result('outcome_unknown', UNKNOWN, request_sent=True, confirmed=False, retryable=True,
                       ack=True, http_status=status, complete=True, attempt_id=attempt_id), None
    if status != 200:
        return _result('failed', REJECTED, request_sent=True, confirmed=True, retryable=False,
                       ack=False, usage=zero, http_status=status, complete=True, attempt_id=attempt_id), None
    if parsed is None:
        try:
            parsed = extraction_provider.strict_json(body or b'')
        except (ValueError, TypeError, UnicodeDecodeError, RecursionError):
            return _result('failed', INVALID_OUTPUT, request_sent=True, confirmed=True,
                           retryable=False, ack=False, http_status=status, complete=True,
                           attempt_id=attempt_id), None
    usage = _usage_known(parsed.get('usage')) if isinstance(parsed, dict) else None
    choices = parsed.get('choices') if isinstance(parsed, dict) else None
    if not choices:
        return _result('failed', INVALID_OUTPUT, request_sent=True, confirmed=True, retryable=False,
                       ack=False, usage=usage, http_status=status, complete=True,
                       attempt_id=attempt_id), None
    choice = choices[0]
    if choice.get('finish_reason') == 'length':
        return _result('failed', OUTPUT_TRUNCATED, request_sent=True, confirmed=True,
                       retryable=False, ack=False, usage=usage, http_status=status, complete=True,
                       attempt_id=attempt_id), None
    content = choice.get('message', {}).get('content')
    if not content or not isinstance(content, str) or not content.strip():
        return _result('failed', INVALID_OUTPUT, request_sent=True, confirmed=True, retryable=False,
                       ack=False, usage=usage, http_status=status, complete=True,
                       attempt_id=attempt_id), None
    if choice.get('finish_reason') != 'stop':
        return _result('failed', INVALID_OUTPUT, request_sent=True, confirmed=True, retryable=False,
                       ack=False, usage=usage, http_status=status, complete=True,
                       attempt_id=attempt_id), None
    try:
        extracted = extraction_provider.strict_json(content)
    except (ValueError, TypeError, RecursionError):
        return _result('failed', INVALID_OUTPUT, request_sent=True, confirmed=True, retryable=False,
                       ack=False, usage=usage, http_status=status, complete=True,
                       attempt_id=attempt_id), None
    return _result('success', None, request_sent=True, confirmed=True, retryable=False, ack=False,
                   usage=usage, http_status=status, complete=True, attempt_id=attempt_id), extracted


async def execute_chat(payload, settings, provider, *, transport=None, attempt_id=None,
                       explicit_429_rejection=False):
    """One attempt. No retries; returns (ProviderExecutionResult, parsed_json_or_none)."""
    try:
        async with asyncio.timeout(settings.extraction_timeout_seconds):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(settings.extraction_timeout_seconds),
                follow_redirects=False, trust_env=False,
                transport=transport if transport is not None else httpx.AsyncHTTPTransport(retries=0),
            ) as client:
                async with client.stream(
                    'POST', provider.deepseek_base_url + '/chat/completions', json=payload,
                    headers={'Authorization': 'Bearer ' + provider.deepseek_api_key.get_secret_value()},
                ) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > extraction_provider.MAX_RESPONSE_BYTES:
                            return _result('failed', OUTPUT_TRUNCATED, request_sent=True,
                                           confirmed=True, retryable=False, ack=False,
                                           http_status=response.status_code, complete=False,
                                           attempt_id=attempt_id), None
                        body.extend(chunk)
                    return classify_response(response.status_code, body=bytes(body),
                                             explicit_429_rejection=explicit_429_rejection,
                                             attempt_id=attempt_id)
    except Exception as exc:
        return classify_exception(exc, attempt_id=attempt_id), None

import asyncio
import json

import httpx
import pytest

from app.core.settings import ExtractionSettings, Settings
from app.services import provider_execution as pe

SETTINGS = Settings(_env_file=None)


def provider():
    return ExtractionSettings(_env_file=None, deepseek_api_key='test-key')


def body(content='{"answer": 1}', *, usage=None, finish='stop'):
    payload = {'choices': [{'message': {'content': content}, 'finish_reason': finish}]}
    if usage is not None:
        payload['usage'] = usage
    return payload


USAGE = {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}


def run(handler, **kwargs):
    return asyncio.run(pe.execute_chat({'model': 'deepseek-flash', 'messages': []}, SETTINGS,
                                       provider(), transport=httpx.MockTransport(handler), **kwargs))


def test_success_known_usage_and_no_retry():
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=body(usage=USAGE))
    result, parsed = run(handler)
    assert result.execution_status == 'success' and result.error_code is None
    assert result.request_sent and result.execution_confirmed and result.response_complete
    assert result.usage_known and result.total_tokens == 15
    assert parsed == {'answer': 1} and len(calls) == 1


def test_pre_send_dns_and_connect_failures_are_retryable_with_zero_usage():
    for error in (httpx.ConnectError('dns failure'), httpx.ConnectTimeout('connect')):
        def handler(request, error=error):
            raise error
        result, parsed = run(handler)
        assert result.execution_status == 'failed' and result.error_code == 'PROVIDER_UNAVAILABLE'
        assert result.request_sent is False and result.retryable is True
        assert result.requires_acknowledgement is False
        assert result.usage_known and result.prompt_tokens == 0 and parsed is None


@pytest.mark.parametrize('error', [httpx.WriteTimeout('write'), httpx.ReadTimeout('read'),
                                   httpx.ReadError('read error'), httpx.RemoteProtocolError('reset')])
def test_send_and_read_failures_are_outcome_unknown(error):
    calls = []
    def handler(request):
        calls.append(1)
        raise error
    result, parsed = run(handler)
    assert result.execution_status == 'outcome_unknown' and result.error_code == 'OUTCOME_UNKNOWN'
    assert result.request_sent is True and result.execution_confirmed is False
    assert result.retryable and result.requires_acknowledgement
    assert result.usage_known is False and result.prompt_tokens is None
    assert parsed is None and len(calls) == 1


def test_http_401_and_402_are_terminal_failures():
    for status, code in ((401, 'PROVIDER_AUTH'), (402, 'PROVIDER_BALANCE')):
        result, _ = run(lambda request, status=status: httpx.Response(status, json={'error': 'x'}))
        assert result.execution_status == 'failed' and result.error_code == code
        assert result.retryable is False and result.requires_acknowledgement is False
        assert result.usage_known and result.total_tokens == 0


def test_429_defaults_to_unknown_and_explicit_rejection_can_be_confirmed():
    result, _ = run(lambda request: httpx.Response(429, json={'message': 'limit'}))
    assert result.execution_status == 'outcome_unknown' and result.requires_acknowledgement
    assert result.usage_known is False
    rejected, _ = run(lambda request: httpx.Response(429, json={'message': 'limit'}),
                      explicit_429_rejection=True)
    assert rejected.execution_status == 'failed' and rejected.error_code == 'PROVIDER_REJECTED'
    assert rejected.retryable is True and rejected.usage_known and rejected.total_tokens == 0


@pytest.mark.parametrize('status', [500, 502, 503])
def test_5xx_is_conservatively_unknown_not_unavailable(status):
    result, _ = run(lambda request: httpx.Response(status, json={'error': 'server'}))
    assert result.execution_status == 'outcome_unknown' and result.error_code == 'OUTCOME_UNKNOWN'
    assert result.execution_confirmed is False and result.requires_acknowledgement
    assert result.response_complete is True and result.http_status == status


def test_complete_invalid_json_is_failed_not_unknown():
    result, _ = run(lambda request: httpx.Response(200, content=b'not json'))
    assert result.execution_status == 'failed' and result.error_code == 'INVALID_OUTPUT'
    assert result.retryable is False and result.execution_confirmed is True


def test_truncated_output_is_terminal_failure_and_keeps_usage():
    result, _ = run(lambda request: httpx.Response(200, json=body(finish='length', usage=USAGE)))
    assert result.execution_status == 'failed' and result.error_code == 'OUTPUT_TRUNCATED'
    assert result.retryable is False and result.usage_known and result.total_tokens == 15


@pytest.mark.parametrize('usage', [None, {'prompt_tokens': 1},
                                   {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 5}])
def test_missing_or_inconsistent_usage_is_unknown_not_zero(usage):
    result, parsed = run(lambda request: httpx.Response(200, json=body(usage=usage)))
    assert result.execution_status == 'success' and parsed == {'answer': 1}
    assert result.usage_known is False
    assert result.prompt_tokens is None and result.completion_tokens is None and result.total_tokens is None


def test_success_with_consistent_usage_is_known():
    result, _ = run(lambda request: httpx.Response(200, json=body(usage=USAGE)))
    assert result.usage_known and result.prompt_tokens == 10 and result.completion_tokens == 5


def test_generic_http_error_is_unknown_not_failed():
    def handler(request):
        raise httpx.HTTPError('unknown transport state')
    result, _ = run(handler)
    assert result.execution_status == 'outcome_unknown' and result.request_sent


def test_unknown_exception_types_are_conservatively_unknown():
    def handler(request):
        raise RuntimeError('unexpected transport state')
    result, _ = run(handler)
    assert result.execution_status == 'outcome_unknown'
    assert result.request_sent is True and result.requires_acknowledgement
    assert result.usage_known is False


def test_existing_call_provider_unchanged():
    from app.services.extraction_provider import call_provider

    def handler(request):
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok": true}'},
                                                      'finish_reason': 'stop'}],
                                          'usage': USAGE})
    extracted, usage = asyncio.run(call_provider({'model': 'deepseek-flash'}, SETTINGS, provider(),
                                                 httpx.MockTransport(handler)))
    assert extracted == {'ok': True} and usage == USAGE

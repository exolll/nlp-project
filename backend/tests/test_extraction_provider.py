import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.core.settings import ExtractionSettings, Settings
from app.services.extraction_provider import MAX_RESPONSE_BYTES, ProviderError, call_provider


def settings():
    return Settings(environment='test', _env_file=None), ExtractionSettings(deepseek_api_key='test-only', _env_file=None)


def invoke(handler, limits=None):
    config, provider = settings()
    return asyncio.run(call_provider({'model': 'deepseek-flash'}, limits or config, provider, httpx.MockTransport(handler)))


def success(content='{}', usage=None, reason='stop'):
    return {'choices': [{'message': {'content': content}, 'finish_reason': reason}], 'usage': usage}


@pytest.mark.parametrize('status,code', [(401, 'PROVIDER_AUTH'), (402, 'PROVIDER_BALANCE'), (429, 'PROVIDER_RATE_LIMIT'),
    (500, 'PROVIDER_UNAVAILABLE'), (503, 'PROVIDER_UNAVAILABLE'), (302, 'PROVIDER_HTTP_ERROR'), (400, 'PROVIDER_HTTP_ERROR')])
def test_statuses_safe_no_retries_or_redirects(status, code, caplog):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text='secret-body-credential', headers={'Location': 'https://evil.invalid'})
    with pytest.raises(ProviderError) as error:
        invoke(handler)
    assert error.value.code == code
    assert len(calls) == 1
    assert str(calls[0].url) == 'https://api.deepseek.com/chat/completions'
    assert 'secret-body' not in str(error.value) + caplog.text
    assert 'test-only' not in str(error.value) + caplog.text
    assert error.value.public()['retryable'] is False


@pytest.mark.parametrize('response,code', [
    (success('', None), 'PROVIDER_EMPTY'), ({'choices': []}, 'PROVIDER_EMPTY'),
    (success('{}', None, 'length'), 'PROVIDER_OUTPUT_LIMIT'),
    (success('{}', None, 'content_filter'), 'PROVIDER_FINISH_REASON'),
    (success('not-json'), 'PROVIDER_INVALID_JSON'), (success('{"x":1,"x":2}'), 'PROVIDER_INVALID_JSON'),
    ({'choices': 'invalid'}, 'PROVIDER_INVALID_JSON'),
])
def test_invalid_responses(response, code):
    with pytest.raises(ProviderError) as error:
        invoke(lambda request: httpx.Response(200, json=response))
    assert error.value.code == code


def test_invalid_outer_json():
    with pytest.raises(ProviderError) as error:
        invoke(lambda request: httpx.Response(200, text='{broken'))
    assert error.value.code == 'PROVIDER_INVALID_JSON'


def test_usage_preserves_real_fields_and_unknown_null():
    usage = {'prompt_tokens': 17, 'completion_tokens': 9, 'prompt_cache_hit_tokens': 5}
    assert invoke(lambda req: httpx.Response(200, json=success('{"ok":true}', usage))) == ({'ok': True}, usage)
    assert invoke(lambda req: httpx.Response(200, json=success())) == ({}, None)
    with pytest.raises(ProviderError) as error:
        invoke(lambda req: httpx.Response(200, json=success('{}', usage, 'length')))
    assert error.value.usage == usage


@pytest.mark.parametrize('error_type,code', [(httpx.ReadTimeout, 'PROVIDER_TIMEOUT'), (httpx.ConnectError, 'PROVIDER_NETWORK')])
def test_network_exceptions_safe_and_single_attempt(error_type, code):
    calls = []
    def handler(request):
        calls.append(1)
        raise error_type('Authorization: test-only secret-body')
    with pytest.raises(ProviderError) as error:
        invoke(handler)
    assert error.value.code == code and len(calls) == 1
    assert 'test-only' not in str(error.value)


def test_total_deadline_bounds_executor():
    async def handler(request):
        await asyncio.Event().wait()
    with pytest.raises(ProviderError) as error:
        invoke(handler, settings()[0].model_copy(update={'extraction_timeout_seconds': 0.02}))
    assert error.value.code == 'PROVIDER_TIMEOUT'


def test_response_size_bound():
    with pytest.raises(ProviderError) as error:
        invoke(lambda request: httpx.Response(200, content=b'x' * (MAX_RESPONSE_BYTES + 1)))
    assert error.value.code == 'PROVIDER_RESPONSE_LIMIT'


@pytest.mark.parametrize('url', ['http://api.deepseek.com', 'https://evil.invalid', 'https://api.deepseek.com.evil.invalid',
                               'https://api.deepseek.com/', 'https://api.deepseek.com/chat/completions'])
def test_official_fixed_https_only(url):
    with pytest.raises(ValidationError):
        ExtractionSettings(deepseek_base_url=url, deepseek_api_key='test-only', _env_file=None)


def test_secret_configuration_not_public_repr():
    config = settings()[1]
    assert isinstance(config.deepseek_api_key, SecretStr)
    assert 'test-only' not in repr(config)
    assert Settings.model_config['env_file'] is None
    assert ExtractionSettings.model_config['env_file'] is None


def test_local_provider_environment_file_and_process_override(tmp_path, monkeypatch):
    env = tmp_path / '.env'
    env.write_text('DEEPSEEK_API_KEY=synthetic-file-key\nDEEPSEEK_MODEL=deepseek-flash\n', encoding='utf-8')
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    config = ExtractionSettings(_env_file=env)
    assert config.deepseek_api_key.get_secret_value() == 'synthetic-file-key'
    assert 'synthetic-file-key' not in repr(config)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'synthetic-process-key')
    assert ExtractionSettings(_env_file=env).deepseek_api_key.get_secret_value() == 'synthetic-process-key'

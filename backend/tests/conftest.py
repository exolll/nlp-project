"""All tests are offline. Real model/network transports are forbidden, not stubbed in production."""
import os

import httpx
import pytest

from app.core.settings import ExtractionSettings, Settings, SiliconFlowSettings


def pytest_configure():
    Settings.model_config['env_file'] = None
    ExtractionSettings.model_config['env_file'] = None
    SiliconFlowSettings.model_config['env_file'] = None
    os.environ['EMBEDDING_ENABLED'] = 'false'
    os.environ.setdefault('SILICONFLOW_API_KEY', 'test-only')


@pytest.fixture(autouse=True)
def offline_models(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', '')
    def forbidden(*args, **kwargs):
        raise AssertionError('Tests must use MockTransport; real HTTP is forbidden.')
    async def async_forbidden(*args, **kwargs):
        raise AssertionError('Tests must use MockTransport; real HTTP is forbidden.')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', async_forbidden)

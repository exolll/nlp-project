"""Single-attempt, fixed-origin GitHub API client and search query compiler."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import math
import re
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import httpx
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.schemas.search import SearchQueryDocument
from app.services.search_contracts import canonicalize_query


class GitHubClientError(ValueError):
    """Safe local configuration/input error (never includes input values)."""


class GitHubSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / '.env',
        env_file_encoding='utf-8', extra='ignore', hide_input_in_errors=True,
    )
    github_token: SecretStr = SecretStr('')
    github_timeout_seconds: float = Field(default=10, ge=1, le=30)
    github_max_response_bytes: int = Field(default=4 * 1024**2, ge=1024, le=8 * 1024**2)


_SHA = re.compile(r'[0-9a-f]{40}\Z')
_TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9.-]*\Z')


def render_query(query: SearchQueryDocument) -> str:
    if not isinstance(query, SearchQueryDocument):
        raise GitHubClientError('Invalid query document')
    try:
        canonicalize_query(query.query_text, language_filter=query.language_filter,
                           qualifiers=query.qualifiers)
    except ValueError:
        raise GitHubClientError('Invalid query document') from None
    quoted = False
    for char in query.query_text:
        if char == '\\' or (char == ':' and not quoted):
            raise GitHubClientError('Unsafe query text')
        if char == '"':
            quoted = not quoted
    if quoted:
        raise GitHubClientError('Unpaired query quotes')
    parts = [query.query_text]
    language = query.language_filter
    if language is not None:
        if not re.fullmatch(r'[A-Za-z0-9+#. -]+', language):
            raise GitHubClientError('Invalid language filter')
        parts.append('language:' + (f'"{language}"' if ' ' in language else language))
    for key, values in query.qualifiers.items():
        for value in values:
            valid = False
            if key in ('stars', 'forks'):
                valid = bool(re.fullmatch(r'(?:[<>]=?)?[0-9]+|[0-9]+\.\.[0-9]+', value))
                if valid and '..' in value:
                    lo, hi = value.split('..')
                    valid = (len(lo.lstrip('0')), lo.lstrip('0')) <= (len(hi.lstrip('0')), hi.lstrip('0'))
            elif key == 'in':
                valid = all(v in ('name', 'description', 'readme') for v in value.split(','))
            elif key == 'archived':
                valid = value in ('true', 'false')
            elif key in ('topic', 'license'):
                valid = bool(_TOKEN.fullmatch(value))
            if not valid:
                raise GitHubClientError('Invalid qualifier')
            parts.append(f'{key}:{value}')
    return ' '.join(parts)


def _full_name(value: str) -> str:
    if type(value) is not str or not re.fullmatch(
        r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}', value
    ) or value.split('/')[-1] in ('.', '..'):
        raise GitHubClientError('Invalid repository name')
    return value


def _json_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError
    return result


def _seconds(value: str | None) -> float:
    if value is None:
        return 0.0
    try:
        result = float(value)
    except ValueError:
        try:
            result = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 0.0
    return max(0.0, result) if math.isfinite(result) else 0.0


def _rate(headers: httpx.Headers) -> dict:
    result = {}
    resource = headers.get('x-ratelimit-resource', '')
    if re.fullmatch(r'[a-z_]{1,64}', resource):
        result['resource'] = resource
    for key in ('remaining', 'limit', 'reset'):
        value = headers.get('x-ratelimit-' + key, '')
        if re.fullmatch(r'[0-9]{1,20}', value):
            result[key] = int(value)
    if 'retry-after' in headers:
        result['retry_after'] = _seconds(headers['retry-after'])
    return result


def _envelope(status=0, data=None, code=None, retryable=False, delay=0.0,
              rate=None, has_next=False) -> dict:
    return dict(status=status, data=data, error_code=code, retryable=retryable,
                retry_after=delay, rate_limit=rate or {}, has_next=has_next)


class GitHubService:
    def __init__(self, settings: GitHubSettings, *, transport=None):
        token = settings.github_token.get_secret_value()
        if not token or not re.fullmatch(r'[\x21-\x7e]+', token):
            raise GitHubClientError('GitHub token is required and must be header-safe')
        self._settings = settings
        self._runner = asyncio.Runner()
        self._cooldown_until = 0.0
        self._authentication_failed = False
        self._client = httpx.AsyncClient(
            base_url='https://api.github.com', transport=transport, trust_env=False,
            follow_redirects=False, timeout=settings.github_timeout_seconds,
            headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28', 'Accept-Encoding': 'identity'},
        )

    def close(self):
        if not self._client.is_closed:
            self._runner.run(self._client.aclose())
            self._runner.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def search(self, query: str | SearchQueryDocument, page: int, per_page: int,
               timeout: float | None = None) -> dict:
        if isinstance(query, SearchQueryDocument):
            query = render_query(query)
        if (type(query) is not str or not query.strip()
                or any(ord(c) < 32 or ord(c) == 127 for c in query)
                or type(page) is not int or not 1 <= page <= 1000
                or type(per_page) is not int or not 1 <= per_page <= 100):
            raise GitHubClientError('Invalid search input')
        return self._request('/search/repositories', {'q': query, 'page': page, 'per_page': per_page},
                             'search', timeout)

    def commit(self, full_name: str, ref: str, timeout: float | None = None) -> dict:
        name = _full_name(full_name)
        if (type(ref) is not str or not ref or len(ref) > 1024
                or any(part in ('.', '..', '') for part in ref.split('/'))
                or any(ord(c) < 33 or ord(c) > 126 for c in ref)
                or any(c in ref for c in '\\?#%')):
            raise GitHubClientError('Invalid commit reference')
        return self._request(f'/repos/{name}/commits/{quote(ref, safe="")}', None, 'commit', timeout)

    def readme(self, full_name: str, sha: str, timeout: float | None = None) -> dict:
        name = _full_name(full_name)
        if type(sha) is not str or not _SHA.fullmatch(sha):
            raise GitHubClientError('Invalid commit SHA')
        return self._request(f'/repos/{name}/readme', {'ref': sha}, 'readme', timeout)

    def _request(self, path: str, params: dict | None, kind: str,
                 timeout: float | None) -> dict:
        if timeout is None:
            timeout = self._settings.github_timeout_seconds
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise GitHubClientError('Invalid request timeout')
        if self._authentication_failed:
            return _envelope(401, code='AUTHENTICATION')
        remaining = self._cooldown_until - time.monotonic()
        if remaining > 0:
            return _envelope(429, code='RATE_LIMITED', retryable=True, delay=remaining)
        result = self._runner.run(self._timed_request(path, params, kind, timeout))
        if result['error_code'] == 'AUTHENTICATION':
            self._authentication_failed = True
        if result['error_code'] in ('RATE_LIMITED', 'SECONDARY_RATE_LIMIT'):
            self._cooldown_until = time.monotonic() + max(1.0, result['retry_after'])
        if result['rate_limit'].get('remaining') == 0:
            self._cooldown_until = max(self._cooldown_until, time.monotonic() +
                                       max(0.0, result['rate_limit'].get('reset', 0) - time.time()))
        return result

    async def _timed_request(self, path, params, kind, timeout):
        try:
            return await asyncio.wait_for(self._request_async(path, params, kind, timeout), timeout)
        except TimeoutError:
            return _envelope(code='TIMEOUT')

    async def _request_async(self, path, params, kind, timeout):
        status, rate = 0, {}
        try:
            async with self._client.stream('GET', path, params=params, timeout=timeout) as response:
                status = response.status_code
                rate = _rate(response.headers)
                delay = rate.get('retry_after', 0.0)
                if 300 <= status < 400:
                    return _envelope(status, code='REDIRECT', rate=rate)
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(body) + len(chunk) > self._settings.github_max_response_bytes:
                        return _envelope(status, code='RESPONSE_TOO_LARGE', rate=rate)
                    body.extend(chunk)
                try:
                    data = json.loads(body, parse_float=_json_float,
                                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                except (ValueError, UnicodeError, RecursionError):
                    data = None
                if status != 200:
                    code = {401: 'AUTHENTICATION', 404: 'NOT_FOUND', 422: 'INVALID_QUERY',
                            429: 'RATE_LIMITED'}.get(status, 'HTTP_ERROR')
                    if status == 403:
                        message = data.get('message', '') if type(data) is dict else ''
                        if rate.get('remaining') == 0:
                            code = 'RATE_LIMITED'
                            delay = max(delay, max(0.0, rate.get('reset', 0) - time.time()))
                        elif 'retry-after' in response.headers or (type(message) is str and
                                ('secondary rate limit' in message.lower() or 'abuse detection' in message.lower())):
                            code = 'SECONDARY_RATE_LIMIT'
                        else:
                            code = 'FORBIDDEN'
                    if code == 'RATE_LIMITED' and rate.get('remaining') == 0:
                        delay = max(delay, max(0.0, rate.get('reset', 0) - time.time()))
                    if 500 <= status <= 599:
                        code = 'SERVER_ERROR'
                    return _envelope(status, code=code, retryable=code in
                                     ('RATE_LIMITED', 'SECONDARY_RATE_LIMIT', 'SERVER_ERROR'),
                                     delay=delay, rate=rate)
                try:
                    self._validate(data, kind)
                except _ReadmeTooLarge:
                    return _envelope(status, code='README_TOO_LARGE', rate=rate)
                except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error):
                    return _envelope(status, code='INVALID_RESPONSE', rate=rate)
                # Parse only relation metadata; never request the URL in Link.
                has_next = any('next' in (m.group(1) or m.group(2) or '').split() for m in re.finditer(
                    r';\s*rel\s*=\s*(?:"([^"]*)"|([^\s;,]+))',
                    response.headers.get('link', ''), re.I))
                return _envelope(status, data=data, rate=rate, has_next=has_next)
        except httpx.TimeoutException:
            return _envelope(status, code='TIMEOUT', rate=rate)
        except httpx.RequestError:
            return _envelope(status, code='NETWORK_ERROR', rate=rate)

    @staticmethod
    def _validate(data, kind):
        if type(data) is not dict:
            raise ValueError
        if kind == 'commit':
            if type(data.get('sha')) is not str or not _SHA.fullmatch(data['sha']):
                raise ValueError
        elif kind == 'search':
            if (type(data.get('total_count')) is not int or data['total_count'] < 0
                    or type(data.get('incomplete_results')) is not bool or type(data.get('items')) is not list):
                raise ValueError
            from app.persistence.search_repository import SearchRepository
            for repo in data['items']:
                if type(repo) is not dict:
                    raise ValueError
                name = _full_name(repo.get('full_name'))
                if (type(repo.get('id')) is not int or not 1 <= repo['id'] < 2**64
                        or repo.get('name') != name.split('/')[1]
                        or repo.get('html_url') != f'https://github.com/{name}'):
                    raise ValueError
                SearchRepository.github_patch(repo)
        else:
            size = data.get('size')
            if type(size) is int and size > 1024**2:
                raise _ReadmeTooLarge
            if (data.get('type') != 'file' or type(data.get('path')) is not str or not data['path']
                    or type(data.get('sha')) is not str or not _SHA.fullmatch(data['sha'])
                    or data.get('encoding') != 'base64' or type(size) is not int or size < 0
                    or type(data.get('content')) is not str):
                raise ValueError
            encoded = data['content'].replace('\r', '').replace('\n', '')
            if len(encoded) > 4 * ((1024**2 + 2) // 3):
                raise _ReadmeTooLarge
            raw = base64.b64decode(encoded, validate=True)
            if base64.b64encode(raw).decode('ascii') != encoded:
                raise ValueError
            if len(raw) > 1024**2:
                raise _ReadmeTooLarge
            if len(raw) != size or hashlib.sha1(b'blob ' + str(size).encode() + b'\0' + raw).hexdigest() != data['sha']:
                raise ValueError


class _ReadmeTooLarge(ValueError):
    pass

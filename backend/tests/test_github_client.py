import base64
import hashlib
import json
from datetime import datetime, timezone
from email.utils import formatdate
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.schemas.search import SearchQueryDocument
from app.services.github_client import GitHubClientError, GitHubService, GitHubSettings, render_query
from app.services.search_contracts import canonicalize_query, query_hash


SHA = 'a' * 40
TOKEN = 'fake-unit-test-token'


def settings(**kw):
    return GitHubSettings(_env_file=None, github_token=TOKEN, **kw)


def query(text='Machine Learning NOT "hello:world"', language=None, qualifiers=None):
    fields = canonicalize_query(text, language_filter=language, qualifiers=qualifiers or {})
    return SearchQueryDocument(**fields, query_id=str(uuid4()), query_type='primary',
                               query_hash=query_hash(**fields), source_requirement_pointers=['/project_topic'],
                               created_at=datetime.now(timezone.utc))


def invoke(response, method='commit', **kw):
    calls = []
    def handler(request):
        calls.append(request)
        if isinstance(response, Exception):
            raise response
        return response
    with GitHubService(settings(**kw), transport=httpx.MockTransport(handler)) as service:
        result = {'commit': lambda: service.commit('owner/repo', 'main'),
                  'search': lambda: service.search('test', 1, 10),
                  'readme': lambda: service.readme('owner/repo', SHA)}[method]()
    assert len(calls) == 1
    assert TOKEN not in json.dumps(result)
    return result


def readme(raw=b'hello\n'):
    return dict(type='file', path='README.md', size=len(raw), encoding='base64',
                sha=hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest(),
                content=base64.b64encode(raw).decode(), download_url='http://127.0.0.1/secret')


def test_paths_params_headers_and_close():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.host == 'api.github.com'
        assert request.headers['authorization'] == 'Bearer ' + TOKEN
        if request.url.path.endswith('/readme'):
            return httpx.Response(200, json=readme())
        if request.url.path.startswith('/search'):
            return httpx.Response(200, json={'total_count': 0, 'incomplete_results': False, 'items': []})
        return httpx.Response(200, json={'sha': SHA})
    with GitHubService(settings(), transport=httpx.MockTransport(handler)) as service:
        assert service.commit('Owner/repo.name', 'feature/one')['data']['sha'] == SHA
        assert calls[-1].url.raw_path == b'/repos/Owner/repo.name/commits/feature%2Fone'
        service.readme('owner/repo', SHA)
        assert calls[-1].url.params['ref'] == SHA
        service.search('C++ "one two" &x=y', 2, 30, timeout=0.5)
        assert calls[-1].url.params['q'] == 'C++ "one two" &x=y'
        assert calls[-1].url.params['page'] == '2'
        assert calls[-1].extensions['timeout']['read'] == 0.5
    assert service._client.is_closed
    assert len(calls) == 3


@pytest.mark.parametrize('status,headers,payload,code,retryable', [
    (401, {}, {}, 'AUTHENTICATION', False), (404, {}, {}, 'NOT_FOUND', False),
    (422, {}, {}, 'INVALID_QUERY', False), (403, {}, {}, 'FORBIDDEN', False),
    (403, {'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '9999999999'}, {}, 'RATE_LIMITED', True),
    (403, {}, {'message': 'You have exceeded a secondary rate limit.'}, 'SECONDARY_RATE_LIMIT', True),
    (403, {'retry-after': '2'}, {}, 'SECONDARY_RATE_LIMIT', True),
    (429, {'retry-after': '3'}, {}, 'RATE_LIMITED', True),
    (503, {}, {}, 'SERVER_ERROR', True),
    (302, {'location': 'https://attacker.invalid/'}, {}, 'REDIRECT', False),
])
def test_errors(status, headers, payload, code, retryable):
    result = invoke(httpx.Response(status, headers=headers, json=payload))
    assert result['error_code'] == code
    assert result['retryable'] is retryable
    assert result['data'] is None


@pytest.mark.parametrize('delay,expected', [('3', 3), ('-3', 0), ('NaN', 0), ('inf', 0), ('garbage', 0)])
def test_retry_after(delay, expected):
    result = invoke(httpx.Response(429, headers={'retry-after': delay}))
    assert result['retry_after'] == expected


def test_date_rate_headers_and_pagination(monkeypatch):
    monkeypatch.setattr('app.services.github_client.time.time', lambda: 1000)
    result = invoke(httpx.Response(429, headers={'retry-after': formatdate(1060, usegmt=True),
        'x-ratelimit-limit': '17', 'x-ratelimit-remaining': '5', 'x-ratelimit-resource': 'search',
        'x-ratelimit-reset': '1200', 'authorization': TOKEN}))
    assert result['retry_after'] == 60
    assert result['rate_limit'] == dict(retry_after=60, limit=17, remaining=5, resource='search', reset=1200)
    result = invoke(httpx.Response(200, json={'total_count': 0, 'incomplete_results': False, 'items': []},
                                   headers={'link': '<http://localhost/secret>; rel="next"'}), 'search')
    assert result['has_next'] is True


@pytest.mark.parametrize('body', [b'bad', b'[]', b'null', b'{"sha":true}', b'{"sha":"' + b'A'*40 + b'"}', b'{"sha":NaN}', b'{"sha":"' + b'a'*40 + b'","extra":1e999}'])
def test_bad_json_and_commit(body):
    assert invoke(httpx.Response(200, content=body))['error_code'] == 'INVALID_RESPONSE'


def test_search_shape_and_metadata():
    repo = dict(id=2**64-1, name='repo', full_name='owner/repo', html_url='https://github.com/owner/repo')
    payload = dict(total_count=1, incomplete_results=False, items=[repo])
    assert invoke(httpx.Response(200, json=payload), 'search')['data'] == payload
    for key, value in [('id', True), ('id', 2**64), ('name', '../repo'),
                       ('full_name', 'owner/..'), ('html_url', 'https://github.com.evil/owner/repo'),
                       ('stargazers_count', -1)]:
        bad = dict(payload, items=[dict(repo, **{key: value})])
        assert invoke(httpx.Response(200, json=bad), 'search')['error_code'] == 'INVALID_RESPONSE'
    for key, value in [('total_count', True), ('total_count', -1), ('incomplete_results', 0), ('items', {})]:
        assert invoke(httpx.Response(200, json=dict(payload, **{key: value})), 'search')['error_code'] == 'INVALID_RESPONSE'


def test_readme_integrity():
    payload = readme()
    payload['content'] = '\r\n' + payload['content'] + '\n'
    assert invoke(httpx.Response(200, json=payload), 'readme')['data'] == payload
    for key, value in [('sha', SHA), ('size', 0), ('encoding', 'none'), ('type', 'symlink'),
                       ('path', ''), ('content', '!!!'), ('content', payload['content'] + ' ')]:
        assert invoke(httpx.Response(200, json=dict(payload, **{key: value})), 'readme')['error_code'] == 'INVALID_RESPONSE'
    assert invoke(httpx.Response(200, json=dict(payload, size=1024**2+1)), 'readme')['error_code'] == 'README_TOO_LARGE'


def test_total_response_deadline_cancels_slow_stream():
    import asyncio
    class SlowChunks(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.005)
                yield b' '
        async def aclose(self):
            self.closed = True
    stream = SlowChunks()
    with GitHubService(settings(), transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=stream))) as service:
        result = service.commit('owner/repo', 'main', timeout=0.03)
        assert result['error_code'] == 'TIMEOUT'
        assert not result['retryable']
        assert stream.closed


def test_shared_client_obeys_cooldown_across_calls():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={'retry-after': '120'})
    with GitHubService(settings(), transport=httpx.MockTransport(handler)) as service:
        assert service.search('search', 1, 30)['error_code'] == 'RATE_LIMITED'
        assert service.commit('owner/repo', 'main')['error_code'] == 'RATE_LIMITED'
        assert len(calls) == 1


def test_size_and_timeout():
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'x' * 700
            yield b'x' * 700
    assert invoke(httpx.Response(200, stream=Chunks()), github_max_response_bytes=1024)['error_code'] == 'RESPONSE_TOO_LARGE'
    result = invoke(httpx.ReadTimeout(TOKEN))
    assert result['error_code'] == 'TIMEOUT' and not result['retryable']
    assert invoke(httpx.ConnectError(TOKEN))['error_code'] == 'NETWORK_ERROR'


@pytest.mark.parametrize('name', ['https://evil/repo', '//evil/repo', 'a/..', 'a/b?x', 'a/b#x', 'a/b/c', 'a/%2e'])
def test_ssrf_input(name):
    with GitHubService(settings(), transport=httpx.MockTransport(lambda _: pytest.fail('Unexpected HTTP'))) as service:
        with pytest.raises(GitHubClientError):
            service.commit(name, 'main')


@pytest.mark.parametrize('ref', ['..', '.', '?x', '#x', '%2e%2e', 'a\\b', 'a\n', 'feature/../main', '/main'])
def test_ref_input(ref):
    with GitHubService(settings(), transport=httpx.MockTransport(lambda _: pytest.fail('Unexpected HTTP'))) as service:
        with pytest.raises(GitHubClientError):
            service.commit('owner/repo', ref)


def test_settings_and_inputs():
    for kw in [dict(github_timeout_seconds=0), dict(github_timeout_seconds=31),
               dict(github_max_response_bytes=1), dict(github_max_response_bytes=8*1024**2+1)]:
        with pytest.raises(ValidationError):
            settings(**kw)
    with pytest.raises(GitHubClientError):
        GitHubService(GitHubSettings(_env_file=None, github_token=''))
    assert TOKEN not in repr(settings())
    with GitHubService(settings(), transport=httpx.MockTransport(lambda _: pytest.fail('Unexpected HTTP'))) as service:
        for timeout in [0, -1, float('inf'), float('nan'), True]:
            with pytest.raises(GitHubClientError):
                service.commit('owner/repo', 'main', timeout)
        with pytest.raises(GitHubClientError):
            service.readme('owner/repo', 'main')
        with pytest.raises(GitHubClientError):
            service.search('x', 1, 101)


@pytest.mark.parametrize('language', ['C++', 'C#', 'Objective-C', 'Jupyter Notebook'])
def test_compiler_preserves_semantics(language):
    doc = query(language=language, qualifiers={'stars': ['>=10', '1..5'], 'topic': ['z', 'a']})
    before = doc.query_hash
    rendered = render_query(doc)
    assert rendered.startswith(doc.query_text + ' language:')
    assert ('language:"Jupyter Notebook"' in rendered) if ' ' in language else ('language:' + language in rendered)
    assert rendered.endswith('stars:1..5 stars:>=10 topic:a topic:z')
    assert doc.query_hash == before


@pytest.mark.parametrize('text', ['x language:python', 'x repo:a/b', '"unpaired', 'x\\y'])
def test_compiler_text_rejections(text):
    with pytest.raises(GitHubClientError):
        render_query(query(text))


@pytest.mark.parametrize('qualifiers', [{'stars': ['5..1']}, {'forks': ['-1']}, {'archived': ['TRUE']},
                                       {'in': ['code']}, {'topic': ['x y']}, {'license': ['mit OR x']}])
def test_compiler_qualifier_rejections(qualifiers):
    with pytest.raises(GitHubClientError):
        render_query(query(qualifiers=qualifiers))


def test_compiler_language_injection():
    with pytest.raises(GitHubClientError):
        render_query(query(language='Python topic:evil'))

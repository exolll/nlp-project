"""Offline HTTP acceptance for deep-review authorization, persistence and read-only GET."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.settings import ExtractionSettings, Settings
from app.main import create_app
from app.persistence.input_state import InputState
from app.persistence.run_manifest import RunManifestStore
from app.schemas.search import SearchGroupDocument, SearchPlanDocument
from app.services.requirement_quality import revision_hash
from app.services.search_persistence import SearchPersistence, _digest, _encode
from test_api import auth, owner, task
from test_deep_review_pipeline import (assessment, make_candidate, make_chunk, make_revision,
                                       request_envelope, response)
from test_search_persistence import FaultStorage, RepositoryStub

USAGE = {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}


class Factory:
    def __init__(self, persistence, storage):
        self.persistence, self.storage = persistence, storage
        self.mode = 'normal'
        self.fail_open = False
        self.fail_manifest = False
        self.opens = 0
        self.calls = 0
        self.requests = []

    def preflight(self, settings, *, external):
        assert external is False
        return False

    @contextmanager
    def open(self, store, config, budget=None, app_settings=None):
        self.opens += 1
        if self.fail_open:
            raise RuntimeError('offline backend unavailable')
        if self.fail_manifest:
            self.storage.failure_prefix, self.storage.failure_mode = 'runs/', 'before'
        try:
            yield SimpleNamespace(persistence=self.persistence)
        finally:
            self.storage.failure_prefix = self.storage.failure_mode = None

    def handler(self, request):
        self.calls += 1
        self.requests.append(json.loads(request.content))
        envelope = request_envelope(request)
        output = {'assessments': [assessment(item['requirement_item_id'],
                                             chunk_id=envelope['chunks'][0]['chunk_id'],
                                             line_start=envelope['chunks'][0]['line_start'],
                                             line_end=envelope['chunks'][0]['line_end'])
                                  for item in envelope['requirements']]}
        if self.mode == 'auth' and self.calls == 1:
            return httpx.Response(401, text='private provider detail')
        if self.mode == 'partial' and self.calls == 2:
            return httpx.Response(400, text='private provider detail')
        if self.mode == 'unknown' and self.calls == 2:
            raise httpx.ReadTimeout('private provider detail', request=request)
        return response(output, usage=USAGE)


def seed_manifest(storage, persistence, identity, task_id, revision, run_id, mode):
    persistence.create_run(identity, task_id, revision, run_id)
    now = datetime.now(timezone.utc)
    groups = [SearchGroupDocument(group_id=str(uuid4()), group_name=name,
                                  group_purpose='离线夹具', source_requirement_pointers=[],
                                  group_status='skipped', skip_reason='离线夹具', created_at=now,
                                  queries=[]) for name in 'ABC']
    plan = SearchPlanDocument(plan_id=str(uuid4()), run_id=run_id,
                              confirmed_revision_id=str(revision.revision_id),
                              planner_version='fixture', model_version='fixture', prompt_version='fixture',
                              created_at=now, groups=groups)
    persistence.publish_plan(identity, task_id, revision, plan)
    persistence.manifests.start(identity, run_id)
    raws = [b'# Audio\r\nSupports task management.\r\n', b'# Audio\r\nTask export is supported.\r\n']
    if mode == 'six':
        raws = [f'# Audio {index}\r\nSupports task management.\r\n'.encode() for index in range(6)]
    item_ids = [str(item.requirement_item_id) for item in revision.requirement_items]
    candidates = []
    for index, raw in enumerate(raws):
        commit = f'{index + 1:040x}'
        blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        snapshot = persistence.collect_readme(identity, run_id, 10 + index, commit,
                                              'README.md', blob, raw)
        chunks = [] if mode == 'empty' else [make_chunk(raw, f'chunk-{index}')]
        candidate = make_candidate(raw, index, item_ids, chunks,
                                   repository_id=str(10 + index), snapshot_id=snapshot['snapshot_id'],
                                   commit_sha=commit)
        candidate['collected_at'] = snapshot['collected_at']
        candidates.append(candidate)
    persistence.manifests.seal_dispatch(identity, run_id)
    persistence.manifests.finish(identity, run_id)
    manifest = persistence.manifests.read(identity, run_id)
    index_key = uuid4()
    chunk = {'operation_id': str(uuid4()), 'version': 'chunk-index-v1', 'index_key': str(index_key),
             'revision_hash': revision_hash(revision), 'plan_hash': manifest['plan']['plan_hash'],
             'model': 'BAAI/bge-m3', 'dimension': 1024, 'preprocess_version': 'raw-v1',
             'config_hash': 'c' * 64, 'status': 'ready',
             'chunk_manifest_key': f'chunk-index/{index_key}/manifest.json',
             'chunk_manifest_hash': '9' * 64,
             'usage': {candidate['snapshot_id']: True for candidate in candidates},
             'error_code': None, 'failed_operation_ids': [],
             'created_at': now.isoformat(), 'updated_at': now.isoformat()}
    pending = {**chunk, 'status': 'pending', 'chunk_manifest_key': None,
               'chunk_manifest_hash': None, 'usage': {}}
    persistence.manifests.update(identity, run_id, lambda m: m.update(chunk_index_operation=pending))
    persistence.manifests.update(identity, run_id, lambda m: m.update(chunk_index_operation=chunk))
    if mode == 'not-ready':
        return
    bundle = {'version': 'evidence-bundle-v2', 'schema_version': 2,
              'owner': 'wrong-owner' if mode == 'wrong-owner' else identity,
              'task_id': task_id, 'run_id': run_id,
              'confirmed_revision_id': str(revision.revision_id),
              'revision_hash': revision_hash(revision), 'plan_hash': manifest['plan']['plan_hash'],
              'candidates': candidates}
    operation_id = str(uuid4())
    key = f'retrieval-bundles/{operation_id}.json'
    data = _encode(bundle)
    storage.put(key, data, 'application/json')
    retrieval = {'operation_id': operation_id, 'version': 'evidence-bundle-v2',
                 'chunk_index_key': str(index_key), 'revision_hash': revision_hash(revision),
                 'plan_hash': manifest['plan']['plan_hash'], 'config_hash': 'c' * 64,
                 'bundle_hash': _digest(data), 'model': 'BAAI/bge-m3', 'dimension': 1024,
                 'storage_key': key, 'status': 'ready', 'error_code': None,
                 'created_at': now.isoformat(), 'updated_at': now.isoformat()}
    persistence.manifests.update(identity, run_id, lambda m: m.update(evidence_bundle_operation=retrieval))


@pytest.fixture
def deep_api(tmp_path, request):
    mode = getattr(request, 'param', 'normal')
    storage = FaultStorage(tmp_path)
    repository = RepositoryStub()
    persistence = SearchPersistence(storage, repository)
    settings = Settings(environment='test', _env_file=None, write_requests_per_minute=1000)
    provider = ExtractionSettings(deepseek_api_key='test-only', _env_file=None)
    factory = Factory(persistence, storage)
    transport = httpx.MockTransport(factory.handler)
    app = create_app(settings, storage, extraction_settings=provider, extraction_transport=transport,
                     search_backend_factory=factory)
    with TestClient(app) as client:
        headers = auth(client)
        identity = owner(headers)
        task_id = task(client, headers)
        revision = make_revision([{'field': 'project_topic', 'text': 'Audio export'},
                                  {'text': '支持任务管理'}]).model_copy(update={'task_id': UUID(task_id)})
        run_id = str(uuid4())
        store = InputState(storage, settings)
        def seed(session):
            record = session['tasks'][task_id]
            record['requirements'] = {'revisions': [revision.model_dump(mode='json')]}
            record['search_runs'] = {run_id: {'run_id': run_id, 'task_id': task_id,
                                               'confirmed_revision_id': str(revision.revision_id)}}
        store.session(identity, seed)
        seed_manifest(storage, persistence, identity, task_id, revision, run_id, mode)
        url = f'/api/v1/tasks/{task_id}/search-runs/{run_id}/deep-review'
        body = {'revision_id': str(revision.revision_id), 'consent': True}
        yield SimpleNamespace(client=client, headers=headers, identity=identity, task_id=task_id,
                              revision=revision, run_id=run_id, url=url, body=body, storage=storage,
                              persistence=persistence, factory=factory, settings=settings,
                              provider=provider, transport=transport, manifests=persistence.manifests,
                              mode=mode)


def post(api, body=None):
    return api.client.post(api.url, headers=api.headers, json=body or api.body)


def test_consent_and_revision_fail_before_authorization(deep_api):
    assert post(deep_api, {**deep_api.body, 'consent': False}).status_code == 422
    assert post(deep_api, {**deep_api.body, 'revision_id': str(uuid4())}).status_code == 409
    assert deep_api.client.get(deep_api.url, headers=deep_api.headers).status_code == 404
    assert deep_api.factory.calls == deep_api.factory.opens == 0
    assert 'deep_review_operation' not in deep_api.manifests.read(deep_api.identity, deep_api.run_id)


def test_first_post_repeat_and_get_share_one_operation_and_report(deep_api):
    first = post(deep_api)
    assert first.status_code == 202, first.text
    operation_id = first.json()['operation_id']
    assert first.json()['status'] == 'running' and first.json()['report'] is None
    assert first.json()['model'] == 'deepseek-flash'
    authorized = datetime.fromisoformat(first.json()['authorized_at'])
    deadline = datetime.fromisoformat(first.json()['deadline'])
    assert deadline - authorized == timedelta(seconds=5 * deep_api.settings.extraction_timeout_seconds + 120)
    result = deep_api.client.get(deep_api.url, headers=deep_api.headers)
    assert result.status_code == 200, result.text
    view = result.json()
    assert view['operation_id'] == operation_id and view['status'] == 'completed'
    assert view['persisted_status'] == 'completed' and view['error_code'] is None
    report = view['report']
    assert report['operation_id'] == operation_id and 'owner' not in report
    assert report['status'] == 'completed' and report['summary']['usage']['calls'] == 2
    assert report['summary']['usage']['total_tokens'] == 20
    assert all(repository['status'] == 'completed' for repository in report['repositories'])
    for repository in report['repositories']:
        assert repository['trace']['readme_path'] == 'README.md'
        assert repository['trace']['commit_sha'] and repository['snapshot_id']
        for review in repository['requirements']:
            assert review['verdict'] == 'supports' and review['citations'][0]['quote']
            assert review['citations'][0]['line_start'] >= 1
    manifest = deep_api.manifests.read(deep_api.identity, deep_api.run_id)
    operation = manifest['deep_review_operation']
    key = f'deep-reviews/{operation_id}/report.json'
    raw, _ = deep_api.storage.read_version(key)
    assert operation['report_key'] == key and operation['report_hash'] == hashlib.sha256(raw).hexdigest()
    before = deep_api.storage.read_version(f'runs/{deep_api.identity}/{deep_api.run_id}.json')[1]
    again = post(deep_api)
    assert again.status_code == 202 and again.json()['operation_id'] == operation_id
    assert again.json()['report'] == report
    assert deep_api.client.get(deep_api.url, headers=deep_api.headers).json() == view
    assert deep_api.factory.calls == 2 and deep_api.factory.opens == 1
    assert deep_api.storage.read_version(f'runs/{deep_api.identity}/{deep_api.run_id}.json')[1] == before


def test_concurrent_clicks_register_only_one_background_execution(deep_api):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: post(deep_api), range(4)))
    assert all(result.status_code == 202 for result in results)
    assert len({result.json()['operation_id'] for result in results}) == 1
    view = deep_api.client.get(deep_api.url, headers=deep_api.headers).json()
    assert view['status'] == 'completed' and view['report']['summary']['usage']['calls'] == 2
    assert deep_api.factory.calls == 2 and deep_api.factory.opens == 1


def test_get_is_pure_read_and_cross_owner_is_denied(deep_api):
    deep_api.factory.fail_open = True
    assert post(deep_api).json()['status'] == 'running'
    keys = [f'sessions/{deep_api.identity}.json', f'runs/{deep_api.identity}/{deep_api.run_id}.json']
    versions = [deep_api.storage.read_version(key)[1] for key in keys]
    calls, opens = deep_api.factory.calls, deep_api.factory.opens
    for _ in range(3):
        result = deep_api.client.get(deep_api.url, headers=deep_api.headers)
        assert result.status_code == 200 and result.json()['status'] == 'running'
    assert [deep_api.storage.read_version(key)[1] for key in keys] == versions
    assert deep_api.factory.calls == calls and deep_api.factory.opens == opens
    other = auth(deep_api.client)
    assert deep_api.client.get(deep_api.url, headers=other).status_code == 404
    assert post(deep_api).json()['status'] == 'running'
    assert deep_api.factory.calls == calls and deep_api.factory.opens == opens


def test_expired_running_is_unknown_without_manifest_write(deep_api, monkeypatch):
    deep_api.factory.fail_open = True
    operation = post(deep_api).json()
    key = f'runs/{deep_api.identity}/{deep_api.run_id}.json'
    before, version = deep_api.storage.read_version(key)
    future = datetime.fromisoformat(operation['deadline']).replace(microsecond=0)
    monkeypatch.setattr('app.services.deep_review_routes.utcnow', lambda: future + timedelta(seconds=1))
    result = deep_api.client.get(deep_api.url, headers=deep_api.headers)
    assert result.status_code == 200
    assert result.json()['status'] == 'unknown'
    assert result.json()['persisted_status'] == 'running' and result.json()['report'] is None
    assert deep_api.storage.read_version(key) == (before, version)
    assert json.loads(before)['deep_review_operation']['status'] == 'running'


@pytest.mark.parametrize('mode,status,calls,completed,skipped', [
    ('auth', 'failed', 1, 0, 1),
    ('partial', 'partial', 2, 1, 0),
    ('unknown', 'unknown', 2, 1, 0),
])
def test_provider_outcomes_persist_frozen_aggregation(deep_api, mode, status, calls, completed, skipped):
    deep_api.factory.mode = mode
    assert post(deep_api).status_code == 202
    view = deep_api.client.get(deep_api.url, headers=deep_api.headers).json()
    assert view['status'] == view['report']['status'] == status
    assert view['report']['summary']['usage']['calls'] == calls
    assert view['report']['summary']['repositories_completed'] == completed
    assert view['report']['summary']['repositories_skipped'] == skipped
    assert deep_api.factory.calls == calls
    assert post(deep_api).json()['operation_id'] == view['operation_id']
    assert deep_api.client.get(deep_api.url, headers=deep_api.headers).json()['status'] == status
    assert deep_api.factory.calls == calls


@pytest.mark.parametrize('deep_api,status', [
    ('empty', 422), ('six', 422), ('not-ready', 409), ('wrong-owner', 409),
], indirect=['deep_api'])
def test_unusable_bundle_is_rejected_before_authorization(deep_api, status):
    result = post(deep_api)
    assert result.status_code == status
    if deep_api.mode == 'six':
        assert result.json()['error_code'] == 'DEEP_REVIEW_REPOSITORY_BUDGET'
    assert deep_api.factory.calls == deep_api.factory.opens == 0
    assert 'deep_review_operation' not in deep_api.manifests.read(deep_api.identity, deep_api.run_id)


def test_snapshot_integrity_failure_is_terminal_without_provider_call(deep_api):
    manifest = deep_api.manifests.read(deep_api.identity, deep_api.run_id)
    operation = next(iter(manifest['readme_persistence_operations'].values()))
    raw, version = deep_api.storage.read_version(operation['storage_key'])
    deep_api.storage.compare_and_swap(operation['storage_key'], version, raw + b'tampered')
    assert post(deep_api).status_code == 202
    view = deep_api.client.get(deep_api.url, headers=deep_api.headers).json()
    assert view['status'] == 'failed' and view['error_code'] == 'DEEP_REVIEW_SNAPSHOT_UNAVAILABLE'
    assert view['report'] is None and deep_api.factory.calls == 0
    assert not deep_api.manifests.read(deep_api.identity, deep_api.run_id)['integrity_blocked']


def test_unknown_manifest_write_leaves_running_and_never_replays(deep_api):
    deep_api.factory.fail_manifest = True
    operation = post(deep_api).json()
    view = deep_api.client.get(deep_api.url, headers=deep_api.headers).json()
    assert view['status'] == 'running' and view['persisted_status'] == 'running'
    assert view['operation_id'] == operation['operation_id'] and view['report'] is None
    assert deep_api.factory.calls == 2
    assert post(deep_api).json()['status'] == 'running'
    assert deep_api.factory.calls == 2


def test_report_hash_mismatch_is_rejected_without_rewrite(deep_api):
    post(deep_api)
    manifest = deep_api.manifests.read(deep_api.identity, deep_api.run_id)
    operation = manifest['deep_review_operation']
    raw, version = deep_api.storage.read_version(operation['report_key'])
    deep_api.storage.compare_and_swap(operation['report_key'], version, raw + b' ')
    result = deep_api.client.get(deep_api.url, headers=deep_api.headers)
    assert result.status_code == 409 and result.json()['error_code'] == 'DEEP_REVIEW_INTEGRITY'
    assert deep_api.storage.read_version(operation['report_key'])[0] == raw + b' '
    assert deep_api.manifests.read(deep_api.identity, deep_api.run_id) == manifest


def test_test_and_production_gates_do_not_call_provider(deep_api):
    for environment in ('test', 'production'):
        settings = deep_api.settings.model_copy(update={'environment': environment})
        app = create_app(settings, deep_api.storage, search_backend_factory=deep_api.factory)
        with TestClient(app) as client:
            result = client.post(deep_api.url, headers=deep_api.headers, json=deep_api.body)
            assert result.status_code == 503
            assert result.json()['error_code'] in ('DEEP_REVIEW_DISABLED', 'DEEP_REVIEW_NOT_PUBLIC')
    assert deep_api.factory.calls == deep_api.factory.opens == 0

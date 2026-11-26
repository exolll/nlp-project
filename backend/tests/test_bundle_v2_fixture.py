"""Offline Bundle v2 acceptance fixture built from real snapshot metadata (no external calls).

TEST FIXTURE: this never pretends to be a real search run and never touches historical runs.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.settings import Settings
from app.persistence.run_manifest import RunManifestStore
from app.schemas.requirements import RequirementItem, RequirementJSON, RequirementRevision
from app.services.chunking import chunk_readme, normalize_readme
from app.services.evidence_bundle import EvidenceBundleService, EvidenceBundleStore
from app.services.search_contracts import SNAPSHOT_KEY_ALGORITHM, snapshot_key
from app.services.storage import LocalStorage

ROOT = Path(__file__).resolve().parents[2]
OWNER = 'd' * 64
REAL = {
    'repository_id': 17020719, 'snapshot_id': '4d220eec-4486-4b9d-a036-1bd211e7d98f',
    'commit_sha': '1d0639416ecc6b068d849f78029dc9c2a7594b6b',
    'blob_sha': 'f227055633d95d5edd336fd17ab3d26b6f662538',
    'content_hash': '378cf5c3f8e072d37d4037e26c9da727390f82e53cb0bce525c7a92ef9b865b6',
    'readme_path': 'README.md', 'collected_at': '2026-11-23T09:00:07.102610+00:00',
    'storage_key': ('repository-snapshots/d5f6598e097f909923f4ae2078ce333f2616bd3ae22432ae'
                    '089648756715ed43/readme.raw'),
}
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def uid():
    return str(uuid4())


class Repository:
    def get_snapshot_by_id(self, snapshot_id):
        if snapshot_id != REAL['snapshot_id']:
            return None
        return {key: REAL[key] for key in ('snapshot_id', 'repository_id', 'commit_sha', 'blob_sha',
                                           'content_hash', 'readme_path', 'storage_key')}


class Persistence:
    def __init__(self, storage, chunk_manifest):
        self.storage = storage
        self.manifests = RunManifestStore(storage)
        self.repository = Repository()
        self.chunk_manifest = chunk_manifest

    def _put_immutable(self, key, data, content_type):
        existing, _ = self.storage.read_version(key)
        if existing is not None:
            assert existing == data
            return
        self.storage.put(key, data, content_type)
        assert self.storage.read_version(key)[0] == data

    def read_plan(self, owner, run_id, read_only=False):
        return SimpleNamespace(validate_revision=lambda revision, task_id: None)


class Indexer:
    def __init__(self, chunk_manifest):
        self.chunk_manifest = chunk_manifest
        self.last_limits = None
        self.embeddings = SimpleNamespace(embed_sync=self._embed)
        self.chroma = SimpleNamespace(query=self._query)

    def read(self, owner, task_id, run_id, revision):
        return {'chunk_manifest': self.chunk_manifest}

    def chroma_factory(self):
        return self.chroma

    def _embed(self, texts, progress, **limits):
        self.last_limits = limits
        vector = [1.0] + [0.0] * 1023
        return SimpleNamespace(vectors={text: vector for text in texts}, cache_hits=0,
                               batches=1, attempts=1)

    def _query(self, vector, *, snapshot_id, repository_id, limit=10):
        rows = [{'chunk_id': chunk['chunk_id'], 'distance': 0.1 + index * 0.01}
                for index, chunk in enumerate(self.chunk_manifest['snapshots'][0]['chunks'])]
        return rows[:limit]


def revision():
    requirements = RequirementJSON(
        project_topic=None, project_description=None, project_type=None, domain=None,
        required_features=['Build a project tracker.'], preferred_features=None,
        programming_languages=None, required_technologies=None, preferred_technologies=None,
        missing_information=[], uncertain_information=None, source_evidence=[])
    item = RequirementItem(requirement_item_id=uid(), field='required_features',
                           value_path='required_features[0]', text='Build a project tracker.',
                           modality='required', operator='not_applicable', logic_group_id=None,
                           source_kind='assignment_document', evidence_ids=[], supersedes_item_id=None)
    return RequirementRevision(
        revision_id=uid(), task_id=uid(), revision_number=1, parent_revision_id=None,
        status='confirmed', requirements=requirements, requirement_items=[item], change_log=[],
        created_at=NOW, confirmed_at=NOW)


def stable_manifest(tmp_path, rev, readme_bytes):
    storage = LocalStorage(tmp_path / 'objects')
    store = RunManifestStore(storage)
    run_id = uid()
    store.create(OWNER, run_id, str(rev.task_id), str(rev.revision_id))
    store.start(OWNER, run_id)
    plan_id = uid()
    plan = dict(plan_id=plan_id, plan_storage_key=f'search-plans/{plan_id}.json',
                plan_hash='e' * 64, confirmed_revision_id=str(rev.revision_id))
    store.bind_plan(OWNER, run_id, plan)
    store.publish_plan(OWNER, run_id, plan)
    operation_id = uid()
    key = snapshot_key(REAL['repository_id'], REAL['commit_sha'], REAL['readme_path'], REAL['blob_sha'])
    operation = dict(operation_id=operation_id, repository_id=REAL['repository_id'],
                     commit_sha=REAL['commit_sha'], readme_path=REAL['readme_path'],
                     blob_sha=REAL['blob_sha'], content_hash=REAL['content_hash'],
                     snapshot_key=key, storage_key=REAL['storage_key'],
                     operation_status='prepared', snapshot_id=REAL['snapshot_id'],
                     staging_key=f'staging/readmes/{operation_id}.raw', byte_length=len(readme_bytes),
                     created_at=NOW.isoformat(), updated_at=NOW.isoformat(),
                     collected_at=REAL['collected_at'], snapshot_key_algorithm=SNAPSHOT_KEY_ALGORITHM)
    store.register_operation(OWNER, run_id, operation)
    store.update(OWNER, run_id, lambda m: m['candidates'].update(
        {str(REAL['repository_id']): {'sources': ['fixture'], 'metadata': {
            'full_name': 'scorelab/Tracker', 'html_url': 'https://github.com/scorelab/Tracker',
            'stars': 30, 'description': 'Tracker project first for trains', 'language': 'JavaScript'}}}))
    for stage in ('staged', 'object_verified', 'snapshot_committed', 'completed'):
        store.advance_operation(OWNER, run_id, operation_id, stage, uid())
    store.seal_dispatch(OWNER, run_id)
    return storage, store, run_id


def revision_digest(rev):
    return hashlib.sha256(json.dumps(rev.model_dump(mode='json'), ensure_ascii=False,
                                     sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def install_chunk_index(store, run_id, rev, raw):
    digest = revision_digest(rev)
    normalized = normalize_readme(raw)
    report = chunk_readme(normalized, snapshot_id=REAL['snapshot_id'],
                          repository_id=REAL['repository_id'])
    for chunk in report['chunks']:
        chunk.update(commit_sha=REAL['commit_sha'], readme_path=REAL['readme_path'])
    document = {'version': 'chunk-index-v1', 'owner': OWNER, 'task_id': str(rev.task_id),
                'run_id': run_id, 'revision_hash': digest, 'plan_hash': '',
                'model': 'BAAI/bge-m3', 'dimension': 1024, 'preprocess_version': 'raw-v1',
                'snapshots': [{'snapshot_id': REAL['snapshot_id'],
                               'repository_id': str(REAL['repository_id']),
                               'commit_sha': REAL['commit_sha'], 'readme_path': REAL['readme_path'],
                               'content_hash': REAL['content_hash'], 'coverage': report['coverage'],
                               'chunk_count': report['chunk_count'],
                               'total_estimate_tokens': report['total_estimate_tokens'],
                               'chunks': report['chunks']}],
                'chunk_count': report['chunk_count'],
                'total_estimate_tokens': report['total_estimate_tokens']}
    index_key = uid()
    document['plan_hash'] = store.read(OWNER, run_id)['plan']['plan_hash']
    data = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    store.storage.put(f'chunk-index/{index_key}/manifest.json', data, 'application/json')
    pending = {'operation_id': uid(), 'version': 'chunk-index-v1', 'index_key': index_key,
               'revision_hash': digest, 'plan_hash': document['plan_hash'],
               'model': 'BAAI/bge-m3', 'dimension': 1024, 'preprocess_version': 'raw-v1',
               'config_hash': 'a' * 64, 'status': 'pending', 'chunk_manifest_key': None,
               'chunk_manifest_hash': None, 'usage': {}, 'error_code': None,
               'failed_operation_ids': [], 'created_at': NOW.isoformat(), 'updated_at': NOW.isoformat()}
    store.update(OWNER, run_id, lambda m: m.update(chunk_index_operation=pending))
    key = f'chunk-index/{index_key}/manifest.json'
    embedding = {**pending, 'status': 'embedding', 'chunk_manifest_key': key,
                 'chunk_manifest_hash': hashlib.sha256(data).hexdigest(),
                 'usage': {REAL['snapshot_id']: False}}
    store.update(OWNER, run_id, lambda m: m.update(chunk_index_operation=embedding))
    ready = {**embedding, 'status': 'ready', 'usage': {REAL['snapshot_id']: True}}
    store.update(OWNER, run_id, lambda m: m.update(chunk_index_operation=ready))
    return document, index_key


def test_bundle_v2_fixture_end_to_end(tmp_path):
    raw_path = ROOT / 'data' / 'objects' / REAL['storage_key']
    if not raw_path.exists():
        pytest.skip('real snapshot bytes are not available')
    raw = raw_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != REAL['content_hash']:
        pytest.skip('real snapshot metadata does not match local bytes')
    rev = revision()
    storage, store, run_id = stable_manifest(tmp_path, rev, raw)
    document, index_key = install_chunk_index(store, run_id, rev, raw)
    persistence = Persistence(storage, document)
    indexer = Indexer(document)
    service = EvidenceBundleService(persistence, indexer,
                                    SimpleNamespace(embedding_model='BAAI/bge-m3',
                                                    embedding_dimension=1024))
    bundle = service.publish(OWNER, rev.task_id, run_id, rev)
    manifest = store.read(OWNER, run_id)
    operation = manifest['evidence_bundle_operation']
    stored, _ = storage.read_version(operation['storage_key'])
    assert hashlib.sha256(stored).hexdigest() == operation['bundle_hash']
    assert bundle['version'] == 'evidence-bundle-v2' and bundle['schema_version'] == 2
    candidate = bundle['candidates'][0]
    for field in ('commit_sha', 'blob_sha', 'content_hash', 'readme_path', 'collected_at'):
        assert candidate[field] == REAL[field]
    assert indexer.last_limits['max_batches'] >= 2
    assert candidate['items'][0]['status'] == 'retrieved'
    assert candidate['items'][0]['relevance'] == 'unverified'
    again, _ = EvidenceBundleStore(storage).read(OWNER, run_id, rev)
    assert again == bundle

    # Real ASGI GET through the existing route with a fixture session.
    from fastapi.testclient import TestClient
    from app.main import create_app
    app = create_app(Settings(_env_file=None), storage)
    with TestClient(app, base_url='http://projectpilot.test') as client:
        token = client.post('/api/v1/sessions', json={}).json()['access_token']
        owner = hashlib.sha256(token.encode()).hexdigest()
        session_raw, version = storage.read_version(f'sessions/{owner}.json')
        session = json.loads(session_raw)
        session['tasks'][str(rev.task_id)] = {
            'id': str(rev.task_id), 'created_at': NOW.isoformat(), 'status': 'completed', 'document': None,
            'requirements': {'extractions': [], 'edit_fingerprints': {},
                             'revisions': [rev.model_dump(mode='json')]},
            'search_runs': {'fixture-key': {'run_id': run_id, 'task_id': str(rev.task_id),
                                            'confirmed_revision_id': str(rev.revision_id),
                                            'request_hash': 'f' * 64, 'status': 'completed',
                                            'pipeline_status': 'completed'}}}
        storage.compare_and_swap(f'sessions/{owner}.json', version,
                                 json.dumps(session, ensure_ascii=False, sort_keys=True,
                                            separators=(',', ':')).encode())
        # The manifest is owned by a different owner; rebind the fixture run under this token.
        fixture_raw, fixture_version = storage.read_version(f'runs/{OWNER}/{run_id}.json')
        fixture = json.loads(fixture_raw)
        fixture['owner'] = owner
        # Keep validators honest: this fixture is explicitly test data, not a real run.
        storage.put(f'runs/{owner}/{run_id}.json',
                    json.dumps(fixture, ensure_ascii=False, sort_keys=True,
                               separators=(',', ':')).encode(), 'application/json')
        response = client.get(f'/api/v1/tasks/{rev.task_id}/search-runs/{run_id}/retrieval-evidence',
                              headers={'Authorization': f'Bearer {token}'})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['schema_version'] == 2
        assert payload['candidates'][0]['commit_sha'] == REAL['commit_sha']
        filtered = client.get(f'/api/v1/tasks/{rev.task_id}/search-runs/{run_id}/retrieval-evidence',
                              headers={'Authorization': f'Bearer {token}'},
                              params={'repository_id': str(REAL['repository_id'])})
        assert filtered.status_code == 200
        assert filtered.json()['filters']['repository_id'] == str(REAL['repository_id'])

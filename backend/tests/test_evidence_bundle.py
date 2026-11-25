import pytest

from app.schemas.search import SearchIntegrityError
from app.services.evidence_bundle import snapshot_traces

COMMIT = 'c' * 40
BLOB = 'b' * 40
HASH = 'h' * 64


def manifest(snapshot_id='s1'):
    return {'candidates': {'7': {'metadata': {'full_name': 'owner/repo', 'html_url': 'https://github.com/owner/repo',
                                              'stars': 3, 'description': 'd', 'language': 'Python'}}},
            'readme_persistence_operations': {'op': {
                'operation_status': 'completed', 'snapshot_id': snapshot_id, 'repository_id': 7,
                'commit_sha': COMMIT, 'blob_sha': BLOB, 'content_hash': HASH,
                'readme_path': 'docs/README.md', 'collected_at': '2026-01-01T00:00:00+00:00'}}}


class Repository:
    def __init__(self, **overrides):
        self.value = {'snapshot_id': 's1', 'commit_sha': COMMIT, 'blob_sha': BLOB,
                      'content_hash': HASH, 'readme_path': 'docs/README.md'}
        self.value.update(overrides)

    def get_snapshot_by_id(self, snapshot_id):
        return dict(self.value)


def report(**overrides):
    row = {'repository_id': '7', 'snapshot_id': 's1', 'coverage': 'full', 'chunks': [],
           'commit_sha': COMMIT, 'content_hash': HASH, 'readme_path': 'docs/README.md'}
    row.update(overrides)
    return {'snapshots': [row]}


def test_trace_fields_come_from_manifest_and_sql():
    traces = snapshot_traces(manifest(), Repository(), report())
    assert traces[0] == {'repository_id': '7', 'snapshot_id': 's1', 'coverage': 'full', 'chunks': [],
                         'readme_path': 'docs/README.md', 'commit_sha': COMMIT, 'blob_sha': BLOB,
                         'content_hash': HASH, 'collected_at': '2026-01-01T00:00:00+00:00',
                         'repository': {'full_name': 'owner/repo', 'html_url': 'https://github.com/owner/repo',
                                        'stars': 3, 'description': 'd', 'language': 'Python'}}


@pytest.mark.parametrize('overrides', [
    {'commit_sha': 'x' * 40}, {'blob_sha': 'x' * 40}, {'content_hash': 'x' * 64},
    {'readme_path': 'README.md'},
])
def test_sql_snapshot_mismatch_is_refused(overrides):
    with pytest.raises(SearchIntegrityError):
        snapshot_traces(manifest(), Repository(**overrides), report())


@pytest.mark.parametrize('overrides', [
    {'commit_sha': 'x' * 40}, {'content_hash': 'x' * 64}, {'readme_path': 'README.md'},
])
def test_chunk_manifest_mismatch_is_refused(overrides):
    with pytest.raises(SearchIntegrityError):
        snapshot_traces(manifest(), Repository(), report(**overrides))


def test_missing_readme_operation_is_refused():
    with pytest.raises(SearchIntegrityError):
        snapshot_traces(manifest(), Repository(), report(snapshot_id='missing'))

import pytest

from app.services.chroma_store import ChromaStore, ChromaStoreError

DIM = 4


def store(tmp_path, **overrides):
    values = dict(model='BAAI/bge-m3', dimension=DIM, version='BAAI/bge-m3-4-v1')
    values.update(overrides)
    return ChromaStore.persistent(tmp_path, **values)


def chunk(chunk_id, snapshot_id, repository_id, ordinal=0, digest=None):
    return {'chunk_id': chunk_id, 'snapshot_id': snapshot_id, 'repository_id': str(repository_id),
            'commit_sha': 'c' * 40, 'readme_path': 'README.md', 'ordinal': ordinal,
            'heading_path': ['Usage', 'Export'], 'line_start': 3, 'line_end': 5,
            'byte_start': 10, 'byte_end': 50,
            'content_hash': digest or f'hash-{chunk_id}', 'text': 'ignored here'}


def vectors(ids, values):
    return {chunk_id: value for chunk_id, value in zip(ids, values)}


def test_upsert_count_and_idempotent(tmp_path):
    svc = store(tmp_path)
    chunks = [chunk('a', 's1', 1, 0), chunk('b', 's1', 1, 1)]
    assert svc.upsert(chunks, vectors(['a', 'b'], [[1, 0, 0, 0], [0, 1, 0, 0]])) == 2
    assert svc.count() == 2 and svc.count('s1') == 2
    svc.upsert(chunks, vectors(['a', 'b'], [[1, 0, 0, 0], [0, 1, 0, 0]]))
    assert svc.count() == 2


def test_query_is_filtered_by_snapshot_and_repository(tmp_path):
    svc = store(tmp_path)
    svc.upsert([chunk('a', 's1', 1), chunk('b', 's2', 1), chunk('c', 's1', 2)],
               vectors(['a', 'b', 'c'], [[1, 0, 0, 0], [1, 0, 0, 0], [1, 0, 0, 0]]))
    rows = svc.query([1, 0, 0, 0], snapshot_id='s1', repository_id='1')
    assert [row['chunk_id'] for row in rows] == ['a']
    assert rows[0]['metadata']['snapshot_id'] == 's1'
    assert rows[0]['metadata']['repository_id'] == '1'
    assert 'run_id' not in rows[0]['metadata']
    assert rows[0]['metadata']['model_version'] == 'BAAI/bge-m3-4-v1'


def test_distance_semantics_lower_is_closer(tmp_path):
    svc = store(tmp_path)
    svc.upsert([chunk('near', 's1', 1), chunk('far', 's1', 1)],
               vectors(['near', 'far'], [[1, 0, 0, 0], [0, 1, 0, 0]]))
    rows = svc.query([1, 0, 0, 0], snapshot_id='s1', repository_id=1, limit=2)
    assert rows[0]['chunk_id'] == 'near' and rows[0]['distance'] < rows[1]['distance']
    assert 0 <= rows[0]['distance'] < 0.001


def test_verify_detects_missing_extra_and_tamper(tmp_path):
    svc = store(tmp_path)
    svc.upsert([chunk('a', 's1', 1), chunk('b', 's1', 1)], vectors(['a', 'b'], [[1, 0, 0, 0], [0, 1, 0, 0]]))
    good = svc.verify('s1', {'a': 'hash-a', 'b': 'hash-b'})
    assert good['ok'] is True
    bad = svc.verify('s1', {'a': 'hash-a', 'c': 'hash-c'})
    assert bad['ok'] is False and bad['missing'] == ['c'] and bad['extra'] == ['b']
    tampered = svc.verify('s1', {'a': 'other', 'b': 'hash-b'})
    assert tampered['mismatched'] == ['a']


def test_invalid_vector_and_metadata_are_refused(tmp_path):
    svc = store(tmp_path)
    with pytest.raises(ChromaStoreError):
        svc.upsert([chunk('a', 's1', 1)], {'a': [1, 0]})
    with pytest.raises(ChromaStoreError):
        svc.upsert([chunk('a', 's1', 1)], {'a': [0, 0, 0, 0]})
    broken = chunk('a', 's1', 1)
    broken['readme_path'] = ''
    with pytest.raises(ChromaStoreError):
        svc.upsert([broken], {'a': [1, 0, 0, 0]})


def test_collections_are_isolated_by_model_and_version(tmp_path):
    first = store(tmp_path)
    second = store(tmp_path, model='BAAI/bge-m3', dimension=8)
    assert first.name != second.name
    first.upsert([chunk('a', 's1', 1)], {'a': [1, 0, 0, 0]})
    assert second.count() == 0


def test_persistence_survives_new_client(tmp_path):
    store(tmp_path).upsert([chunk('a', 's1', 1)], {'a': [1, 0, 0, 0]})
    assert store(tmp_path).count() == 1

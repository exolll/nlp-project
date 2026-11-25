import pytest

from app.services.retrieval import (FINAL_TOP, WARNING, bm25_scores, keyword_scores,
                                    reciprocal_rank_fusion, retrieve, tokenize)


def chunk(chunk_id, text, snapshot='s1', repo='1', start=1):
    return {'chunk_id': chunk_id, 'text': text, 'snapshot_id': snapshot, 'repository_id': repo,
            'line_start': start, 'line_end': start + 1, 'byte_start': 0, 'byte_end': 10}


def requirement(text, item='r1'):
    return {'requirement_item_id': item, 'text': text, 'modality': 'required',
            'operator': 'not_applicable', 'logic_group_id': None}


def snapshots():
    return [{'repository_id': '1', 'snapshot_id': 's1', 'coverage': 'full',
             'chunks': [chunk('a', 'upload audio file through the web page'),
                        chunk('b', 'export the transcript as a text file', start=5),
                        chunk('c', 'completely unrelated content about cooking', start=9)]},
            {'repository_id': '2', 'snapshot_id': 's2', 'coverage': 'full',
             'chunks': [chunk('d', 'audio transcription timestamps', snapshot='s2', repo='2')]}]


def fake_embed(texts):
    return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def dense(vector, snapshot_id, repository_id, limit):
    table = {'s1': [{'chunk_id': 'b', 'distance': 0.1}, {'chunk_id': 'a', 'distance': 0.2}],
             's2': [{'chunk_id': 'd', 'distance': 0.3}]}
    return table.get(snapshot_id, [])[:limit]


def test_tokenize_handles_ascii_and_chinese_bigrams():
    tokens = tokenize('Export 转录 timestamps')
    assert 'export' in tokens and 'timestamps' in tokens
    assert '转录' in tokens


def test_bm25_ranks_matching_chunk_and_ignores_non_matches():
    documents = [('a', 'export transcript as text'), ('b', 'cooking recipes')]
    ranked = bm25_scores('export transcript', documents)
    assert ranked and ranked[0][0] == 'a' and all(chunk_id != 'b' for chunk_id, _ in ranked)
    assert bm25_scores('nonexistentterm', documents) == []


def test_keyword_scores_are_literal_only():
    documents = [('a', 'Users can export the transcript'), ('b', 'Cooking with butter')]
    assert [chunk_id for chunk_id, _ in keyword_scores('export transcript', documents)] == ['a']


def test_rrf_uses_rank_not_raw_scale():
    fused = dict(reciprocal_rank_fusion({
        'dense': [('a', 0.01), ('b', 0.99)],
        'bm25': [('c', 1000.0), ('a', 1.0)],
    }))
    assert list(fused) == ['a', 'b', 'c'] or list(fused) == ['a', 'c', 'b']
    assert fused['a']['score'] > fused['c']['score']


def test_retrieve_fuses_three_methods_and_respects_snapshot_isolation():
    bundle = retrieve([requirement('export transcript as text file')], snapshots(),
                      embed=fake_embed, dense_query=dense)
    assert bundle['warning'] == WARNING
    first, second = bundle['candidates']
    assert first['snapshot_id'] == 's1' and second['snapshot_id'] == 's2'
    ids = {row['chunk_id'] for row in first['items'][0]['chunks']}
    assert 'd' not in ids
    assert ids.issubset({'a', 'b'})
    assert first['items'][0]['chunks'][0]['chunk_id'] == 'b'
    assert 'dense' in first['items'][0]['chunks'][0]['methods']
    assert first['items'][0]['chunks'][0]['score_direction'] == 'higher_is_better'


def test_final_top_and_empty_reason():
    many = [chunk(f'c{i}', f'export transcript number {i}', start=i) for i in range(9)]
    bundle = retrieve([requirement('export transcript')],
                      [{'repository_id': '1', 'snapshot_id': 's1', 'coverage': 'full', 'chunks': many}],
                      embed=fake_embed, dense_query=lambda *args: [], final_top=3)
    assert len(bundle['candidates'][0]['items'][0]['chunks']) == 3
    empty = retrieve([requirement('不存在的需求词汇')],
                     [{'repository_id': '1', 'snapshot_id': 's1', 'coverage': 'full',
                       'chunks': [chunk('a', 'cooking recipes')]}],
                     embed=fake_embed, dense_query=lambda *args: [])
    assert empty['candidates'][0]['items'][0]['empty_reason'] == 'no_match_in_saved_snapshot'


def test_bundle_carries_requirement_projection_and_config():
    bundle = retrieve([requirement('export')], snapshots(), embed=fake_embed, dense_query=dense)
    assert bundle['requirements'][0]['requirement_item_id'] == 'r1'
    assert bundle['config'] == {'dense_top': 10, 'bm25_top': 10, 'final_top': FINAL_TOP, 'rrf_k': 60}
    item = bundle['candidates'][0]['items'][0]
    assert item['status'] == 'retrieved' and item['relevance'] == 'unverified'


def test_retrieved_and_empty_are_the_only_item_statuses():
    retrieved = retrieve([requirement('export')], snapshots(), embed=fake_embed, dense_query=dense)
    assert retrieved['candidates'][0]['items'][0]['status'] == 'retrieved'
    empty = retrieve([requirement('完全无关的需求')],
                     [{'repository_id': '1', 'snapshot_id': 's1', 'coverage': 'full',
                       'chunks': [chunk('a', 'cooking recipes')]}],
                     embed=fake_embed, dense_query=lambda *args: [])
    item = empty['candidates'][0]['items'][0]
    assert item['status'] == 'empty' and item['relevance'] == 'unverified' and not item['chunks']


def test_snapshot_trace_fields_are_carried_to_candidates():
    traced = snapshots()
    for row in traced:
        row.update(commit_sha='c' * 40, blob_sha='b' * 40, content_hash='h' * 64,
                   collected_at='2026-01-01T00:00:00+00:00')
    bundle = retrieve([requirement('export')], traced, embed=fake_embed, dense_query=dense)
    for row in bundle['candidates']:
        assert row['commit_sha'] == 'c' * 40 and row['blob_sha'] == 'b' * 40
        assert row['content_hash'] == 'h' * 64 and row['collected_at'].startswith('2026-01-01')


def test_dense_failure_degrades_to_partial_but_keeps_keyword_hits():
    def failing_dense(vector, snapshot_id, repository_id, limit):
        if snapshot_id == 's1':
            raise RuntimeError('chroma unavailable')
        return dense(vector, snapshot_id, repository_id, limit)

    bundle = retrieve([requirement('export transcript')], snapshots(),
                      embed=fake_embed, dense_query=failing_dense)
    first = next(row for row in bundle['candidates'] if row['snapshot_id'] == 's1')
    assert first['status'] == 'partial' and first['error_code'] == 'DENSE_RETRIEVAL_FAILED'
    assert first['items'][0]['chunks'], 'BM25/keyword evidence must survive a dense failure'


def test_failed_when_dense_fails_and_nothing_is_recalled():
    def failing_dense(*args):
        raise RuntimeError('chroma unavailable')

    bundle = retrieve([requirement('完全无关的需求')],
                      [{'repository_id': '1', 'snapshot_id': 's1', 'coverage': 'full',
                        'chunks': [chunk('a', 'cooking recipes')]}],
                      embed=fake_embed, dense_query=failing_dense)
    row = bundle['candidates'][0]
    assert row['status'] == 'failed' and row['error_code'] == 'DENSE_RETRIEVAL_FAILED'


def _bundle_for_view():
    bundle = retrieve([requirement('export transcript'), requirement('audio upload', 'r2')],
                      snapshots(), embed=fake_embed, dense_query=dense)
    bundle.update(task_id='task', run_id='run', confirmed_revision_id='rev', status='ready',
                  warning=WARNING, created_at='2026-01-01T00:00:00+00:00', model='BAAI/bge-m3',
                  dimension=1024, empty_reason=None, query_usage={'tokens': 1})
    return bundle


def test_retrieval_view_filters_share_one_envelope():
    from app.services.search_routes import retrieval_view

    full = retrieval_view(_bundle_for_view())
    assert len(full['candidates']) == 2 and len(full['requirements']) == 2
    assert full['notice'] and 'filters' in full
    by_repo = retrieval_view(_bundle_for_view(), repository_id='2')
    assert [row['repository_id'] for row in by_repo['candidates']] == ['2']
    assert by_repo['filters']['repository_id'] == '2'
    by_item = retrieval_view(_bundle_for_view(), requirement_item_id='r2')
    assert [row['requirement_item_id'] for row in by_item['requirements']] == ['r2']
    assert all(len(row['items']) == 1 and row['items'][0]['requirement_item_id'] == 'r2'
               for row in by_item['candidates'])
    with pytest.raises(Exception) as error:
        retrieval_view(_bundle_for_view(), repository_id='999')
    assert getattr(error.value, 'status', None) == 404


def test_retrieval_chunks_never_claim_satisfaction_or_percentages():
    bundle = retrieve([requirement('export')], snapshots(), embed=fake_embed, dense_query=dense)
    for row in bundle['candidates']:
        for item in row['items']:
            for entry in item['chunks']:
                assert entry['score_direction'] == 'higher_is_better'
                assert 'percent' not in entry and 'satisfied' not in entry
    assert '不是需求满足证据' in WARNING

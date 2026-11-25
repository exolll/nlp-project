"""Dense + BM25 + literal keyword retrieval fused by rank (RRF). Relevance only, never proof."""
import math
import re

from app.services.candidate_review import _tokens

VERSION = 'evidence-bundle-v2'
DENSE_TOP = 10
BM25_TOP = 10
FINAL_TOP = 5
RRF_K = 60
BM25_K1 = 1.2
BM25_B = 0.75
WARNING = ('召回候选片段只表示“可能相关”，相关性尚未核查，也不是需求满足证据；'
           '相似度与排名不进入推荐总分。未召回不代表项目不满足，只说明该片段未命中。')

_ASCII = re.compile(r'[a-z0-9_+#.-]+')
_CJK = re.compile(r'[\u3400-\u9fff]')


def tokenize(text):
    lowered = text.lower()
    tokens = [match.group(0) for match in _ASCII.finditer(lowered)]
    characters = _CJK.findall(text)
    tokens.extend(characters[index] + characters[index + 1] for index in range(len(characters) - 1))
    tokens.extend(characters)
    return [token for token in tokens if token.strip()]


def bm25_scores(query, documents):
    """documents: [(chunk_id, text)] -> [(chunk_id, score)] sorted by score then id."""
    query_tokens = tokenize(query)
    if not query_tokens or not documents:
        return []
    tokenized = [(chunk_id, tokenize(text)) for chunk_id, text in documents]
    total = len(tokenized)
    average = sum(len(tokens) for _, tokens in tokenized) / total or 1
    frequencies = {}
    for _, tokens in tokenized:
        for token in set(tokens):
            frequencies[token] = frequencies.get(token, 0) + 1
    scores = []
    for chunk_id, tokens in tokenized:
        score = 0.0
        for token in set(query_tokens):
            count = tokens.count(token)
            if not count:
                continue
            document_frequency = frequencies.get(token, 0)
            idf = math.log(1 + (total - document_frequency + 0.5) / (document_frequency + 0.5))
            score += idf * count * (BM25_K1 + 1) / (
                count + BM25_K1 * (1 - BM25_B + BM25_B * len(tokens) / average))
        if score:
            scores.append((chunk_id, score))
    return sorted(scores, key=lambda pair: (-pair[1], pair[0]))


def keyword_scores(query, documents):
    wanted = set(_tokens(query))
    if not wanted:
        return []
    scores = []
    for chunk_id, text in documents:
        haystack = ' '.join(tokenize(text))
        hits = sum(1 for token in wanted if token in haystack)
        if hits:
            scores.append((chunk_id, float(hits)))
    return sorted(scores, key=lambda pair: (-pair[1], pair[0]))


def reciprocal_rank_fusion(rankings, k=RRF_K):
    """rankings: {method: [(chunk_id, score)] ranked best-first}; different scales are never added."""
    fused = {}
    for method, rows in rankings.items():
        seen = set()
        rank = 0
        for chunk_id, _score in rows:
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            rank += 1
            entry = fused.setdefault(chunk_id, {'score': 0.0, 'ranks': {}, 'distances': {}})
            entry['score'] += 1.0 / (k + rank)
            entry['ranks'][method] = rank
            entry['distances'][method] = _score
    return sorted(fused.items(), key=lambda pair: (-pair[1]['score'], pair[0]))


def _value(item, key):
    return item.get(key) if isinstance(item, dict) else getattr(item, key, None)


def retrieve(requirements, snapshots, *, embed, dense_query, dense_top=DENSE_TOP,
             bm25_top=BM25_TOP, final_top=FINAL_TOP, k=RRF_K):
    """requirements: item dicts or objects; snapshots: [{repository_id,snapshot_id,coverage,repository,chunks}]."""
    queries = [_value(item, 'text') for item in requirements]
    vectors = embed(queries) if queries else []
    rows = []
    for snapshot in sorted(snapshots, key=lambda row: int(row['repository_id'])):
        chunks = snapshot['chunks']
        documents = [(chunk['chunk_id'], chunk['text']) for chunk in chunks]
        by_id = {chunk['chunk_id']: chunk for chunk in chunks}
        failures = []
        items = []
        for item, vector in zip(requirements, vectors):
            try:
                dense_rows = dense_query(vector, snapshot['snapshot_id'], snapshot['repository_id'], dense_top)
                dense = [(row['chunk_id'], float(row['distance'])) for row in dense_rows
                         if row['chunk_id'] in by_id][:dense_top]
            except Exception:
                dense, dense_rows = [], []
                if 'DENSE_RETRIEVAL_FAILED' not in failures:
                    failures.append('DENSE_RETRIEVAL_FAILED')
            bm25 = [(chunk_id, score) for chunk_id, score in bm25_scores(_value(item, 'text'), documents)[:bm25_top]
                    if chunk_id in by_id]
            keyword = [(chunk_id, score) for chunk_id, score in keyword_scores(_value(item, 'text'), documents)[:bm25_top]
                       if chunk_id in by_id]
            fused = reciprocal_rank_fusion({'dense': dense, 'bm25': bm25, 'keyword': keyword}, k)[:final_top]
            chunks_out = []
            for chunk_id, entry in fused:
                chunk = by_id[chunk_id]
                chunks_out.append({'chunk_id': chunk_id, 'text': chunk['text'],
                                   'line_start': chunk['line_start'], 'line_end': chunk['line_end'],
                                   'byte_start': chunk['byte_start'], 'byte_end': chunk['byte_end'],
                                   'methods': sorted(entry['ranks']),
                                   'ranks': entry['ranks'],
                                   'dense_distance': entry['distances'].get('dense'),
                                   'rrf_score': round(entry['score'], 8),
                                   'score_direction': 'higher_is_better'})
            items.append({'requirement_item_id': _value(item, 'requirement_item_id'),
                          'text': _value(item, 'text'),
                          'chunks': chunks_out,
                          'status': 'retrieved' if chunks_out else 'empty',
                          'relevance': 'unverified',
                          'empty_reason': None if chunks_out else 'no_match_in_saved_snapshot'})
        found = sum(1 for item in items if item['chunks'])
        if failures and not found:
            status, error = 'failed', failures[0]
        elif failures or found < len(items):
            status, error = 'partial', failures[0] if failures else None
        else:
            status, error = 'ok', None
        rows.append({'repository_id': snapshot['repository_id'], 'snapshot_id': snapshot['snapshot_id'],
                     'coverage': snapshot.get('coverage', 'full'),
                     'readme_path': snapshot.get('readme_path'),
                     'commit_sha': snapshot.get('commit_sha'),
                     'blob_sha': snapshot.get('blob_sha'),
                     'content_hash': snapshot.get('content_hash'),
                     'collected_at': snapshot.get('collected_at'),
                     'repository': snapshot.get('repository'),
                     'status': status, 'error_code': error, 'items': items})
    return {'version': VERSION, 'model': None, 'dimension': None,
            'config': {'dense_top': dense_top, 'bm25_top': bm25_top, 'final_top': final_top, 'rrf_k': k},
            'warning': WARNING,
            'requirements': [{key: _value(item, key) for key in
                              ('requirement_item_id', 'field', 'text', 'modality', 'operator', 'logic_group_id')}
                             for item in requirements],
            'candidates': rows}

import hashlib

import pytest

from app.services.chunking import (CHUNK_TARGET_TOKENS, ChunkingError, chunk_manifest_hash, chunk_readme,
                                   estimate_tokens, normalize_readme)

SAMPLE = '''# Audio Helper

A tool that turns audio into text.

## Features

- Upload an audio file through the web page.
- Export the transcript as a `.txt` file.
- 不得使用付费云 API。

### Details

| field | meaning |
|---|---|
| time | per segment |

```python
def export(path):
    return path + ".txt"
```

## Limits

Runs locally without a paid API.
'''


def manifest(text, **kwargs):
    return chunk_readme(normalize_readme(text.encode()), snapshot_id='snap-1', repository_id=7, **kwargs)


def assert_offsets(normalized, chunks):
    raw = normalized.encode()
    for chunk in chunks:
        assert raw[chunk['byte_start']:chunk['byte_end']].decode() == chunk['text']
        assert chunk['line_start'] <= chunk['line_end']
        assert hashlib.sha256(chunk['text'].encode()).hexdigest() == chunk['content_hash']


def test_offsets_and_line_numbers_roundtrip():
    normalized = normalize_readme(SAMPLE.encode())
    result = chunk_readme(normalized, snapshot_id='snap-1', repository_id=7)
    assert result['coverage'] == 'full'
    assert result['chunk_count'] == len(result['chunks']) and result['chunk_count'] > 0
    assert_offsets(normalized, result['chunks'])
    assert len({chunk['chunk_id'] for chunk in result['chunks']}) == result['chunk_count']


def test_code_fence_and_table_stay_whole():
    result = manifest(SAMPLE)
    for chunk in result['chunks']:
        if '```python' in chunk['text']:
            assert '```' in chunk['text'].split('```python', 1)[1]
        if '| field |' in chunk['text']:
            assert '| time |' in chunk['text']


def test_heading_path_and_negation_are_preserved():
    result = manifest(SAMPLE)
    feature = next(chunk for chunk in result['chunks'] if '不得使用付费云 API' in chunk['text'])
    assert feature['heading_path'][-1] in ('Features', 'Details')
    assert '不得使用付费云 API。' in feature['text']
    details = next(chunk for chunk in result['chunks'] if '| field |' in chunk['text'])
    assert details['heading_path'] == ['Audio Helper', 'Features', 'Details']


def test_crlf_is_normalized_before_offsets():
    raw = SAMPLE.replace('\n', '\r\n').encode()
    normalized = normalize_readme(raw)
    result = chunk_readme(normalized, snapshot_id='snap-1', repository_id=7)
    assert_offsets(normalized, result['chunks'])
    assert '\r' not in normalized


def test_chunk_limits_mark_truncated():
    text = '\n\n'.join(f'## Section {i}\n\n' + ('word ' * 300) for i in range(20))
    result = chunk_readme(normalize_readme(text.encode()), snapshot_id='snap-1', repository_id=7, max_chunks=5)
    assert result['chunk_count'] == 5
    assert result['coverage'] == 'truncated'


def test_oversized_single_line_is_split_by_chars():
    line = '必须支持转录' * 400
    result = chunk_readme(line, snapshot_id='snap-1', repository_id=7)
    assert len(result['chunks']) > 1
    assert all(chunk['split'] == 'chars' for chunk in result['chunks'])
    assert all(chunk['token_estimate'] <= CHUNK_TARGET_TOKENS + 64 for chunk in result['chunks'])
    for chunk in result['chunks']:
        assert line.encode()[chunk['byte_start']:chunk['byte_end']].decode() == chunk['text']


def test_empty_and_non_utf8_inputs():
    empty = chunk_readme('  \n\n', snapshot_id='snap-1', repository_id=7)
    assert empty['coverage'] == 'empty' and empty['chunks'] == []
    with pytest.raises(ChunkingError):
        normalize_readme(b'\xff\xfe')


def test_manifest_hash_is_stable_and_content_sensitive():
    first = manifest(SAMPLE)
    second = manifest(SAMPLE)
    assert chunk_manifest_hash(first) == chunk_manifest_hash(second)
    changed = dict(first, coverage='truncated')
    assert chunk_manifest_hash(changed) != chunk_manifest_hash(first)


def test_token_estimate_is_conservative_for_chinese():
    assert estimate_tokens('你好世界') == 4
    assert estimate_tokens('abcd') == 1
    assert estimate_tokens('中文abc') == 3


def test_paragraph_overlap_stays_inside_previous_chunk():
    text = '\n\n'.join('句子' * 150 for _ in range(6))
    result = chunk_readme(normalize_readme(text.encode()), snapshot_id='snap-1', repository_id=7)
    assert result['chunk_count'] > 1
    for previous, current in zip(result['chunks'], result['chunks'][1:]):
        # Paragraphs are separated by blank lines, so at most one line may be skipped.
        assert current['line_start'] <= previous['line_end'] + 2
        assert current['byte_start'] >= previous['byte_start']

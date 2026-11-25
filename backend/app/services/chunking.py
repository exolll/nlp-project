"""Markdown-aware README chunking with byte/line traceability. No model calls, no I/O."""
import hashlib
import math
import re
from dataclasses import dataclass

VERSION = 'readme-chunks-v1'
CHUNK_TARGET_TOKENS = 512
CHUNK_OVERLAP_TOKENS = 64
MAX_CHUNKS_PER_REPOSITORY = 100
MAX_CHUNKS_PER_RUN = 1000
MAX_LINE_BYTES = 2000

_HEADING = re.compile(r'^(#{1,6})\s+(.+)$')
_LIST = re.compile(r'^\s*(?:[-*+]|\d+\.)\s+')
_FENCE = re.compile(r'^\s*(```|~~~)')


class ChunkingError(ValueError):
    pass


def estimate_tokens(text: str) -> int:
    """Conservative: every CJK/full-width char = 1 token, ASCII runs = 4 chars per token."""
    wide = sum(1 for char in text if ord(char) > 127)
    return wide + math.ceil((len(text) - wide) / 4)


def normalize_readme(raw: bytes) -> str:
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ChunkingError('README 不是 UTF-8 文本。') from None
    return text.replace('\r\n', '\n').replace('\r', '\n')


@dataclass(frozen=True)
class Block:
    start: int  # 1-based inclusive line numbers
    end: int
    kind: str


def _blocks(lines: list[str]) -> list[Block]:
    blocks, index, total = [], 0, len(lines)
    while index < total:
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            end = index + 1
            while end < total and not lines[end].lstrip().startswith(marker):
                end += 1
            end = min(end, total - 1)
            blocks.append(Block(index + 1, end + 1, 'code'))
            index = end + 1
            continue
        if line.lstrip().startswith('|'):
            end = index
            while end + 1 < total and lines[end + 1].lstrip().startswith('|'):
                end += 1
            blocks.append(Block(index + 1, end + 1, 'table'))
            index = end + 1
            continue
        if _HEADING.match(line):
            blocks.append(Block(index + 1, index + 1, 'heading'))
            index += 1
            continue
        if _LIST.match(line):
            end = index
            while end + 1 < total and lines[end + 1].strip() and not any(
                    pattern.match(lines[end + 1]) for pattern in (_HEADING, _FENCE, _LIST)) \
                    and not lines[end + 1].lstrip().startswith('|'):
                end += 1
            blocks.append(Block(index + 1, end + 1, 'list'))
            index = end + 1
            continue
        end = index
        while end + 1 < total and lines[end + 1].strip() and not any(
                pattern.match(lines[end + 1]) for pattern in (_HEADING, _FENCE, _LIST)) \
                and not lines[end + 1].lstrip().startswith('|'):
            end += 1
        blocks.append(Block(index + 1, end + 1, 'paragraph'))
        index = end + 1
    return blocks


def _heading_update(path: list[tuple[int, str]], line: str):
    match = _HEADING.match(line)
    if not match:
        return
    level, title = len(match.group(1)), match.group(2).strip()
    while path and path[-1][0] >= level:
        path.pop()
    path.append((level, title))


def _line_offsets(lines: list[str]) -> list[int]:
    offsets, position = [], 0
    for line in lines:
        offsets.append(position)
        position += len(line.encode('utf-8')) + 1
    return offsets


def _split_oversized_block(block: Block, lines: list[str]) -> list[tuple[int, int]]:
    ranges, start, size = [], block.start, 0
    for number in range(block.start, block.end + 1):
        line_size = estimate_tokens(lines[number - 1]) + 1
        if size and size + line_size > CHUNK_TARGET_TOKENS:
            ranges.append((start, number - 1))
            start, size = number, 0
        size += line_size
    if start <= block.end:
        ranges.append((start, block.end))
    return ranges


class _Builder:
    def __init__(self, normalized, snapshot_id, repository_id, max_chunks):
        self.normalized = normalized
        self.raw = normalized.encode('utf-8')
        self.lines = normalized.split('\n')
        self.offsets = _line_offsets(self.lines)
        self.snapshot_id, self.repository_id, self.max_chunks = snapshot_id, str(repository_id), max_chunks
        self.chunks, self.truncated, self.total_estimate = [], False, 0

    def line_text(self, start, end):
        return '\n'.join(self.lines[start - 1:end])

    def line_span(self, start, end):
        return self.offsets[start - 1], self.offsets[end - 1] + len(self.lines[end - 1].encode('utf-8'))

    def emit(self, start, end, path, split):
        if len(self.chunks) >= self.max_chunks:
            self.truncated = True
            return False
        text = self.line_text(start, end)
        if not text.strip():
            return True
        byte_start, byte_end = self.line_span(start, end)
        if self.raw[byte_start:byte_end].decode('utf-8') != text:
            raise ChunkingError('分块偏移无法回查原文。')
        tokens = estimate_tokens(text)
        self.chunks.append({
            'chunk_id': f'{self.snapshot_id}:{len(self.chunks)}:{hashlib.sha256(text.encode()).hexdigest()[:16]}',
            'snapshot_id': self.snapshot_id, 'repository_id': self.repository_id,
            'ordinal': len(self.chunks), 'heading_path': [title for _, title in path],
            'text': text, 'content_hash': hashlib.sha256(text.encode()).hexdigest(),
            'byte_start': byte_start, 'byte_end': byte_end,
            'line_start': start, 'line_end': end,
            'token_estimate': tokens, 'split': split,
        })
        self.total_estimate += tokens
        return True

    def emit_oversized_line(self, number, path):
        line = self.lines[number - 1]
        base = self.offsets[number - 1]
        target = max(1, int(CHUNK_TARGET_TOKENS * len(line) / max(1, estimate_tokens(line))))
        position = 0
        while position < len(line):
            piece = line[position:position + target]
            prefix = len(line[:position].encode('utf-8'))
            if not piece.strip():
                position += len(piece)
                continue
            tokens = estimate_tokens(piece)
            self.chunks.append({
                'chunk_id': f'{self.snapshot_id}:{len(self.chunks)}:{hashlib.sha256(piece.encode()).hexdigest()[:16]}',
                'snapshot_id': self.snapshot_id, 'repository_id': self.repository_id,
                'ordinal': len(self.chunks), 'heading_path': [title for _, title in path],
                'text': piece, 'content_hash': hashlib.sha256(piece.encode()).hexdigest(),
                'byte_start': base + prefix, 'byte_end': base + prefix + len(piece.encode('utf-8')),
                'line_start': number, 'line_end': number,
                'token_estimate': tokens, 'split': 'chars',
            })
            self.total_estimate += tokens
            position += len(piece)
            if len(self.chunks) >= self.max_chunks:
                self.truncated = True
                return True
        return True


def chunk_readme(normalized: str, *, snapshot_id: str, repository_id, max_chunks=MAX_CHUNKS_PER_REPOSITORY):
    """Split LF-normalized README text; offsets always refer to that normalized text."""
    if not isinstance(normalized, str) or not normalized.strip():
        return {'version': VERSION, 'snapshot_id': snapshot_id, 'repository_id': str(repository_id),
                'coverage': 'empty', 'chunks': [], 'chunk_count': 0, 'total_estimate_tokens': 0}
    max_chunks = max(1, min(int(max_chunks), MAX_CHUNKS_PER_REPOSITORY))
    builder = _Builder(normalized, snapshot_id, repository_id, max_chunks)
    blocks = _blocks(builder.lines)
    path: list[tuple[int, str]] = []
    start = end = None
    current_tokens = 0
    current_path: list[tuple[int, str]] = []
    last_kind = ''

    def flush():
        nonlocal start, end, current_tokens, current_path
        if start is not None:
            builder.emit(start, end, current_path, 'block')
        start = end = None
        current_tokens = 0
        current_path = []

    for block in blocks:
        text = builder.line_text(block.start, block.end)
        if block.kind == 'heading':
            _heading_update(path, builder.lines[block.start - 1])
            if start is not None:
                flush()
        tokens = estimate_tokens(text)
        if block.start == block.end and tokens > CHUNK_TARGET_TOKENS:
            if start is not None:
                flush()
            builder.emit_oversized_line(block.start, path)
            last_kind = block.kind
            if builder.truncated:
                break
            continue
        if tokens > CHUNK_TARGET_TOKENS:
            if start is not None:
                flush()
            for sub_start, sub_end in _split_oversized_block(block, builder.lines):
                builder.emit(sub_start, sub_end, path, 'lines')
                if builder.truncated:
                    break
            continue
        if start is None:
            start, end, current_tokens, current_path = block.start, block.end, tokens, list(path)
        elif current_tokens + tokens <= CHUNK_TARGET_TOKENS:
            end, current_tokens = block.end, current_tokens + tokens
        else:
            previous_end, previous_start, previous_path = end, start, current_path
            flush()
            overlap = 0
            if last_kind in ('paragraph', 'list') and builder.offsets:
                candidate = previous_end
                while candidate > previous_start:
                    size = estimate_tokens(builder.lines[candidate - 1])
                    if overlap + size > CHUNK_OVERLAP_TOKENS:
                        break
                    overlap += size
                    candidate -= 1
                overlap = previous_end - candidate
            start = block.start - overlap
            end, current_tokens = block.end, estimate_tokens('\n'.join(builder.lines[start - 1:block.end]))
            current_path = list(path)
        last_kind = block.kind
        if builder.truncated:
            break
    flush()
    coverage = 'truncated' if builder.truncated else 'full'
    return {'version': VERSION, 'snapshot_id': snapshot_id, 'repository_id': str(repository_id),
            'coverage': coverage, 'chunks': builder.chunks, 'chunk_count': len(builder.chunks),
            'total_estimate_tokens': builder.total_estimate}


def chunk_manifest_hash(manifest: dict) -> str:
    import json
    data = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(data).hexdigest()

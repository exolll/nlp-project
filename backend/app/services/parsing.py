"""Bounded, in-memory document parsing; safe to call in a worker process."""

import hashlib
import io
import json
import re
import unicodedata
import zipfile
from pathlib import PurePath

import fitz
from docx import Document
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph


class ParseError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(code, message)

    def __str__(self):
        return self.message


def _normalize(value):
    # Do not strip individual lines: indentation inside code is meaningful.
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    value = re.sub(r'\n(?:[^\S\n]*\n)+', '\n\n', value)
    return value.strip()


def _visible(value):
    return any(not ch.isspace() and unicodedata.category(ch) not in {'Cf', 'Cc', 'Mn', 'Me'} for ch in value)


def _pdf(data, limits):
    if not data.startswith(b'%PDF-'):
        raise ParseError('FILE_SIGNATURE_MISMATCH', '文件内容与 PDF 扩展名不符。')
    pieces, warnings = [], []
    try:
        with fitz.open(stream=data, filetype='pdf') as document:
            if not document.is_pdf:
                raise ParseError('FILE_SIGNATURE_MISMATCH', '文件内容不是 PDF。')
            if document.is_encrypted or document.needs_pass:
                raise ParseError('PDF_ENCRYPTED', 'PDF 已加密，请移除密码后上传。')
            if document.is_repaired:
                raise ParseError('PDF_INVALID', 'PDF 文件损坏，请重新导出后上传。')
            pages = document.page_count
            if pages > limits['max_pdf_pages']:
                raise ParseError('PDF_PAGE_LIMIT', 'PDF 页数超过允许上限。')
            for index in range(pages):
                page = document[index]
                blocks = page.get_text('blocks', sort=True)
                has_text = False
                for block_index, block in enumerate(blocks, 1):
                    if block[6] != 0:
                        continue
                    raw = block[4]
                    pieces.append((raw, f'第 {index + 1} 页，第 {block_index} 块',
                                   {'page_number': index + 1, 'block_number': block_index,
                                    'bbox': list(block[:4])}))
                    has_text = has_text or _visible(raw)
                if not has_text:
                    kind = '含图片但无可提取文字' if page.get_images() else '空白或无可提取文字'
                    warnings.append(f'PDF 第 {index + 1} 页{kind}；未执行 OCR。')
    except ParseError:
        raise
    except Exception:
        raise ParseError('PDF_INVALID', 'PDF 文件损坏或无法读取，请重新导出后上传。') from None
    if not any(_visible(raw) for raw, _, _ in pieces):
        raise ParseError('PDF_NO_EXTRACTABLE_TEXT', 'PDF 为扫描件或空白文件，无可提取文字；本服务不执行 OCR。')
    return pieces, pages, warnings


def _validate_docx(data, limits):
    if not data.startswith(b'PK\x03\x04'):
        raise ParseError('FILE_SIGNATURE_MISMATCH', '文件内容与 DOCX 扩展名不符。')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > limits['max_zip_entries']:
            raise ParseError('DOCX_ZIP_ENTRIES_LIMIT', 'DOCX 压缩包条目数超过允许上限。')
        if sum(item.file_size for item in entries) > limits['max_zip_bytes']:
            raise ParseError('DOCX_ZIP_SIZE_LIMIT', 'DOCX 解压大小超过允许上限。')
        names = [item.filename for item in entries]
        if len(set(names)) != len(names):
            raise ParseError('DOCX_INVALID', 'DOCX 包含重复压缩条目，无法安全读取。')
        for item in entries:
            if item.flag_bits & 1:
                raise ParseError('DOCX_ENCRYPTED', 'DOCX 压缩条目已加密，请移除密码后上传。')
            if item.file_size and (not item.compress_size or item.file_size / item.compress_size > limits['max_zip_ratio']):
                raise ParseError('DOCX_ZIP_RATIO_LIMIT', 'DOCX 压缩比超过允许上限。')
        if not {'[Content_Types].xml', 'word/document.xml'}.issubset(names):
            raise ParseError('DOCX_INVALID', '文件不是有效 DOCX，缺少必要文档结构。')
        # Read every entry under the validated bounds, checking CRC before python-docx.
        for item in entries:
            archive.read(item)


def _heading_level(paragraph):
    props = paragraph._p.pPr
    outline = props.find(qn('w:outlineLvl')) if props is not None else None
    if outline is not None:
        level = int(outline.get(qn('w:val')))
        return level + 1 if level < 9 else None
    style = paragraph.style
    seen = set()
    while style is not None and style.style_id not in seen:
        seen.add(style.style_id)
        match = re.fullmatch(r'Heading\s*([1-9])', style.name or '', re.IGNORECASE)
        if match:
            return int(match.group(1))
        props = style.element.pPr
        outline = props.find(qn('w:outlineLvl')) if props is not None else None
        if outline is not None:
            level = int(outline.get(qn('w:val')))
            return level + 1 if level < 9 else None
        style = style.base_style
    return None


def _docx(data, limits):
    pieces = []
    try:
        _validate_docx(data, limits)
        document = Document(io.BytesIO(data))
        paragraph_number = table_number = 0
        for child in document.element.body.iterchildren():
            if isinstance(child, CT_P):
                paragraph_number += 1
                paragraph = Paragraph(child, document)
                pieces.append((paragraph.text, f'第 {paragraph_number} 段',
                               {'paragraph_number': paragraph_number,
                                'heading_level': _heading_level(paragraph)}))
            elif isinstance(child, CT_Tbl):
                table_number += 1
                table = Table(child, document)
                seen_cells = set()
                for row_number, row in enumerate(table.rows, 1):
                    cells = []
                    for cell in row.cells:
                        # python-docx maps horizontally/vertically merged cells to one XML node.
                        if cell._tc not in seen_cells:
                            seen_cells.add(cell._tc)
                            cells.append(cell.text)
                    pieces.append(('\t'.join(cells), f'表 {table_number}，第 {row_number} 行',
                                   {'table_number': table_number, 'row_number': row_number}))
    except ParseError:
        raise
    except Exception:
        raise ParseError('DOCX_INVALID', 'DOCX 文件损坏或格式无效，请重新导出后上传。') from None
    if not any(_visible(raw) for raw, _, _ in pieces):
        raise ParseError('DOCX_NO_EXTRACTABLE_TEXT', 'DOCX 无可提取文字，请上传包含正文的文档。')
    return pieces, None, []


def parse_content(data: bytes | None, filename: str | None, text: str, limits: dict) -> dict:
    # Offsets count Unicode code points, not UTF-16 units.
    has_file = data is not None
    supplemental = text if has_file else ''
    pages, warnings, mime = None, [], None
    if has_file:
        if not data:
            raise ParseError('EMPTY_FILE', '上传文件为空，补充说明不能替代文件正文。')
        if len(data) > limits['max_file_bytes']:
            raise ParseError('FILE_SIZE_LIMIT', '文件大小超过允许上限。')
        extension = PurePath(filename or '').suffix.lower()
        if extension == '.pdf':
            mode, mime = 'pdf', 'application/pdf'
            pieces, pages, warnings = _pdf(data, limits)
        elif extension == '.docx':
            mode, mime = 'docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
            pieces, pages, warnings = _docx(data, limits)
        else:
            raise ParseError('UNSUPPORTED_FILE_TYPE', '仅支持 PDF 和 DOCX 文件。')
    else:
        mode = 'text'
        pieces = [(text, '用户输入正文', {})]
        if not _visible(text):
            raise ParseError('EMPTY_INPUT', '请输入可见的任务正文或上传文档。')
    primary = [(raw, _normalize(raw), location, detail) for raw, location, detail in pieces if _visible(raw)]
    primary_body = '\n\n'.join(content for _, content, _, _ in primary)
    if len(primary_body) > limits['max_primary_characters']:
        raise ParseError('PRIMARY_CHARACTER_LIMIT', '主正文字符数超过允许上限。')
    supplement = _normalize(supplemental)
    if len(supplement) > limits['max_supplement_characters']:
        raise ParseError('SUPPLEMENT_CHARACTER_LIMIT', '补充说明字符数超过允许上限。')
    normalized = '[Assignment Document]\n\n' if has_file else '[Assignment Text]\n\n'
    segments = []

    def append_segment(raw, content, location, detail, source_type, source_kind):
        nonlocal normalized
        start = len(normalized)
        normalized += content
        segments.append({'segment_id': f'SEG-{len(segments) + 1:03d}',
                         'source_type': source_type, 'source_kind': source_kind,
                         'content': content, 'original_content': raw,
                         'source_location': location, 'start_offset': start,
                         'end_offset': len(normalized), 'location_detail': detail})

    for index, (raw, content, location, detail) in enumerate(primary):
        if index:
            normalized += '\n\n'
        append_segment(raw, content, location, detail, 'file' if has_file else 'user_text',
                       'assignment_document' if has_file else 'user_primary_text')
    if has_file and _visible(supplement):
        mode += '_text'
        normalized += '\n\n[User Supplementary Information]\n\n'
        append_segment(supplemental, supplement, '用户补充说明', {}, 'user_text', 'user_supplement')
    identity = {'file_sha256': hashlib.sha256(data).hexdigest() if has_file else None, 'text': text}
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                       separators=(',', ':')).encode('utf-8')).hexdigest()
    return {'input_mode': mode, 'original_filename': filename if has_file else None,
            'mime_type': mime or 'text/plain', 'file_size': len(data) if has_file else 0,
            'total_pages': pages, 'extracted_text': '\n\n'.join(raw for raw, _, _ in pieces),
            'supplemental_text': supplemental, 'normalized_text': normalized,
            'source_segments': segments, 'content_hash': digest,
            'character_count': len(primary_body), 'warnings': warnings}

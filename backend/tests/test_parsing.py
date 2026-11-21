from datetime import datetime, timezone
from uuid import uuid4

import hashlib
import io
import json
import pickle
import zipfile

import fitz
import pytest
from docx import Document

from app.services.parsing import ParseError, parse_content


@pytest.fixture
def limits():
    return dict(max_file_bytes=10_000_000, max_pdf_pages=30,
                max_primary_characters=30_000, max_supplement_characters=2_000,
                max_zip_bytes=52_428_800, max_zip_entries=1_000, max_zip_ratio=100)


def pdf_bytes(text='Assignment: do not remove negation.', pages=1, encrypted=False, image=False):
    with fitz.open() as document:
        for _ in range(pages):
            page = document.new_page()
            if text:
                page.insert_text((72, 72), text)
            if image:
                pixel = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2, 2), False)
                pixel.clear_with(255)
                page.insert_image(fitz.Rect(20, 20, 40, 40), pixmap=pixel)
        if encrypted:
            return document.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256,
                                    owner_pw='owner', user_pw='reader')
        return document.tobytes()


def docx_bytes(text='正文不能删除否定词。'):
    document = Document()
    document.add_paragraph(text)
    return save_docx(document)


def save_docx(document):
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def assert_error(code, data, filename, text, limits):
    with pytest.raises(ParseError) as caught:
        parse_content(data, filename, text, limits)
    assert caught.value.code == code
    assert caught.value.message
    return caught.value


def assert_offsets(result):
    previous = 0
    for index, segment in enumerate(result['source_segments'], 1):
        assert segment['segment_id'] == f'SEG-{index:03d}'
        assert previous <= segment['start_offset'] < segment['end_offset']
        assert result['normalized_text'][segment['start_offset']:segment['end_offset']] == segment['content']
        previous = segment['end_offset']


@pytest.mark.parametrize('kind,supplement,mode', [
    ('text', '', 'text'), ('pdf', '', 'pdf'), ('docx', '', 'docx'),
    ('pdf', '不要省略代码', 'pdf_text'), ('docx', '不要省略代码', 'docx_text'),
])
def test_five_modes(kind, supplement, mode, limits):
    data = {'text': lambda: None, 'pdf': pdf_bytes, 'docx': docx_bytes}[kind]()
    text = '纯文本任务' if kind == 'text' else supplement
    result = parse_content(data, None if kind == 'text' else f'task.{kind}', text, limits)
    from app.schemas.documents import NormalizedDocument
    NormalizedDocument(**result, document_id=uuid4(), task_id=uuid4(), created_at=datetime.now(timezone.utc))
    assert result['input_mode'] == mode
    assert result['total_pages'] == (1 if kind == 'pdf' else None)
    assert result['supplemental_text'] == supplement
    assert result['character_count'] == sum(len(s['content']) for s in result['source_segments']
                                           if s['source_kind'] != 'user_supplement')
    assert_offsets(result)


@pytest.mark.parametrize('text', ['', ' \r\n\t ', '\u200b\u200c\u200d\ufeff', '\u2060\u200e'])
def test_empty_text(text, limits):
    assert_error('EMPTY_INPUT', None, None, text, limits)


def test_empty_file_never_falls_back(limits):
    assert_error('EMPTY_FILE', b'', 'task.pdf', '这只是补充', limits)
    assert_error('DOCX_NO_EXTRACTABLE_TEXT', docx_bytes(''), 'task.docx', '补充', limits)


@pytest.mark.parametrize('image', [False, True])
def test_pdf_without_text(image, limits):
    error = assert_error('PDF_NO_EXTRACTABLE_TEXT', pdf_bytes('', image=image), 'scan.pdf', '补充', limits)
    assert 'OCR' in error.message


def test_pdf_blank_page_warning(limits):
    with fitz.open(stream=pdf_bytes(), filetype='pdf') as document:
        document.new_page()
        data = document.tobytes()
    result = parse_content(data, 'task.pdf', '', limits)
    assert result['total_pages'] == 2
    assert '第 2 页' in result['warnings'][0]
    assert result['source_segments'][0]['location_detail']['page_number'] == 1


def test_pdf_encryption_and_pages(limits):
    assert_error('PDF_ENCRYPTED', pdf_bytes(encrypted=True), 'locked.pdf', '', limits)
    assert_error('PDF_PAGE_LIMIT', pdf_bytes(pages=2), 'task.pdf', '', dict(limits, max_pdf_pages=1))
    assert parse_content(pdf_bytes(pages=2), 'task.pdf', '', dict(limits, max_pdf_pages=2))['total_pages'] == 2


def test_docx_order_heading_empty_paragraph_and_merges(limits):
    document = Document()
    document.add_heading('标题', level=2)
    document.add_paragraph('')
    document.add_paragraph('表前')
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).merge(table.cell(1, 0)).text = '合并单元'
    table.cell(0, 1).text = '第一行'
    table.cell(1, 1).text = '第二行'
    document.add_paragraph('表后')
    result = parse_content(save_docx(document), 'task.docx', '', limits)
    segments = result['source_segments']
    assert [s['content'] for s in segments] == ['标题', '表前', '合并单元\t第一行', '第二行', '表后']
    assert segments[0]['location_detail']['heading_level'] == 2
    assert segments[1]['location_detail']['paragraph_number'] == 3
    assert segments[2]['location_detail'] == {'table_number': 1, 'row_number': 1}
    assert segments[-1]['location_detail']['paragraph_number'] == 4
    assert result['character_count'] == len('\n\n'.join(s['content'] for s in segments))
    assert_offsets(result)


@pytest.mark.parametrize('data,name,code', [
    (b'plain text', 'fake.pdf', 'FILE_SIGNATURE_MISMATCH'),
    (b'plain text', 'fake.docx', 'FILE_SIGNATURE_MISMATCH'),
    (b'%PDF-invalid', 'bad.pdf', 'PDF_INVALID'),
    (b'PK\x03\x04broken', 'bad.docx', 'DOCX_INVALID'),
    (b'anything', 'task.txt', 'UNSUPPORTED_FILE_TYPE'),
])
def test_invalid_formats(data, name, code, limits):
    assert_error(code, data, name, '补充不能替代正文', limits)


def test_cross_format_signatures(limits):
    assert_error('FILE_SIGNATURE_MISMATCH', docx_bytes(), 'fake.pdf', '', limits)
    assert_error('FILE_SIGNATURE_MISMATCH', pdf_bytes(), 'fake.docx', '', limits)


@pytest.mark.parametrize('key,value,code', [
    ('max_file_bytes', 1, 'FILE_SIZE_LIMIT'),
    ('max_primary_characters', 1, 'PRIMARY_CHARACTER_LIMIT'),
    ('max_supplement_characters', 1, 'SUPPLEMENT_CHARACTER_LIMIT'),
    ('max_zip_bytes', 1, 'DOCX_ZIP_SIZE_LIMIT'),
    ('max_zip_entries', 1, 'DOCX_ZIP_ENTRIES_LIMIT'),
    ('max_zip_ratio', 1, 'DOCX_ZIP_RATIO_LIMIT'),
])
def test_configured_limits(key, value, code, limits):
    assert_error(code, docx_bytes(), 'task.docx', '补充说明', dict(limits, **{key: value}))


def test_character_boundary_and_separators(limits):
    assert parse_content(None, None, '甲\n\n乙', dict(limits, max_primary_characters=4))['character_count'] == 4
    assert_error('PRIMARY_CHARACTER_LIMIT', None, None, '甲\n\n乙', dict(limits, max_primary_characters=3))


def test_normalization_preserves_unicode_code_and_original(limits):
    raw = '  不得删除否定词：α ≠ β 😀\r\n\r\n \r\n```python\r\nif x:\r\n    print("你好")\r\n```  '
    result = parse_content(None, None, raw, limits)
    expected = '不得删除否定词：α ≠ β 😀\n\n```python\nif x:\n    print("你好")\n```'
    assert result['extracted_text'] == raw
    assert result['source_segments'][0]['original_content'] == raw
    assert result['normalized_text'] == '[Assignment Text]\n\n' + expected
    assert result['character_count'] == len(expected)
    assert_offsets(result)


def test_supplement_separate_and_hash_identity(limits):
    data = docx_bytes()
    raw = '  补充😀\r\n不可合并来源  '
    result = parse_content(data, 'task.docx', raw, limits)
    other = parse_content(data, 'task.docx', '其他补充', limits)
    assert result['content_hash'] != other['content_hash']
    assert result['content_hash'] == parse_content(data, 'renamed.docx', raw, limits)['content_hash']
    identity = {'file_sha256': hashlib.sha256(data).hexdigest(), 'text': raw}
    assert result['content_hash'] == hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                                              separators=(',', ':')).encode()).hexdigest()
    assert raw == result['supplemental_text']
    assert raw not in result['extracted_text']
    assert result['source_segments'][-1]['source_kind'] == 'user_supplement'
    assert result['source_segments'][-1]['source_type'] == 'user_text'
    assert '[User Supplementary Information]\n\n' in result['normalized_text']
    assert_offsets(result)
    assert parse_content(None, None, '甲', limits)['content_hash'] != parse_content(None, None, '乙', limits)['content_hash']


def test_invalid_docx_structure_and_xml(limits):
    for entries in ({'unrelated': b'x'}, {'[Content_Types].xml': b'invalid', 'word/document.xml': b'invalid'}):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
        assert_error('DOCX_INVALID', buffer.getvalue(), 'task.docx', '', limits)


def test_worker_pickling(limits):
    worker = pickle.loads(pickle.dumps(parse_content))
    assert worker(None, None, '任务', limits)['input_mode'] == 'text'
    error = pickle.loads(pickle.dumps(ParseError('EMPTY_INPUT', '请输入正文。')))
    assert error.code == 'EMPTY_INPUT'
    assert str(error) == '请输入正文。'

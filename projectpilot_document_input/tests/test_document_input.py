from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pymupdf
from docx import Document

from projectpilot.document_input import DocumentInputError, DocumentInputService


class DocumentInputServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.service = DocumentInputService()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_reads_pdf_and_combines_supplement(self) -> None:
        path = self.root / "requirements.pdf"
        pdf = pymupdf.open()
        page = pdf.new_page()
        page.insert_text((72, 72), "Project must export a report.")
        pdf.save(path)
        pdf.close()

        parsed = self.service.prepare(path, "Use the final project version.")

        self.assertEqual(parsed.document_type, "pdf")
        self.assertIn("export a report", parsed.document_text)
        self.assertIn("Use the final project version", parsed.combined_text)

    def test_reads_docx_paragraphs_and_tables(self) -> None:
        path = self.root / "requirements.docx"
        doc = Document()
        doc.add_paragraph("The system supports document upload.")
        table = doc.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "Limit"
        table.cell(0, 1).text = "10 MB"
        doc.save(path)

        parsed = self.service.prepare(path)

        self.assertEqual(parsed.document_type, "docx")
        self.assertIn("document upload", parsed.document_text)
        self.assertIn("Limit 10 MB", parsed.document_text)

    def test_rejects_unsupported_file(self) -> None:
        path = self.root / "notes.txt"
        path.write_text("hello", encoding="utf-8")
        with self.assertRaisesRegex(DocumentInputError, "仅支持"):
            self.service.prepare(path)

    def test_rejects_long_supplement(self) -> None:
        path = self._make_docx("short")
        with self.assertRaisesRegex(DocumentInputError, "2,000"):
            self.service.prepare(path, "x" * 2_001)

    def test_rejects_file_over_ten_megabytes(self) -> None:
        path = self.root / "large.pdf"
        with path.open("wb") as file:
            file.seek(10 * 1024 * 1024)
            file.write(b"x")
        with self.assertRaisesRegex(DocumentInputError, "10 MB"):
            self.service.prepare(path)

    def test_rejects_empty_document(self) -> None:
        path = self._make_docx("")
        with self.assertRaisesRegex(DocumentInputError, "没有可用文本"):
            self.service.prepare(path)

    def test_rejects_combined_text_over_limit(self) -> None:
        path = self._make_docx("x" * 29_999)
        with self.assertRaisesRegex(DocumentInputError, "30,000"):
            self.service.prepare(path, "extra")

    def test_both_types_use_same_extractor_contract(self) -> None:
        class Recorder:
            def extract(self, document):
                return document.document_type

        pdf_path = self.root / "one.pdf"
        pdf = pymupdf.open()
        pdf.new_page().insert_text((72, 72), "PDF requirement")
        pdf.save(pdf_path)
        pdf.close()
        docx_path = self._make_docx("DOCX requirement")

        self.assertEqual(self.service.extract_requirements(pdf_path, Recorder()), "pdf")
        self.assertEqual(self.service.extract_requirements(docx_path, Recorder()), "docx")

    def _make_docx(self, text: str) -> Path:
        path = self.root / "sample.docx"
        doc = Document()
        if text:
            doc.add_paragraph(text)
        doc.save(path)
        return path


if __name__ == "__main__":
    unittest.main()



"""Validated PDF/DOCX input and text extraction for ProjectPilot."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import pymupdf
from docx import Document

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_SUPPLEMENTARY_CHARS = 2_000
MAX_COMBINED_CHARS = 30_000
SUPPORTED_SUFFIXES = {".pdf", ".docx"}


class DocumentInputError(ValueError):
    """A user-facing validation or document parsing error."""


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """Common representation passed to the requirement extraction module."""

    filename: str
    document_type: str
    document_text: str
    supplementary_text: str
    combined_text: str


class RequirementExtractor(Protocol):
    """Contract implemented by the team's single LLM requirement extractor."""

    def extract(self, document: ParsedDocument) -> Any:
        """Extract requirements from one normalized document input."""


class DocumentInputService:
    """Validate one file, parse it, and optionally call one LLM entry point."""

    def prepare(
        self,
        file_path: str | Path,
        supplementary_text: str | None = None,
    ) -> ParsedDocument:
        path = Path(file_path)
        supplement = supplementary_text or ""
        self._validate_input(path, supplement)

        suffix = path.suffix.lower()
        try:
            raw_text = (
                self._extract_pdf(path)
                if suffix == ".pdf"
                else self._extract_docx(path)
            )
        except DocumentInputError:
            raise
        except Exception as exc:
            raise DocumentInputError(f"无法解析文件 {path.name}：{exc}") from exc

        document_text = self._normalize_text(raw_text)
        supplement = self._normalize_text(supplement)
        if not document_text:
            raise DocumentInputError("文档解析后没有可用文本")

        combined_text = self._combine(document_text, supplement)
        if len(combined_text) > MAX_COMBINED_CHARS:
            raise DocumentInputError(
                "解析后的整合文本超过 30,000 字符，"
                f"当前为 {len(combined_text):,} 字符"
            )

        return ParsedDocument(
            filename=path.name,
            document_type=suffix.removeprefix("."),
            document_text=document_text,
            supplementary_text=supplement,
            combined_text=combined_text,
        )

    def extract_requirements(
        self,
        file_path: str | Path,
        extractor: RequirementExtractor,
        supplementary_text: str | None = None,
    ) -> Any:
        """Send PDF and DOCX through the same requirement-extraction contract."""
        document = self.prepare(file_path, supplementary_text)
        return extractor.extract(document)

    @staticmethod
    def _validate_input(path: Path, supplement: str) -> None:
        if not path.is_file():
            raise DocumentInputError(f"文件不存在：{path}")
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise DocumentInputError("仅支持 PDF 或 DOCX 文件")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise DocumentInputError(
                f"单个文件不能超过 10 MB，当前为 {size / 1024 / 1024:.2f} MB"
            )
        if len(supplement) > MAX_SUPPLEMENTARY_CHARS:
            raise DocumentInputError(
                "补充说明不能超过 2,000 字符，"
                f"当前为 {len(supplement):,} 字符"
            )

    @staticmethod
    def _extract_pdf(path: Path) -> str:
        pieces: list[str] = []
        with pymupdf.open(path) as pdf:
            if pdf.needs_pass:
                raise DocumentInputError("暂不支持加密 PDF")
            for page in pdf:
                text = page.get_text("text")
                if text:
                    pieces.append(text)
        return "\n".join(pieces)

    @staticmethod
    def _extract_docx(path: Path) -> str:
        doc = Document(path)
        pieces = [paragraph.text for paragraph in doc.paragraphs if paragraph.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                if any(cells):
                    pieces.append("\t".join(cells))
        return "\n".join(pieces)

    @staticmethod
    def _normalize_text(text: str) -> str:
        lines = [" ".join(line.split()) for line in text.replace("\x00", "").splitlines()]
        return "\n".join(line for line in lines if line).strip()

    @staticmethod
    def _combine(document_text: str, supplement: str) -> str:
        if not supplement:
            return document_text
        return f"{document_text}\n\n【补充说明】\n{supplement}"



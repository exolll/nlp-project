"""Small command-line example for ProjectPilot document input."""

from __future__ import annotations

import argparse
import json

from projectpilot.document_input import DocumentInputService, ParsedDocument


class DemoRequirementExtractor:
    """Placeholder showing the one interface that a real LLM module implements."""

    def extract(self, document: ParsedDocument) -> dict[str, object]:
        return {
            "filename": document.filename,
            "document_type": document.document_type,
            "input_characters": len(document.combined_text),
            "requirements": ["此处替换为 LLM 提取结果"],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="解析一个 PDF 或 DOCX 并进入统一需求提取入口")
    parser.add_argument("file", help="PDF 或 DOCX 文件路径")
    parser.add_argument("--supplement", default="", help="补充说明，最多 2,000 字符")
    args = parser.parse_args()

    result = DocumentInputService().extract_requirements(
        args.file,
        DemoRequirementExtractor(),
        args.supplement,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()



"""Local Streamlit test page for ProjectPilot document input v1.0."""

from __future__ import annotations

import tempfile
from pathlib import Path

import streamlit as st

from projectpilot.document_input import (
    MAX_COMBINED_CHARS,
    MAX_FILE_BYTES,
    MAX_SUPPLEMENTARY_CHARS,
    DocumentInputError,
    DocumentInputService,
)


def parse_upload(filename: str, content: bytes, supplementary_text: str):
    """Bridge Streamlit's in-memory upload to the path-based core service."""
    suffix = Path(filename).suffix.lower()
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory) / f"upload{suffix}"
        temporary_path.write_bytes(content)
        prepared_document = DocumentInputService().prepare(
            temporary_path,
            supplementary_text,
        )
        # Keep the original user-facing filename instead of the temporary filename.
        return prepared_document.__class__(
            filename=filename,
            document_type=prepared_document.document_type,
            document_text=prepared_document.document_text,
            supplementary_text=prepared_document.supplementary_text,
            combined_text=prepared_document.combined_text,
        )


st.set_page_config(
    page_title="ProjectPilot 文档输入测试",
    page_icon="📄",
    layout="centered",
)

st.title("ProjectPilot 文档输入测试")
st.caption("v1.0 · PDF / DOCX 单文件解析与统一文本整合")

with st.expander("输入规则", expanded=False):
    st.markdown(
        """
        - 每次上传一个 PDF 或 DOCX 文件
        - 文件最大 10 MB
        - 补充说明最多 2,000 字符
        - 解析后的整合文本最多 30,000 字符
        """
    )

uploaded_file = st.file_uploader(
    "上传需求文档",
    type=["pdf", "docx"],
    accept_multiple_files=False,
    help="仅支持 PDF 或 DOCX，单个文件不超过 10 MB。",
)

supplement_input = st.text_area(
    "补充说明（可选）",
    max_chars=MAX_SUPPLEMENTARY_CHARS,
    height=140,
    placeholder="例如：请以最终提交版本为准。",
)
st.caption(
    f"补充说明：{len(supplement_input):,} / "
    f"{MAX_SUPPLEMENTARY_CHARS:,} 字符"
)

if uploaded_file is not None:
    file_bytes = uploaded_file.getvalue()
    size_mb = len(file_bytes) / 1024 / 1024
    left, right = st.columns(2)
    left.metric("文件大小", f"{size_mb:.2f} MB")
    right.metric("文件格式", Path(uploaded_file.name).suffix.upper().removeprefix("."))
    if len(file_bytes) > MAX_FILE_BYTES:
        st.error("文件超过 10 MB，请压缩或更换文件。")

parse_clicked = st.button(
    "解析文档",
    type="primary",
    use_container_width=True,
    disabled=uploaded_file is None,
)

if parse_clicked and uploaded_file is not None:
    try:
        parsed_document = parse_upload(
            uploaded_file.name,
            uploaded_file.getvalue(),
            supplement_input,
        )
    except DocumentInputError as exc:
        st.error(str(exc))
    except Exception as exc:
        st.error(f"处理失败：{exc}")
    else:
        st.success("解析成功，文档已转换为统一输入格式。")
        col1, col2, col3 = st.columns(3)
        col1.metric("正文字符", f"{len(parsed_document.document_text):,}")
        col2.metric("补充字符", f"{len(parsed_document.supplementary_text):,}")
        col3.metric(
            "整合字符",
            f"{len(parsed_document.combined_text):,} / {MAX_COMBINED_CHARS:,}",
        )

        details_tab, text_tab, combined_tab = st.tabs(
            ["统一输入信息", "文档正文", "整合文本"]
        )
        with details_tab:
            st.json(
                {
                    "filename": parsed_document.filename,
                    "document_type": parsed_document.document_type,
                    "document_characters": len(parsed_document.document_text),
                    "supplementary_characters": len(
                        parsed_document.supplementary_text
                    ),
                    "combined_characters": len(parsed_document.combined_text),
                    "next_module": "RequirementExtractor.extract(document)",
                }
            )
        with text_tab:
            st.text_area(
                "解析后的正文",
                value=parsed_document.document_text,
                height=360,
                disabled=True,
            )
        with combined_tab:
            st.text_area(
                "发送给需求提取模块的文本",
                value=parsed_document.combined_text,
                height=360,
                disabled=True,
            )
            st.download_button(
                "下载整合文本",
                data=parsed_document.combined_text.encode("utf-8"),
                file_name=f"{Path(parsed_document.filename).stem}_combined.txt",
                mime="text/plain",
                use_container_width=True,
            )



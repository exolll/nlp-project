# ProjectPilot 文档输入方案 v1.0

这是一个可独立运行的 PDF/DOCX 文档输入模块。它先完成文件与字符上限校验，再把两种格式转换成同一种 `ParsedDocument`，最后通过统一的 `extract()` 接口交给 LLM 需求提取模块。

## 已实现规则

- 每次接收一个 `.pdf` 或 `.docx` 文件。
- 单文件最大 10 MB。
- 补充说明最多 2,000 字符。
- 整合文本最多 30,000 字符，超限会明确报错，不会静默截断。
- PDF 使用 PyMuPDF；DOCX 使用 python-docx，并读取正文段落和表格。
- 空文档、错误格式、加密 PDF、解析失败均返回可读错误。
- PDF 与 DOCX 共用 `RequirementExtractor.extract(document)` 入口。

“每次只能上传一个文件”由 `prepare(file_path, ...)` 的单文件参数自然保证。前端应使用单选文件控件，不要启用 `multiple`。

## PyCharm 设置

1. 用 PyCharm 打开本目录。
2. 选择 Python 3.10 或更高版本的解释器。
3. 在 PyCharm Terminal 中执行：

```bash
python -m pip install -e ".[ui]"
```

## 启动测试页

在 PyCharm 中，打开 `run_app.py` 后点击右上角绿色运行按钮即可。不要直接运行 `app.py`，否则 Streamlit 会进入 bare mode。

也可以在终端运行：

```bash
streamlit run app.py
```

浏览器会打开本地测试页。上传一个 PDF 或 DOCX，可检查文件大小、补充说明字数、解析正文、整合字符数以及最终发送给需求提取模块的文本。页面每次只接受一个文件；上传层与解析层都会执行大小和格式校验。

## 运行测试

测试使用 Python 自带的 `unittest`，不需要额外安装 pytest：

```bash
python -m unittest discover -s tests -v
```

## 运行示例

```bash
python examples/run_document_input.py path/to/requirements.pdf --supplement "以最终提交版本为准"
```

也可以传入 DOCX：

```bash
python examples/run_document_input.py path/to/requirements.docx
```

## 接入真正的 LLM 模块

后续只需让需求提取类实现一个方法：

```python
class LLMRequirementExtractor:
    def extract(self, document: ParsedDocument):
        # 把 document.combined_text 发送给模型，并返回结构化需求
        ...
```

调用方式保持不变：

```python
result = DocumentInputService().extract_requirements(
    file_path="requirements.pdf",
    supplementary_text="补充说明",
    extractor=LLMRequirementExtractor(),
)
```

这样解析层不依赖具体模型，PDF 与 DOCX 也不会形成两套 LLM 流程。



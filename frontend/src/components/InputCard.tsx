import { useRef, useState } from 'react'
import type { Config } from '../api'
import type { SaveStatus } from '../autosave'
import { fileLabel } from '../inputRules'

function draftText(status: SaveStatus, label: string): string {
  if (status === 'saving') return '正在保存本地草稿…'
  if (status === 'saved') return '本地草稿已保存'
  if (status === 'failed') return '本地草稿保存失败'
  return label
}

export default function InputCard({
  config, file, text, count, limit, busy, ready, pending, locked,
  draftStatus, draftMessage, restoredNote,
  onPick, onText, onClear, onRemoveFile, onSubmit, onRetryDraft, onDismissRestored,
}: {
  config: Config | null
  file: File | null
  text: string
  count: number
  limit: number
  busy: boolean
  ready: boolean
  pending: boolean
  locked: boolean
  draftStatus: SaveStatus
  draftMessage: string
  restoredNote: string
  onPick: (files: FileList | null) => void
  onText: (value: string) => void
  onClear: () => void
  onRemoveFile: () => void
  onSubmit: () => void
  onRetryDraft: () => void
  onDismissRestored: () => void
}) {
  const input = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)
  const disabled = locked || !config
  return <section className="card input-card" aria-labelledby="input-title">
    <div className="card-title">
      <div><span className="section-index">01 / INPUT</span><h2 id="input-title">提供项目材料</h2></div>
      <button type="button" className="text-button" disabled={locked} onClick={onClear}>清空</button>
    </div>
    {restoredNote && <div className="draft-restore" role="status">
      <span>{restoredNote}</span>
      <button type="button" className="text-button" aria-label="关闭草稿恢复提示" onClick={onDismissRestored}>×</button>
    </div>}
    <div className="field-heading"><label>任务文档</label><span>选填 · 单个文件</span></div>
    <input
      ref={input} className="visually-hidden" type="file" accept=".pdf,.docx" disabled={disabled}
      onChange={event => { onPick(event.target.files); event.target.value = '' }}
    />
    <button
      type="button"
      className={`dropzone ${dragging ? 'dragging' : ''}`}
      disabled={disabled}
      onClick={() => input.current?.click()}
      onDragOver={event => { event.preventDefault(); if (!disabled) setDragging(true) }}
      onDragLeave={() => setDragging(false)}
      onDrop={event => { event.preventDefault(); setDragging(false); if (!disabled) onPick(event.dataTransfer.files) }}
    >
      <span className="upload-icon" aria-hidden="true">↑</span>
      <strong>{file ? file.name : '点击上传，或将文件拖到这里'}</strong>
      <span>{file ? `${fileLabel(file)} · 点击替换文件` : '支持 PDF、DOCX，保留内容来源'}</span>
      <small>{config ? `最大 ${(config.max_file_bytes / 1_000_000).toFixed(1)} MB · PDF 最多 ${config.max_pdf_pages} 页` : '正在获取上传限制…'}</small>
    </button>
    {file && <div className="file-actions"><span>已选择 1 个文件 · {fileLabel(file)}</span><button type="button" className="text-button" disabled={locked} onClick={onRemoveFile}>删除文件</button></div>}
    <div className="field-heading text-heading"><label htmlFor="requirements">{file ? '补充说明' : '项目需求'}</label><span className="mode">{file ? '补充文本' : '主输入文本'}</span></div>
    <textarea
      id="requirements" disabled={locked} value={text}
      onChange={event => onText(event.target.value)}
      placeholder={file ? '补充文档中未提及的目标、约束或特别说明…' : '描述你想完成的项目。可以包含项目背景、目标、功能要求与交付形式…'}
    />
    <div className={`counter ${config && count > limit ? 'over' : ''}`}>
      <span>{file ? '文本将作为文档的补充材料' : '未上传文档时，文本作为主输入'}</span>
      <span>{count.toLocaleString()} / {config ? limit.toLocaleString() : '—'} 字符</span>
    </div>
    <div className={`draft-line ${draftStatus}`} role="status" aria-live="polite">
      <span className="draft-state" title={draftMessage || undefined}>{draftText(draftStatus, '文本会保存为本地草稿')}</span>
      {draftStatus === 'failed' && <button type="button" className="text-button" disabled={busy} onClick={onRetryDraft}>重试保存</button>}
      <span className="draft-scope">仅保存在本浏览器 · 不代表服务端成功</span>
    </div>
    <div className="submit-area">
      <p className="submit-cost">点击后将创建任务、解析材料并自动提取需求；提取会调用 AI 模型并可能产生费用。</p>
      <button
        type="button" className="primary"
        disabled={busy || !ready || (!file && !text.trim()) || count > limit}
        onClick={onSubmit}
      >
        {busy ? '正在处理…' : pending ? '重试上传 · 同任务与幂等键' : '提交并开始分析'}<span>→</span>
      </button>
      <p>提交后将自动解析材料并提取项目需求。费用以服务方实际计量为准；不会自动搜索 GitHub。</p>
    </div>
  </section>
}
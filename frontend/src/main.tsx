import { createRoot } from 'react-dom/client'
import { useEffect, useRef, useState } from 'react'
import { codePoints, Failure, request, session, startSession, upload } from './api'
import type { Config, InputStatus, NormalizedDocument, Task } from './api'
import RequirementsPanel from './RequirementsPanel'
import './styles.css'

type Pending = { task: string; key: string; file: File | null; text: string }
const historyKey = 'projectpilot.history'
function cachedHistory(): Task[] { try { return JSON.parse(sessionStorage.getItem(historyKey) || '[]') } catch { return [] } }
const statusLabel = (status: string) => ({ completed: '已完成', processing: '解析中', failed: '失败', empty: '待输入', created: '待输入', pending: '待处理' }[status] || status)
function App() {
  const [config, setConfig] = useState<Config | null>(null)
  const [tasks, setTasks] = useState<Task[]>(cachedHistory)
  const [file, setFile] = useState<File | null>(null)
  const [text, setText] = useState('')
  const [error, setError] = useState<Failure | null>(null)
  const [notice, setNotice] = useState('正在连接工作区…')
  const [busy, setBusy] = useState(false)
  const [ready, setReady] = useState(false)
  const [progress, setProgress] = useState<number | null>(null)
  const [pending, setPending] = useState<Pending | null>(null)
  const [document, setDocument] = useState<NormalizedDocument | null>(null)
  const [active, setActive] = useState('')
  const [tab, setTab] = useState<'text' | 'segments'>('text')
  const [dragging, setDragging] = useState(false)
  const lock = useRef(false)
  const input = useRef<HTMLInputElement>(null)
  const polling = useRef<AbortController | null>(null)
  const count = codePoints(text).length
  const limit = config ? (file ? config.max_supplement_characters : config.max_primary_characters) : 0
  const locked = busy || !!pending
  function saveTasks(next: Task[]) { setTasks(next); sessionStorage.setItem(historyKey, JSON.stringify(next)) }
  async function history() {
    const result = await request<{ tasks: Task[] }>('/tasks')
    // Keep the previous session's visible records when explicitly starting another session.
    setTasks(previous => {
      const ids = new Set(result.tasks.map(task => task.task_id))
      const next = [...result.tasks, ...previous.filter(task => !ids.has(task.task_id))].sort((a, b) => b.created_at.localeCompare(a.created_at))
      sessionStorage.setItem(historyKey, JSON.stringify(next)); return next
    })
  }
  function showError(value: unknown) {
    const failure = value instanceof Failure ? value : new Failure(0, { error_code: 'CLIENT_ERROR', message: value instanceof Error ? value.message : '操作失败，请重试。', retryable: true, details: null, request_id: '' })
    setError(failure); setNotice('')
    if (failure.status === 401) setReady(false)
  }
  async function connect(restart = false) {
    if (lock.current) return
    lock.current = true; setBusy(true); setError(null)
    try {
      if (restart || !session()) await startSession()
      const settings = await request<Config>('/config')
      setConfig(settings); await history(); setReady(true); setNotice('工作区已就绪，添加材料开始吧。')
      if (restart) { setPending(null); setDocument(null); setActive('') }
    } catch (e) { showError(e) } finally { lock.current = false; setBusy(false) }
  }
  useEffect(() => { void connect(); return () => polling.current?.abort() }, [])
  async function run(action: () => Promise<void>) {
    if (lock.current) return
    lock.current = true; setBusy(true); setError(null)
    try { await action() } catch (e) { showError(e) } finally { lock.current = false; setBusy(false); setProgress(null) }
  }
  async function applyStatus(state: InputStatus): Promise<boolean> {
    if (state.status === 'empty') {
      setNotice('任务尚未提交材料，可返回输入区提交。'); return true
    }
    setTasks(previous => {
      const next = previous.map(task => task.task_id === state.task_id ? { ...task, status: state.status, document_id: state.document_id } : task)
      sessionStorage.setItem(historyKey, JSON.stringify(next)); return next
    })
    if (state.status === 'failed') {
      setPending(previous => previous?.task === state.task_id ? null : previous)
      throw new Failure(422, state.error || { error_code: 'EXTRACTION_FAILED', message: '材料解析失败，请调整内容后创建新任务。', retryable: false, details: null, request_id: '' })
    }
    if (state.status === 'completed') {
      setPending(previous => previous?.task === state.task_id ? null : previous)
      const result = await request<NormalizedDocument>(`/tasks/${encodeURIComponent(state.task_id)}/input`)
      setDocument(result); setNotice('材料解析完成，已生成标准化输入。'); return true
    }
    return false
  }
  async function poll(id: string) {
    polling.current?.abort()
    const controller = new AbortController(); polling.current = controller
    const deadline = Date.now() + 60000
    const timeout = window.setTimeout(() => controller.abort(), 60000)
    setNotice('材料已上传，正在解析…'); setProgress(null)
    try {
    while (Date.now() < deadline && !controller.signal.aborted) {
      await new Promise<void>(resolve => {
        const onAbort = () => { clearTimeout(timer); resolve() }
        const timer = window.setTimeout(() => { controller.signal.removeEventListener('abort', onAbort); resolve() }, 1000)
        controller.signal.addEventListener('abort', onAbort, { once: true })
      })
      if (controller.signal.aborted) break
      const state = await request<InputStatus>(`/tasks/${encodeURIComponent(id)}/input/status`, 'GET', controller.signal)
      if (await applyStatus(state)) return
    }
    } catch (e) {
      if (!controller.signal.aborted) throw e
    } finally { clearTimeout(timeout) }
    setNotice('解析仍在进行，尚未判定失败。可稍后从任务历史重新查询。')
  }
  function choose(files: FileList | null) {
    if (locked || !files) return
    setError(null)
    const candidate = files[0]
    let message = ''
    if (files.length !== 1) message = '每次仅支持一个文件，请选择单个 PDF 或 DOCX。'
    else if (!/\.(pdf|docx)$/i.test(candidate.name)) message = '仅支持 PDF 或 DOCX 文件。'
    else if (!candidate.size) message = '不能上传空文件。'
    else if (!config) message = '请等待服务配置加载后再选择文件。'
    else if (candidate.size > config.max_file_bytes) message = '文件超过服务允许的大小。'
    if (message) { showError(new Error(message)); return }
    setFile(candidate)
  }
  async function submit() {
    if (!ready || !config || (!file && !text.trim()) || count > limit) return
    await run(async () => {
      let attempt = pending
      if (!attempt) {
        setNotice('正在创建任务…')
        const task = await request<Task>('/tasks', 'POST')
        saveTasks([task, ...tasks]); setActive(task.task_id); setDocument(null)
        attempt = { task: task.task_id, key: crypto.randomUUID(), file, text }
        setPending(attempt)
      }
      setActive(attempt.task); setNotice('正在上传材料…'); setProgress(0)
      try {
        const state = await upload(attempt.task, attempt.key, attempt.file, attempt.text, value => {
          setProgress(value); if (value === 100) setNotice('上传完成，正在等待解析…')
        })
        setPending(null)
        if (state.status !== 'failed') { setFile(null); setText('') }
        if (!await applyStatus(state)) await poll(attempt.task)
      } catch (e) {
        // Failed extraction may be corrected and submitted as a new task.
        if (e instanceof Failure && e.status === 422) { setFile(attempt.file); setText(attempt.text) }
        // Unknown transport outcomes retain the exact task, key and payload for safe retry.
        if (e instanceof Failure && e.status !== 0 && e.status < 500 && e.status !== 401) setPending(null)
        throw e
      }
    })
  }
  function openTask(task: Task) {
    void run(async () => {
      setActive(task.task_id); setDocument(null); setNotice('正在查询任务…')
      const state = await request<InputStatus>(`/tasks/${encodeURIComponent(task.task_id)}/input/status`)
      if (state.status === 'processing' || state.status === 'completed') {
        if (pending?.task === task.task_id) { setPending(null); setFile(null); setText('') }
      }
      if (!await applyStatus(state)) await poll(task.task_id)
    })
  }
  return <div className="app">
    <header className="topbar"><a className="brand" href="./"><span className="logo">P</span>ProjectPilot<span className="brand-sub">项目领航</span></a><span className="workspace"><i />个人工作区</span></header>
    <main>
      <div className="intro"><div><div className="eyebrow">从一份材料，开始一个好项目</div><h1>让项目的起点，更清晰<span>。</span></h1><p>上传材料，主动提取并确认需求，再单独授权搜索候选与核对 README 证据。</p></div><div className="phase"><span>02</span><div>需求确认<small>REQUIREMENTS</small></div></div></div>
      <div className="steps"><b><span>1</span>输入与解析</b><div /><span>2 · 主动提取需求</span><div /><span>3 · 编辑并确认</span></div>
      <div className="columns">
        <section className="card input-card"><div className="card-title"><div><span className="section-index">01 / INPUT</span><h2>提供项目材料</h2></div><button className="text-button" disabled={locked} onClick={() => { setFile(null); setText(''); setError(null) }}>清空</button></div>
          <div className="field-heading"><label>任务文档</label><span>选填 · 单个文件</span></div>
          <input ref={input} className="visually-hidden" type="file" accept=".pdf,.docx" disabled={locked || !config} onChange={event => { choose(event.target.files); event.target.value = '' }} />
          <button className={`dropzone ${dragging ? 'dragging' : ''}`} disabled={locked || !config} onClick={() => input.current?.click()} onDragOver={event => { event.preventDefault(); if (!locked) setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); setDragging(false); choose(event.dataTransfer.files) }}>
            <span className="upload-icon" aria-hidden="true">↑</span><strong>{file ? file.name : '点击上传，或将文件拖到这里'}</strong><span>{file ? `${(file.size / 1024).toFixed(1)} KB · 点击替换文件` : '支持 PDF、DOCX，保留内容来源'}</span><small>{config ? `最大 ${(config.max_file_bytes / 1_000_000).toFixed(1)} MB · PDF 最多 ${config.max_pdf_pages} 页` : '正在获取上传限制…'}</small>
          </button>
          {file && <div className="file-actions"><span>已选择 1 个文件</span><button className="text-button" disabled={locked} onClick={() => setFile(null)}>删除文件</button></div>}
          <div className="field-heading text-heading"><label htmlFor="requirements">{file ? '补充说明' : '项目需求'}</label><span className="mode">{file ? '补充文本' : '主输入文本'}</span></div>
          <textarea id="requirements" disabled={locked} value={text} onChange={event => setText(event.target.value)} placeholder={file ? '补充文档中未提及的目标、约束或特别说明…' : '描述你想完成的项目。可以包含项目背景、目标、功能要求与交付形式…'} />
          <div className={`counter ${config && count > limit ? 'over' : ''}`}><span>{file ? '文本将作为文档的补充材料' : '未上传文档时，文本作为主输入'}</span><span>{count.toLocaleString()} / {config ? limit.toLocaleString() : '—'} 字符</span></div>
          <div className="submit-area"><button className="primary" disabled={busy || !ready || (!file && !text.trim()) || count > limit} onClick={() => void submit()}>{busy ? '正在处理…' : pending ? '重试上传 · 同任务与幂等键' : '提交并解析材料'}<span>→</span></button><p>此按钮仅解析材料；之后需单独点击“提取需求”才调用模型。</p></div>
        </section>
        <section className="card preview-card"><div className="card-title"><div><span className="section-index">02 / PREVIEW</span><h2>标准化预览</h2></div><span className={`badge ${document ? 'success' : ''}`}>{document ? '解析完成' : '等待材料'}</span></div>
          <div className="tabs" role="tablist" aria-label="预览内容"><button role="tab" aria-selected={tab === 'text'} onClick={() => setTab('text')}>normalized_text</button><button role="tab" aria-selected={tab === 'segments'} onClick={() => setTab('segments')}>source_segments</button></div>
          {!document ? <div className="empty"><div className="paper-icon" aria-hidden="true"><i /><i /><i /></div><h3>{busy ? '正在整理你的材料' : '清晰的输入，从这里呈现'}</h3><p>提交材料后，在这里查看标准化文本<br />与每一段内容的原始来源。</p><span>真实解析 · 来源可追溯</span></div> : <div className="preview-content" role="tabpanel"><div className="doc-meta"><strong>{document.original_filename || '文本需求'}</strong><span>{document.character_count} 字符 · {document.total_pages === null ? '文本材料' : `${document.total_pages} 页`} · {document.input_mode}</span></div>{document.warnings.map((warning, i) => <p className="warning" key={i}>{warning}</p>)}{tab === 'text' ? <pre>{document.normalized_text}</pre> : document.source_segments.map((segment, index) => <article className="segment" key={segment.segment_id}><div><b>{String(index + 1).padStart(2, '0')} · {segment.source_kind === 'assignment_document' ? '任务文档' : segment.source_kind === 'user_primary_text' ? '主输入文本' : '补充文本'}</b><small>码点 {segment.start_offset}–{segment.end_offset}</small></div><p>{codePoints(document.normalized_text).slice(segment.start_offset, segment.end_offset).join('')}</p><details><summary>查看来源与原文</summary><pre>{segment.original_content}</pre><code>{JSON.stringify({ source_location: segment.source_location, location_detail: segment.location_detail }, null, 2)}</code></details></article>)}</div>}
        </section>
      </div>
      <section className="feedback" aria-live="polite">{progress !== null && <div className="upload-progress"><span>{progress === 100 ? '上传完成 · 解析中' : `上传中 ${progress}%`}</span><progress value={progress} max="100" /></div>}{notice && <p>{busy && <span className="spinner" />}{notice}</p>}{error && <div className="error" role="alert"><strong>{error.message}</strong><small>{error.info.error_code}{error.info.request_id && ` · 请求编号 ${error.info.request_id}`}</small>{error.info.details != null && <details><summary>错误详情</summary><pre>{JSON.stringify(error.info.details, null, 2)}</pre></details>}{error.status === 401 ? <button disabled={busy} onClick={() => { if (window.confirm('开始新的会话？旧历史仍会保留，但旧会话的任务可能无法访问。')) void connect(true) }}>重新开始会话</button> : !ready && <button disabled={busy} onClick={() => void connect()}>重新连接</button>}{pending && <p>上传结果未确认。请重试同一任务，或先从历史检查状态；当前材料已锁定以避免重复。</p>}</div>}</section>
      {document && <RequirementsPanel key={document.document_id} document={document} />}
      <section className="card history"><div className="card-title"><div><span className="section-index">YOUR WORKSPACE</span><h2>任务历史 <span className="history-count">{tasks.length}</span></h2></div><button className="text-button" disabled={busy || !ready} onClick={() => void run(history)}>刷新历史 ↻</button></div>{!tasks.length ? <div className="history-empty">还没有任务。提交第一份材料，记录会保存在当前会话中。</div> : <div className="history-list">{tasks.map(task => <button className={`history-row ${active === task.task_id ? 'selected' : ''}`} key={task.task_id} disabled={busy || !ready} onClick={() => openTask(task)}><span className="task-icon">▤</span><span className="task-name"><strong>任务 {task.task_id}</strong><small>{new Date(task.created_at).toLocaleString('zh-CN')}</small></span><span className={`task-status ${task.status}`}>{statusLabel(task.status)}</span><span>查看 →</span></button>)}</div>}<p className="history-note">刷新页面可恢复当前会话与任务历史。历史结果始终从服务端查询。</p></section>
      <footer><span>ProjectPilot · 把想法变成清晰的起点</span><span>{config ? `${config.environment} / ${config.storage_backend}` : '本地工作区'} · 搜索须单独授权</span></footer>
    </main>
  </div>
}
createRoot(document.getElementById('root')!).render(<App />)

import { createRoot } from 'react-dom/client'
import { useEffect, useRef, useState } from 'react'
import { codePoints, Failure, request, session, startSession, upload } from './api'
import type { Config, InputStatus, NormalizedDocument, Task } from './api'
import RequirementsPanel from './RequirementsPanel'
import AppShell from './components/AppShell'
import type { Connection } from './components/AppShell'
import HistoryDrawer from './components/HistoryDrawer'
import SettingsDrawer from './components/SettingsDrawer'
import type { ProbeResult } from './components/SettingsDrawer'
import Toast from './components/Toast'
import InputCard from './components/InputCard'
import PreviewPanel from './components/PreviewPanel'
import * as router from './router'
import * as uiStore from './history'
import type { UiState } from './history'
import { titleEditedFor, titleFor, toTaskSummary, topicPatch } from './adapters/task'
import { deriveStages } from './adapters/stages'
import type { StageView } from './adapters/stages'
import { loadStageSignals } from './adapters/workflow'
import { createAutosave } from './autosave'
import type { SaveStatus } from './autosave'
import { clearDraft, readDraft, writeDraft } from './draft'
import type { DraftFileHint, InputDraft } from './draft'
import { inputLimit, validateFileChoice } from './inputRules'
import { listTasks, readInput, readInputStatus } from './services/tasks'
import { newId } from './uuid'
import { capabilities } from './capabilities'
import './styles.css'

type Pending = { task: string; key: string; file: File | null; text: string }
type TitleJob = { taskId: string; title: string }
type DraftJob = { text: string; fileHint: DraftFileHint | null }
const historyKey = 'projectpilot.history'
const mock = import.meta.env.VITE_MOCK === '1'
const loadingStages = deriveStages({ hasTask: true, inputCompleted: false, extractionCompleted: false, confirmedRevisionId: null, run: null })
function cachedHistory(): Task[] { try { return JSON.parse(sessionStorage.getItem(historyKey) || '[]') } catch { return [] } }
function draftNote(draft: InputDraft): string {
  const time = new Date(draft.updatedAt)
  const when = Number.isNaN(time.getTime()) ? '' : `（保存于 ${time.toLocaleString('zh-CN', { hour12: false })}）`
  const base = `已恢复本地文本草稿${when}。`
  return draft.fileHint ? `${base}之前的文件“${draft.fileHint.name}”无法自动恢复，请重新选择文件。` : `${base}草稿只保存在本浏览器，尚未提交。`
}
function PanelIcon({ open }: { open: boolean }) {
  return <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
    <rect x="1.5" y="2.5" width="13" height="11" rx="2" stroke="currentColor" strokeWidth="1.3" />
    <path d="M6 2.5v11" stroke="currentColor" strokeWidth="1.3" />
    {open
      ? <path d="M12.5 8h-4m1.5-1.5L8.5 8l1.5 1.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
      : <path d="M8.5 8h4m-1.5-1.5L12.5 8 11 9.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />}
  </svg>
}

function App() {
  const [config, setConfig] = useState<Config | null>(null)
  const [initialDraft] = useState(() => readDraft(localStorage))
  const [tasks, setTasks] = useState<Task[]>(cachedHistory)
  const [file, setFile] = useState<File | null>(null)
  const [text, setText] = useState(() => initialDraft.draft?.text ?? '')
  const [fileHint, setFileHint] = useState<DraftFileHint | null>(() => initialDraft.draft?.fileHint ?? null)
  const [restoredBanner, setRestoredBanner] = useState(() => initialDraft.draft ? draftNote(initialDraft.draft) : '')
  const [draftStatus, setDraftStatus] = useState<SaveStatus>('idle')
  const [draftMessage, setDraftMessage] = useState('')
  const [previewOpen, setPreviewOpen] = useState(() => uiStore.readPreviewOpen(localStorage))
  const [autoExtractDoc, setAutoExtractDoc] = useState<string | null>(null)
  const [error, setError] = useState<Failure | null>(null)
  const [notice, setNotice] = useState('正在连接工作区…')
  const [busy, setBusy] = useState(false)
  const [ready, setReady] = useState(false)
  const [connecting, setConnecting] = useState(true)
  const [progress, setProgress] = useState<number | null>(null)
  const [pending, setPending] = useState<Pending | null>(null)
  const [doc, setDoc] = useState<NormalizedDocument | null>(null)
  const [active, setActive] = useState('')
  const [tab, setTab] = useState<'text' | 'segments'>('text')
  const [route, setRoute] = useState<router.Route>(() => router.parseRoute(window.location.hash))
  const [ui, setUi] = useState<UiState>(() => uiStore.readUi(localStorage).state)
  const [stages, setStages] = useState<StageView[] | null>(null)
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('idle')
  const [saveMessage, setSaveMessage] = useState('')
  const [titleDraft, setTitleDraft] = useState('')
  const [historyOpen, setHistoryOpen] = useState(false)
  const [toast, setToast] = useState('')
  const [restoreNotice, setRestoreNotice] = useState('')
  const [restoreTick, setRestoreTick] = useState(0)
  const [draftId] = useState(() => initialDraft.draft?.draftId ?? newId())
  const lock = useRef(false)
  const draftSuppressed = useRef(false)
  const polling = useRef<AbortController | null>(null)
  const count = codePoints(text).length
  const limit = inputLimit(config, !!file)
  const locked = busy || !!pending

  const routeRef = useRef(route)
  const activeRef = useRef(active)
  const docRef = useRef(doc)
  const tasksRef = useRef(tasks)
  const uiRef = useRef(ui)
  const settingsReturn = useRef<router.Route | null>(null)
  const signalScope = useRef<AbortController | null>(null)
  routeRef.current = route; activeRef.current = active; docRef.current = doc; tasksRef.current = tasks; uiRef.current = ui

  function commitUi(update: (state: UiState) => UiState) {
    const next = update(uiRef.current)
    uiStore.writeUi(localStorage, next)
    uiRef.current = next
    setUi(next)
  }
  function commitQuiet(update: (state: UiState) => UiState) {
    try { commitUi(update) } catch { /* 偏好写入失败不阻塞业务；标题保存状态另有明确提示 */ }
  }
  const commitUiRef = useRef(commitUi)
  commitUiRef.current = commitUi
  const autosaveRef = useRef<ReturnType<typeof createAutosave<TitleJob>> | null>(null)
  if (!autosaveRef.current) {
    autosaveRef.current = createAutosave<TitleJob>({
      delay: 900,
      save: job => {
        const trimmed = job.title.trim().slice(0, 80)
        commitUiRef.current(state => uiStore.patchTask(state, job.taskId, {
          title: trimmed || null,
          titleEdited: titleEditedFor(uiRef.current.tasks[job.taskId], job.taskId, trimmed),
        }))
      },
      onChange: (status, detail) => {
        setSaveStatus(status)
        setSaveMessage(detail.message ?? '')
        if (status === 'failed') setToast(detail.message || '标题保存失败，请重试。')
      },
    })
  }
  const autosave = autosaveRef.current
  const draftIdRef = useRef(initialDraft.draft?.draftId ?? newId())
  const draftSaveRef = useRef<ReturnType<typeof createAutosave<DraftJob>> | null>(null)
  if (!draftSaveRef.current) {
    draftSaveRef.current = createAutosave<DraftJob>({
      delay: 900,
      save: job => {
        if (draftSuppressed.current) return
        writeDraft(localStorage, { draftId: draftIdRef.current, text: job.text, fileHint: job.fileHint })
      },
      onChange: (status, detail) => {
        setDraftStatus(status)
        setDraftMessage(detail.message ?? '')
        if (status === 'failed') setToast(detail.message || '本地输入草稿保存失败，请重试。')
      },
    })
  }
  const draftSave = draftSaveRef.current

  function navigate(next: router.Route, replace = false) {
    routeRef.current = next
    setRoute(next)
    const hash = router.routeHash(next)
    if (window.location.hash === hash) return
    if (replace) window.history.replaceState(null, '', hash)
    else window.location.hash = hash
  }
  async function flushLocal(requireDraft = false): Promise<boolean> {
    const titleSaved = await autosave.flush()
    if (!titleSaved) { setToast('标题尚未保存成功，未执行切换；请点击保存状态重试。'); return false }
    const draftSaved = await draftSave.flush()
    if (!draftSaved && requireDraft) { setToast('本地输入草稿保存失败，未执行操作；请重试保存或先处理本地存储问题。'); return false }
    return true
  }
  function saveTasks(next: Task[]) { setTasks(next); sessionStorage.setItem(historyKey, JSON.stringify(next)) }
  async function refreshTasks() {
    const result = await listTasks()
    setTasks(previous => {
      // Keep the previous session's visible records when explicitly starting another session.
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
    lock.current = true; setBusy(true); setError(null); setConnecting(true)
    try {
      if (restart || !session()) await startSession()
      const settings = await request<Config>('/config')
      setConfig(settings); await refreshTasks(); setReady(true); setNotice('工作区已就绪，添加材料开始吧。')
      if (restart) { setPending(null); setDoc(null); setActive(''); setStages(null); setAutoExtractDoc(null); navigate({ kind: 'home' }, true) }
    } catch (e) { showError(e) } finally { lock.current = false; setBusy(false); setConnecting(false) }
  }
  useEffect(() => { void connect(); return () => polling.current?.abort() }, [])
  useEffect(() => {
    const parsed = uiStore.readUi(localStorage)
    const messages = [parsed.error, initialDraft.error].filter(Boolean)
    if (messages.length) setToast(messages.join(' '))
  }, [])
  async function run(action: () => Promise<void>): Promise<boolean> {
    if (lock.current) return false
    lock.current = true; setBusy(true); setError(null)
    try { await action(); return true } catch (e) { showError(e); return false }
    finally { lock.current = false; setBusy(false); setProgress(null) }
  }
  async function applyStatus(state: InputStatus): Promise<{ done: boolean; documentId: string | null }> {
    if (state.status === 'empty') {
      setNotice('任务尚未提交材料，可返回输入区提交。'); return { done: true, documentId: null }
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
      const result = await readInput(state.task_id)
      setDoc(result); setNotice('材料解析完成，已生成标准化输入。')
      return { done: true, documentId: result.document_id }
    }
    return { done: false, documentId: state.document_id }
  }
  async function poll(id: string): Promise<string | null> {
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
      const state = await readInputStatus(id, controller.signal)
      const settled = await applyStatus(state)
      if (settled.done) return settled.documentId
    }
    } catch (e) {
      if (!controller.signal.aborted) throw e
    } finally { clearTimeout(timeout) }
    setNotice('解析仍在进行，尚未判定失败。可稍后从任务历史重新查询。')
    return null
  }
  function choose(files: FileList | null) {
    if (locked || !files) return
    setError(null)
    const list = Array.from(files)
    const message = validateFileChoice(list.map(file => ({ name: file.name, size: file.size })), config)
    if (message) { showError(new Error(message)); return }
    const [candidate] = list
    setFile(candidate)
    const hint = { name: candidate.name, size: candidate.size }
    setFileHint(hint)
    setRestoredBanner('')
    queueDraft(text, hint)
  }
  function removeFile() {
    setFile(null)
    setFileHint(null)
    queueDraft(text, null)
  }
  function onTextChange(value: string) {
    setText(value)
    queueDraft(value, fileHint)
  }
  function clearInput() {
    setFile(null); setText(''); setFileHint(null); setRestoredBanner(''); setError(null)
    queueDraft('', null)
  }
  function queueDraft(nextText: string, hint: DraftFileHint | null) {
    draftSuppressed.current = false
    draftSave.schedule({ text: nextText, fileHint: hint })
  }
  function resetCompose() {
    draftSuppressed.current = true
    draftSave.cancel()
    try { clearDraft(localStorage) } catch { /* 本地存储不可用不影响已提交材料 */ }
    draftIdRef.current = newId()
    setFile(null); setText(''); setFileHint(null); setRestoredBanner('')
    setDraftStatus('idle'); setDraftMessage('')
  }
  function advanceToConfirm(taskId: string, documentId: string) {
    resetCompose()
    setAutoExtractDoc(documentId)
    navigate({ kind: 'task', taskId, stage: 2 })
    commitQuiet(state => uiStore.patchTask(state, taskId, { stage: 2 }))
    void refreshSignals(taskId, documentId)
  }
  async function submit() {
    if (!ready || !config || (!file && !text.trim()) || count > limit) return
    await run(async () => {
      let attempt = pending
      if (!attempt) {
        setNotice('正在创建任务…')
        const task = await request<Task>('/tasks', 'POST')
        saveTasks([task, ...tasks]); setActive(task.task_id); setDoc(null); setAutoExtractDoc(null)
        navigate({ kind: 'task', taskId: task.task_id, stage: 1 })
        commitQuiet(state => uiStore.patchTask(state, task.task_id, { stage: 1 }))
        attempt = { task: task.task_id, key: newId(), file, text }
        setPending(attempt)
      }
      setActive(attempt.task); setNotice('正在上传材料…'); setProgress(0)
      try {
        const state = await upload(attempt.task, attempt.key, attempt.file, attempt.text, value => {
          setProgress(value); if (value === 100) setNotice('上传完成，正在等待解析…')
        })
        setPending(null)
        if (state.status !== 'failed') { setFile(null); setText(''); setFileHint(null) }
        const settled = await applyStatus(state)
        const documentId = settled.done ? settled.documentId : await poll(attempt.task)
        if (documentId) advanceToConfirm(attempt.task, documentId)
      } catch (e) {
        // Failed extraction may be corrected and submitted as a new task.
        if (e instanceof Failure && e.status === 422) {
          setFile(attempt.file); setText(attempt.text)
          setFileHint(attempt.file ? { name: attempt.file.name, size: attempt.file.size } : null)
        }
        // Unknown transport outcomes retain the exact task, key and payload for safe retry.
        if (e instanceof Failure && e.status !== 0 && e.status < 500 && e.status !== 401) setPending(null)
        throw e
      }
    })
  }
  function applyTopic(taskId: string, topic: string | null) {
    if (!topic || autosave.hasUnsaved()) return
    const patch = topicPatch(uiRef.current.tasks[taskId], topic)
    if (!patch) return
    commitQuiet(state => uiStore.patchTask(state, taskId, patch))
  }
  async function refreshSignals(taskId: string, documentId: string | null) {
    signalScope.current?.abort()
    const controller = new AbortController(); signalScope.current = controller
    const record = uiRef.current.tasks[taskId]
    const result = await loadStageSignals({
      taskId, documentId, inputCompleted: !!documentId,
      runId: record?.runId ?? null, confirmedRevisionId: record?.revisionId ?? null,
    }, controller.signal)
    if (controller.signal.aborted || taskId !== activeRef.current) return
    setStages(deriveStages(result.signals))
    applyTopic(taskId, result.projectTopic)
    const confirmed = result.signals.confirmedRevisionId
    if (confirmed && confirmed !== record?.revisionId) {
      commitQuiet(state => uiStore.patchTask(state, taskId, { revisionId: confirmed, touch: false }))
    }
  }
  function pulse() {
    const taskId = activeRef.current
    if (!taskId) return
    void refreshSignals(taskId, docRef.current?.task_id === taskId ? docRef.current.document_id : null)
  }
  function handleRunId(runId: string) {
    const taskId = activeRef.current
    if (!taskId) return
    try { commitUi(state => uiStore.patchTask(state, taskId, { runId, touch: false })) } catch { return }
    void refreshSignals(taskId, docRef.current?.document_id ?? null)
  }
  async function openTask(task: Task, stage: number | null = null, restored = false): Promise<boolean> {
    if (!(await flushLocal())) return false
    return run(async () => {
      setNotice('正在查询任务…')
      setAutoExtractDoc(null)
      const state = await readInputStatus(task.task_id)
      if (state.status === 'failed') { await applyStatus(state); return }
      setActive(task.task_id); setDoc(null)
      const settled = await applyStatus(state)
      const documentId = settled.done ? settled.documentId : await poll(task.task_id)
      const record = uiRef.current.tasks[task.task_id]
      const requested = stage ?? record?.stage ?? 1
      navigate({ kind: 'task', taskId: task.task_id, stage: requested })
      commitQuiet(s => uiStore.patchTask(s, task.task_id, { stage: requested }))
      setRestoreNotice('')
      if (restored) setToast('已恢复上次任务')
      await refreshSignals(task.task_id, documentId)
    })
  }
  function changeTitle(value: string) {
    if (!activeRef.current) return
    setTitleDraft(value)
    autosave.schedule({ taskId: activeRef.current, title: value })
  }
  function cancelTitle() {
    const taskId = activeRef.current
    if (!taskId) return
    const saved = titleFor(uiRef.current.tasks[taskId], taskId)
    setTitleDraft(saved)
    autosave.schedule({ taskId, title: saved })
  }
  function renameTask(id: string, title: string) {
    const trimmed = title.trim().slice(0, 80)
    if (!trimmed) return
    if (id === activeRef.current) {
      setTitleDraft(trimmed)
      autosave.schedule({ taskId: id, title: trimmed })
      return
    }
    try { commitUi(state => uiStore.patchTask(state, id, { title: trimmed, titleEdited: titleEditedFor(uiRef.current.tasks[id], id, trimmed), touch: false })) }
    catch { setToast('本地保存失败，标题未更新。') }
  }
  function deleteTask() {
    // capabilities.taskDelete=false：后端没有任何 DELETE 路由，不执行本地假删除。
    setToast('后端暂不支持删除任务（待后端接口支持），未执行任何删除。')
  }
  async function selectTask(id: string) {
    const task = tasksRef.current.find(row => row.task_id === id)
    if (!task) { setToast('该任务不在当前会话列表中。'); return }
    const ok = await openTask(task)
    if (ok) setHistoryOpen(false)
    else if (!autosave.hasUnsaved()) setToast(busy ? '当前操作尚未结束，暂不能切换任务。' : '未切换任务，当前任务保持不变。')
  }
  async function newTask() {
    if (busy) { setToast('当前操作尚未结束，请稍后再新建任务。'); return }
    if (!(await flushLocal())) return
    setHistoryOpen(false)
    setActive(''); setDoc(null); setStages(null); setPending(null); setError(null); setAutoExtractDoc(null)
    resetCompose()
    setNotice('添加新的材料，开始一个项目。')
    navigate({ kind: 'home' })
  }
  function selectStage(stage: StageView) {
    const taskId = activeRef.current
    if (!taskId) return
    navigate({ kind: 'task', taskId, stage: stage.index })
    commitQuiet(state => uiStore.patchTask(state, taskId, { stage: stage.index }))
  }
  async function advanceToSearch() {
    const taskId = activeRef.current
    if (!taskId) return
    const documentId = docRef.current?.task_id === taskId ? docRef.current.document_id : null
    // 先刷新阶段信号：viewingStage 受 currentStage 限制，确认结果未进入信号前切换会被钳回当前阶段。
    try { await refreshSignals(taskId, documentId) } catch { /* 读取失败不阻塞导航，视图仍按服务端信号钳制 */ }
    if (taskId !== activeRef.current) return
    navigate({ kind: 'task', taskId, stage: 4 }, true)
    commitQuiet(state => uiStore.patchTask(state, taskId, { stage: 4 }))
  }
  async function openDeepStage() {
    const taskId = activeRef.current
    if (!taskId) return
    const documentId = docRef.current?.task_id === taskId ? docRef.current.document_id : null
    // 与初筛入口相同：先刷新只读阶段信号，避免刚发布的证据尚未进入信号时被钳回阶段 4。
    try { await refreshSignals(taskId, documentId) } catch { /* 读取失败不阻塞导航，视图仍按服务端信号钳制 */ }
    if (taskId !== activeRef.current) return
    navigate({ kind: 'task', taskId, stage: 5 })
    commitQuiet(state => uiStore.patchTask(state, taskId, { stage: 5 }))
  }
  async function openResultStage() {
    const taskId = activeRef.current
    if (!taskId) return
    const documentId = docRef.current?.task_id === taskId ? docRef.current.document_id : null
    // 阶段 6 由 rankingAvailable 门控：先刷新只读信号，排名尚未进入信号时切换会被钳回阶段 5。
    try { await refreshSignals(taskId, documentId) } catch { /* 读取失败不阻塞导航，视图仍按服务端信号钳制 */ }
    if (taskId !== activeRef.current) return
    navigate({ kind: 'task', taskId, stage: 6 })
    commitQuiet(state => uiStore.patchTask(state, taskId, { stage: 6 }))
  }
  function openSettings(target: router.SettingsTab) {
    if (routeRef.current.kind !== 'settings') settingsReturn.current = routeRef.current
    navigate({ kind: 'settings', tab: target })
  }
  function closeSettings() {
    const back = settingsReturn.current
    settingsReturn.current = null
    navigate(back && back.kind !== 'settings' ? back : { kind: 'home' })
  }
  async function probe(): Promise<ProbeResult> {
    const started = performance.now()
    try {
      await request<Config>('/config')
      return { ok: true, latencyMs: Math.round(performance.now() - started), message: '后端可达（GET /config 成功）；不代表模型密钥有效。' }
    } catch (e) {
      const message = e instanceof Failure ? e.message : e instanceof Error ? e.message : '检测失败'
      return { ok: false, latencyMs: Math.round(performance.now() - started), message: `后端不可达：${message}` }
    }
  }
  function togglePreview() {
    const next = !previewOpen
    setPreviewOpen(next)
    try { uiStore.writePreviewOpen(localStorage, next) }
    catch { setToast('预览显示偏好未能保存到本地；本次仍会切换。') }
  }

  const routeHandler = useRef<(next: router.Route) => Promise<void>>(async () => {})
  routeHandler.current = async next => {
    if (next.kind === 'task' && next.taskId !== activeRef.current) {
      const task = tasksRef.current.find(row => row.task_id === next.taskId)
      if (task) {
        const ok = await openTask(task, next.stage)
        if (!ok && activeRef.current) navigate({ kind: 'task', taskId: activeRef.current, stage: null }, true)
      } else {
        setToast('链接中的任务不存在，或属于已结束的会话。')
        navigate({ kind: 'home' }, true)
      }
    } else if (next.kind === 'home' && activeRef.current) {
      if (!(await flushLocal())) { navigate({ kind: 'task', taskId: activeRef.current, stage: null }, true); return }
      setActive(''); setDoc(null); setStages(null); setAutoExtractDoc(null)
      setNotice('添加新的材料，开始一个项目。')
    }
  }
  useEffect(() => {
    const onHash = () => {
      const next = router.parseRoute(window.location.hash)
      if (router.sameRoute(next, routeRef.current)) return
      routeRef.current = next
      setRoute(next)
      void routeHandler.current(next)
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  useEffect(() => {
    if (!active) { setTitleDraft(''); return }
    if (autosave.hasUnsaved()) return
    setTitleDraft(titleFor(ui.tasks[active], active))
  }, [active, ui])
  useEffect(() => {
    const flush = () => {
      if (autosave.hasUnsaved()) void autosave.flush()
      if (draftSave.hasUnsaved()) void draftSave.flush()
    }
    window.addEventListener('pagehide', flush)
    return () => window.removeEventListener('pagehide', flush)
  }, [autosave, draftSave])
  useEffect(() => {
    const opened = historyOpen || route.kind === 'settings'
    window.document.body.classList.toggle('drawer-open', opened)
    return () => { window.document.body.classList.remove('drawer-open') }
  }, [historyOpen, route])
  useEffect(() => {
    if (!ready) return
    let cancelled = false
    void (async () => {
      const current = router.parseRoute(window.location.hash)
      if (current.kind === 'task') {
        const task = tasksRef.current.find(row => row.task_id === current.taskId)
        if (task) {
          const ok = await openTask(task, current.stage, true)
          if (!ok && !cancelled) {
            setRestoreNotice('上次任务恢复失败，可能属于已结束的会话。可重试，或从任务历史重新选择。')
            navigate({ kind: 'home' }, true)
          }
          return
        }
        if (cancelled) return
        setRestoreNotice('链接中的任务无法恢复，可能属于已结束的会话。可重试，或从任务历史重新选择。')
        navigate({ kind: 'home' }, true)
        return
      }
      if (current.kind === 'settings') return
      const last = uiRef.current.last
      if (!last) return
      const task = tasksRef.current.find(row => row.task_id === last.taskId)
      if (!task) {
        if (!cancelled) setRestoreNotice('上次任务属于已结束的会话，无法自动恢复。可重试，或从任务历史重新选择。')
        return
      }
      const ok = await openTask(task, null, true)
      if (!ok && !cancelled) setRestoreNotice('上次任务恢复失败，请重试，或从任务历史重新选择。')
    })()
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, restoreTick])
  useEffect(() => {
    if (route.kind !== 'settings') window.scrollTo({ top: 0 })
  }, [route])

  const navStages = stages ?? loadingStages
  const currentStage = navStages.find(view => view.current)?.index ?? 1
  const requestedStage = route.kind === 'task' ? route.stage : null
  const viewingStage = requestedStage ? Math.min(requestedStage, currentStage) : currentStage
  const mode: 'home' | 'task' = route.kind === 'task' && active ? 'task' : 'home'
  const connection: Connection = ready ? 'ready' : connecting ? 'connecting' : 'error'
  const summaries = tasks.map(task => toTaskSummary(task, ui.tasks[task.task_id]))
  const stageGroup: 'input' | 'requirements' | 'search' =
    mode === 'home' || viewingStage <= 1 ? 'input' : viewingStage <= 3 ? 'requirements' : 'search'
  const searchStage: 4 | 5 | 6 = viewingStage >= 6 ? 6 : viewingStage === 5 ? 5 : 4
  const draftState: SaveStatus = !text && !file && !fileHint ? 'idle' : draftStatus

  return <>
    <AppShell
      mode={mode}
      title={mode === 'task' ? titleDraft : ''}
      titlePlaceholder={mode === 'task' ? '任务标题' : '新建任务'}
      onTitleChange={changeTitle}
      onTitleCancel={cancelTitle}
      saveStatus={saveStatus}
      saveMessage={saveMessage}
      onRetrySave={() => void autosave.retry()}
      connection={connection}
      stages={navStages}
      viewingStage={viewingStage}
      onSelectStage={selectStage}
      onOpenHistory={() => setHistoryOpen(true)}
      onOpenSettings={() => openSettings('api')}
      onOpenApi={() => openSettings('api')}
      mock={mock}
      restoreNotice={restoreNotice}
      onRestoreRetry={restoreNotice ? () => setRestoreTick(value => value + 1) : undefined}
    >
      <div className="stage-view" hidden={stageGroup !== 'input'}>
        <div className="intro"><div><div className="eyebrow">从一份材料，开始一个好项目</div><h1>让项目的起点，更清晰<span>。</span></h1><p>上传材料，主动提取并确认需求，再单独授权搜索候选与核对 README 证据。</p></div>{mode === 'task' && <div className="phase"><span>{String(viewingStage).padStart(2, '0')}</span><div>{navStages[viewingStage - 1]?.label}<small>STAGE</small></div></div>}</div>
        <div className="input-workspace">
          <div className={`workspace-toolbar ${previewOpen ? 'split' : 'single'}`}>
            <button type="button" className="preview-toggle" aria-expanded={previewOpen} aria-controls="preview-panel" onClick={togglePreview}>
              <PanelIcon open={previewOpen} />{previewOpen ? '隐藏标准化预览' : '显示标准化预览'}
            </button>
          </div>
      <div className={`columns ${previewOpen ? 'split' : 'single'}`}>
            <InputCard
              config={config} file={file} text={text} count={count} limit={limit}
              busy={busy} ready={ready} pending={!!pending} locked={locked}
              draftStatus={draftState} draftMessage={draftMessage} restoredNote={restoredBanner}
              onPick={choose} onText={onTextChange} onClear={clearInput} onRemoveFile={removeFile}
              onSubmit={() => void submit()} onRetryDraft={() => void draftSave.retry()}
              onDismissRestored={() => setRestoredBanner('')}
            />
            {previewOpen && <PreviewPanel doc={doc} tab={tab} onTab={setTab} busy={busy} />}
          </div>
      <section className="feedback" aria-live="polite">{progress !== null && <div className="upload-progress"><span>{progress === 100 ? '上传完成 · 解析中' : `上传中 ${progress}%`}</span><progress value={progress} max="100" /></div>}{notice && <p>{busy && <span className="spinner" />}{notice}</p>}{error && <div className="error" role="alert"><strong>{error.message}</strong><small>{error.info.error_code}{error.info.request_id && ` · 请求编号 ${error.info.request_id}`}</small>{error.info.details != null && <details><summary>错误详情</summary><pre>{JSON.stringify(error.info.details, null, 2)}</pre></details>}{error.status === 401 ? <button disabled={busy} onClick={() => { if (window.confirm('开始新的会话？旧历史仍会保留，但旧会话的任务可能无法访问。')) void connect(true) }}>重新开始会话</button> : !ready && <button disabled={busy} onClick={() => void connect()}>重新连接</button>}{pending && <p>上传结果未确认。请重试同一任务，或先从历史检查状态；当前材料已锁定以避免重复。</p>}</div>}</section>
        </div>
      </div>
      <div className="stage-view" hidden={stageGroup === 'input'}>
        {doc
          ? <RequirementsPanel
              key={doc.document_id}
              document={doc}
              view={stageGroup === 'search' ? 'search' : 'requirements'}
              searchStage={searchStage}
              autoExtract={autoExtractDoc === doc.document_id}
              onAutoExtract={() => setAutoExtractDoc(null)}
              onProgress={pulse}
              onRunId={handleRunId}
              onContinue={advanceToSearch}
              onOpenDeep={openDeepStage}
              onOpenResult={openResultStage}
            />
          : <section className="card stage-empty">
              <div className="card-title"><div><span className="section-index">02 / REQUIREMENTS</span><h2>需求提取与确认</h2></div></div>
              <p>还没有读取到标准化输入，暂时无法打开需求或搜索子界面。</p>
              <button type="button" className="secondary" onClick={() => selectStage(navStages[0])}>回到输入子界面</button>
            </section>}
      </div>
      <footer><span>ProjectPilot · 把想法变成清晰的起点</span><span>{mock ? '演示数据（Mock） · ' : ''}{config ? `${config.environment} / ${config.storage_backend}` : '本地工作区'} · 搜索须单独授权</span></footer>
    </AppShell>
    <HistoryDrawer
      open={historyOpen}
      onClose={() => setHistoryOpen(false)}
      tasks={summaries}
      activeId={active}
      onSelect={id => void selectTask(id)}
      onNew={() => void newTask()}
      onRename={renameTask}
      deleteAvailable={capabilities.taskDelete.available}
      deleteReason={capabilities.taskDelete.reason}
      onDelete={deleteTask}
      onNotice={setToast}
    />
    <SettingsDrawer
      open={route.kind === 'settings'}
      tab={route.kind === 'settings' ? route.tab : 'api'}
      onTab={openSettings}
      onClose={closeSettings}
      connection={connection}
      config={config}
      tokenPresent={!!session()}
      mock={mock}
      onProbe={probe}
    />
    <Toast message={toast} onClose={() => setToast('')} />
  </>
}
createRoot(document.getElementById('root')!).render(<App />)
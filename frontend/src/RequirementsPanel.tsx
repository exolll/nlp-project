import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Failure, hasLlmApiKey, request } from './api'
import SearchPanel from './SearchPanel'
import type { NormalizedDocument } from './api'
import {
  completeness, contentChanged, createDraft, draftIssues, evidenceSlice, fieldProvided, fields, isPending, languageGroupIssue,
  lines, mayStartExtraction, meaningChanged, revisionPayload, settleAttempt, updateField,
} from './requirements'
import type { Draft, Evidence, ExtractionAttempt, ExtractionJob, ItemEdit, RequirementField, RequirementsResponse, Revision } from './requirements'

const jobLabels = { queued: '排队中', extracting: '提取中', validating: '校验中', completed: '提取完成', failed: '提取失败' }
const revisionLabels = { extracted: '待核对', draft: '草稿', confirmed: '已确认' }
const sourceLabels = { assignment_document: '任务文档', user_primary_text: '主输入文本', user_supplement: '补充说明', user_revision: '用户修订' }
type Attempt = ExtractionAttempt
type Operation = 'extract' | 'save' | 'confirm'
type GroupKey = 'basic' | 'features' | 'tech' | 'other'
const groups: { key: GroupKey; index: string; label: string; fields: RequirementField[]; hint: string }[] = [
  { key: 'basic', index: '01', label: '项目基本信息', fields: ['project_topic', 'project_description', 'project_type', 'domain'], hint: '无信息请勾选“未提供”（保存为 null）；文本字段留空会被服务端拒绝。' },
  { key: 'features', index: '02', label: '功能需求', fields: ['required_features', 'preferred_features'], hint: '每行一条，可增删与重排；留空表示明确的空数组 []，不是 null。' },
  { key: 'tech', index: '03', label: '技术要求', fields: ['programming_languages', 'required_technologies', 'preferred_technologies'], hint: '语言以标签展示，点击文字编辑、× 删除；其他列表每行一条；空为 []，未提供为 null。' },
  { key: 'other', index: '04', label: '其他要求', fields: [], hint: '缺失信息与待澄清歧义；当前 schema 未提供其他独立限制字段。' },
]
const fullFields = new Set<RequirementField>(['project_topic', 'project_description', 'programming_languages'])
function restoreAttempt(key: string): { attempt: Attempt | null; error: string } {
  try {
    const raw = sessionStorage.getItem(key)
    if (!raw) return { attempt: null, error: '' }
    const value = JSON.parse(raw) as Attempt
    if (!value || typeof value.key !== 'string' || !/^[0-9a-f-]{36}$/i.test(value.key) || !['unknown', 'accepted', 'completed', 'failed'].includes(value.state) || !(value.baseline_id === null || typeof value.baseline_id === 'string') || !(value.extraction_id === null || typeof value.extraction_id === 'string')) throw new Error('invalid attempt')
    return { attempt: value, error: '' }
  } catch { return { attempt: null, error: '无法读取当前会话的提取记录。为避免重复计费，已停止发送；仍可刷新服务端结果。请恢复浏览器会话存储后重载页面。' } }
}
function asFailure(value: unknown): Failure {
  return value instanceof Failure ? value : new Failure(0, { error_code: 'CLIENT_ERROR', message: value instanceof Error ? value.message : '操作未完成，请先刷新核对。', retryable: true, details: null, request_id: '' })
}
function friendlyError(error: Failure): string {
  if (error.status === 401) return '会话已失效或无权访问此任务。本地编辑内容仍保留；请恢复有效会话后刷新，不会自动开始新会话或重新提取。'
  if (error.info.error_code === 'REQUIREMENTS_UNRESOLVED') return '服务端仍检测到未解决歧义或多语言硬要求缺少同组 ALL / ANY。请刷新核对，再编辑并保存新版本。'
  if (error.info.error_code === 'INVALID_REVISION') return '服务端未接受条目属性或引用关系。请检查字段类别、禁止语义、逻辑组及歧义引用；本地编辑已保留。'
  if (error.info.error_code === 'LLM_API_KEY_REQUIRED') return '尚未填写模型 API Key，本次模型调用未发起。请在设置中填写 Key（当前只支持 DeepSeek 兼容的 API）后重试；本地内容已保留，不会重复计费。'
  if (error.info.error_code === 'LLM_API_KEY_INVALID') return 'API Key 格式无效（要求 8–512 位可打印 ASCII 字符，不含空格）。请在设置中检查后重新填写；本地内容已保留。'
  if (error.info.error_code === 'REQUIREMENT_EVIDENCE_INVALID') return '需求证据缺失、归属无效或原文位置不一致，不能确认。请按错误详情逐条修改后保存新版本。'
  if (error.status === 409) return '任务状态或版本已发生变化。请先刷新核对最新版本；本地编辑内容未丢弃，也不会自动覆盖服务器版本。'
  if (/not_configured|configuration|provider_unavailable|extraction_disabled|extraction_misconfigured/i.test(error.info.error_code)) return '模型服务暂不可用。请在设置中检查 API Key 是否已填写；若已填写，请稍后重试或联系后端维护者。本地内容已保留。'
  if (error.status === 0 || error.status >= 500) return `${error.message} 本地内容已保留，请先刷新核对请求结果。`
  return error.message
}
function printable(value: unknown): string { return typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? '未提供' }
function shorten(value: string, limit = 42): string {
  const points = Array.from(value)
  return points.length > limit ? `${points.slice(0, limit).join('')}…` : value
}
function mergeRevisions(previous: Revision[], incoming: Revision[]): Revision[] {
  const revisions = new Map(previous.map(revision => [revision.revision_id, revision]))
  incoming.forEach(revision => revisions.set(revision.revision_id, revision))
  return [...revisions.values()].sort((a, b) => b.revision_number - a.revision_number)
}
function EvidenceView({ evidence, document }: { evidence: Evidence; document: NormalizedDocument }) {
  const { segment, quote, matches } = evidenceSlice(evidence, document)
  return <details className="requirement-evidence" id={`evidence-${evidence.evidence_id}`}>
    <summary><span className="evidence-source">{sourceLabels[evidence.source_kind]} · {fields.find(field => field.key === evidence.field)?.label ?? evidence.field}</span><span className="evidence-quote">“{shorten(evidence.original_text)}”</span></summary>
    <blockquote>{evidence.original_text}</blockquote>
    <dl><dt>证据编号</dt><dd>{evidence.evidence_id}</dd><dt>来源位置</dt><dd><pre>{printable(evidence.source_location)}</pre></dd><dt>关联条目</dt><dd>{evidence.requirement_item_id ?? '无'}</dd><dt>源片段</dt><dd>{evidence.segment_id ?? '无'} · 原文码点 [{evidence.start_offset ?? '—'}, {evidence.end_offset ?? '—'})</dd></dl>
    {quote !== null ? <><p className={matches ? 'verification-ok' : 'warning'}>{matches ? '已按原始片段 Unicode 码点截取，引文逐字一致。' : '引文与原始片段不一致，请人工核对后再确认。'}</p><pre className="source-quote">{quote}</pre></> : <p className="warning">{evidence.source_kind === 'user_revision' ? '用户修订来源，不对应原始材料片段；请结合版本变更核对。' : '无法核对原始片段：片段缺失或码点范围无效，请检查来源。'}</p>}
    {segment && <details><summary>查看完整原始片段（非标准化文本）</summary><pre>{segment.original_content}</pre><pre>{printable(segment.source_location)}</pre></details>}
  </details>
}

export default function RequirementsPanel({ document, onProgress, onRunId, onContinue, onOpenDeep, onOpenResult, view = 'requirements', autoExtract = false, onAutoExtract, searchStage = 4 }: {
  document: NormalizedDocument
  onProgress?: () => void
  onRunId?: (runId: string) => void
  onContinue?: () => void
  onOpenDeep?: () => void
  onOpenResult?: () => void
  view?: 'requirements' | 'search'
  autoExtract?: boolean
  onAutoExtract?: () => void
  searchStage?: 4 | 5 | 6
}) {
  const storageKey = `projectpilot.requirement-extraction.${document.document_id}`
  const [initial] = useState(() => restoreAttempt(storageKey))
  const [attempt, setAttempt] = useState(initial.attempt)
  const attemptRef = useRef(initial.attempt)
  const [storageError, setStorageError] = useState(initial.error)
  const [job, setJob] = useState<ExtractionJob | null>(null)
  const [revisions, setRevisions] = useState<Revision[]>([])
  const [latestId, setLatestId] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState('')
  const [drafts, setDrafts] = useState<Record<string, Draft>>({})
  const [error, setError] = useState<Failure | null>(null)
  const [notice, setNotice] = useState(autoExtract
    ? '材料提交时已授权自动提取，正在核对服务端状态…'
    : '正在恢复需求状态，仅查询，不会调用模型…')
  const [autoStarted, setAutoStarted] = useState(autoExtract)
  const [reading, setReading] = useState(false)
  const [synced, setSynced] = useState(false)
  const [checkedAttempt, setCheckedAttempt] = useState(false)
  const [operation, setOperation] = useState<Operation | null>(null)
  const [pollCycle, setPollCycle] = useState(0)
  const [pollPaused, setPollPaused] = useState(false)
  const [groupName, setGroupName] = useState('')
  const [groupOperator, setGroupOperator] = useState<'ALL' | 'ANY' | ''>('')
  const [activeGroup, setActiveGroup] = useState<GroupKey>('basic')
  const [openGroups, setOpenGroups] = useState<Record<GroupKey, boolean>>({ basic: true, features: true, tech: true, other: false })
  const [evidenceOpen, setEvidenceOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [extractionOpenOverride, setExtractionOpenOverride] = useState<boolean | null>(null)
  const [tagEdit, setTagEdit] = useState<{ index: number; value: string } | null>(null)
  const [newTag, setNewTag] = useState('')
  const tagEditRef = useRef<{ index: number; value: string } | null>(null)
  const lifetime = useRef<AbortController | null>(null)
  const autoTried = useRef(false)
  const readLock = useRef(false)
  const mutationLock = useRef(false)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const sectionRefs = useRef<Partial<Record<GroupKey, HTMLElement | null>>>({})
  const evidenceRef = useRef<HTMLDetailsElement | null>(null)
  const historyRef = useRef<HTMLDetailsElement | null>(null)
  const pending = isPending(job)
  const selected = revisions.find(revision => revision.revision_id === selectedId)
  const latest = !!selected && selected.revision_id === latestId
  const latestNumber = revisions.find(revision => revision.revision_id === latestId)?.revision_number
  const cachedDraft = selected ? drafts[selected.revision_id] : undefined
  // History always displays the immutable server version, never a cached edit.
  const draft = selected ? latest ? cachedDraft ?? createDraft(selected) : createDraft(selected) : null
  const staleDraft = selected && !latest && cachedDraft && (contentChanged(cachedDraft, selected) || cachedDraft.reason.trim()) ? cachedDraft : null
  const changed = !!selected && !!draft && contentChanged(draft, selected)
  const dirty = changed || !!draft?.reason.trim()
  const semanticChange = !!selected && !!draft && meaningChanged(draft, selected)
  const languagesIssue = draft ? languageGroupIssue(draft.items) : null
  const validationIssues = selected && draft ? draftIssues(draft, selected) : []
  const uncertainties = selected?.requirements.uncertain_information ?? []
  const unresolved = uncertainties.length > 0 && !(draft?.resolved && semanticChange)
  const editable = latest && !!draft?.editing && !operation && !pending
  const writeBlocked = !synced || reading || !!operation || pending || !latest
  const attemptUnsettled = attempt?.state === 'unknown' || attempt?.state === 'accepted'
  const canNewExtraction = synced && !reading && !operation && !storageError && mayStartExtraction(job, attempt)
  const canRecover = synced && checkedAttempt && attemptUnsettled && !pending && !reading && !operation && !storageError
  // Keep retry/refresh visible by default until a job succeeds; collapse only routine re-checks.
  const extractionNeedsAttention = !job || job.status === 'failed' || !!job.error || attemptUnsettled
  const extractionOpen = extractionOpenOverride ?? extractionNeedsAttention
  const stats = draft ? completeness(draft.requirements) : null
  const pendingChecks = validationIssues.length + (languagesIssue ? 1 : 0) + (unresolved ? 1 : 0)
  const missingCount = draft ? lines(draft.missingText).length : 0
  const evidenceCount = selected?.requirements.source_evidence.length ?? 0

  const active = (scope: AbortController) => lifetime.current === scope && !scope.signal.aborted
  function persistAttempt(value: Attempt) {
    try { sessionStorage.setItem(storageKey, JSON.stringify(value)) }
    catch { setStorageError('无法保存幂等键，已停止发送。请恢复浏览器会话存储后再刷新核对；不要清除已有记录来重试。'); throw new Error('无法持久化提取记录，未发起新的模型请求。') }
    attemptRef.current = value; setAttempt(value)
  }
  async function call<T>(scope: AbortController, path: string, method = 'GET', body: unknown = {}, key?: string): Promise<T> {
    const controller = new AbortController()
    const abort = () => controller.abort()
    scope.signal.addEventListener('abort', abort, { once: true })
    if (scope.signal.aborted) controller.abort()
    const timeout = window.setTimeout(abort, 20000)
    try { return await request<T>(path, method, controller.signal, body, key) }
    catch (value) {
      if (controller.signal.aborted && !scope.signal.aborted) throw new Failure(0, { error_code: 'REQUEST_TIMEOUT', message: '请求超时，结果尚未确认。', retryable: true, details: null, request_id: '' })
      throw value
    } finally { clearTimeout(timeout); scope.signal.removeEventListener('abort', abort) }
  }
  function reconcileAttempt(result: RequirementsResponse) {
    const saved = attemptRef.current
    const next = settleAttempt(saved, result.extraction)
    if (next && next !== saved) persistAttempt(next)
  }
  async function refresh(manual = false): Promise<boolean> {
    const scope = lifetime.current
    if (!scope || !active(scope) || readLock.current || mutationLock.current) return false
    readLock.current = true; setReading(true)
    if (manual) { setError(null); setPollPaused(false); setNotice('正在核对服务端状态，不会重新发送材料…') }
    try {
      const result = await call<RequirementsResponse>(scope, `/documents/${encodeURIComponent(document.document_id)}/requirements`)
      if (!active(scope)) return false
      setJob(result.extraction); setRevisions(previous => mergeRevisions(previous, result.revisions)); setLatestId(result.latest_revision_id)
      setSelectedId(previous => previous || result.latest_revision_id || result.revisions[0]?.revision_id || '')
      reconcileAttempt(result)
      setSynced(true); setCheckedAttempt(true)
      setNotice(isPending(result.extraction) ? '正在读取提取进度。离开页面不会再次提交；任务仍可能在服务器继续执行。' : result.extraction?.status === 'completed' ? '提取结果已恢复，请核对后单独确认。' : result.extraction?.status === 'failed' ? '提取未成功。请查看失败原因，修复后可显式重新提取。' : result.revisions.length ? '需求版本已恢复。' : '暂无需求版本。只有点击“提取需求”才会发送材料给模型。')
      if (manual) setPollCycle(value => value + 1)
      return true
    } catch (value) {
      if (active(scope)) { setError(asFailure(value)); setSynced(false); setCheckedAttempt(false) }
      return false
    } finally { if (active(scope)) { readLock.current = false; setReading(false) } }
  }
  useEffect(() => {
    const scope = new AbortController(); lifetime.current = scope
    readLock.current = false; mutationLock.current = false
    void refresh()
    return () => scope.abort()
    // The parent keys this component by document_id: no task shares state or work.
  }, [document.document_id])
  // The submit button is the one explicit authorization; this fires at most once per
  // successful submit and never after a restore, refresh or task switch.
  useEffect(() => {
    if (!autoExtract || autoTried.current) return
    if (!synced) {
      if (!reading && error) {
        autoTried.current = true; setAutoStarted(false); onAutoExtract?.()
        setNotice('自动提取未发送：读取服务端状态失败。请手动刷新核对后，再明确点击“提取需求”；不会自动重试。')
      }
      return
    }
    autoTried.current = true; onAutoExtract?.()
    if (job || attempt || !canNewExtraction) { setAutoStarted(false); return }
    if (!hasLlmApiKey()) {
      setAutoStarted(false)
      setNotice('材料解析完成。未检测到模型 API Key，自动提取未发起；请在设置中填写 Key（当前只支持 DeepSeek 兼容的 API）后点击“提取需求”，无需重新上传材料。')
      return
    }
    setAutoStarted(true)
    void extract(false, true)
  }, [autoExtract, synced, reading, error, job, attempt, canNewExtraction, onAutoExtract])
  useEffect(() => {
    if (!pending) { setPollPaused(false); return }
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let count = 0
    const deadline = Date.now() + 90000
    const tick = async () => {
      if (cancelled) return
      if (++count > 30 || Date.now() >= deadline) { setPollPaused(true); return }
      const ok = await refresh()
      if (cancelled) return
      if (!ok) { setPollPaused(true); return }
      timer = setTimeout(tick, 2500)
    }
    timer = setTimeout(tick, 2500)
    return () => { cancelled = true; if (timer) clearTimeout(timer) }
  }, [job?.extraction_id, pending, pollCycle])

  // 阶段信号只关心提取/最新版本/确认数量；刷新数组本身不触发重读。
  const confirmedCount = revisions.filter(revision => revision.status === 'confirmed').length
  useEffect(() => { onProgress?.() }, [job?.status, latestId, confirmedCount])

  async function mutate(kind: Operation, action: (scope: AbortController) => Promise<void>) {
    const scope = lifetime.current
    if (!scope || !active(scope) || mutationLock.current || readLock.current) return
    mutationLock.current = true; setOperation(kind); setError(null)
    try { await action(scope) }
    catch (value) {
      if (active(scope)) {
        setError(asFailure(value)); setSynced(false); setCheckedAttempt(false)
        setNotice('本次操作尚未确认成功。请先“刷新核对状态”，不会自动重试或生成新的幂等键。')
      }
    } finally { if (active(scope)) { mutationLock.current = false; setOperation(null) } }
  }
  async function extract(reuse = false, auto = false) {
    if (reuse ? !canRecover : !canNewExtraction) return
    if (!hasLlmApiKey()) {
      setNotice('尚未填写模型 API Key，本次提取未发送，也未生成新的幂等键。请在设置中填写 Key（当前只支持 DeepSeek 兼容的 API）后重试。')
      return
    }
    let accepted = false
    await mutate('extract', async scope => {
      const saved = reuse ? attemptRef.current : { key: crypto.randomUUID(), state: 'unknown' as const, baseline_id: job?.extraction_id ?? null, extraction_id: null }
      if (!saved) return
      persistAttempt({ ...saved, state: 'unknown' }); setCheckedAttempt(false)
      setNotice(auto
        ? '材料提交时已授权本次提取，正在使用你在设置中填写的 DeepSeek API Key…'
        : '已显式提交需求提取，将使用你在设置中填写的 DeepSeek API Key…')
      const result = await call<ExtractionJob>(scope, `/documents/${encodeURIComponent(document.document_id)}/requirement-extractions`, 'POST', {}, saved.key)
      if (!active(scope)) return
      persistAttempt({ ...saved, extraction_id: result.extraction_id, state: result.status === 'failed' ? 'failed' : result.status === 'completed' ? 'completed' : 'accepted' })
      setJob(result); setSynced(true); setPollPaused(false); setPollCycle(value => value + 1)
      setNotice(isPending(result) ? '提取请求已受理，正在等待结构化结果。' : '本次提取已有结果，正在读取需求版本；不会重新调用模型。')
      accepted = true
    })
    if (accepted) await refresh(true)
  }
  function updateDraft(update: (value: Draft) => Draft) {
    if (!selected) return
    setDrafts(previous => ({ ...previous, [selected.revision_id]: update(previous[selected.revision_id] ?? createDraft(selected)) }))
  }
  function updateItem(index: number, patch: Partial<ItemEdit>) {
    updateDraft(value => ({ ...value, resolved: false, items: value.items.map((item, i) => i === index ? { ...item, ...patch } : item) }))
  }
  async function saveRevision() {
    if (!selected || !draft || writeBlocked || !editable || validationIssues.length || !draft.reason.trim() || (!changed && selected.status !== 'confirmed')) return
    const payload = revisionPayload(draft, selected)
    await mutate('save', async scope => {
      const result = await call<Revision>(scope, `/requirement-revisions/${encodeURIComponent(selected.revision_id)}/revisions`, 'POST', payload)
      if (!active(scope)) return
      setRevisions(previous => mergeRevisions(previous, [result])); setLatestId(result.revision_id); setSelectedId(result.revision_id)
      setDrafts(previous => {
        const next = { ...previous, [result.revision_id]: createDraft(result) }
        delete next[selected.revision_id]
        return next
      })
      setNotice('新版本已保存，尚未确认。请再次核对，并单独点击“确认此版本”。')
    })
  }
  async function confirmRevision() {
    if (!selected || !draft || writeBlocked || dirty || unresolved || languagesIssue || selected.status === 'confirmed') return
    let confirmed = false
    await mutate('confirm', async scope => {
      const result = await call<Revision>(scope, `/requirement-revisions/${encodeURIComponent(selected.revision_id)}/confirm`, 'POST')
      if (!active(scope)) return
      setRevisions(previous => mergeRevisions(previous, [result]))
      setDrafts(previous => ({ ...previous, [result.revision_id]: createDraft(result) }))
      setNotice('此版本已确认，正在进入 GitHub 初筛；确认本身不会发起搜索。')
      confirmed = true
    })
    const scope = lifetime.current
    if (confirmed && scope && active(scope)) onContinue?.()
  }
  function selectRevision(id: string) {
    setSelectedId(id)
    const target = revisions.find(revision => revision.revision_id === id)
    setNotice(target?.status === 'confirmed'
      ? '正在查看已确认版本；确认记录不会被改写，也不能再次确认。'
      : id === latestId
        ? '正在查看最新版本；修改后需保存新版本，并单独确认。'
        : '正在查看历史版本（只读）；后端只允许编辑和确认最新版本。')
    setGroupName('')
    setGroupOperator('')
    setTagEdit(null)
    tagEditRef.current = null
    setNewTag('')
  }
  function groupStatus(key: GroupKey): { label: string; warn: boolean } {
    if (!draft) return { label: '等待提取', warn: false }
    if (key === 'other') return unresolved ? { label: '待检查', warn: true } : { label: '已填写', warn: false }
    const group = groups.find(item => item.key === key)
    const complete = !!group && group.fields.every(field => fieldProvided(draft.requirements, field))
    return complete ? { label: '已填写', warn: false } : { label: '待检查', warn: true }
  }
  function scrollToGroup(key: GroupKey) {
    setActiveGroup(key)
    setOpenGroups(value => value[key] ? value : { ...value, [key]: true })
    window.setTimeout(() => {
      const scroller = scrollRef.current
      const target = sectionRefs.current[key]
      if (!scroller || !target) return
      scroller.scrollTo({ top: Math.max(target.offsetTop - 8, 0), behavior: 'smooth' })
    }, 0)
  }
  function onEditorScroll() {
    const scroller = scrollRef.current
    if (!scroller) return
    const top = scroller.scrollTop + 48
    let current: GroupKey = 'basic'
    for (const group of groups) {
      const node = sectionRefs.current[group.key]
      if (node && node.offsetTop <= top) current = group.key
    }
    setActiveGroup(value => value === current ? value : current)
  }
  function revealEvidence(id?: string) {
    setEvidenceOpen(true)
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
      const target = id ? window.document.getElementById(`evidence-${id}`) : evidenceRef.current
      if (!target) return
      if (id && target instanceof HTMLDetailsElement) target.open = true
      target.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }))
  }
  function revealHistory() {
    setHistoryOpen(true)
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => historyRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })))
  }
  function languageTags(): string[] {
    const value = draft?.requirements.programming_languages
    return Array.isArray(value) ? value : []
  }
  function setLanguages(next: string[]) {
    updateDraft(value => updateField(value, 'programming_languages', next.join('\n'), false))
  }
  function startTagEdit(index: number, value: string) {
    const next = { index, value }
    tagEditRef.current = next
    setTagEdit(next)
  }
  function cancelTagEdit() {
    tagEditRef.current = null
    setTagEdit(null)
  }
  function commitTagEdit() {
    const current = tagEditRef.current
    if (!current) return
    tagEditRef.current = null
    setTagEdit(null)
    const value = current.value.trim()
    const list = [...languageTags()]
    if (value) list[current.index] = value
    else list.splice(current.index, 1)
    setLanguages(list)
  }
  function removeTag(index: number) {
    const list = [...languageTags()]
    list.splice(index, 1)
    cancelTagEdit()
    setLanguages(list)
  }
  function addTag() {
    const value = newTag.trim()
    if (!value) return
    setNewTag('')
    setLanguages([...languageTags(), value])
  }
  function applyToAllLanguages() {
    if (!groupName.trim() || !groupOperator) return
    updateDraft(value => ({ ...value, resolved: false, items: value.items.map(item => item.field === 'programming_languages' ? { ...item, logic_group_id: groupName.trim(), operator: groupOperator as 'ALL' | 'ANY' } : item) }))
  }
  function metadata(item: ItemEdit, index: number) {
    return <div className="item-metadata" key={`${item.requirement_item_id ?? 'new'}-${index}`}>
      <div className="item-text"><strong>{item.text}</strong><small>{item.value_path} · {item.requirement_item_id ?? '新条目（保存时分配 ID）'}</small></div>
      <div className="metadata-controls">
        <label>约束强度<select aria-label={`${item.value_path} 约束强度`} value={item.modality} disabled={!editable} onChange={event => updateItem(index, { modality: event.target.value as ItemEdit['modality'] })}><option value="required">必须</option><option value="preferred">偏好</option><option value="prohibited">禁止</option><option value="informational">信息说明</option></select></label>
        <label>组合逻辑<select aria-label={`${item.value_path} 组合逻辑`} value={item.operator} disabled={!editable} onChange={event => updateItem(index, { operator: event.target.value as ItemEdit['operator'] })}><option value="not_applicable">未指定 / 不适用</option><option value="ALL">ALL · 全部满足</option><option value="ANY">ANY · 任选其一</option></select></label>
        <label>逻辑组名<input aria-label={`${item.value_path} 逻辑组名`} value={item.logic_group_id ?? ''} disabled={!editable} placeholder="无分组" onChange={event => updateItem(index, { logic_group_id: event.target.value || null })} /></label>
      </div>
      <small className="item-origin">{sourceLabels[item.source_kind]}{item.evidence_ids.length ? ' · 原版本证据：' : ' · 暂无原版本证据'}{item.evidence_ids.map(id => <a key={id} href={`#evidence-${id}`} onClick={event => { event.preventDefault(); revealEvidence(id) }}>{id} </a>)}</small>
    </div>
  }
  function renderField(key: RequirementField) {
    if (!draft) return null
    const spec = fields.find(field => field.key === key)!
    const absent = draft.requirements[key] === null
    const list = spec.list
    const value = draft.requirements[key]
    const listValue = Array.isArray(value) ? value : []
    const entries = draft.items.map((item, index) => ({ item, index })).filter(entry => entry.item.field === key)
    const isLanguages = key === 'programming_languages'
    const fieldLabel = isLanguages
      ? <span className="field-label">{spec.label}</span>
      : <label className="field-label" htmlFor={`requirement-${key}`}>{spec.label}</label>
    let control: ReactNode
    if (isLanguages) {
      control = <div className="tag-field">
        <div className="tags">
          {absent ? <span className="field-empty">未提供（null）</span> : listValue.length ? listValue.map((tag, index) => editable && tagEdit?.index === index
            ? <span className="tag editing" key={`${tag}-${index}`}><input autoFocus aria-label={`编辑语言 ${tag}`} value={tagEdit.value} onChange={event => { const next = { index, value: event.target.value }; tagEditRef.current = next; setTagEdit(next) }} onBlur={commitTagEdit} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); commitTagEdit() } else if (event.key === 'Escape') { event.preventDefault(); cancelTagEdit() } }} /></span>
            : <span className="tag" key={`${tag}-${index}`}>{editable ? <button type="button" className="tag-label" title="点击编辑" onClick={() => startTagEdit(index, tag)}>{tag}</button> : <span>{tag}</span>}{editable && <button type="button" className="tag-remove" aria-label={`删除语言 ${tag}`} onClick={() => removeTag(index)}>×</button>}</span>)
            : <span className="field-empty">明确为空 []</span>}
          {editable && !absent && <span className="tag-add"><input aria-label="新增语言" value={newTag} placeholder="新语言" onChange={event => setNewTag(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); addTag() } }} /><button type="button" className="text-button" disabled={!newTag.trim()} onClick={addTag}>添加</button></span>}
        </div>
        {editable && <p className="tag-hint">输入后按 Enter 添加；空数组保存为 []，未提供请勾选“未提供”。</p>}
      </div>
    } else if (list) {
      control = editable
        ? <textarea id={`requirement-${key}`} className="field-input" rows={2} value={absent ? '' : draft.text[key]} disabled={absent} placeholder={absent ? '未提供（null）' : '每行一条；留空表示 []'} onChange={event => updateDraft(value => updateField(value, key, event.target.value, false))} />
        : <div className="tags read-only">{absent ? <span className="field-empty">未提供（null）</span> : listValue.length ? listValue.map((tag, index) => <span className="tag" key={`${tag}-${index}`}>{tag}</span>) : <span className="field-empty">明确为空 []</span>}</div>
    } else if (key === 'project_description') {
      control = editable
        ? <textarea id={`requirement-${key}`} className="field-input" rows={2} value={absent ? '' : draft.text[key]} disabled={absent} placeholder={absent ? '未提供（null）' : '输入项目描述'} onChange={event => updateDraft(value => updateField(value, key, event.target.value, false))} />
        : <p className="field-value">{absent ? <span className="field-empty">未提供（null）</span> : draft.text[key] || <span className="field-empty">空文本</span>}</p>
    } else {
      control = editable
        ? <input id={`requirement-${key}`} className="field-input" value={absent ? '' : draft.text[key]} disabled={absent} placeholder={absent ? '未提供（null）' : '输入内容'} onChange={event => updateDraft(value => updateField(value, key, event.target.value, false))} />
        : <p className="field-value">{absent ? <span className="field-empty">未提供（null）</span> : draft.text[key] || <span className="field-empty">空文本</span>}</p>
    }
    return <div className={`field-row${fullFields.has(key) ? ' full' : ''}`} key={key}>
      <div className="field-row-head">
        {fieldLabel}
        <div className="field-row-meta">
          {list && <span className="field-count">{absent ? 'null' : listValue.length ? `${listValue.length} 项` : '[]'}</span>}
          {editable && <label className="null-toggle" title="勾选后保存为 null；取消勾选并留空表示空文本或空数组 []。"><input type="checkbox" checked={absent} onChange={event => updateDraft(value => updateField(value, key, value.text[key], event.target.checked))} />未提供</label>}
        </div>
      </div>
      {control}
      <details className="field-items field-constraints">
        <summary>查看约束与来源 · {entries.length} 项</summary>
        <p className="muted">字段文本、增删与重排会同步到条目；保存时由服务器分配修订 ID 并重建证据。</p>
        {entries.length ? entries.map(({ item, index }) => metadata(item, index)) : <p className="muted">此字段暂无单独条目。</p>}
      </details>
    </div>
  }
  function renderAdvanced() {
    const languageItems = draft?.items.filter(item => item.field === 'programming_languages') ?? []
    return <details className="advanced-constraints">
      <summary>高级约束 · 语言 ALL / ANY 组合{languagesIssue ? <span className="fold-flag">待处理</span> : null}</summary>
      <p className="muted">多个语言不会自动推断为“全部”或“任选”。请给相关条目相同的逻辑组名，并显式选择 ALL / ANY；下方工具可一次应用到全部语言。</p>
      {languageItems.length > 1 ? <div className="metadata-controls">
        <label>共同语言组<input value={groupName} disabled={!editable} placeholder="例如：开发语言" onChange={event => setGroupName(event.target.value)} /></label>
        <label>语言组合<select value={groupOperator} disabled={!editable} onChange={event => setGroupOperator(event.target.value as typeof groupOperator)}><option value="">请明确选择</option><option value="ALL">ALL · 全部使用</option><option value="ANY">ANY · 任选一种</option></select></label>
        <button className="secondary" disabled={!editable || !groupName.trim() || !groupOperator} onClick={applyToAllLanguages}>应用到全部语言</button>
      </div> : <p className="muted">当前只有一个语言条目，无需设置 ALL / ANY；如需分组请先添加多个语言。</p>}
      {languagesIssue && <p className="warning">{languagesIssue}</p>}
    </details>
  }
  function renderOther() {
    if (!draft) return null
    return <div className="other-fields">
      <div className="field-row full">
        <label className="field-label" htmlFor="missing-information">缺失信息</label>
        <textarea id="missing-information" className="field-input" rows={3} value={draft.missingText} disabled={!editable} placeholder="没有缺失信息时留空，保存为空列表 []" onChange={event => updateDraft(value => ({ ...value, missingText: event.target.value }))} />
        <p className="tag-hint">每行一条；没有时留空，保存为 []。</p>
      </div>
      {!uncertainties.length ? <p className="muted">此版本没有标记待澄清歧义，仍请核对材料与约束。</p> : <><ul className="ambiguity-list">{uncertainties.map((item, index) => <li key={index}><strong>{fields.find(field => field.key === item.field)?.label ?? item.field}：{item.reason}</strong><p>{item.question}</p><small>关联条目：{item.requirement_item_ids.join('、') || '未关联'}</small><div className="evidence-links">{item.evidence_ids.map(id => <a key={id} href={`#evidence-${id}`} onClick={event => { event.preventDefault(); revealEvidence(id) }}>查看证据 {id}</a>)}</div></li>)}</ul><label className="resolution-check"><input type="checkbox" checked={draft.resolved && semanticChange} disabled={!editable || !semanticChange || !!languagesIssue} onChange={event => updateDraft(value => ({ ...value, resolved: event.target.checked }))} />已核对并解决上述歧义</label><p className="muted">请先修改相关字段或明确条目逻辑，再主动勾选。只有勾选后才将 uncertain_information 保存为 null；编辑不会自动清空歧义。</p></>}
      <p className="muted">当前 schema 未提供独立的“其他限制条件”字段；以上缺失信息与歧义为全部可编辑内容。</p>
    </div>
  }
  function renderGroup(group: typeof groups[number]) {
    const status = groupStatus(group.key)
    const open = openGroups[group.key]
    return <section className="requirement-group card" id={`requirements-group-${group.key}`} key={group.key} ref={node => { sectionRefs.current[group.key] = node }} aria-labelledby={`requirements-group-${group.key}-title`}>
      <header className="requirement-group-head">
        <div className="group-title"><span className="group-index">{group.index}</span><h3 id={`requirements-group-${group.key}-title`}>{group.label}</h3></div>
        <div className="group-head-actions">
          <span className={`badge ${status.warn ? 'warn' : 'success'}`}>{status.label}</span>
          <button type="button" className="group-toggle" aria-expanded={open} onClick={() => setOpenGroups(value => ({ ...value, [group.key]: !value[group.key] }))}>{open ? '收起' : '展开'}</button>
        </div>
      </header>
      {open && <div className="group-body">
        <p className="group-hint">{group.hint}</p>
        {group.key === 'other' ? renderOther() : <div className="field-grid">{group.fields.map(key => renderField(key))}</div>}
        {group.key === 'tech' && renderAdvanced()}
      </div>}
    </section>
  }
  return <section className="requirements-panel" data-view={view} aria-labelledby={view === 'requirements' ? 'requirements-title' : 'search-title'}>
    <div className="requirements-workspace">
      <aside className="requirements-overview card" aria-label="需求概览">
        <header className="overview-head">
          <span className="section-index">03 / REQUIREMENTS</span>
          <h2 id="requirements-title">需求确认</h2>
          <p className="overview-subtitle">检查 AI 提取结果，修改后保存并确认版本。</p>
          <div className="overview-badges">
            <span className={`badge ${job?.status === 'completed' ? 'success' : ''}`}>{job ? jobLabels[job.status] : '等待提取'}</span>
            {selected && <span className={`badge ${selected.status === 'confirmed' ? 'success' : ''}`}>{revisionLabels[selected.status]}{dirty ? ' · 有本地修改' : ''}</span>}
            {selected && !latest && <span className="badge warn">历史版本 · 只读</span>}
          </div>
        </header>
        {selected ? <div className="overview-version">
          <label htmlFor="revision-select">当前需求版本</label>
          <select id="revision-select" value={selectedId} disabled={!!operation} onChange={event => selectRevision(event.target.value)}>
            {revisions.map(revision => <option key={revision.revision_id} value={revision.revision_id}>版本 {revision.revision_number} · {revisionLabels[revision.status]}{revision.revision_id === latestId ? '（最新）' : '（只读）'}</option>)}
          </select>
          <div className="revision-meta"><span>创建于 {new Date(selected.created_at).toLocaleString('zh-CN')}</span>{selected.confirmed_at && <span>确认于 {new Date(selected.confirmed_at).toLocaleString('zh-CN')}</span>}{latestId && selectedId !== latestId && <button className="text-button" disabled={!!operation} onClick={() => selectRevision(latestId)}>回到最新版本</button>}</div>
        </div> : <p className="overview-empty">等待可展示的需求版本。</p>}
        <section className="completeness" aria-label="需求完整度">
          <div className="completeness-head"><span>需求完整度</span><strong>{stats ? `${stats.percent}%` : '—'}</strong></div>
          <div className="completeness-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={stats?.percent ?? 0}><i style={{ width: `${stats?.percent ?? 0}%` }} /></div>
          <small>{stats ? `${stats.filled}/${stats.total} 个字段已提供${pendingChecks ? ` · ${pendingChecks} 项待检查` : ' · 暂无待检查项'}` : '提取完成后按实际字段计算'}</small>
          <small className="completeness-note">按 9 个需求字段是否已提供计算；空数组 [] 视为已明确为空，null 视为未提供。</small>
        </section>
        <nav className="group-nav" aria-label="需求分组">
          {groups.map(group => {
            const status = groupStatus(group.key)
            return <button type="button" key={group.key} className={activeGroup === group.key ? 'active' : ''} disabled={!draft} onClick={() => scrollToGroup(group.key)}>
              <span className="nav-index">{group.index}</span><span className="nav-label">{group.label}</span><span className={`nav-status${status.warn ? ' warn' : ''}`}>{status.label}</span>
            </button>
          })}
        </nav>
        <button type="button" className={`overview-missing${unresolved ? ' unresolved' : ''}`} disabled={!draft} onClick={() => scrollToGroup('other')}>
          <span className="overview-missing-title">缺失信息与待澄清{unresolved && <em className="missing-flag">待处理</em>}</span>
          <small>{draft ? (missingCount || uncertainties.length ? `缺失 ${missingCount} 条 · 待澄清 ${uncertainties.length} 项` : '未标记缺失信息或歧义') : '等待需求版本'}</small>
        </button>
        <div className="overview-links">
          <button type="button" className="overview-link" disabled={!selected} onClick={() => revealEvidence()}>来源证据<span>{evidenceCount} 条</span></button>
          <button type="button" className="overview-link" disabled={!selected} onClick={revealHistory}>版本记录<span>{revisions.length} 个版本</span></button>
        </div>
        <div className="overview-feedback" aria-live="polite">
          <p>{(reading || operation || (pending && !pollPaused)) && <span className="spinner" />}{notice}</p>
          {pollPaused && pending && <p className="warning">自动读取已暂停（达到时限或读取失败），不代表提取失败。请稍后手动刷新核对；不会自动重新提取。</p>}
          {error && <div className="error" role="alert"><strong>{friendlyError(error)}</strong><small>{error.info.error_code}{error.info.request_id && ` · 请求编号 ${error.info.request_id}`}</small><button disabled={reading || !!operation} onClick={() => void refresh(true)}>刷新核对，保留编辑</button>{error.info.details != null && <details><summary>错误详情</summary><pre>{printable(error.info.details)}</pre></details>}</div>}
        </div>
        {storageError && <p className="warning" role="alert">{storageError}</p>}
        {attemptUnsettled && !pending && <div className="warning overview-warning"><p>已保留本次提取的幂等键，尚未核实对应结果。必须先 GET 刷新核对；即使暂未查询到结果，也不会自动生成新键或重新付费。</p><button className="secondary" disabled={!canRecover} onClick={() => void extract(true)}>核对后重试同一次提取（沿用幂等键）</button></div>}
        {job?.error && <p className="warning" role="alert">{friendlyError(new Failure(422, job.error))}（{job.error.error_code}）</p>}
        <details className="overview-extraction" open={extractionOpen} onToggle={event => { if (event.currentTarget.open !== extractionOpen) setExtractionOpenOverride(event.currentTarget.open) }}>
          <summary>提取与核对{job ? ` · ${jobLabels[job.status]}` : ''}</summary>
          <div className="overview-extraction-body">
            <div className="overview-extract-actions">
              <button className="primary" disabled={!canNewExtraction} onClick={() => void extract()}>{operation === 'extract' ? '正在提交…' : job?.status === 'failed' || attempt?.state === 'failed' ? '重新提取（新请求）' : '提取需求'}</button>
              <button className="secondary" disabled={reading || !!operation} onClick={() => void refresh(true)}>{reading ? '正在读取…' : '刷新核对'}</button>
            </div>
            <p className="overview-cost">提取会调用模型并可能产生费用；刷新不会重复调用。</p>
            <details className="extraction-details">
              <summary>说明与记录{job ? ` · ${jobLabels[job.status]}` : ''}</summary>
              <p>{autoStarted ? '提交材料时已明确授权本次自动提取一次；刷新、预览、切换子界面或恢复任务都不会再次调用模型。' : '只有显式点击“提取需求/重新提取”才会把已解析文档发送给你在设置中填写的 DeepSeek API Key 对应的官方端点，可能产生 API 费用；刷新、预览或切换任务不会触发模型调用。请确认材料中不含不应发送的敏感信息。'}</p>
              <p className="muted">费用以服务方实际计量为准；API Key 仅保存在当前页面内存，不写入浏览器存储或服务端。</p>
              {job && <dl><dt>模型</dt><dd>{job.model || '服务端未提供'}</dd><dt>提示词版本</dt><dd>{job.prompt_version || '服务端未提供'}</dd><dt>提取 ID</dt><dd>{job.extraction_id}</dd><dt>创建时间</dt><dd>{new Date(job.created_at).toLocaleString('zh-CN')}</dd></dl>}
              {job?.usage ? <><h4>服务端实际用量</h4><pre>{printable(job.usage)}</pre><p className="muted">仅展示返回的用量，不据此编造费用或性能数据。</p></> : job ? <p className="muted">服务端暂未提供用量数据。</p> : null}
            </details>
            {latest && selected?.status !== 'confirmed' && <p className="overview-gate">确认此版本后，才能在下一阶段单独授权 GitHub 搜索；确认本身不会发起搜索。</p>}
            {selected && !latest && <p className="overview-gate">历史版本只读。后端只允许确认最新版本，此版本不会被确认，也不会触发搜索。</p>}
          </div>
        </details>
      </aside>
      <div className="requirements-editor">
        {!selected || !draft ? <div className="requirements-empty card"><h3>{pending ? '正在提取结构化需求' : '还没有可展示的需求版本'}</h3><p>{pending ? '只在任务排队、提取或校验期间有限轮询。你可以稍后从历史任务恢复结果。' : '请先主动提取，或刷新恢复服务端已有版本。提取失败不会清除原输入材料。'}</p></div> : <>
          <div className="requirements-editor-scroll" ref={scrollRef} onScroll={onEditorScroll}>
            {!latest && <p className="warning editor-banner">这是服务端历史版本，只读展示，不会被覆盖，也不能保存或确认。请切换到最新版本{latestNumber ? `（版本 ${latestNumber}）` : ''}核对后编辑；后端只允许操作最新版本。</p>}
            {staleDraft && <details className="stale-draft" open><summary>已保留此版本未提交的本地编辑（仅供核对和复制）</summary><p className="muted">服务端已有更新版本，不能覆盖提交。以下不是已保存的历史内容；请与最新版本对照后重新编辑。</p><dl>{fields.map(field => <div key={field.key}><dt>{field.label}</dt><dd><pre>{staleDraft.requirements[field.key] === null ? '未提供（null）' : staleDraft.text[field.key] || (field.list ? '空列表 []' : '空文本')}</pre></dd></div>)}</dl><h4>本地条目逻辑</h4>{staleDraft.items.map((item, index) => <p key={index}>{item.value_path} · {item.text} · {item.modality} / {item.operator} / {item.logic_group_id ?? '无组'}</p>)}<h4>本地缺失信息</h4><pre>{staleDraft.missingText || '无'}</pre><p>本地歧义核对：{staleDraft.resolved ? '已勾选（未提交）' : '未勾选'}</p><h4>本地保存理由</h4><pre>{staleDraft.reason || '未填写'}</pre></details>}
            {latest && selected.status === 'confirmed' && <div className="confirmed-notice"><p>已确认版本不可覆写。可从此版本编辑并保存一个新的草稿，之后仍需单独确认。</p><button className="secondary" disabled={!!operation || pending || draft.editing} onClick={() => updateDraft(value => ({ ...value, editing: true }))}>{draft.editing ? '正在编辑新草稿（尚未保存）' : '新建修订草稿'}</button></div>}
            {groups.map(renderGroup)}
            <details className="requirements-fold card" id="requirements-evidence" open={evidenceOpen} onToggle={event => setEvidenceOpen(event.currentTarget.open)} ref={evidenceRef}>
              <summary><span className="fold-title">来源证据</span><span className="fold-count">{evidenceCount} 条</span></summary>
              <div className="fold-body">
                <p className="muted">以下为所选版本的原始证据，只读且不允许伪造。核对使用 source_segments.original_content 内的 Unicode 码点范围，不使用 normalized_text；修改后的证据在保存新版本时由服务器重建。</p>
                {!evidenceCount && <p className="warning">此版本未提供来源证据，请人工核对输入材料。</p>}
                {selected.requirements.source_evidence.map(evidence => <EvidenceView key={evidence.evidence_id} evidence={evidence} document={document} />)}
              </div>
            </details>
            <details className="requirements-fold card" id="requirements-history" open={historyOpen} onToggle={event => setHistoryOpen(event.currentTarget.open)} ref={historyRef}>
              <summary><span className="fold-title">版本变更记录</span><span className="fold-count">{revisions.length} 个版本</span></summary>
              <div className="fold-body">
                <p className="muted">切换版本只读取服务端已保存内容；历史版本只读，不会被覆盖。修改内容与保存理由按服务端 change_log 原样展示。</p>
                <ul className="revision-history-list">{revisions.map(revision => <li key={revision.revision_id} className={revision.revision_id === selectedId ? 'current' : ''}>
                  <div className="revision-history-head"><strong>版本 {revision.revision_number}</strong><span className={`badge ${revision.status === 'confirmed' ? 'success' : ''}`}>{revisionLabels[revision.status]}</span>{revision.revision_id === latestId && <span className="badge info">最新</span>}{revision.revision_id === selectedId && <span className="revision-current">当前查看</span>}<button className="secondary" disabled={!!operation || revision.revision_id === selectedId} onClick={() => selectRevision(revision.revision_id)}>查看此版本</button></div>
                  <small>创建于 {new Date(revision.created_at).toLocaleString('zh-CN')}{revision.confirmed_at && ` · 确认于 ${new Date(revision.confirmed_at).toLocaleString('zh-CN')}`}<code className="revision-id">{revision.revision_id}</code></small>
                  {revision.revision_id === selectedId && <div className="revision-history-change"><span>父版本：{revision.parent_revision_id ?? '首个版本'}</span><pre>{printable(revision.change_log)}</pre></div>}
                </li>)}</ul>
              </div>
            </details>
          </div>
          <div className="requirements-actionbar card">
            {validationIssues.length > 0 && <div className="warning action-issues" role="alert"><strong>保存前请检查</strong><ul>{validationIssues.map(issue => <li key={issue}>{issue}</li>)}</ul></div>}
            <label className="reason-field" htmlFor="change-reason">保存理由 <span>保存新版本时必填</span></label>
            <textarea id="change-reason" className="reason-input" rows={2} maxLength={2000} value={draft.reason} disabled={!editable} placeholder="说明修改了什么、为何这样修改…" onChange={event => updateDraft(value => ({ ...value, reason: event.target.value }))} />
            <div className="revision-buttons"><button className="primary" disabled={writeBlocked || !editable || validationIssues.length > 0 || !draft.reason.trim() || (!changed && selected.status !== 'confirmed')} onClick={() => void saveRevision()}>{operation === 'save' ? '正在保存…' : !latest ? '历史版本不可保存' : '保存为新版本'}</button><button className="secondary confirm-button" disabled={writeBlocked || dirty || unresolved || !!languagesIssue || selected.status === 'confirmed'} onClick={() => void confirmRevision()}>{operation === 'confirm' ? '正在确认…' : selected.status === 'confirmed' ? '此版本已确认' : !latest ? '仅最新版本可确认' : '确认此版本'}</button>{selected.status === 'confirmed' && <button type="button" className="primary continue-button" disabled={!!operation || pending || !onContinue} onClick={() => onContinue?.()}>进入 GitHub 初筛 →</button>}{!latest && <button type="button" className="secondary history-return" disabled={!!operation || pending || !latestId} onClick={() => { if (latestId) selectRevision(latestId) }}>回到最新版本</button>}</div>
            <p className="muted action-help">{!latest ? `历史版本只读：后端只允许编辑和确认最新版本${latestNumber ? `（版本 ${latestNumber}）` : ''}。如需采用此版本内容，请回到最新版本修改后保存并确认。` : dirty ? '有未保存的编辑，请先填写理由并保存新版本，再单独确认。' : unresolved ? '仍有未解决歧义，确认已禁用。' : languagesIssue ? languagesIssue : !synced ? '请先刷新核对状态，成功读取后才能提交。' : '保存不会自动确认或搜索 GitHub；切换版本保留本页编辑，离开前请先保存。'}</p>
          </div>
        </>}
      </div>
    </div>
    {selected?.status === 'confirmed' && <SearchPanel key={`${document.task_id}.${selected.revision_id}`} taskId={document.task_id} revisionId={selected.revision_id} enabled={synced && !reading && !operation && !pending && !dirty && !draft?.editing} stage={searchStage} onProgress={onProgress} onRunId={onRunId} onOpenDeep={onOpenDeep} onOpenResult={onOpenResult} />}
  </section>
}
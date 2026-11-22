import { useEffect, useRef, useState } from 'react'
import { Failure, request } from './api'
import SearchPanel from './SearchPanel'
import type { NormalizedDocument } from './api'
import {
  contentChanged, createDraft, draftIssues, evidenceSlice, fields, isPending, languageGroupIssue,
  mayStartExtraction, meaningChanged, revisionPayload, settleAttempt, updateField,
} from './requirements'
import type { Draft, Evidence, ExtractionAttempt, ExtractionJob, ItemEdit, RequirementsResponse, Revision } from './requirements'

const jobLabels = { queued: '排队中', extracting: '提取中', validating: '校验中', completed: '提取完成', failed: '提取失败' }
const revisionLabels = { extracted: '待核对', draft: '草稿', confirmed: '已确认' }
const sourceLabels = { assignment_document: '任务文档', user_primary_text: '主输入文本', user_supplement: '补充说明', user_revision: '用户修订' }
type Attempt = ExtractionAttempt
type Operation = 'extract' | 'save' | 'confirm'
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
  if (error.status === 409) return '任务状态或版本已发生变化。请先刷新核对最新版本；本地编辑内容未丢弃，也不会自动覆盖服务器版本。'
  if (/not_configured|configuration|api_key|provider_unavailable|extraction_disabled|extraction_misconfigured/i.test(error.info.error_code)) return '模型服务尚未配置或暂不可用，请联系后端维护者处理。不要在前端填写服务密钥；本地内容已保留。'
  if (error.status === 0 || error.status >= 500) return `${error.message} 本地内容已保留，请先刷新核对请求结果。`
  return error.message
}
function printable(value: unknown): string { return typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? '未提供' }
function mergeRevisions(previous: Revision[], incoming: Revision[]): Revision[] {
  const revisions = new Map(previous.map(revision => [revision.revision_id, revision]))
  incoming.forEach(revision => revisions.set(revision.revision_id, revision))
  return [...revisions.values()].sort((a, b) => b.revision_number - a.revision_number)
}
function EvidenceView({ evidence, document }: { evidence: Evidence; document: NormalizedDocument }) {
  const { segment, quote, matches } = evidenceSlice(evidence, document)
  return <details className="requirement-evidence" id={`evidence-${evidence.evidence_id}`}>
    <summary>{sourceLabels[evidence.source_kind]} · {fields.find(field => field.key === evidence.field)?.label ?? evidence.field} <span>{evidence.evidence_id}</span></summary>
    <blockquote>{evidence.original_text}</blockquote>
    <dl><dt>来源位置</dt><dd><pre>{printable(evidence.source_location)}</pre></dd><dt>关联条目</dt><dd>{evidence.requirement_item_id ?? '无'}</dd><dt>源片段</dt><dd>{evidence.segment_id ?? '无'} · 原文码点 [{evidence.start_offset ?? '—'}, {evidence.end_offset ?? '—'})</dd></dl>
    {quote !== null ? <><p className={matches ? 'verification-ok' : 'warning'}>{matches ? '已按原始片段 Unicode 码点截取，引文逐字一致。' : '引文与原始片段不一致，请人工核对后再确认。'}</p><pre className="source-quote">{quote}</pre></> : <p className="warning">{evidence.source_kind === 'user_revision' ? '用户修订来源，不对应原始材料片段；请结合版本变更核对。' : '无法核对原始片段：片段缺失或码点范围无效，请检查来源。'}</p>}
    {segment && <details><summary>查看完整原始片段（非标准化文本）</summary><pre>{segment.original_content}</pre><pre>{printable(segment.source_location)}</pre></details>}
  </details>
}

export default function RequirementsPanel({ document }: { document: NormalizedDocument }) {
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
  const [notice, setNotice] = useState('正在恢复需求状态，仅查询，不会调用模型…')
  const [reading, setReading] = useState(false)
  const [synced, setSynced] = useState(false)
  const [checkedAttempt, setCheckedAttempt] = useState(false)
  const [operation, setOperation] = useState<Operation | null>(null)
  const [pollCycle, setPollCycle] = useState(0)
  const [pollPaused, setPollPaused] = useState(false)
  const [groupName, setGroupName] = useState('')
  const [groupOperator, setGroupOperator] = useState<'ALL' | 'ANY' | ''>('')
  const lifetime = useRef<AbortController | null>(null)
  const readLock = useRef(false)
  const mutationLock = useRef(false)
  const pending = isPending(job)
  const selected = revisions.find(revision => revision.revision_id === selectedId)
  const latest = !!selected && selected.revision_id === latestId
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
      setNotice(isPending(result.extraction) ? '正在读取提取进度。离开页面不会再次提交；任务仍可能在服务器继续执行。' : result.extraction?.status === 'completed' ? '提取结果已恢复，请核对字段、来源及歧义，再单独确认。' : result.extraction?.status === 'failed' ? '提取未成功。请查看失败原因，修复后可显式重新提取。' : result.revisions.length ? '需求版本已恢复。' : '暂无需求版本。只有点击“提取需求”才会发送材料给模型。')
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
  async function extract(reuse = false) {
    if (reuse ? !canRecover : !canNewExtraction) return
    let accepted = false
    await mutate('extract', async scope => {
      const saved = reuse ? attemptRef.current : { key: crypto.randomUUID(), state: 'unknown' as const, baseline_id: job?.extraction_id ?? null, extraction_id: null }
      if (!saved) return
      persistAttempt({ ...saved, state: 'unknown' }); setCheckedAttempt(false)
      setNotice('已显式提交需求提取，将使用后端配置的 DeepSeek 服务…')
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
    await mutate('confirm', async scope => {
      const result = await call<Revision>(scope, `/requirement-revisions/${encodeURIComponent(selected.revision_id)}/confirm`, 'POST')
      if (!active(scope)) return
      setRevisions(previous => mergeRevisions(previous, [result]))
      setDrafts(previous => ({ ...previous, [result.revision_id]: createDraft(result) }))
      setNotice('此版本已确认。当前阶段仅确认需求，尚未搜索 GitHub。')
    })
  }
  function metadata(item: ItemEdit, index: number) {
    return <div className="item-metadata" key={`${item.requirement_item_id ?? 'new'}-${index}`}>
      <div className="item-text"><strong>{item.text}</strong><small>{item.value_path} · {item.requirement_item_id ?? '新条目（保存时分配 ID）'}</small></div>
      <div className="metadata-controls">
        <label>约束强度<select aria-label={`${item.value_path} 约束强度`} value={item.modality} disabled={!editable} onChange={event => updateItem(index, { modality: event.target.value as ItemEdit['modality'] })}><option value="required">必须</option><option value="preferred">偏好</option><option value="prohibited">禁止</option><option value="informational">信息说明</option></select></label>
        <label>组合逻辑<select aria-label={`${item.value_path} 组合逻辑`} value={item.operator} disabled={!editable} onChange={event => updateItem(index, { operator: event.target.value as ItemEdit['operator'] })}><option value="not_applicable">未指定 / 不适用</option><option value="ALL">ALL · 全部满足</option><option value="ANY">ANY · 任选其一</option></select></label>
        <label>逻辑组名<input aria-label={`${item.value_path} 逻辑组名`} value={item.logic_group_id ?? ''} disabled={!editable} placeholder="无分组" onChange={event => updateItem(index, { logic_group_id: event.target.value || null })} /></label>
      </div>
      <small className="item-origin">{sourceLabels[item.source_kind]}{item.evidence_ids.length ? ' · 原版本证据：' : ' · 暂无原版本证据'}{item.evidence_ids.map(id => <a key={id} href={`#evidence-${id}`}>{id} </a>)}</small>
    </div>
  }
  return <section className="card requirements-panel" aria-labelledby="requirements-title">
    <div className="card-title"><div><span className="section-index">03 / REQUIREMENTS</span><h2 id="requirements-title">需求提取与确认</h2></div><span className={`badge ${selected?.status === 'confirmed' ? 'success' : ''}`}>{job ? jobLabels[job.status] : '等待提取'}</span></div>
    <p className="stage-boundary">确认需求不会自动搜索 GitHub；已确认版本可在下方单独授权搜索预算并查看候选证据。</p>
    <div className="extraction-consent"><div><strong>由你决定何时发送给模型</strong><p>点击“提取需求”或“重新提取”后，已解析的文档与补充文本将发送到后端配置的 DeepSeek 服务，可能产生 API 费用。仅预览、刷新或切换任务不会触发模型调用。请先检查材料中是否含有不应发送的敏感信息。</p><small>费用以服务方实际计量为准；不在前端配置模型密钥。</small></div><div className="requirement-actions">
      <button className="primary" disabled={!canNewExtraction} onClick={() => void extract()}>{operation === 'extract' ? '正在提交…' : job?.status === 'failed' || attempt?.state === 'failed' ? '重新提取（新请求）' : '提取需求'}</button>
      <button className="secondary" disabled={reading || !!operation} onClick={() => void refresh(true)}>{reading ? '正在读取…' : '刷新核对状态'}</button>
    </div></div>
    {storageError && <p className="warning" role="alert">{storageError}</p>}
    {attemptUnsettled && !pending && <div className="warning"><p>已保留本次提取的幂等键，尚未核实对应结果。必须先 GET 刷新核对；即使暂未查询到结果，也不会自动生成新键或重新付费。</p><button className="secondary" disabled={!canRecover} onClick={() => void extract(true)}>核对后重试同一次提取（沿用幂等键）</button></div>}
    <div className="requirement-feedback" aria-live="polite"><p>{(reading || operation || (pending && !pollPaused)) && <span className="spinner" />}{notice}</p>{pollPaused && pending && <p className="warning">自动读取已暂停（达到时限或读取失败），不代表提取失败。请稍后手动刷新核对；不会自动重新提取。</p>}{error && <div className="error" role="alert"><strong>{friendlyError(error)}</strong><small>{error.info.error_code}{error.info.request_id && ` · 请求编号 ${error.info.request_id}`}</small><button disabled={reading || !!operation} onClick={() => void refresh(true)}>刷新核对，保留编辑</button></div>}</div>
    {job && <details className="extraction-details"><summary>提取记录 · {jobLabels[job.status]} · {new Date(job.created_at).toLocaleString('zh-CN')}</summary><dl><dt>模型</dt><dd>{job.model || '服务端未提供'}</dd><dt>提示词版本</dt><dd>{job.prompt_version || '服务端未提供'}</dd><dt>提取 ID</dt><dd>{job.extraction_id}</dd></dl>{job.error && <div className="error"><strong>{friendlyError(new Failure(422, job.error))}</strong><small>{job.error.error_code}</small></div>}{job.usage ? <><h4>服务端实际用量</h4><pre>{printable(job.usage)}</pre><p className="muted">仅展示返回的用量，不据此编造费用或性能数据。</p></> : <p className="muted">服务端暂未提供用量数据。</p>}</details>}
    {job?.error && <p className="warning" role="alert">{friendlyError(new Failure(422, job.error))}（{job.error.error_code}）</p>}
    {!selected || !draft ? <div className="requirements-empty"><h3>{pending ? '正在提取结构化需求' : '还没有可展示的需求版本'}</h3><p>{pending ? '只在任务排队、提取或校验期间有限轮询。你可以稍后从历史任务恢复结果。' : '请先主动提取，或刷新恢复服务端已有版本。提取失败不会清除原输入材料。'}</p></div> : <>
      <div className="revision-toolbar"><label>需求版本<select value={selectedId} disabled={!!operation} onChange={event => { setSelectedId(event.target.value); setGroupName(''); setGroupOperator('') }}>{revisions.map(revision => <option key={revision.revision_id} value={revision.revision_id}>版本 {revision.revision_number} · {revisionLabels[revision.status]}{revision.revision_id === latestId ? '（最新）' : '（只读）'}</option>)}</select></label><span className={`badge ${selected.status === 'confirmed' ? 'success' : ''}`}>{revisionLabels[selected.status]}{dirty ? ' · 有本地修改' : ''}</span>{selectedId !== latestId && latestId && <button className="secondary" disabled={!!operation} onClick={() => { setSelectedId(latestId); setGroupName(''); setGroupOperator('') }}>查看最新版本</button>}</div>
      <div className="revision-info"><span>创建于 {new Date(selected.created_at).toLocaleString('zh-CN')}</span>{selected.confirmed_at && <span>确认于 {new Date(selected.confirmed_at).toLocaleString('zh-CN')}</span>}<small>{selected.revision_id}</small></div>
      {!latest && <p className="warning">这是服务端历史版本，只读展示，不会被覆盖。请切换最新版本核对后编辑。</p>}
      {staleDraft && <details className="stale-draft" open><summary>已保留此版本未提交的本地编辑（仅供核对和复制）</summary><p className="muted">服务端已有更新版本，不能覆盖提交。以下不是已保存的历史内容；请与最新版本对照后重新编辑。</p><dl>{fields.map(field => <div key={field.key}><dt>{field.label}</dt><dd><pre>{staleDraft.requirements[field.key] === null ? '未提供（null）' : staleDraft.text[field.key] || (field.list ? '空列表 []' : '空文本')}</pre></dd></div>)}</dl><h4>本地条目逻辑</h4>{staleDraft.items.map((item, index) => <p key={index}>{item.value_path} · {item.text} · {item.modality} / {item.operator} / {item.logic_group_id ?? '无组'}</p>)}<h4>本地缺失信息</h4><pre>{staleDraft.missingText || '无'}</pre><p>本地歧义核对：{staleDraft.resolved ? '已勾选（未提交）' : '未勾选'}</p><h4>本地保存理由</h4><pre>{staleDraft.reason || '未填写'}</pre></details>}
      {latest && selected.status === 'confirmed' && <div className="confirmed-notice"><p>已确认版本不可覆写。可从此版本编辑并保存一个新的草稿，之后仍需单独确认。</p><button className="secondary" disabled={!!operation || pending || draft.editing} onClick={() => updateDraft(value => ({ ...value, editing: true }))}>{draft.editing ? '正在编辑新草稿（尚未保存）' : '新建修订草稿'}</button></div>}
      <div className="requirement-fields">{fields.map(field => {
        const absent = draft.requirements[field.key] === null
        return <section className={`requirement-field ${field.key === 'project_description' || field.key === 'programming_languages' ? 'wide' : ''}`} key={field.key}>
          <div className="field-heading"><label htmlFor={`requirement-${field.key}`}>{field.label}</label><label className="null-toggle"><input type="checkbox" checked={absent} disabled={!editable} onChange={event => updateDraft(value => updateField(value, field.key, value.text[field.key], event.target.checked))} />未提供（null）</label></div>
          {field.list || field.key === 'project_description' ? <textarea id={`requirement-${field.key}`} rows={field.list ? 3 : 4} value={absent ? '' : draft.text[field.key]} disabled={!editable || absent} placeholder={absent ? '未提供此信息' : field.list ? '每行一条；空白表示明确的空列表 []' : '输入项目描述'} onChange={event => updateDraft(value => updateField(value, field.key, event.target.value, false))} /> : <input id={`requirement-${field.key}`} value={absent ? '' : draft.text[field.key]} disabled={!editable || absent} placeholder={absent ? '未提供此信息' : '输入内容'} onChange={event => updateDraft(value => updateField(value, field.key, event.target.value, false))} />}
          <small className="field-hint">{absent ? '未提供，不等于明确留空。' : field.list ? `${(draft.requirements[field.key] as string[]).length} 项 · 一行一条，空行忽略；零项为 []，不是 null。` : '服务端要求非空文本；暂无信息请勾选“未提供”。'}</small>
          {field.key === 'programming_languages' && draft.items.filter(item => item.field === field.key).length > 1 && <div className="language-group"><strong>明确语言之间的关系</strong><p>多个语言不自动推断为“全部”或“任选”。请给相关条目相同的逻辑组名，并显式选择 ALL / ANY；下方工具可一次应用到全部语言。</p><div className="metadata-controls"><label>共同语言组<input value={groupName} disabled={!editable} placeholder="例如：开发语言" onChange={event => setGroupName(event.target.value)} /></label><label>语言组合<select value={groupOperator} disabled={!editable} onChange={event => setGroupOperator(event.target.value as typeof groupOperator)}><option value="">请明确选择</option><option value="ALL">ALL · 全部使用</option><option value="ANY">ANY · 任选一种</option></select></label><button className="secondary" disabled={!editable || !groupName.trim() || !groupOperator} onClick={() => updateDraft(value => ({ ...value, resolved: false, items: value.items.map(item => item.field === 'programming_languages' ? { ...item, logic_group_id: groupName.trim(), operator: groupOperator as 'ALL' | 'ANY' } : item) }))}>应用到全部语言</button></div>{languagesIssue && <p className="warning">{languagesIssue}</p>}</div>}
          <details className="field-items" open={field.key === 'programming_languages'}><summary>条目约束与来源 · {draft.items.filter(item => item.field === field.key).length} 项</summary><p className="muted">字段文本、增删与重排会同步到条目；保存时由服务器分配修订 ID 并重建证据。新增条目按所在字段设为必须、偏好或信息说明；禁止条件请显式选择“禁止”，语言强度也需自行明确。</p>{draft.items.map((item, index) => item.field === field.key ? metadata(item, index) : null)}</details>
        </section>
      })}</div>
      {draft.items.some(item => !fields.some(field => field.key === item.field)) && <details><summary>其他需求条目</summary>{draft.items.map((item, index) => !fields.some(field => field.key === item.field) ? metadata(item, index) : null)}</details>}
      <section className="ambiguities"><h3>缺失信息与待澄清歧义</h3><label htmlFor="missing-information">缺失信息（每行一条，可编辑）</label><textarea id="missing-information" rows={3} value={draft.missingText} disabled={!editable} placeholder="没有缺失信息时留空，保存为空列表 []" onChange={event => updateDraft(value => ({ ...value, missingText: event.target.value }))} />
        {!uncertainties.length ? <p className="muted">此版本没有标记待澄清歧义，仍请核对材料与约束。</p> : <><ul className="ambiguity-list">{uncertainties.map((item, index) => <li key={index}><strong>{fields.find(field => field.key === item.field)?.label ?? item.field}：{item.reason}</strong><p>{item.question}</p><small>关联条目：{item.requirement_item_ids.join('、') || '未关联'}</small><div className="evidence-links">{item.evidence_ids.map(id => <a key={id} href={`#evidence-${id}`}>查看证据 {id}</a>)}</div></li>)}</ul><label className="resolution-check"><input type="checkbox" checked={draft.resolved && semanticChange} disabled={!editable || !semanticChange || !!languagesIssue} onChange={event => updateDraft(value => ({ ...value, resolved: event.target.checked }))} />已核对并解决上述歧义</label><p className="muted">请先修改相关字段或明确条目逻辑，再主动勾选。只有勾选后才将 uncertain_information 保存为 null；编辑不会自动清空歧义。</p></>}
      </section>
      <section className="evidence-section"><h3>来源证据 <span className="history-count">{selected.requirements.source_evidence.length}</span></h3><p className="muted">以下为所选版本的原始证据，只读且不允许伪造。核对使用 source_segments.original_content 内的 Unicode 码点范围，不使用 normalized_text。修改后的证据在保存新版本时由服务器重建。</p>{!selected.requirements.source_evidence.length && <p className="warning">此版本未提供来源证据，请人工核对输入材料。</p>}{selected.requirements.source_evidence.map(evidence => <EvidenceView key={evidence.evidence_id} evidence={evidence} document={document} />)}</section>
      <details className="change-log"><summary>版本变更记录</summary><p>父版本：{selected.parent_revision_id ?? '首个版本'}</p><pre>{printable(selected.change_log)}</pre></details>
      {validationIssues.length > 0 && <div className="warning" role="alert"><strong>保存前请检查</strong><ul>{validationIssues.map(issue => <li key={issue}>{issue}</li>)}</ul></div>}
      <div className="revision-save"><label htmlFor="change-reason">保存理由 <span>保存新版本时必填</span></label><textarea id="change-reason" rows={2} maxLength={2000} value={draft.reason} disabled={!editable} placeholder="说明修改了什么、为何这样修改…" onChange={event => updateDraft(value => ({ ...value, reason: event.target.value }))} /><div className="revision-buttons"><button className="primary" disabled={writeBlocked || !editable || validationIssues.length > 0 || !draft.reason.trim() || (!changed && selected.status !== 'confirmed')} onClick={() => void saveRevision()}>{operation === 'save' ? '正在保存…' : '保存为新版本（不确认）'}</button><button className="secondary confirm-button" disabled={writeBlocked || dirty || unresolved || !!languagesIssue || selected.status === 'confirmed'} onClick={() => void confirmRevision()}>{operation === 'confirm' ? '正在确认…' : selected.status === 'confirmed' ? '此版本已确认' : '确认此版本'}</button></div><p className="muted">{!latest ? '历史版本不可编辑或确认。' : dirty ? '有未保存的编辑，请先填写理由并保存新版本，再单独确认。' : unresolved ? '仍有未解决歧义，确认已禁用。' : languagesIssue ? languagesIssue : !synced ? '请先刷新核对状态，成功读取后才能提交。' : '确认不会搜索 GitHub 或启动后续阶段。'} 切换版本保留本页编辑；离开任务或刷新整页前请先保存。</p></div>
    </>}
    {selected?.status === 'confirmed' ? <SearchPanel key={`${document.task_id}.${selected.revision_id}`} taskId={document.task_id} revisionId={selected.revision_id} enabled={synced && !reading && !operation && !pending && !dirty && !draft?.editing} /> : <p className="stage-boundary">请先单独确认需求版本，之后才能授权候选搜索。</p>}
  </section>
}

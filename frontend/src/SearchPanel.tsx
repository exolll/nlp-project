import { useEffect, useRef, useState } from 'react'
import { Failure, request } from './api'
import { deepPhase, deepPhaseLabel, deepReviewPayload, deepReviewStatusLabel, readDeepReview, readSearch, reserveDeep, reserveSearch, restoreDeep, restoreSearch, searchBudget, shouldPollDeep, shouldPollDeepReview, shouldPollSearch, evidenceLabel, validateDeepReview } from './search'
import type { DeepAttempt, DeepReport, DeepReviewOperation, DeepReviewReport as ReviewReport, RetrievalBundle, RetrievalChunk, SearchAttempt, SearchEvidence, SearchRun } from './search'

const runStateLabels: Record<string, string> = {
  reserved: '已受理', queued: '排队中', running: '进行中', completed: '已完成',
  partial: '部分完成', failed: '失败', interrupted: '已中断', not_started: '未开始',
}

export default function SearchPanel({ taskId, revisionId, enabled, stage = 4, onProgress, onRunId }: {
  taskId: string
  revisionId: string
  enabled: boolean
  stage?: 4 | 5 | 6
  onProgress?: () => void
  onRunId?: (runId: string) => void
}) {
  const storageKey = `projectpilot.search.${taskId}.${revisionId}`
  const deepKey = (runId: string) => `projectpilot.deep.${taskId}.${revisionId}.${runId}`
  const [initial] = useState(() => {
    try { return { attempt: restoreSearch(sessionStorage, storageKey, revisionId), error: '' } }
    catch (error) { return { attempt: null, error: error instanceof Error ? error.message : '无法读取会话存储，已停止提交。' } }
  })
  const [initialDeep] = useState(() => {
    const runId = initial.attempt?.run_id
    if (!runId) return { attempt: null as DeepAttempt | null, error: '' }
    try { return { attempt: restoreDeep(sessionStorage, deepKey(runId), revisionId), error: '' } }
    catch (error) { return { attempt: null, error: error instanceof Error ? error.message : '无法读取会话存储，已停止提交。' } }
  })
  const [attempt, setAttempt] = useState<SearchAttempt | null>(initial.attempt)
  const attemptRef = useRef(initial.attempt)
  const [deepAttempt, setDeepAttempt] = useState<DeepAttempt | null>(initialDeep.attempt)
  const deepAttemptRef = useRef(initialDeep.attempt)
  const [storageError, setStorageError] = useState(initial.error)
  const [deepStorageError, setDeepStorageError] = useState(initialDeep.error)
  const [consent, setConsent] = useState(false)
  const [deepConsent, setDeepConsent] = useState(false)
  const [busy, setBusy] = useState(false)
  const [run, setRun] = useState<SearchRun | null>(null)
  const [evidence, setEvidence] = useState<SearchEvidence | null>(null)
  const [report, setReport] = useState<DeepReport | null>(null)
  const [deepError, setDeepError] = useState('')
  const [retrieval, setRetrieval] = useState<RetrievalBundle | null>(null)
  const [retrievalError, setRetrievalError] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('未授权搜索；确认需求不会自动启动搜索。')
  const [deepNotice, setDeepNotice] = useState('深度核对需单独授权；未授权时不会调用模型。')
  const [reviewConsent, setReviewConsent] = useState(false)
  const [review, setReview] = useState<DeepReviewOperation | null>(null)
  const [reviewError, setReviewError] = useState('')
  const [reviewNotice, setReviewNotice] = useState('逐仓库深度核查需单独授权；刷新只执行 GET，不会自动重发 POST。')
  const [reviewBusy, setReviewBusy] = useState(false)
  const [reviewStalled, setReviewStalled] = useState(false)
  const [stalled, setStalled] = useState(false)
  const [activeSection, setActiveSection] = useState<'run' | 'evidence' | 'retrieval'>('run')
  const [authOpenOverride, setAuthOpenOverride] = useState<boolean | null>(null)
  const sectionScrollRef = useRef<HTMLDivElement | null>(null)
  const sectionRefs = useRef<Record<string, HTMLElement | null>>({})
  const [deepSection, setDeepSection] = useState<'check' | 'review' | 'trace'>('check')
  const [deepAuthOpenOverride, setDeepAuthOpenOverride] = useState<boolean | null>(null)
  const [reviewAuthOpenOverride, setReviewAuthOpenOverride] = useState<boolean | null>(null)
  const deepScrollRef = useRef<HTMLDivElement | null>(null)
  const deepSectionRefs = useRef<Record<string, HTMLElement | null>>({})
  const [resultSection, setResultSection] = useState<'ranking' | 'trace'>('ranking')
  const resultScrollRef = useRef<HTMLDivElement | null>(null)
  const resultSectionRefs = useRef<Record<string, HTMLElement | null>>({})
  const scopeRef = useRef<AbortController | null>(null)
  const lock = useRef(false)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const reviewLock = useRef(false)
  const reviewTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const reviewPolls = useRef(0)
  const polls = useRef(0)
  const active = (scope: AbortController) => scopeRef.current === scope && !scope.signal.aborted
  const cancelTimer = () => { clearTimeout(timer.current); timer.current = undefined }
  const cancelReviewTimer = () => { clearTimeout(reviewTimer.current); reviewTimer.current = undefined }
  async function call<T>(scope: AbortController, path: string, method = 'GET', body: unknown = {}, key?: string): Promise<T> {
    const controller = new AbortController()
    const abort = () => controller.abort()
    scope.signal.addEventListener('abort', abort, { once: true })
    if (scope.signal.aborted) controller.abort()
    const timeout = setTimeout(abort, 20000)
    try { return await request<T>(path, method, controller.signal, body, key) }
    catch (value) { if (controller.signal.aborted && !scope.signal.aborted) throw new Error('请求超时，结果未确认；不会自动重发。'); throw value }
    finally { clearTimeout(timeout); scope.signal.removeEventListener('abort', abort) }
  }
  async function refresh(manual = false) {
    const scope = scopeRef.current
    const saved = attemptRef.current
    if (!scope || !active(scope) || lock.current || !saved?.run_id) return
    cancelTimer(); lock.current = true; setBusy(true); setError('')
    if (manual) { polls.current = 0; setStalled(false) }
    try {
      const result = await readSearch((path, method, signal, body, key) => {
        if (signal?.aborted) return Promise.reject(new Error('已停止读取'))
        return call(scope, path, method, body, key)
      }, taskId, revisionId, saved.run_id, scope.signal)
      if (!active(scope)) return
      setRun(result.run)
      if (result.evidence) setEvidence(result.evidence)
      if (result.deep) setReport(result.deep)
      if (result.retrieval) setRetrieval(result.retrieval)
      setError(result.evidenceError)
      setDeepError(result.deepError)
      setRetrievalError(result.retrievalError)
      const polling = shouldPollSearch(result.run)
      const deepPolling = shouldPollDeep(result.run)
      const deep = deepPhase(result.run)
      setNotice(polling ? '正在只读查询进度；部分结果仍可能等待证据发布。' : '运行已停止自动轮询。已保存证据仍可查看；未发布的报告可稍后手动 GET 刷新。不会自动续跑或新建搜索。')
      if (deepPolling || result.deep || deep === 'blocked' || deep === 'interrupted') setDeepNotice(
        deepPolling ? '深度核对正在进行，此处只读 GET 查询进度；不会重复调用模型。'
          : result.deep ? '深度核对报告已发布，自动读取已停止。'
          : deep === 'blocked' ? '完整性校验未通过，深度核对报告已关闭只读入口以外的操作。'
          : '上次授权在模型调用期间中断。可显式重发同一次授权：后端只复用已记录的原始响应，不会再次计费。')
      const budget = deepPolling ? 60 : 30
      if ((polling || deepPolling) && !result.evidenceError && !result.deepError && ++polls.current < budget) timer.current = setTimeout(() => void refresh(), 2500)
      else if (polling || deepPolling) { setNotice('自动读取已暂停（读取错误或达到轮询上限）。请手动刷新；不会重新调用模型或 GitHub。'); if (deepPolling) { setStalled(true); setDeepNotice('深度核对自动读取已暂停（读取错误或达到轮询上限）。可先手动 GET 刷新；仅当确认后端已停止时，才显式重发同一次授权。') } }
    } catch (value) { if (active(scope)) { setError(value instanceof Error ? value.message : '读取失败'); setNotice('读取已暂停。请手动 GET 刷新；已有证据保留。') } }
    finally { if (active(scope)) { lock.current = false; setBusy(false) } }
  }
  async function refreshReview(manual = false) {
    const scope = scopeRef.current
    const saved = attemptRef.current
    if (!scope || !active(scope) || reviewLock.current || !saved?.run_id) return
    cancelReviewTimer(); reviewLock.current = true; setReviewBusy(true)
    if (manual) { reviewPolls.current = 0; setReviewStalled(false) }
    try {
      const result = await readDeepReview((path, method, signal, body, key) => {
        if (signal?.aborted) return Promise.reject(new Error('已停止读取'))
        return call(scope, path, method, body, key)
      }, taskId, revisionId, saved.run_id, scope.signal)
      if (!active(scope)) return
      setReview(result); setReviewError('')
      if (shouldPollDeepReview(result)) {
        setReviewNotice('逐仓库深度核查正在进行；只读 GET 查询进度，不会重复调用模型。')
        if (++reviewPolls.current < 240) reviewTimer.current = setTimeout(() => void refreshReview(), 2500)
        else { setReviewStalled(true); setReviewNotice('自动读取已达到本轮上限。请手动 GET 刷新；不会重新授权或重放任务。') }
      } else {
        setReviewNotice(result.status === 'unknown' && result.persisted_status === 'running'
          ? '持久化 running 已超过授权 deadline，当前显示为结果未知；不会回写 Manifest 或重新调用模型。'
          : result.report ? '深度核查报告已发布，自动轮询已停止；已完成仓库的结果会保留。'
          : `深度核查状态为${deepReviewStatusLabel(result.status)}；不会自动重试。`)
      }
    } catch (value) {
      if (!active(scope)) return
      if (value instanceof Failure && value.status === 404) {
        setReview(null); setReviewNotice('本次搜索运行尚未授权逐仓库深度核查。')
      } else {
        setReviewError(value instanceof Error ? value.message : '深度核查读取失败')
        setReviewNotice('深度核查读取已暂停。已有结果保留；请手动 GET 刷新，不会自动重发 POST。')
      }
    } finally { if (active(scope)) { reviewLock.current = false; setReviewBusy(false) } }
  }
  async function startReview() {
    const scope = scopeRef.current
    const saved = attemptRef.current
    if (!scope || !active(scope) || lock.current || reviewLock.current || !enabled || !reviewReady
        || !saved?.run_id || review || !reviewConsent) return
    cancelReviewTimer(); reviewLock.current = true; setReviewBusy(true); setReviewError('')
    try {
      const result = await call<DeepReviewOperation>(scope,
        `/tasks/${encodeURIComponent(taskId)}/search-runs/${encodeURIComponent(saved.run_id)}/deep-review`,
        'POST', deepReviewPayload(revisionId))
      if (!active(scope)) return
      const operation = validateDeepReview(result, taskId, revisionId, saved.run_id)
      setReview(operation); setReviewConsent(false)
      setReviewNotice(shouldPollDeepReview(operation)
        ? '逐仓库深度核查已受理；后续只读 GET 查询进度，不会自动重复提交。'
        : '后端返回已有深度核查 Operation；不会重复注册后台任务。')
    } catch (value) {
      if (active(scope)) {
        setReviewError(value instanceof Error ? value.message : '深度核查提交结果未确认')
        setReviewNotice('提交结果未确认。请先手动 GET 查询已有 Operation；不会自动重发 POST 或新建调用。')
      }
    } finally { if (active(scope)) { reviewLock.current = false; setReviewBusy(false) } }
    if (active(scope)) await refreshReview(true)
  }
  useEffect(() => {
    const scope = new AbortController(); scopeRef.current = scope; lock.current = false; reviewLock.current = false
    if (attemptRef.current?.run_id) { void refresh(); void refreshReview() }
    return () => { scope.abort(); cancelTimer(); cancelReviewTimer() }
    // Parent keys by task and immutable revision, so no requests or state cross tasks.
  }, [taskId, revisionId])
  // 向外层外壳报告真实运行标识与阶段信号；只在这一层能拿到 run / review 的真实状态。
  useEffect(() => { if (attempt?.run_id) onRunId?.(attempt.run_id) }, [attempt?.run_id])
  const progressKey = run
    ? `${run.status}|${run.evidence_status ?? ''}|${run.retrieval_status ?? ''}|${run.deep_check?.status ?? ''}|${run.deep_check?.published ?? ''}`
    : ''
  useEffect(() => { onProgress?.() }, [progressKey, review?.status, !!review?.report])
  async function start() {
    const scope = scopeRef.current
    if (!scope || !active(scope) || lock.current || !enabled || !consent || storageError || attemptRef.current?.run_id) return
    cancelTimer(); lock.current = true; setBusy(true); setError('')
    let accepted = false
    try {
      let saved: SearchAttempt
      try { saved = reserveSearch(sessionStorage, storageKey, revisionId) }
      catch { setStorageError('无法可靠保存幂等记录，已停止提交。请恢复会话存储，不要清除记录重试。'); return }
      attemptRef.current = saved; setAttempt(saved)
      const result = await call<SearchRun>(scope, `/tasks/${encodeURIComponent(taskId)}/search-runs`, 'POST', saved.payload, saved.key)
      if (!active(scope)) return
      if (result.task_id !== taskId || result.confirmed_revision_id !== revisionId || !result.run_id) throw new Error('返回运行与当前版本不匹配，保留原幂等键。')
      const next = { ...saved, run_id: result.run_id }
      attemptRef.current = next; setAttempt(next); setRun(result); setConsent(false)
      try { sessionStorage.setItem(storageKey, JSON.stringify(next)) }
      catch { setStorageError('运行已受理，但运行 ID 保存失败。原幂等键保留；请恢复存储后核对。') }
      accepted = true
    } catch (value) {
      if (active(scope)) { setError(value instanceof Error ? value.message : '提交结果未确认'); setNotice('提交未确认成功。原载荷与 UUID 已保留；不会自动重试或生成新运行。可显式重试同一次请求。') }
    } finally { if (active(scope)) { lock.current = false; setBusy(false) } }
    if (accepted && active(scope)) await refresh(true)
  }
  async function startDeep() {
    const scope = scopeRef.current
    const saved = attemptRef.current
    const state = run ? deepPhase(run) : 'not_started'
    // A stored attempt may be re-sent after a lost response, a declared interruption, or a stalled read; the backend only ever reuses recorded bytes.
    const canSend = deepAttemptRef.current ? state === 'not_started' || state === 'interrupted' || (state === 'running' && stalled) : state === 'not_started' && deepConsent
    if (!scope || !active(scope) || lock.current || reviewLock.current || !enabled || !saved?.run_id || deepStorageError || !canSend) return
    cancelTimer(); lock.current = true; setBusy(true); setError('')
    let accepted = false
    try {
      let attempt: DeepAttempt
      try { attempt = reserveDeep(sessionStorage, deepKey(saved.run_id), revisionId) }
      catch { setDeepStorageError('无法可靠保存深度核对的幂等记录，已停止提交。请恢复会话存储，不要清除记录重试。'); return }
      deepAttemptRef.current = attempt; setDeepAttempt(attempt)
      const result = await call<SearchRun>(scope, `/tasks/${encodeURIComponent(taskId)}/search-runs/${encodeURIComponent(saved.run_id)}/deep-check`, 'POST', attempt.payload, attempt.key)
      if (!active(scope)) return
      if (result.run_id !== saved.run_id || result.task_id !== taskId || result.confirmed_revision_id !== revisionId) throw new Error('返回运行与当前版本不匹配，保留原幂等键。')
      setRun(result); setDeepConsent(false); setDeepNotice('深度核对已受理，正在只读查询进度；本次授权不会再重复提交。')
      accepted = true
    } catch (value) {
      if (active(scope)) { setError(value instanceof Error ? value.message : '提交结果未确认'); setDeepNotice('深度核对提交未确认成功。原载荷与 UUID 已保留；仅允许按钮重发同一次授权，不会自动重试或新建调用。') }
    } finally { if (active(scope)) { lock.current = false; setBusy(false) } }
    if (accepted && active(scope)) await refresh(true)
  }
  const phase = run ? deepPhase(run) : 'not_started'
  const snapshotsReady = !!run && run.dispatch_closed === true && run.plan_status === 'published'
  const reviewReady = snapshotsReady && (run?.retrieval_status === 'ready' || run?.retrieval_status === 'partial')
  const reviewSendDisabled = !enabled || !attempt?.run_id || busy || reviewBusy || !reviewReady || !!review || !reviewConsent
  const deepResend = !!deepAttempt && (phase === 'not_started' || phase === 'interrupted' || (phase === 'running' && stalled))
  const deepActive = !busy && enabled && !!attempt?.run_id && !deepStorageError && snapshotsReady && phase !== 'ready' && phase !== 'failed' && phase !== 'blocked'
  const deepSendDisabled = !deepActive || (deepAttempt ? !deepResend : !deepConsent)
  const searchAuthNeedsAttention = !attempt?.run_id || !!error || !!storageError
  const searchAuthOpen = authOpenOverride ?? searchAuthNeedsAttention
  const deepAuthNeedsAttention = !attempt?.run_id || !!deepError || !!deepStorageError || stalled
  const deepAuthOpen = deepAuthOpenOverride ?? (deepAuthNeedsAttention || (!deepAttempt && !report))
  const reviewAuthNeedsAttention = !attempt?.run_id || !!reviewError || reviewStalled
  const reviewAuthOpen = reviewAuthOpenOverride ?? (reviewAuthNeedsAttention || (!review && !!report && reviewReady))
  const ranking = review?.report?.ranking ?? null
  const recommendationCounts = ranking ? ranking.repositories.reduce((counts, row) => {
    counts[row.recommendation.status] += 1
    return counts
  }, { recommended: 0, needs_review: 0, not_recommended: 0 }) : null
  function scrollToSection(key: 'run' | 'evidence' | 'retrieval') {
    setActiveSection(key)
    const scroller = sectionScrollRef.current
    const target = sectionRefs.current[key]
    if (!scroller || !target) return
    scroller.scrollTo({ top: Math.max(target.offsetTop - 4, 0), behavior: 'smooth' })
  }
  function onSectionScroll() {
    const scroller = sectionScrollRef.current
    if (!scroller) return
    const top = scroller.scrollTop + 40
    let current: 'run' | 'evidence' | 'retrieval' = 'run'
    for (const key of ['run', 'evidence', 'retrieval'] as const) {
      const node = sectionRefs.current[key]
      if (node && node.offsetTop <= top) current = key
    }
    setActiveSection(value => value === current ? value : current)
  }
  function scrollToDeepSection(key: 'check' | 'review' | 'trace') {
    setDeepSection(key)
    const scroller = deepScrollRef.current
    const target = deepSectionRefs.current[key]
    if (!scroller || !target) return
    scroller.scrollTo({ top: Math.max(target.offsetTop - 4, 0), behavior: 'smooth' })
  }
  function onDeepScroll() {
    const scroller = deepScrollRef.current
    if (!scroller) return
    const top = scroller.scrollTop + 40
    let current: 'check' | 'review' | 'trace' = 'check'
    for (const key of ['check', 'review', 'trace'] as const) {
      const node = deepSectionRefs.current[key]
      if (node && node.offsetTop <= top) current = key
    }
    // 末节高度可能小于视口，滚到底时无法越过阈值，直接标为末节。
    if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2) current = 'trace'
    setDeepSection(value => value === current ? value : current)
  }
  function scrollToResultSection(key: 'ranking' | 'trace') {
    setResultSection(key)
    const scroller = resultScrollRef.current
    const target = resultSectionRefs.current[key]
    if (!scroller || !target) return
    scroller.scrollTo({ top: Math.max(target.offsetTop - 4, 0), behavior: 'smooth' })
  }
  function onResultScroll() {
    const scroller = resultScrollRef.current
    if (!scroller) return
    const top = scroller.scrollTop + 40
    let current: 'ranking' | 'trace' = 'ranking'
    for (const key of ['ranking', 'trace'] as const) {
      const node = resultSectionRefs.current[key]
      if (node && node.offsetTop <= top) current = key
    }
    if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2) current = 'trace'
    setResultSection(value => value === current ? value : current)
  }
  return <section className="search-panel" aria-label="搜索、深度筛选与结果" data-stage={stage}>
    <div className="search-stage search-workspace-stage" hidden={stage !== 4}>
      <div className="search-workspace screen-workspace">
        <aside className="search-overview screen-overview card" aria-label="搜索运行概览">
          <header className="overview-head">
            <span className="section-index">04 / GITHUB SCREEN</span>
            <h2 id="search-title">GitHub 初筛</h2>
            <p className="overview-subtitle">只搜索已确认并保存的版本；授权一次运行，刷新只读不重复调用。</p>
            <div className="overview-badges">
              <span className={`badge ${run?.status === 'completed' ? 'success' : ''}`}>{run ? runStateLabels[run.status] ?? run.status : '等待授权'}</span>
              {attempt?.run_id && <span className="badge info">运行已受理</span>}
              {evidence && <span className="badge success">证据已发布</span>}
            </div>
          </header>
          <div className="search-binding">
            <span>绑定已确认版本</span>
            <code title={revisionId}>{revisionId}</code>
          </div>
          {run ? <dl className="search-overview-status">
            <div><dt>流水线</dt><dd>{runStateLabels[run.pipeline_status] ?? run.pipeline_status}</dd></div>
            <div><dt>规划 / 搜索</dt><dd>{run.plan_status ?? '未提供'} / {run.search_status ?? '未提供'}</dd></div>
            <div><dt>证据发布</dt><dd>{run.evidence_status ?? 'not_ready'}</dd></div>
            <div><dt>候选仓库</dt><dd>{run.candidates?.length ?? 0} 个</dd></div>
            <div><dt>运行 ID</dt><dd><code>{run.run_id}</code></dd></div>
          </dl> : <p className="overview-empty">还没有搜索运行；确认需求本身不会自动搜索。</p>}
          <div className="search-budget">固定预算 · 模型 {searchBudget.model_calls} 次 · 搜索 {searchBudget.search_requests} 次 · GitHub {searchBudget.github_requests} 次 · README {searchBudget.readme_candidates} 个</div>
          <nav className="group-nav" aria-label="初筛分区">
            <button type="button" className={activeSection === 'run' ? 'active' : ''} onClick={() => scrollToSection('run')}>
              <span className="nav-index">01</span><span className="nav-label">运行与候选</span><span className="nav-status">{run ? runStateLabels[run.pipeline_status] ?? run.pipeline_status : '未开始'}</span>
            </button>
            <button type="button" className={activeSection === 'evidence' ? 'active' : ''} onClick={() => scrollToSection('evidence')}>
              <span className="nav-index">02</span><span className="nav-label">逐需求证据</span><span className="nav-status">{evidence ? `${evidence.requirement_items.length} 条需求` : '未发布'}</span>
            </button>
            <button type="button" className={activeSection === 'retrieval' ? 'active' : ''} onClick={() => scrollToSection('retrieval')}>
              <span className="nav-index">03</span><span className="nav-label">检索片段</span><span className="nav-status">{retrieval ? `${retrieval.candidates.length} 个仓库` : '未生成'}</span>
            </button>
          </nav>
          <details className="overview-extraction" open={searchAuthOpen} onToggle={event => { if (event.currentTarget.open !== searchAuthOpen) setAuthOpenOverride(event.currentTarget.open) }}>
            <summary>授权与运行{run ? ` · ${runStateLabels[run.status] ?? run.status}` : ''}</summary>
            <div className="overview-extraction-body">
              <div className="search-auth-consent">
                <strong>明确授权一次真实外部调用</strong>
                <p>点击开始会向后端 POST 搜索运行，调用真实模型服务与 GitHub，可能产生费用并发送需求。刷新只执行 GET；测试必须拦截 API 或使用本地 Mock。</p>
                <p>上限：模型 {searchBudget.model_calls} 次 · 搜索 {searchBudget.search_requests} 次（每次最多 {searchBudget.per_page} 条、{searchBudget.max_pages} 页）· README {searchBudget.readme_candidates} 个候选 · GitHub 共 {searchBudget.github_requests} 次（含 commit / README 请求）。</p>
                <label className="resolution-check"><input type="checkbox" checked={consent} disabled={busy || !!attempt?.run_id || !!storageError || !enabled} onChange={event => setConsent(event.target.checked)} />我同意将此已确认版本用于上述外部调用与预算</label>
              </div>
              <div className="overview-extract-actions">
                <button className="primary" disabled={!enabled || !consent || busy || !!storageError || !!attempt?.run_id} onClick={() => void start()}>{busy && !run ? '正在提交…' : attempt ? '显式重试同一次搜索（沿用 UUID）' : '按此预算开始搜索'}</button>
                <button className="secondary" disabled={busy || !attempt?.run_id} onClick={() => void refresh(true)}>刷新（仅 GET）</button>
              </div>
            </div>
          </details>
          <div className="overview-feedback" aria-live="polite">
            <p>{busy && <span className="spinner" />}{notice}</p>
            {storageError && <p className="warning" role="alert">{storageError}</p>}
            {error && <p className="error" role="alert">{error}</p>}
          </div>
          {attempt && !attempt.run_id && <p className="warning overview-warning">本次提交结果未知。没有运行 ID 时无法查询；仅允许按钮重发相同载荷和幂等键，不会另建请求。</p>}
          {!enabled && <p className="warning overview-warning">请先保存并确认当前编辑，或退出编辑后选择已确认版本。搜索不会使用未保存内容。</p>}
        </aside>
        <div className="search-editor screen-editor">
          <div className="search-editor-scroll screen-editor-scroll" ref={sectionScrollRef} onScroll={onSectionScroll}>
            <section className="search-section card" id="search-section-run" ref={node => { sectionRefs.current.run = node }} aria-labelledby="search-run-title">
              <header className="search-section-head"><span className="group-index">01</span><h3 id="search-run-title">运行与候选</h3><span className="badge">{run ? runStateLabels[run.status] ?? run.status : '等待授权'}</span></header>
              {!run ? <div className="search-empty"><p className="muted">尚无运行记录。在左侧明确授权并开始搜索后，这里会显示流水线状态、错误代码与候选采集结果。</p></div> : <>
                <dl className="search-status"><dt>运行 ID</dt><dd>{run.run_id}</dd><dt>流水线</dt><dd>{run.pipeline_status}</dd><dt>规划 / 搜索 / 证据发布</dt><dd>{run.plan_status ?? '未提供'} / {run.search_status ?? '未提供'} / {run.evidence_status ?? 'not_ready'}</dd><dt>外部派发关闭</dt><dd>{run.dispatch_closed === undefined ? '未提供' : run.dispatch_closed ? '是' : '否'}</dd></dl>
                {run.search_status === 'partial' && <p className="warning">仅有部分采集结果；不丢弃已保存的 README 证据，也不把缺失视为不满足。</p>}
                {[run.error_code, run.pipeline_error_code, ...(run.search_stop_reasons ?? [])].filter(Boolean).map((code, i) => <p className="warning" key={i}>{code}</p>)}
                {run.operation_failures?.map((failure, i) => <p className="warning" key={i}>{failure.operation} · {failure.status} · {failure.error_code}</p>)}
                <h4>候选与 README 采集状态</h4>
                {!run.candidates?.length && <p className="muted">尚无候选记录，不代表需求不满足。</p>}
                <ul>{run.candidates?.map(candidate => <li key={candidate.repository_id}>仓库 ID {candidate.repository_id} · README：{candidate.collection_status ?? '未提供'}</li>)}</ul>
              </>}
            </section>
            <section className="search-section card" id="search-section-evidence" ref={node => { sectionRefs.current.evidence = node }} aria-labelledby="search-evidence-title">
              <header className="search-section-head"><span className="group-index">02</span><h3 id="search-evidence-title">逐需求证据</h3>{evidence && <span className="badge success">{evidence.candidates.length} 个仓库</span>}</header>
              <p className="warning">字面提及不是满足结论；否定语义、禁止约束及 ALL / ANY 均需人工核对。未找到提及也不代表不满足。</p>
    
    {!evidence ? <p className="muted">证据报告尚未读取或尚未发布。候选采集完成不等于证据已发布；可稍后只读刷新。</p> : <><p>{evidence.warning}</p>{evidence.candidates.length === 0 && <p>已发布报告没有候选。</p>}{evidence.candidates.map(candidate => <article className="search-candidate" key={candidate.repository_id}><h4>仓库 ID {candidate.repository_id} · README {candidate.availability}</h4>{candidate.source ? <details className="search-source"><summary>精确快照来源</summary><dl>{Object.entries(candidate.source).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl></details> : <p>无已保存快照，无法确认。</p>}{evidence.requirement_items.map(requirement => {
                const item = candidate.items.find(value => value.requirement_item_id === requirement.requirement_item_id)
                return <details className="requirement-evidence" key={requirement.requirement_item_id}><summary>{requirement.text} · {evidenceLabel(item?.status ?? 'unconfirmed')}</summary><p>{requirement.requirement_item_id} · {requirement.modality} · {requirement.operator} · 逻辑组 {requirement.logic_group_id ?? '无'}</p>{item?.quotes.length ? item.quotes.map((quote, i) => <div key={i}><small>README 行 {quote.line_start}–{quote.line_end} · UTF-8 字节 [{quote.byte_start}, {quote.byte_end})</small><pre className="source-quote">{quote.text}</pre></div>) : <p>无精确引文，尚未确认。</p>}</details>
              })}</article>)}</>}
            </section>
            <section className="search-section card" id="search-section-retrieval" ref={node => { sectionRefs.current.retrieval = node }} aria-labelledby="search-retrieval-title">
              <header className="search-section-head"><span className="group-index">03</span><h3 id="search-retrieval-title">检索片段</h3>{retrieval && <span className="badge success">{retrieval.candidates.length} 个仓库</span>}</header>
              <p className="warning">检索只表示“相关”，不代表功能已满足；不显示满足百分比。所有片段都来自已保存的 README 快照。</p>
    {run?.retrieval_status === 'failed' && <p className="warning" role="alert">检索证据生成失败（{run.retrieval_error_code ?? '未知原因'}）；搜索、字面证据与深度核对结果不受影响。</p>}
              {run?.retrieval_status === 'partial' && <p className="warning">部分仓库或需求未能完成检索；已完成的片段仍会显示。</p>}
              {!run?.retrieval_status || run.retrieval_status === 'not_started' ? <p className="muted">检索证据尚未生成。</p> : null}
              {retrievalError && <p className="error" role="alert">{retrievalError}</p>}
              {retrieval && <RetrievalEvidenceView bundle={retrieval} />}
            </section>
          </div>
        </div>
      </div>
    </div>
    <div className="search-stage search-workspace-stage" hidden={stage !== 5}>
      <div className="search-workspace deep-workspace">
        <aside className="search-overview deep-overview card" aria-label="深度筛选概览">
          <header className="overview-head">
            <span className="section-index">05 / DEEP SCREEN</span>
            <h2 id="deep-screen-title">深度筛选</h2>
            <p className="overview-subtitle">深度核对与逐仓库核查都需单独授权；切换子界面、刷新或重新进入任务只执行 GET，不会自动重发模型调用。</p>
            <div className="overview-badges">
              <span className={`badge ${phase === 'ready' ? 'success' : ''}`}>{run?.deep_check ? deepPhaseLabel(phase) : '深度核对未授权'}</span>
              {review && <span className={`badge ${review.report ? 'success' : ''}`}>{deepReviewStatusLabel(review.status)}</span>}
              {review?.report?.ranking && <span className="badge success">排名已生成</span>}
            </div>
          </header>
          <dl className="search-overview-status">
            <div><dt>运行 ID</dt><dd><code title={attempt?.run_id ?? ''}>{attempt?.run_id ?? '尚未创建搜索运行'}</code></dd></div>
            <div><dt>版本 ID</dt><dd><code title={revisionId}>{revisionId}</code></dd></div>
            <div><dt>深度核对</dt><dd>{run?.deep_check ? deepPhaseLabel(phase) : '未授权'}</dd></div>
            <div><dt>逐仓库核查</dt><dd>{review ? deepReviewStatusLabel(review.status) : reviewReady ? '可以授权' : '未就绪'}</dd></div>
            <div><dt>检索 Bundle</dt><dd>{run?.retrieval_status ?? '未生成'}</dd></div>
            <div><dt>确定性排名</dt><dd>{review?.report?.ranking ? '已生成' : '未生成'}</dd></div>
          </dl>
          <div className="search-budget">固定预算 · 深度核对 1 次 · 逐仓库最多 5 次模型 · 只读已保存 README</div>
          <nav className="group-nav" aria-label="深度筛选分区">
            <button type="button" className={deepSection === 'check' ? 'active' : ''} onClick={() => scrollToDeepSection('check')}>
              <span className="nav-index">01</span><span className="nav-label">深度核对</span><span className="nav-status">{run?.deep_check ? deepPhaseLabel(phase) : '未授权'}</span>
            </button>
            <button type="button" className={deepSection === 'review' ? 'active' : ''} onClick={() => scrollToDeepSection('review')}>
              <span className="nav-index">02</span><span className="nav-label">逐仓库核查</span><span className="nav-status">{review ? deepReviewStatusLabel(review.status) : '未授权'}</span>
            </button>
            <button type="button" className={deepSection === 'trace' ? 'active' : ''} onClick={() => scrollToDeepSection('trace')}>
              <span className="nav-index">03</span><span className="nav-label">授权与追溯</span><span className="nav-status">{review?.report ? '已发布' : '未发布'}</span>
            </button>
          </nav>
          <div className="search-auth-stack">
            <details className="overview-extraction" open={deepAuthOpen} onToggle={event => { if (event.currentTarget.open !== deepAuthOpen) setDeepAuthOpenOverride(event.currentTarget.open) }}>
              <summary>深度核对授权{run?.deep_check ? ` · ${deepPhaseLabel(phase)}` : ''}</summary>
              <div className="overview-extraction-body">
                <div className="search-auth-consent">
                  <strong>单独授权 1 次深度核对模型调用</strong>
                  <p>只读取本次运行已保存的 README 快照，不再访问 GitHub。模型逐条判定「有证据支持 / 有证据冲突 / 信息不足」并给出行号，后端逐条复核行号后确定性地生成中文推荐标签与缺失功能清单；引文越界一律剔除。刷新只执行 GET，失败与中断都不自动重试。</p>
                  <p>预算：每个搜索运行最多 1 次模型调用；一个任务最多 3 个运行，间接封顶 3 次。</p>
                  <label className="resolution-check"><input type="checkbox" checked={deepConsent} disabled={busy || !!deepStorageError || !!deepAttempt || !snapshotsReady || phase !== 'not_started' || !enabled} onChange={event => setDeepConsent(event.target.checked)} />我单独同意为本次已保存的搜索快照调用 1 次深度核对模型</label>
                </div>
                <div className="overview-extract-actions">
                  <button className="primary" disabled={deepSendDisabled} onClick={() => void startDeep()}>{busy && !deepAttempt ? '正在提交…' : deepResend ? '显式重发同一次授权（沿用 UUID）' : deepAttempt ? '本次运行已授权深度核对' : '开始深度核对（1 次模型调用）'}</button>
                  <button className="secondary" disabled={busy || !attempt?.run_id} onClick={() => void refresh(true)}>刷新深度核对（仅 GET）</button>
                </div>
              </div>
            </details>
            <details className="overview-extraction" open={reviewAuthOpen} onToggle={event => { if (event.currentTarget.open !== reviewAuthOpen) setReviewAuthOpenOverride(event.currentTarget.open) }}>
              <summary>逐仓库核查授权{review ? ` · ${deepReviewStatusLabel(review.status)}` : ''}</summary>
              <div className="overview-extraction-body">
                <div className="search-auth-consent">
                  <strong>单独授权 DeepSeek 逐仓库深度核查</strong>
                  <p>我已知悉：本次深度核查仅向 DeepSeek 发送已确认的需求条目、候选仓库基本信息、已保存的 README 检索片段及必要引用坐标；不会直接上传原始 PDF/Word 文件。系统将按仓库最多发起 5 次模型调用，用于判断需求是否获得 README 文档支持、存在矛盾或证据不足，并可能产生 API 费用。</p>
                  <label className="resolution-check"><input type="checkbox" checked={reviewConsent} disabled={busy || reviewBusy || !enabled || !attempt?.run_id || !reviewReady || !!review} onChange={event => setReviewConsent(event.target.checked)} />我单独同意上述数据范围、用途、调用预算与可能产生的费用</label>
                </div>
                <div className="overview-extract-actions">
                  <button className="primary" disabled={reviewSendDisabled} onClick={() => void startReview()}>{reviewBusy && !review ? '正在提交或读取…' : review ? '本次运行已授权深度核查' : '开始逐仓库深度核查（最多 5 次模型调用）'}</button>
                  <button className="secondary" disabled={busy || reviewBusy || !attempt?.run_id} onClick={() => void refreshReview(true)}>刷新深度核查（仅 GET）</button>
                </div>
              </div>
            </details>
          </div>
          <div className="overview-feedback" aria-live="polite">
            {deepNotice && <p>{deepNotice}</p>}
            {reviewNotice && <p>{reviewBusy && <span className="spinner" />}{reviewNotice}</p>}
            {!attempt?.run_id && <p className="muted">请先完成一次搜索运行；深度核对只作用于已保存的搜索快照。</p>}
            {attempt?.run_id && !deepAttempt && !snapshotsReady && phase === 'not_started' && <p className="warning">候选派发尚未关闭或规划未发布，此时不会授权模型调用。</p>}
            {!attempt?.run_id && <p className="muted">逐仓库核查只作用于已保存的检索 Bundle。</p>}
            {attempt?.run_id && !review && !reviewReady && <p className="warning">检索 Bundle 尚未就绪，或候选派发尚未关闭；此时不会授权模型调用。</p>}
            {deepStorageError && <p className="warning" role="alert">{deepStorageError}</p>}
            {deepError && <p className="error" role="alert">{deepError}</p>}
            {reviewStalled && <p className="warning">自动轮询已暂停；请手动 GET 查询。系统不会自动重发授权 POST。</p>}
            {reviewError && <p className="error" role="alert">{reviewError}</p>}
          </div>
        </aside>
        <div className="search-editor deep-editor">
          <div className="search-editor-scroll deep-editor-scroll" ref={deepScrollRef} onScroll={onDeepScroll}>
            <section className="search-section card" id="deep-section-check" ref={node => { deepSectionRefs.current.check = node }} aria-labelledby="deep-check-title">
              <header className="search-section-head"><span className="group-index">01</span><h3 id="deep-check-title">深度核对与中文推荐</h3>{run?.deep_check && <span className="badge">{deepPhaseLabel(phase)}</span>}</header>
              {run?.deep_check && <dl className="search-status"><dt>深度核对状态</dt><dd>{deepPhaseLabel(phase)}</dd><dt>错误代码</dt><dd>{run.deep_check.error_code ?? '无'}</dd></dl>}
              {!report && <p className="muted">尚无已发布的深度核对报告；报告未发布不代表需求不满足。</p>}
              {report && <DeepReportView report={report} />}
            </section>
            <section className="search-section card" id="deep-section-review" ref={node => { deepSectionRefs.current.review = node }} aria-labelledby="deep-review-title">
              <header className="search-section-head"><span className="group-index">02</span><h3 id="deep-review-title">逐仓库深度核查（MVP）</h3>{review && <span className="badge">{deepReviewStatusLabel(review.status)}</span>}</header>
              <p>这是独立于旧版 deep-check 的新授权。仅使用当前检索 Bundle 的候选仓库与已保存 README 片段；每个仓库最多调用模型一次，总计最多 5 次，不自动重试。</p>
              {review?.report ? <DeepReviewReportView report={review.report} showRanking={false} /> : review ? <p className="muted">尚无已发布的深度核查报告；未发布或结果未知不代表需求不满足。</p> : <p className="muted">尚未授权逐仓库深度核查；请在左侧「逐仓库核查授权」中确认数据范围与预算。</p>}
            </section>
            <section className="search-section card" id="deep-section-trace" ref={node => { deepSectionRefs.current.trace = node }} aria-labelledby="deep-trace-title">
              <header className="search-section-head"><span className="group-index">03</span><h3 id="deep-trace-title">授权与报告追溯</h3>{review?.report ? <span className="badge success">已发布</span> : <span className="badge">未发布</span>}</header>
              {review ? <>
                <dl className="search-status"><dt>深度核查状态</dt><dd>{deepReviewStatusLabel(review.status)}</dd><dt>Manifest 持久化状态</dt><dd>{review.persisted_status}</dd><dt>Operation</dt><dd>{review.operation_id}</dd><dt>模型 / Prompt</dt><dd>{review.model} / {review.prompt_version}</dd><dt>授权时间 / Deadline</dt><dd>{review.authorized_at} / {review.deadline}</dd><dt>错误代码</dt><dd>{review.error_code ?? '无'}</dd></dl>
                <details className="search-source"><summary>授权与报告完整性标识</summary><dl><dt>需求修订</dt><dd>{review.revision_id}</dd><dt>修订哈希</dt><dd>{review.revision_hash}</dd><dt>Bundle 哈希</dt><dd>{review.bundle_hash}</dd><dt>报告对象</dt><dd>{review.report_key ?? '尚未发布'}</dd><dt>报告 SHA-256</dt><dd>{review.report_hash ?? '尚未发布'}</dd></dl></details>
                {review.report?.ranking && <p className="muted">确定性排名已随报告生成；完整排名在「最终结果」子界面展示。</p>}
              </> : <p className="muted">逐仓库核查尚未授权，暂无 Operation 与报告完整性标识。</p>}
            </section>
          </div>
        </div>
      </div>
    </div>
    <div className="search-stage search-workspace-stage" hidden={stage !== 6}>
      <div className="search-workspace result-workspace">
        <aside className="search-overview result-overview card" aria-label="最终结果概览">
          <header className="overview-head">
            <span className="section-index">06 / RESULT</span>
            <h2 id="result-title">最终结果与推荐</h2>
            <p className="overview-subtitle">推荐排名来自已发布的深度核查报告，按确定性规则生成；切换子界面、刷新或重新进入任务只执行 GET，不会重新调用模型。</p>
            <div className="overview-badges">
              <span className={`badge ${ranking ? 'success' : ''}`}>{ranking ? '已发布' : '等待深度筛选'}</span>
              {!ranking && review && <span className="badge">{deepReviewStatusLabel(review.status)}</span>}
              {ranking && <span className="badge info">参与排名 {ranking.repositories.length} 个</span>}
              {ranking && recommendationCounts && <span className="badge warn">待核查 {recommendationCounts.needs_review} 个</span>}
            </div>
          </header>
          <dl className="search-overview-status">
            <div><dt>运行 ID</dt><dd><code title={attempt?.run_id ?? ''}>{attempt?.run_id ?? '尚未创建搜索运行'}</code></dd></div>
            <div><dt>版本 ID</dt><dd><code title={revisionId}>{revisionId}</code></dd></div>
            <div><dt>逐仓库核查</dt><dd>{review ? deepReviewStatusLabel(review.status) : '未授权'}</dd></div>
            <div><dt>排名规则</dt><dd>{ranking ? ranking.ranking_config_version : '未生成'}</dd></div>
            <div><dt>参与 / 未参与</dt><dd>{ranking ? `${ranking.repositories.length} / ${ranking.excluded_repositories.length} 个仓库` : '未生成'}</dd></div>
            <div><dt>报告生成时间</dt><dd>{review?.report?.created_at ?? '未发布'}</dd></div>
          </dl>
          <div className="search-budget">只读结果页 · 不发起新模型调用 · 未参与排名不等于不符合需求</div>
          <nav className="group-nav" aria-label="最终结果分区">
            <button type="button" className={resultSection === 'ranking' ? 'active' : ''} onClick={() => scrollToResultSection('ranking')}>
              <span className="nav-index">01</span><span className="nav-label">推荐排名</span><span className="nav-status">{ranking ? `${ranking.repositories.length} 个仓库` : '未发布'}</span>
            </button>
            <button type="button" className={resultSection === 'trace' ? 'active' : ''} onClick={() => scrollToResultSection('trace')}>
              <span className="nav-index">02</span><span className="nav-label">报告与授权溯源</span><span className="nav-status">{review?.report ? '已发布' : '未发布'}</span>
            </button>
          </nav>
          <div className="overview-feedback" aria-live="polite">
            {!ranking && !review && <p className="muted">尚未授权逐仓库深度核查；本页不会自动重试、重新调用模型或编造排名。</p>}
            {!ranking && review && <p className="muted">深度核查{deepReviewStatusLabel(review.status)}；报告发布后本页自动显示排名，期间只读 GET。</p>}
            {ranking && <p className="muted">排名按确定性规则生成；分数不能覆盖硬约束层级，排除不等于不符合需求。</p>}
          </div>
        </aside>
        <div className="search-editor result-editor">
          <div className="search-editor-scroll result-editor-scroll" ref={resultScrollRef} onScroll={onResultScroll}>
            <section className="search-section card" id="result-section-ranking" ref={node => { resultSectionRefs.current.ranking = node }} aria-labelledby="result-ranking-title">
              <header className="search-section-head"><span className="group-index">01</span><h3 id="result-ranking-title">推荐排名</h3><span className={`badge ${ranking ? 'success' : ''}`}>{ranking ? '已发布' : '等待深度筛选'}</span></header>
              {ranking
                ? <><p className="stage-boundary">推荐排名来自已发布的深度核查报告，按确定性规则生成；分数不能覆盖硬约束层级。</p><RankingView ranking={ranking} repositories={review!.report!.repositories} /></>
                : <div className="requirements-empty"><h3>还没有已发布的最终排名</h3><p>请在“深度筛选”子界面完成逐仓库深度核查并等待报告发布。切换子界面不会自动重试、重新调用模型或编造结果。</p></div>}
            </section>
            <section className="search-section card" id="result-section-trace" ref={node => { resultSectionRefs.current.trace = node }} aria-labelledby="result-trace-title">
              <header className="search-section-head"><span className="group-index">02</span><h3 id="result-trace-title">报告与授权溯源</h3><span className={`badge ${review?.report ? 'success' : ''}`}>{review?.report ? '已发布' : '未发布'}</span></header>
              {review ? <>
                <dl className="search-status"><dt>深度核查状态</dt><dd>{deepReviewStatusLabel(review.status)}</dd><dt>Manifest 持久化状态</dt><dd>{review.persisted_status}</dd><dt>Operation</dt><dd>{review.operation_id}</dd><dt>模型 / Prompt</dt><dd>{review.model} / {review.prompt_version}</dd><dt>授权时间 / Deadline</dt><dd>{review.authorized_at} / {review.deadline}</dd><dt>错误代码</dt><dd>{review.error_code ?? '无'}</dd>{review.report && <><dt>报告版本 / 生成时间</dt><dd>{review.report.version} / {review.report.created_at}</dd></>}<dt>排名规则</dt><dd>{ranking ? ranking.ranking_config_version : '未生成'}{ranking?.scored_at ? ` · ${ranking.scored_at}` : ''}</dd></dl>
                <details className="search-source"><summary>授权与报告完整性标识</summary><dl><dt>需求修订</dt><dd>{review.revision_id}</dd><dt>修订哈希</dt><dd>{review.revision_hash}</dd><dt>Bundle 哈希</dt><dd>{review.bundle_hash}</dd><dt>报告对象</dt><dd>{review.report_key ?? '尚未发布'}</dd><dt>报告 SHA-256</dt><dd>{review.report_hash ?? '尚未发布'}</dd></dl></details>
                <p className="muted">排名只使用已发布报告中的确定性结果；切换子界面或刷新只执行 GET，不会重发已完成的模型调用。</p>
              </> : <p className="muted">逐仓库核查尚未授权，暂无 Operation 与报告完整性标识。请在「深度筛选」完成逐仓库深度核查并等待报告发布。</p>}
            </section>
          </div>
        </div>
      </div>
    </div>
  </section>
}

export function DeepReviewReportView({ report, showRanking = true }: { report: ReviewReport; showRanking?: boolean }) {
  const verdictLabels: Record<string, string> = { supports: '有证据支持', contradicts: '有证据冲突', insufficient: '证据不足' }
  const reasonLabels: Record<string, string> = {
    supported_by_citation: '有效引文支持', conflicted_by_citation: '有效引文显示冲突',
    not_found_in_provided_evidence: '所提供证据中未找到', citation_rejected: '引文未通过校验',
    model_output_incomplete: '模型输出不完整',
  }
  const hardLabels: Record<string, string> = { pass: '通过', fail: '失败', unknown: '未知', not_applicable: '不适用' }
  const repositoryLabels: Record<string, string> = { completed: '已完成', failed: '失败', unknown: '结果未知', skipped: '已跳过' }
  const coverageLabels: Record<string, string> = { full: 'README 全文', truncated: 'README 证据不完整', non_utf8: '非 UTF-8', empty: 'README 为空' }
  return <div className="deep-report">
    <p className="warning">{report.notice}</p>
    <p className="muted">模型：{report.model} · Prompt：{report.prompt_version} · 报告版本：{report.version} · 生成时间：{report.created_at}</p>
    <h4>调用与结果汇总</h4>
    <dl className="search-status"><dt>模型调用次数</dt><dd>{report.summary.usage.calls}（未知用量调用 {report.summary.usage.unknown_calls}）</dd><dt>已报告 Token 用量</dt><dd>输入 {report.summary.usage.prompt_tokens} · 输出 {report.summary.usage.completion_tokens} · 合计 {report.summary.usage.total_tokens}</dd><dt>仓库</dt><dd>总计 {report.summary.repositories_total} · 完成 {report.summary.repositories_completed} · 失败 {report.summary.repositories_failed} · 未知 {report.summary.repositories_unknown} · 跳过 {report.summary.repositories_skipped}</dd><dt>逐需求判定</dt><dd>支持 {report.summary.supports} · 冲突 {report.summary.contradicts} · 证据不足 {report.summary.insufficient} · 未知项 {report.summary.unknown_items}</dd></dl>
    <p className="muted">Token 合计只统计 Provider 明确返回用量的调用；未知用量不会被当作真实的 0。输入字节上界也不是 Tokenizer 用量。</p>
    {showRanking && report.ranking && <RankingView ranking={report.ranking} repositories={report.repositories} />}
    {report.warnings.length > 0 && <div className="warning"><strong>报告警告</strong><ul>{report.warnings.map((warning, index) => <li key={`${warning.code}-${index}`}>{warning.code}{warning.requirement_item_id ? ` · ${warning.requirement_item_id}` : ''}{warning.message ? ` · ${warning.message}` : ''}</li>)}</ul></div>}
    {report.repositories.length === 0 && <p>已发布报告没有可展示的仓库。</p>}
    {report.repositories.map(repository => <article className="search-candidate" key={repository.repository_id}>
      <h4>仓库 ID {repository.repository_id} · {repositoryLabels[repository.status] ?? repository.status}</h4>
      {repository.error_code && <p className="warning" role="alert">仓库错误：{repository.error_code}</p>}
      <p className="muted">证据覆盖：{coverageLabels[repository.coverage] ?? repository.coverage} · 输入 {repository.input.chunk_count} 个唯一 Chunk · UTF-8 字节上界 {repository.input.input_bytes_upper_bound}（不是 Token）</p>
      <p className="muted">预算：每仓库最多 {repository.input.budget.max_chunks} 个 Chunk、{repository.input.budget.max_input_bytes} 字节上界、{repository.input.budget.max_output_tokens} 输出 Token。</p>
      <p className="muted">本仓库用量：{repository.usage.known ? `输入 ${repository.usage.prompt_tokens} · 输出 ${repository.usage.completion_tokens} · 合计 ${repository.usage.total_tokens}` : 'Provider 未返回用量（未知）'}</p>
      <details><summary>精确 README 快照与模型响应</summary><dl>{Object.entries(repository.trace).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}<dt>snapshot_id</dt><dd>{repository.snapshot_id}</dd><dt>response_hash</dt><dd>{repository.response_hash ?? '无已记录响应'}</dd></dl><p className="muted">输入 Chunk：{repository.input.chunk_ids.join('、') || '无'}</p></details>
      <h5>约束汇总</h5>
      <ul><li>必需项：通过 {repository.hard_constraint_summary.required.pass} · 失败 {repository.hard_constraint_summary.required.fail} · 未知 {repository.hard_constraint_summary.required.unknown} · 总计 {repository.hard_constraint_summary.required.total}</li><li>禁止项：通过 {repository.hard_constraint_summary.prohibited.pass} · 失败 {repository.hard_constraint_summary.prohibited.fail} · 未知 {repository.hard_constraint_summary.prohibited.unknown} · 总计 {repository.hard_constraint_summary.prohibited.total}</li><li>偏好项：发现证据 {repository.hard_constraint_summary.preferred.evidence_found} · 未知 {repository.hard_constraint_summary.preferred.unknown} · 总计 {repository.hard_constraint_summary.preferred.total}</li></ul>
      {repository.logic_group_results.length > 0 && <><h5>ALL / ANY 逻辑组</h5><ul>{repository.logic_group_results.map(group => <li key={group.logic_group_id}>{group.logic_group_id} · {group.operator} · {group.result} · 成员 {group.member_item_ids.join('、')}</li>)}</ul></>}
      <h5>逐需求核查</h5>
      {repository.requirements.length === 0 && <p className="muted">该仓库没有逐需求结果；不代表需求已满足。</p>}
      <ul>{repository.requirements.map(item => <li key={item.requirement_item_id}>
        <p>{item.text} · {verdictLabels[item.verdict] ?? item.verdict}（{reasonLabels[item.reason_code] ?? item.reason_code}）</p>
        <p className="muted">{item.requirement_item_id} · {item.field} · {item.modality} · {item.operator} · 逻辑组 {item.logic_group_id ?? '无'} · 硬约束 {hardLabels[item.hard_constraint_check] ?? item.hard_constraint_check}</p>
        {item.model_omitted && <p className="warning">模型遗漏该需求，已由确定性规则补为证据不足。</p>}
        {item.rejected_citations.length > 0 && <p className="warning">剔除引文：{item.rejected_citations.map(citation => `${citation.chunk_id}（${citation.reason}）`).join('、')}</p>}
        {item.citations.length ? item.citations.map((quote, index) => <div key={`${quote.chunk_id}-${index}`}><small>{repository.trace.readme_path} · 行 {quote.line_start}–{quote.line_end} · UTF-8 字节 [{quote.byte_start}, {quote.byte_end}) · Chunk {quote.chunk_id}</small><pre className="source-quote">{quote.quote}</pre></div>) : <p>无通过校验的 README 引文；结论仅由确定性规则给出。</p>}
      </li>)}</ul>
    </article>)}
  </div>
}

function RankingView({ ranking, repositories }: { ranking: NonNullable<ReviewReport['ranking']>; repositories: ReviewReport['repositories'] }) {
  const tierLabels = { meets_requirements: '符合要求', needs_review: '待进一步核查', hard_conflict: '存在硬约束冲突' }
  const preferredLabels = { supports: '有支持', contradicts: '有反证', insufficient: '证据不足', mixed: '支持与反证并存' }
  const recommendationLabels = { recommended: '推荐', needs_review: '待进一步核查', not_recommended: '不推荐' }
  const sourceById = new Map(repositories.map(repository => [repository.repository_id, repository]))
  const requirementText = (repositoryId: string, itemId: string | null) => {
    const item = sourceById.get(repositoryId)?.requirements.find(row => row.requirement_item_id === itemId)
    return item?.text ?? itemId
  }
  return <section className="ranking-view" aria-labelledby="ranking-title">
    <h4 id="ranking-title">推荐排名（确定性规则）</h4>
    <p className="warning">{ranking.ranking_config_version === 'hard-tier-score-v2'
      ? '先比较硬约束层级，再比较同层综合得分与证据覆盖率，最后按仓库 ID 升序。分数不能覆盖硬约束层级。'
      : '先比较硬约束层级，再比较同层 preferred 有效证据数量；同分按仓库 ID 升序。'}</p>
    <p className="muted">规则版本：{ranking.ranking_config_version}。偏好项“有效证据”必须同时满足 supports 且带已校验引用。</p>
    {ranking.repositories.length === 0 && <p className="muted">没有可参与排名的已完成仓库。</p>}
    {ranking.repositories.map(repository => <article className="ranking-item" key={repository.repository_id}>
      <h5>第 {repository.rank} 名 · {repository.html_url
        ? <a href={repository.html_url} target="_blank" rel="noreferrer">{repository.full_name ?? repository.repository_id}</a>
        : repository.full_name ?? repository.repository_id} · <span className={`ranking-tier ${repository.hard_constraint_tier}`}>{tierLabels[repository.hard_constraint_tier]}</span></h5>
      <p className="muted">仓库 ID：{repository.repository_id} · 推荐状态：{recommendationLabels[repository.recommendation.status]}</p>
      {repository.total_score != null && <div className="ranking-score">
        <strong>{repository.total_score.toFixed(2)}</strong><span> / 100</span>
        {repository.evidence_coverage && <span className="muted"> · 证据覆盖率 {(repository.evidence_coverage.coverage * 100).toFixed(0)}%</span>}
      </div>}
      {repository.ranking_explanation && <p className="muted">{repository.ranking_explanation}</p>}
      {repository.score_breakdown && repository.score_breakdown.length > 0 && <table className="ranking-breakdown">
        <thead><tr><th>维度</th><th>权重</th><th>得分</th><th>支持 / 反证 / 不足</th></tr></thead>
        <tbody>{repository.score_breakdown.map(dimension => <tr key={dimension.key}>
          <td>{dimension.label}{dimension.missing_data ? '（元数据缺失）' : ''}</td>
          <td>{(dimension.weight * 100).toFixed(0)}%</td>
          <td>{dimension.applicable ? `${(dimension.score ?? 0).toFixed(2)}` : 'N/A'}</td>
          <td>{dimension.applicable ? `${dimension.supports} / ${dimension.contradicts} / ${dimension.insufficient}` : '—'}</td>
        </tr>)}</tbody>
      </table>}
      <p>{repository.preferred.applicable
        ? `偏好需求：${preferredLabels[repository.preferred.status ?? 'insufficient']} · 有效证据 ${repository.preferred.evidence_match_count} / ${repository.preferred.supports_count + repository.preferred.contradicts_count + repository.preferred.insufficient_count} · 支持 ${repository.preferred.supports_count} · 反证 ${repository.preferred.contradicts_count} · 不足 ${repository.preferred.insufficient_count}`
        : '偏好需求：不适用（没有 preferred 需求，不是证据不足）'}</p>
      <p className="muted">硬约束单元：{repository.hard_constraint_units.length === 0 ? '没有可核查的 required/prohibited 项' : repository.hard_constraint_units.map(unit => `${unit.kind} ${unit.source_id}：${unit.result}`).join('；')}</p>
      <div><strong>推荐理由（{repository.recommendation.reason_code}）</strong><ul>{repository.recommendation.reasons.map((reason, index) => <li key={`${reason.reason_code}-${reason.requirement_item_id ?? 'status'}-${index}`}>
        <p>{reason.text}</p>
        {reason.requirement_item_id && <p className="muted">需求：{requirementText(repository.repository_id, reason.requirement_item_id)} · {reason.requirement_item_id}</p>}
        {reason.citations.map((quote, citationIndex) => <div key={`${quote.chunk_id}-${citationIndex}`} className="ranking-citation">
          <small>{sourceById.get(repository.repository_id)?.trace.readme_path ?? 'README'} · 行 {quote.line_start}–{quote.line_end} · UTF-8 字节 [{quote.byte_start}, {quote.byte_end}) · Chunk {quote.chunk_id}</small>
          <pre className="source-quote">{quote.quote}</pre>
        </div>)}
      </li>)}</ul></div>
      <div><strong>限制说明</strong><ul>{repository.recommendation.limitations.map((limitation, index) => <li key={`${index}-${limitation}`}>{limitation}</li>)}</ul></div>
    </article>)}
    {ranking.excluded_repositories.length > 0 && <div className="ranking-excluded"><h5>未参与排名的仓库</h5><p className="muted">这些仓库状态不是 completed，不参与排名；排除不等于不符合需求。</p><ul>{ranking.excluded_repositories.map(repository => <li key={repository.repository_id}>仓库 ID {repository.repository_id} · 状态 {repository.status} · {repository.reason_code}{repository.error_code ? ` · ${repository.error_code}` : ''}</li>)}</ul></div>}
  </section>
}

function RetrievalEvidenceView({ bundle }: { bundle: RetrievalBundle }) {
  const fieldLabels: Record<string, string> = {
    project_topic: '项目主题', project_description: '项目描述', project_type: '项目类型', domain: '领域',
    required_features: '必需功能', preferred_features: '偏好功能', programming_languages: '编程语言',
    required_technologies: '必需技术', preferred_technologies: '偏好技术',
  }
  const candidateStatus: Record<string, string> = { ok: '检索完成', partial: '部分完成', failed: '检索失败' }
  const coverageLabel: Record<string, string> = {
    full: 'README 全文已索引', truncated: '证据覆盖不完整（README 过长）',
    non_utf8: 'README 不是 UTF-8，无法索引', empty: 'README 为空',
  }
  return <div className="retrieval-report">
    <p className="warning">{bundle.notice}</p>
    <p className="muted">召回只表示片段与需求可能相关（相关性待核查），不是“已找到满足证据”；禁止项未提及不代表符合。</p>
    {bundle.empty_reason && <p className="muted">没有可检索的已保存快照（{bundle.empty_reason}）。</p>}
    {bundle.candidates.length === 0 && !bundle.empty_reason && <p>本次运行没有可展示的检索证据。</p>}
    {bundle.candidates.map(candidate => {
      const repository = candidate.repository
      const requirements = new Map(bundle.requirements.map(row => [row.requirement_item_id, row]))
      const merged = new Map<string, { chunk: RetrievalChunk; associations: string[]; methods: Set<string>; distance: number | null; rrf: number }>()
      for (const item of candidate.items) {
        const requirement = requirements.get(item.requirement_item_id)
        const label = fieldLabels[requirement?.field ?? ''] ?? requirement?.field ?? '需求'
        for (const chunk of item.chunks) {
          const entry = merged.get(chunk.chunk_id) ?? { chunk, associations: [], methods: new Set<string>(), distance: null, rrf: 0 }
          entry.associations.push(`${label}：${item.text}`)
          chunk.methods.forEach(method => entry.methods.add(method))
          entry.distance = chunk.dense_distance === null ? entry.distance
            : entry.distance === null ? chunk.dense_distance : Math.min(entry.distance, chunk.dense_distance)
          entry.rrf = Math.max(entry.rrf, chunk.rrf_score)
          merged.set(chunk.chunk_id, entry)
        }
      }
      const trace = candidate.commit_sha || candidate.blob_sha || candidate.content_hash || candidate.collected_at
      return <article className="search-candidate" key={candidate.repository_id}>
        <h4>{repository?.html_url ? <a href={repository.html_url} target="_blank" rel="noreferrer">{repository.full_name ?? `仓库 ${candidate.repository_id}`}</a> : `仓库 ${candidate.repository_id}`}</h4>
        <p className="muted">{[
          repository?.stars !== null && repository?.stars !== undefined ? `${repository.stars} Stars` : '',
          repository?.language ?? '', candidateStatus[candidate.status] ?? candidate.status,
        ].filter(Boolean).join(' · ')}</p>
        {repository?.description && <p>{repository.description}</p>}
        {coverageLabel[candidate.coverage] && <p className="warning">{coverageLabel[candidate.coverage]}</p>}
        {candidate.status === 'failed' && <p className="error" role="alert">该仓库检索失败：{candidate.error_code ?? '未知原因'}（不影响其他仓库）。</p>}
        {candidate.status === 'partial' && <p className="warning">该仓库部分检索失败：{candidate.error_code ?? '部分检索方式不可用'}；已有片段仍展示。</p>}
        <details><summary>快照追溯</summary>
          {trace ? <dl>{[
            ['README 路径', candidate.readme_path ?? '历史记录未保存文件路径'],
            ['Commit', candidate.commit_sha ?? '历史记录未保存'], ['Blob', candidate.blob_sha ?? '历史记录未保存'],
            ['内容哈希', candidate.content_hash ?? '历史记录未保存'], ['采集时间', candidate.collected_at ?? '历史记录未保存'],
          ].map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>
            : <p>历史记录未保存完整快照标识（v1 Bundle 不补造字段）。</p>}
        </details>
        <h5>需求召回状态</h5>
        <ul>{candidate.items.map(item => {
          const requirement = requirements.get(item.requirement_item_id)
          const label = fieldLabels[requirement?.field ?? ''] ?? requirement?.field ?? '需求'
          return <li key={item.requirement_item_id}>{label} · {item.text} · {item.chunks.length
            ? `已召回 ${item.chunks.length} 条候选片段（相关性待核查）` : '未召回候选片段'}
            {requirement?.modality === 'prohibited' && ' · 该条为禁止项：未提及不代表符合禁止条件'}</li>
        })}</ul>
        <h5>候选片段（按 chunk 去重，关联关系完整保留）</h5>
        {merged.size === 0 && <p className="muted">该仓库没有召回候选片段。</p>}
        {[...merged.values()].map(entry => <details className="requirement-evidence" key={entry.chunk.chunk_id}>
          <summary>行 {entry.chunk.line_start}–{entry.chunk.line_end} · 关联 {entry.associations.length} 条需求</summary>
          <p className="muted">关联需求：{entry.associations.join('；')}</p>
          <small>{candidate.readme_path ?? '历史记录未保存文件路径'} · 行 {entry.chunk.line_start}–{entry.chunk.line_end} · UTF-8 字节 [{entry.chunk.byte_start}, {entry.chunk.byte_end}) · 检索：{[...entry.methods].join(' + ')}{entry.distance !== null ? ` · 余弦距离 ${entry.distance.toFixed(3)}（越小越近）` : ''} · RRF {entry.rrf.toFixed(4)}</small>
          <pre className="source-quote">{entry.chunk.text}</pre>
        </details>)}
      </article>
    })}
  </div>
}

function DeepReportView({ report }: { report: DeepReport }) {
  return <div className="deep-report">
    <p className="warning">{report.notice}</p>
    {report.anomalies.length > 0 && <p className="warning">模型输出异常：{report.anomalies.map(anomaly => `${anomaly.repository_id} · ${anomaly.message}`).join('；')}</p>}
    {report.candidates.length === 0 && <p>已发布报告没有可核对的候选。</p>}
    {report.candidates.map(candidate => <article className="search-candidate" key={candidate.repository_id}>
      <h4>第 {candidate.rank + 1} 名 · 仓库 ID {candidate.repository_id} · {candidate.recommendation.label}</h4>
      <p>{candidate.summary}</p>
      <p className="muted">{candidate.recommendation.reason}（{candidate.availability_label} · {candidate.input_coverage_label} · {candidate.input_bytes} 字节）</p>
      {candidate.source ? <details><summary>核对来源快照</summary><dl>{Object.entries(candidate.source).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl></details> : <p>无已保存快照，未纳入模型核对。</p>}
      {candidate.missing_features.length > 0 && <><h5>缺失功能清单</h5><ul>{candidate.missing_features.map(finding => <li key={finding.requirement_item_id}>{finding.text} · {finding.modality} · {finding.reason}</li>)}</ul></>}
      {candidate.risk_notes.length > 0 && <><h5>风险提示</h5><ul>{candidate.risk_notes.map(finding => <li key={finding.requirement_item_id}>{finding.text} · {finding.modality} · {finding.reason}</li>)}</ul></>}
      <h5>逐需求判定</h5>
      <ul>{candidate.items.map(item => <li key={item.requirement_item_id}><p>{item.text} · {item.verdict_label}（依据：{item.basis_label}）· 逻辑组 {item.logic_group_id ?? '无'}</p>{item.rejections.length > 0 && <p className="warning">引文剔除：{item.rejections.join('、')}（越界的行号不计入结论）</p>}{item.citations.length ? item.citations.map((quote, i) => <div key={i}><small>README 行 {quote.line_start}–{quote.line_end} · UTF-8 字节 [{quote.byte_start}, {quote.byte_end})</small><pre className="source-quote">{quote.text}</pre></div>) : <p>无通过复核的引文，结论仅由确定性规则给出。</p>}</li>)}</ul>
    </article>)}
  </div>
}

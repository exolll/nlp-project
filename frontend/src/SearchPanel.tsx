import { useEffect, useRef, useState } from 'react'
import { Failure, request } from './api'
import { deepPhase, deepPhaseLabel, deepReviewPayload, deepReviewStatusLabel, readDeepReview, readSearch, reserveDeep, reserveSearch, restoreDeep, restoreSearch, searchBudget, shouldPollDeep, shouldPollDeepReview, shouldPollSearch, validateDeepReview } from './search'
import type { DeepAttempt, DeepReport, DeepReviewOperation, DeepReviewReport as ReviewReport, RetrievalBundle, SearchAttempt, SearchEvidence, SearchRun } from './search'
import { statusOf } from './display/status'
import { formatStamp } from './display/format'
import { summarizeRanking, summarizeReview, summarizeScreening, type EvidenceViewModel, type RankingRowViewModel, type RepositoryReviewViewModel, type ScreeningChunkViewModel, type ScreeningRepositoryViewModel } from './display/viewModels'
import { AuditDetails, EmptyState, EvidenceCard, MetricGrid, StatusBadge, SummaryCard } from './components/display'

const runStateLabels: Record<string, string> = {
  reserved: '已受理', queued: '排队中', running: '进行中', completed: '已完成',
  partial: '部分完成', failed: '失败', interrupted: '已中断', not_started: '未开始',
}

export default function SearchPanel({ taskId, revisionId, enabled, stage = 4, onProgress, onRunId, onOpenDeep, onOpenResult }: {
  taskId: string
  revisionId: string
  enabled: boolean
  stage?: 4 | 5 | 6
  onProgress?: () => void
  onRunId?: (runId: string) => void
  onOpenDeep?: () => void
  onOpenResult?: () => void
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
  const [screeningView, setScreeningView] = useState<'cards' | 'table'>('cards')
  const [screeningFocusId, setScreeningFocusId] = useState<string | null>(null)
  const [authOpenOverride, setAuthOpenOverride] = useState<boolean | null>(null)
  const sectionScrollRef = useRef<HTMLDivElement | null>(null)
  const sectionRefs = useRef<Record<string, HTMLElement | null>>({})
  const [deepSection, setDeepSection] = useState<'check' | 'review' | 'trace'>('check')
  const [deepAuthOpenOverride, setDeepAuthOpenOverride] = useState<boolean | null>(null)
  const [reviewAuthOpenOverride, setReviewAuthOpenOverride] = useState<boolean | null>(null)
  const deepScrollRef = useRef<HTMLDivElement | null>(null)
  const deepSectionRefs = useRef<Record<string, HTMLElement | null>>({})
  const [resultSection, setResultSection] = useState<'ranking' | 'trace'>('ranking')
  const [detailRepositoryId, setDetailRepositoryId] = useState<string | null>(null)
  const resultScrollRef = useRef<HTMLDivElement | null>(null)
  const resultSectionRefs = useRef<Record<string, HTMLElement | null>>({})
  const scopeRef = useRef<AbortController | null>(null)
  const lock = useRef(false)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const reviewLock = useRef(false)
  const reviewTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const reviewPolls = useRef(0)
  const reviewStartedHere = useRef(false)
  const reviewAutoOpened = useRef(false)
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
      reviewStartedHere.current = true; reviewAutoOpened.current = false
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
  const repositoryNames = new Map<string, string | null>((retrieval?.candidates ?? []).map(candidate => [candidate.repository_id, candidate.repository?.full_name ?? null]))
  const reviewReport = review?.report ?? null
  const ranking = reviewReport?.ranking ?? null
  const rankingSummary = reviewReport?.ranking ? summarizeRanking(reviewReport.ranking, reviewReport.repositories, repositoryNames) : null
  const reviewSummaries = reviewReport ? summarizeReview(reviewReport, repositoryNames) : []
  const reviewPendingCount = reviewSummaries.reduce((total, repository) => total + repository.pending.length, 0)
  const reviewCoverageGaps = reviewSummaries.filter(repository => repository.coverage.raw !== 'full').length
  const screening = summarizeScreening(run, evidence, retrieval)
  const screeningNextStep = !evidence
    ? '证据尚未发布；可稍后手动刷新（仅 GET），不要重复发起搜索。'
    : screening.mentioned > 0
      ? '建议展开候选卡片逐条核对引用行、否定语义与禁止项；字面提及不能直接视为满足。'
      : '当前没有发现相关提及；这不代表需求不满足，建议查看检索片段，或在阶段 5 单独授权深度筛选。'
  const screeningRepositories = [...screening.repositories].sort((left, right) =>
    right.retrievalChunkCount - left.retrievalChunkCount
    || (evidence ? right.mentioned - left.mentioned : 0)
    || left.reference.localeCompare(right.reference, 'zh-CN'))
  const retrievalReadyCount = screening.repositories.filter(repository => repository.retrievalChunkCount > 0).length
  const focusRepository = screeningFocusId ? screening.repositories.find(repository => repository.repositoryId === screeningFocusId) ?? null : null
  const detailRepository = detailRepositoryId ? reviewSummaries.find(repository => repository.repositoryId === detailRepositoryId) ?? null : null
  const detailRanking = detailRepositoryId ? rankingSummary?.rows.find(row => row.repositoryId === detailRepositoryId) ?? null : null
  const detailScreening = detailRepositoryId ? screening.repositories.find(repository => repository.repositoryId === detailRepositoryId) ?? null : null
  const detailOpen = !!detailRepository && !!detailRanking
  useEffect(() => { if (stage !== 6) setDetailRepositoryId(null) }, [stage])
  useEffect(() => { setScreeningFocusId(null) }, [stage, run?.run_id])
  // 只在本会话刚授权并完成深度筛选时自动前进；已有报告再次进入阶段 5 时保留手动入口。
  useEffect(() => {
    if (!ranking || !reviewStartedHere.current || reviewAutoOpened.current) return
    reviewAutoOpened.current = true
    reviewStartedHere.current = false
    if (stage === 5) onOpenResult?.()
  }, [ranking, stage, onOpenResult])
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
    if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2) current = 'retrieval'
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
            <p className="overview-subtitle">先看候选与相关提及，再展开 README 原文；字面提及不是满足，原始状态与标识都保留在默认折叠的技术审计中。</p>
            <div className="overview-badges">
              <StatusBadge status={statusOf('run', run?.status ?? 'not_started')} />
              {evidence ? <span className="badge success">证据已发布</span> : <span className="badge">证据未发布</span>}
              {retrieval && <span className="badge info">检索片段已生成</span>}
            </div>
          </header>
          {run ? <dl className="search-overview-status">
            <div><dt>搜索运行</dt><dd>{statusOf('run', run.status).label}</dd></div>
            <div><dt>候选仓库</dt><dd>{screening.candidateCount} 个</dd></div>
            <div><dt>逐需求证据</dt><dd>{evidence ? `发现提及 ${screening.mentioned} · 尚未核实 ${screening.unconfirmed}` : '未发布'}</dd></div>
            <div><dt>检索片段</dt><dd>{retrieval ? `${screening.chunkCount} 条 · ${statusOf('retrieval', retrieval.status).label}` : statusOf('retrieval', run.retrieval_status ?? 'not_started').label}</dd></div>
            <div><dt>绑定版本</dt><dd>已确认并保存</dd></div>
          </dl> : <p className="overview-empty">还没有搜索运行；确认需求本身不会自动搜索。</p>}
          <div className="search-budget">固定预算 · 模型 {searchBudget.model_calls} 次 · 搜索 {searchBudget.search_requests} 次 · GitHub {searchBudget.github_requests} 次 · README {searchBudget.readme_candidates} 个</div>
          <nav className="group-nav" aria-label="初筛分区">
            <button type="button" className={activeSection === 'run' ? 'active' : ''} onClick={() => scrollToSection('run')}>
              <span className="nav-index">01</span><span className="nav-label">初筛结论</span><span className="nav-status">{run ? statusOf('run', run.status).label : '未开始'}</span>
            </button>
            <button type="button" className={activeSection === 'evidence' ? 'active' : ''} onClick={() => scrollToSection('evidence')}>
              <span className="nav-index">02</span><span className="nav-label">筛选结果</span><span className="nav-status">{screening.repositories.length > 0 ? `${screening.repositories.length} 个候选` : '未发布'}</span>
            </button>
            <button type="button" className={activeSection === 'retrieval' ? 'active' : ''} onClick={() => scrollToSection('retrieval')}>
              <span className="nav-index">03</span><span className="nav-label">检索与追溯</span><span className="nav-status">{retrieval ? `${screening.chunkCount} 个片段` : '未生成'}</span>
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
            <section className="search-section card candidate-hero" id="search-section-run" ref={node => { sectionRefs.current.run = node }} aria-labelledby="search-run-title">
              <header className="candidate-hero-head">
                <div>
                  <span className="summary-eyebrow">04 / INITIAL SCREENING</span>
                  <h3 id="search-run-title">GitHub 候选项目</h3>
                  <p>汇总本次运行的候选仓库、相关提及与检索片段；只读取已保存结果，不会重新访问 GitHub。</p>
                </div>
                {evidence ? <span className="badge success">初筛完成</span> : <StatusBadge status={statusOf('run', run?.status ?? 'not_started')} />}
              </header>
              {!run ? <EmptyState
                title="还没有搜索运行"
                description="在左侧明确授权并开始搜索后，这里会显示候选、相关提及与检索摘要；确认需求本身不会自动搜索。"
              /> : <>
                <div className="candidate-stats">
                  <div className="candidate-stat"><span>检索仓库</span><strong>{screening.candidateCount}</strong><small>本次运行已记录的候选仓库</small></div>
                  <div className="candidate-stat"><span>有检索片段</span><strong>{retrieval ? retrievalReadyCount : '—'}</strong><small>仅表示相关，不是通过率</small></div>
                  <div className="candidate-stat"><span>发现相关提及</span><strong>{evidence ? screening.mentioned : '—'}</strong><small>字面提及，不是满足结论</small></div>
                </div>
                <p className="stage-boundary">“发现相关提及”仅表示 README 中出现相关文字，不等同功能已满足；“尚未核实”也不代表不满足。检索片段仅表示相关；否定语义、禁止项与 ALL / ANY 约束仍需人工核对。</p>
                <p className="hint-text">建议下一步：{screeningNextStep}</p>
                {run.search_status === 'partial' && <p className="warning">本次仅完成部分采集；已保存证据仍可查看，缺失部分不代表需求不满足。</p>}
                {screening.candidateCount === 0 && <p className="muted">尚无候选记录，不代表需求不满足。</p>}
                <AuditDetails title="运行技术审计与原始状态" fields={runAuditFields(run, revisionId)} />
              </>}
              {!run && <AuditDetails title="运行技术审计与原始状态" fields={[{ label: '绑定版本 ID', value: revisionId }]} />}
            </section>
            <section className="search-section card" id="search-section-evidence" ref={node => { sectionRefs.current.evidence = node }} aria-labelledby="search-evidence-title">
              <header className="search-section-head"><span className="group-index">02</span><h3 id="search-evidence-title">筛选结果</h3>{screening.repositories.length > 0 && <span className="badge info">{screening.repositories.length} 个候选</span>}</header>
              <p className="stage-boundary">“发现相关提及”只表示 README 中出现相关文字，不是“已满足”；“尚未核实”也不是“不满足”。后端未提供匹配度评分，本页按已保存的检索片段数排序，不代表最终推荐顺序。</p>
              {screening.repositories.length > 0 && <div className="screening-toolbar">
                <span className="screening-sort">排序：检索片段数 ↓<small>后端未提供评分，仅按片段数排序</small></span>
                <div className="view-toggle" role="group" aria-label="切换筛选结果视图">
                  <button type="button" className={screeningView === 'cards' ? 'active' : ''} aria-pressed={screeningView === 'cards'} onClick={() => setScreeningView('cards')}>卡片</button>
                  <button type="button" className={screeningView === 'table' ? 'active' : ''} aria-pressed={screeningView === 'table'} onClick={() => setScreeningView('table')}>表格</button>
                </div>
              </div>}
              {!evidence && <p className="warning">逐需求证据尚未发布；以下仓库与快照仅来自已保存的检索数据，不代表需求不满足。</p>}
              {screening.repositories.length === 0
                ? <EmptyState
                    title={run ? '还没有候选仓库' : '还没有搜索运行'}
                    description={run ? '本次运行没有已保存的候选；这不代表需求不满足。' : '请先在左侧明确授权并开始搜索；这里只读取已保存结果。'}
                  />
                : screeningView === 'cards'
                  ? screeningRepositories.map(repository => <ScreeningRepositoryCard key={repository.repositoryId} repository={repository} evidencePublished={!!evidence} />)
                  : <div className="screening-table-wrap">
                      <table className="screening-table" aria-label="筛选结果候选仓库列表">
                        <thead><tr><th scope="col">仓库</th><th scope="col">语言</th><th scope="col">相关提及</th><th scope="col">尚未核实</th><th scope="col">检索片段</th><th scope="col">状态</th><th scope="col"><span className="visually-hidden">操作</span></th></tr></thead>
                        <tbody>{screeningRepositories.map(repository => {
                          const name = repository.name === '未命名仓库' ? repository.reference : repository.name
                          return <tr key={repository.repositoryId}>
                            <td><strong>{repository.htmlUrl ? <a href={repository.htmlUrl} target="_blank" rel="noreferrer">{name}</a> : name}</strong><small>{repository.reference}</small></td>
                            <td>{repository.language ?? '—'}</td>
                            <td>{evidence ? repository.mentioned : '—'}</td>
                            <td>{evidence ? repository.unconfirmed : '—'}</td>
                            <td>{repository.retrievalChunkCount}</td>
                            <td><StatusBadge status={repository.retrievalStatus} /></td>
                            <td><button type="button" className="text-button" aria-expanded={screeningFocusId === repository.repositoryId} aria-controls={screeningFocusId === repository.repositoryId ? 'screening-focus-detail' : undefined} onClick={() => setScreeningFocusId(repository.repositoryId)}>查看初筛依据</button></td>
                          </tr>
                        })}</tbody>
                      </table>
                      {focusRepository && <div className="screening-focus" id="screening-focus-detail">
                        <div className="screening-focus-head"><strong>初筛依据 · {focusRepository.reference}</strong><button type="button" className="text-button" onClick={() => setScreeningFocusId(null)}>收起</button></div>
                        <ScreeningRepositoryCard repository={focusRepository} evidencePublished={!!evidence} />
                      </div>}
                    </div>}
            </section>
            <section className="search-section card" id="search-section-retrieval" ref={node => { sectionRefs.current.retrieval = node }} aria-labelledby="search-retrieval-title">
              <header className="search-section-head"><span className="group-index">03</span><h3 id="search-retrieval-title">检索与追溯</h3>{retrieval && <span className="badge info">{screening.chunkCount} 个片段</span>}</header>
              <p className="warning">检索只表示“相关”，不代表功能已满足；不显示满足百分比。所有片段都来自已保存的 README 快照。</p>
              {run?.retrieval_status === 'failed' && <p className="warning" role="alert">检索证据生成失败；搜索与字面证据不受影响，原始错误代码保留在技术审计中。</p>}
              {run?.retrieval_status === 'partial' && <p className="warning">部分仓库或需求未能完成检索；已完成的片段仍会显示。</p>}
              {retrievalError && <p className="error" role="alert">{retrievalError}</p>}
              {!retrieval
                ? <EmptyState
                    title="检索片段未生成"
                    description={run?.retrieval_status === 'failed' ? '本次检索失败；这不影响已发布的搜索与字面证据。' : '检索证据尚未生成；稍后只读刷新即可，不会重新发起搜索。'}
                  />
                : <ScreeningRetrievalView repositories={screening.repositories} bundle={retrieval} />}
            </section>
          </div>
          <div className="screening-actionbar">
            <div className="screening-selection">
              <strong>{run ? `候选 ${screening.candidateCount} 个` : '尚未开始初筛'}</strong>
              <span>{evidence ? '深度筛选在阶段 5 统一执行并单独授权；当前接口不提供项目勾选。' : '证据发布后可进入深度筛选；进入本身不会调用模型。'}</span>
            </div>
            <button type="button" className="primary screening-next" disabled={!evidence || !onOpenDeep} onClick={() => onOpenDeep?.()} title={!evidence ? '等待初筛证据发布后才能进入深度筛选。' : !onOpenDeep ? '当前视图未提供阶段导航。' : '仅切换阶段，不发起模型或 GitHub 调用；深度筛选需在阶段 5 单独授权。'}>进入深度筛选 →</button>
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
            <p className="overview-subtitle">先看结论，再按需展开证据；原始状态、标识与完整字段都保留在默认折叠的技术审计中。切换子界面只读取已保存结果。</p>
            <div className="overview-badges">
              <span className={`badge ${reviewReport ? 'success' : review ? 'info' : ''}`}>{review ? deepReviewStatusLabel(review.status) : '逐仓库核查未授权'}</span>
              {reviewReport && <span className="badge info">{reviewReport.summary.repositories_completed}/{reviewReport.summary.repositories_total} 个仓库完成</span>}
              {reviewReport && reviewPendingCount > 0 && <span className="badge warn">待核查 {reviewPendingCount} 项</span>}
              {review?.report?.ranking && <span className="badge success">排名已生成</span>}
            </div>
          </header>
          <dl className="search-overview-status">
            <div><dt>逐仓库核查</dt><dd>{review ? deepReviewStatusLabel(review.status) : reviewReady ? '可以授权' : '等待检索证据'}</dd></div>
            <div><dt>需求结论</dt><dd>{reviewReport ? `支持 ${reviewReport.summary.supports} · 冲突 ${reviewReport.summary.contradicts} · 信息不足 ${reviewReport.summary.insufficient}` : '报告未发布'}</dd></div>
            <div><dt>覆盖不完整</dt><dd>{reviewReport ? `${reviewCoverageGaps} 个仓库` : '未提供'}</dd></div>
            <div><dt>报告发布时间</dt><dd>{reviewReport ? formatStamp(reviewReport.created_at) : '未发布'}</dd></div>
            <div><dt>第一轮深度核对</dt><dd>{run?.deep_check ? deepPhaseLabel(phase) : '未授权（可选）'}</dd></div>
            <div><dt>确定性排名</dt><dd>{review?.report?.ranking ? '已生成，见最终结果' : '未生成'}</dd></div>
          </dl>
          <div className="search-budget">固定预算 · 深度核对 1 次 · 逐仓库最多 5 次模型 · 只读已保存 README</div>
          <nav className="group-nav" aria-label="深度筛选分区">
            <button type="button" className={deepSection === 'check' ? 'active' : ''} onClick={() => scrollToDeepSection('check')}>
              <span className="nav-index">01</span><span className="nav-label">结论总览</span><span className="nav-status">{reviewReport ? '已发布' : '等待'}</span>
            </button>
            <button type="button" className={deepSection === 'review' ? 'active' : ''} onClick={() => scrollToDeepSection('review')}>
              <span className="nav-index">02</span><span className="nav-label">仓库与证据</span><span className="nav-status">{reviewReport ? `${reviewSummaries.length} 个仓库` : '未发布'}</span>
            </button>
            <button type="button" className={deepSection === 'trace' ? 'active' : ''} onClick={() => scrollToDeepSection('trace')}>
              <span className="nav-index">03</span><span className="nav-label">授权与追溯</span><span className="nav-status">{reviewReport ? '已发布' : '未授权'}</span>
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
              <header className="search-section-head"><span className="group-index">01</span><h3 id="deep-check-title">深度筛选结论</h3><span className={`badge ${reviewReport ? 'success' : ''}`}>{reviewReport ? '已发布' : review ? deepReviewStatusLabel(review.status) : '未授权'}</span></header>
              {reviewReport ? <>
                <SummaryCard
                  eyebrow="逐仓库深度核查"
                  title={`${reviewReport.summary.repositories_completed} / ${reviewReport.summary.repositories_total} 个仓库完成逐需求核查`}
                  statuses={[statusOf('review', review?.status ?? reviewReport.status), statusOf('evidence', 'published')]}
                  description={reviewReport.notice}
                />
                <MetricGrid items={[
                  { label: '需求条目', value: reviewReport.summary.requirements_total },
                  { label: '有证据支持', value: reviewReport.summary.supports, hint: '按仓库需求条目计' },
                  { label: '有证据冲突', value: reviewReport.summary.contradicts, hint: '需人工确认' },
                  { label: '信息不足', value: reviewReport.summary.insufficient, hint: '不等于不满足' },
                  { label: '模型遗漏', value: reviewReport.summary.model_omitted, hint: '已补为信息不足' },
                  { label: '覆盖不完整', value: reviewCoverageGaps, hint: '个仓库，仅基于已保存 README' },
                ]} />
                <p className="stage-boundary">“信息不足”不等于不满足；“有证据支持”也只代表已保存 README 中的证据。需要时可在仓库卡片中展开原文核对。</p>
                {reviewReport.warnings.length > 0 && <p className="warning">报告包含 {reviewReport.warnings.length} 条提示（原始代码保留在技术审计中）。{reviewReport.warnings.map(warning => warning.message).filter(Boolean).slice(0, 3).join('；')}</p>}
                <p className="hint-text">建议下一步：{reviewPendingCount > 0 ? `优先人工核查 ${reviewPendingCount} 项冲突或信息不足的需求，重点查看仓库卡片中的证据。` : '逐条抽查支持证据后，再进入最终结果查看确定性排名。'}</p>
              </> : <EmptyState
                title={review ? `深度核查${deepReviewStatusLabel(review.status)}` : '还没有逐仓库核查结果'}
                description={review ? '报告发布后这里会显示结论摘要；未发布或结果未知不代表需求不满足。' : '请先在左侧完成逐仓库核查授权；本页只读取已保存的检索证据，不会自动调用模型。'}
              />}
              <details className="requirement-evidence deep-check-fold">
                <summary>第一轮深度核对（可选） · {report ? `${report.candidates.length} 个候选` : '未授权'}</summary>
                {report ? <DeepReportView report={report} names={repositoryNames} /> : <p className="muted">深度核对需在左侧单独授权 1 次模型调用；未授权不影响逐仓库核查，也不代表需求不满足。</p>}
              </details>
            </section>
            <section className="search-section card" id="deep-section-review" ref={node => { deepSectionRefs.current.review = node }} aria-labelledby="deep-review-title">
              <header className="search-section-head"><span className="group-index">02</span><h3 id="deep-review-title">仓库结论与需求匹配</h3>{reviewReport && <span className="badge success">{reviewSummaries.length} 个仓库</span>}</header>
              <p className="stage-boundary">每个仓库先显示结论、优势与待核查项；展开“需求匹配与证据”可查看原引文和行号，模型原因与原始字段保留在默认折叠的审计信息中。</p>
              {reviewReport ? <DeepReviewReportView report={reviewReport} names={repositoryNames} showRanking={false} /> : review ? <p className="muted">尚无已发布的深度核查报告；未发布或结果未知不代表需求不满足。</p> : <p className="muted">尚未授权逐仓库深度核查；请在左侧「逐仓库核查授权」中确认数据范围与预算。</p>}
            </section>
            <section className="search-section card" id="deep-section-trace" ref={node => { deepSectionRefs.current.trace = node }} aria-labelledby="deep-trace-title">
              <header className="search-section-head"><span className="group-index">03</span><h3 id="deep-trace-title">授权与报告追溯</h3>{reviewReport ? <span className="badge success">已发布</span> : <span className="badge">未发布</span>}</header>
              {review ? <>
                <dl className="search-status">
                  <dt>核查状态</dt><dd><StatusBadge status={statusOf('review', review.status)} /></dd>
                  <dt>报告发布</dt><dd>{review.report ? '已发布' : '未发布'}</dd>
                  <dt>授权时间</dt><dd>{formatStamp(review.authorized_at)}</dd>
                  <dt>截止时间</dt><dd>{formatStamp(review.deadline)}</dd>
                  <dt>错误状态</dt><dd>{review.error_code ? '存在错误，原始代码见技术审计' : '无'}</dd>
                </dl>
                <AuditDetails title="技术审计信息（默认折叠，可复制）" fields={reviewAuditFields(review)} />
                {review.report && <AuditDetails title="报告清单与完整性标识" fields={reportAuditFields(review.report)} />}
                {review.report && review.report.warnings.length > 0 && <AuditDetails title={`报告提示（${review.report.warnings.length}）`} fields={review.report.warnings.map((warning, index) => ({
                  label: `提示 ${index + 1}`,
                  value: [warning.code, warning.requirement_item_id ?? '无需求项', warning.logic_group_id ?? '无逻辑组', warning.message ?? '无说明'].join(' · '),
                }))} />}
                {run?.deep_check && <AuditDetails title="第一轮深度核对运行标识" fields={deepCheckAuditFields(run.deep_check)} />}
                <p className="muted">本页结果来自已发布报告，切换子界面或刷新只执行 GET；不会重发已完成的模型调用。确定性排名已生成时，完整排名在「最终结果」子界面展示。</p>
              </> : <p className="muted">逐仓库核查尚未授权，暂无授权与报告追溯信息。</p>}
            </section>
          </div>
          <div className="screening-actionbar">
            <div className="screening-selection">
              <strong>{ranking ? '深度筛选完成 · 排名已生成' : reviewReport ? '报告已发布 · 排名未生成' : '深度筛选尚未完成'}</strong>
              <span>{ranking ? '进入最终结果只切换阶段，不重新调用模型或 GitHub；仍可随时返回本页核对证据。' : reviewReport ? '最终结果需要确定性排名；可稍后刷新只读报告，不会重发模型调用。' : '完成逐仓库深度核查并发布报告后，可进入最终结果。'}</span>
            </div>
            <button type="button" className="primary screening-next" disabled={!ranking || !onOpenResult} onClick={() => onOpenResult?.()} title={!ranking ? '确定性排名尚未生成，暂不能进入最终结果。' : !onOpenResult ? '当前视图未提供阶段导航。' : '仅切换阶段；最终结果页只读取已发布报告。'}>查看最终结果 →</button>
          </div>
        </div>
      </div>
    </div>
    <div className="search-stage search-workspace-stage" hidden={stage !== 6}>
      <div className="search-workspace result-workspace" hidden={detailOpen}>
        <aside className="search-overview result-overview card" aria-label="最终结果概览">
          <header className="overview-head">
            <span className="section-index">06 / RESULT</span>
            <h2 id="result-title">最终结果与推荐</h2>
            <p className="overview-subtitle">推荐排名来自已发布的深度核查报告，按确定性规则生成；切换子界面、刷新或重新进入任务只执行 GET，不会重新调用模型。</p>
            <div className="overview-badges">
              <span className={`badge ${rankingSummary ? 'success' : ''}`}>{rankingSummary ? '已发布' : '等待深度筛选'}</span>
              {!rankingSummary && review && <span className="badge">{deepReviewStatusLabel(review.status)}</span>}
              {rankingSummary && <span className="badge info">参与排名 {rankingSummary.counts.participated} 个</span>}
              {rankingSummary && <span className="badge warn">待核查 {rankingSummary.counts.needsReview} 个</span>}
            </div>
          </header>
          <dl className="search-overview-status">
            <div><dt>推荐结论</dt><dd>{rankingSummary?.top ? `${rankingSummary.top.recommendation.label} · ${rankingSummary.top.name}` : '尚未生成'}</dd></div>
            <div><dt>参与排名</dt><dd>{rankingSummary ? `${rankingSummary.counts.participated} 个仓库` : '尚未生成'}</dd></div>
            <div><dt>推荐 / 待核查 / 不推荐</dt><dd>{rankingSummary ? `${rankingSummary.counts.recommended} / ${rankingSummary.counts.needsReview} / ${rankingSummary.counts.notRecommended}` : '尚未生成'}</dd></div>
            <div><dt>未参与排名</dt><dd>{rankingSummary ? `${rankingSummary.counts.excluded} 个 · 不等于不符合需求` : '尚未生成'}</dd></div>
            <div><dt>排名规则</dt><dd>{rankingSummary?.rule.label ?? '尚未生成'}</dd></div>
            <div><dt>报告生成</dt><dd>{rankingSummary?.scoredAt ?? (reviewReport ? formatStamp(reviewReport.created_at) : '尚未发布')}</dd></div>
          </dl>
          <div className="search-budget">只读结果页 · 不发起新模型调用 · 未参与排名不等于不符合需求</div>
          <nav className="group-nav" aria-label="最终结果分区">
            <button type="button" className={resultSection === 'ranking' ? 'active' : ''} onClick={() => scrollToResultSection('ranking')}>
              <span className="nav-index">01</span><span className="nav-label">推荐结论与依据</span><span className="nav-status">{rankingSummary ? `${rankingSummary.counts.participated} 个仓库` : '未发布'}</span>
            </button>
            <button type="button" className={resultSection === 'trace' ? 'active' : ''} onClick={() => scrollToResultSection('trace')}>
              <span className="nav-index">02</span><span className="nav-label">报告与授权溯源</span><span className="nav-status">{review?.report ? '已发布' : '未发布'}</span>
            </button>
          </nav>
          <div className="overview-feedback" aria-live="polite">
            {!rankingSummary && !review && <p className="muted">尚未授权逐仓库深度核查；本页不会自动重试、重新调用模型或编造排名。</p>}
            {!rankingSummary && review && <p className="muted">深度核查{deepReviewStatusLabel(review.status)}；报告发布后本页自动显示排名，期间只读 GET。</p>}
            {rankingSummary && <p className="muted">排名按确定性规则生成；分数不能覆盖硬约束层级，排除不等于不符合需求。</p>}
          </div>
        </aside>
        <div className="search-editor result-editor">
          <div className="search-editor-scroll result-editor-scroll" ref={resultScrollRef} onScroll={onResultScroll}>
            <section className="search-section card" id="result-section-ranking" ref={node => { resultSectionRefs.current.ranking = node }} aria-labelledby="result-ranking-title">
              <header className="search-section-head"><span className="group-index">01</span><h3 id="result-ranking-title">推荐结论与依据</h3><span className={`badge ${rankingSummary ? 'success' : ''}`}>{rankingSummary ? '已发布' : '等待深度筛选'}</span></header>
              {ranking && reviewReport
                ? <><p className="stage-boundary">推荐排名来自已发布的深度核查报告，按确定性规则生成；分数不能覆盖硬约束层级，字面提及也不能直接视为满足。展开“推荐依据与证据”可查看引文和行号，原始字段保留在技术审计中。</p><RankingView ranking={ranking} repositories={reviewReport.repositories} names={repositoryNames} onOpenDetail={setDetailRepositoryId} /></>
                : <EmptyState title="还没有已发布的最终排名" description="请在“深度筛选”子界面完成逐仓库深度核查并等待报告发布。切换子界面不会自动重试、重新调用模型或编造结果。" />}
            </section>
            <section className="search-section card" id="result-section-trace" ref={node => { resultSectionRefs.current.trace = node }} aria-labelledby="result-trace-title">
              <header className="search-section-head"><span className="group-index">02</span><h3 id="result-trace-title">报告与授权溯源</h3><span className={`badge ${review?.report ? 'success' : ''}`}>{review?.report ? '已发布' : '未发布'}</span></header>
              {review ? <>
                <dl className="search-status">
                  <dt>深度核查结论</dt><dd><StatusBadge status={statusOf('review', review.status)} /></dd>
                  <dt>报告发布</dt><dd>{review.report ? '已发布' : '未发布'}</dd>
                  <dt>报告生成</dt><dd>{review.report ? formatStamp(review.report.created_at) : '尚未发布'}</dd>
                  <dt>授权时间 / 截止时间</dt><dd>{formatStamp(review.authorized_at)} / {formatStamp(review.deadline)}</dd>
                  <dt>排名规则</dt><dd>{rankingSummary?.rule.label ?? '尚未生成'}</dd>
                  <dt>错误状态</dt><dd>{review.error_code ? '存在错误，原始代码见技术审计' : '无'}</dd>
                </dl>
                <AuditDetails title="授权与报告技术审计（默认折叠，可复制）" fields={reviewAuditFields(review)} />
                {review.report && <AuditDetails title="报告清单与完整性标识" fields={reportAuditFields(review.report)} />}
                {rankingSummary && <AuditDetails title="排名规则与原始状态" fields={rankingSummary.auditFields} />}
                {review.report && review.report.warnings.length > 0 && <AuditDetails title={`报告提示（${review.report.warnings.length}）`} fields={review.report.warnings.map((warning, index) => ({
                  label: `提示 ${index + 1}`,
                  value: [warning.code, warning.requirement_item_id ?? '无需求项', warning.logic_group_id ?? '无逻辑组', warning.message ?? '无说明'].join(' · '),
                }))} />}
                <p className="muted">排名只使用已发布报告中的确定性结果；切换子界面或刷新只执行 GET，不会重发已完成的模型调用。</p>
              </> : <p className="muted">逐仓库核查尚未授权，暂无未发布报告与完整性标识。请在「深度筛选」完成逐仓库深度核查并等待报告发布。</p>}
            </section>
          </div>
        </div>
      </div>
      {detailOpen && detailRepository && detailRanking && <ProjectDetailWorkspace
        key={detailRepository.repositoryId}
        repository={detailRepository}
        ranking={detailRanking}
        screening={detailScreening}
        rankingAuditFields={rankingSummary?.auditFields ?? []}
        onBack={() => setDetailRepositoryId(null)}
      />}
    </div>
  </section>
}

export function DeepReviewReportView({ report, showRanking = true, names = new Map() }: {
  report: ReviewReport
  showRanking?: boolean
  names?: Map<string, string | null>
}) {
  const repositories = summarizeReview(report, names)
  return <div className="deep-report">
    {showRanking && report.ranking && <RankingView ranking={report.ranking} repositories={report.repositories} names={names} />}
    {repositories.length === 0 && <p>已发布报告没有可展示的仓库。</p>}
    {repositories.map(repository => <RepositoryReviewCard key={repository.repositoryId} repository={repository} />)}
  </div>
}

function RepositoryReviewCard({ repository }: { repository: RepositoryReviewViewModel }) {
  return <article className="search-candidate display-repo-card">
    <header className="display-repo-head">
      <div>
        <span className="display-repo-ref">{repository.reference}</span>
        <h4>{repository.displayName}</h4>
      </div>
      <div className="display-repo-badges">
        <StatusBadge status={repository.status} />
        <StatusBadge status={repository.coverage} />
      </div>
    </header>
    <MetricGrid items={[
      { label: '需求条目', value: repository.requirementCount },
      { label: '有证据支持', value: repository.supportCount },
      { label: '证据冲突', value: repository.conflictCount },
      { label: '信息不足', value: repository.insufficientCount },
    ]} />
    {repository.status.raw !== 'completed' && <p className="warning">该仓库核查{repository.status.label}；未完成或结果未知不代表需求不满足，原始状态见技术审计。</p>}
    {repository.strengths.length > 0 && <section className="repo-findings">
      <h5>优势与支持证据</h5>
      <ul>{repository.strengths.map((item, index) => <li key={`${repository.repositoryId}-strength-${index}`}>{item}</li>)}</ul>
    </section>}
    {repository.gaps.length > 0 && <section className="repo-findings">
      <h5>待核查 / 缺口</h5>
      <ul>{repository.gaps.map((item, index) => <li key={`${repository.repositoryId}-gap-${index}`}>{item}</li>)}</ul>
    </section>}
    {repository.gaps.length === 0 && repository.requirementCount > 0 && <p className="hint-text">没有待核查的必需或禁止项；支持结论仍建议抽查引文。</p>}
    <p className="hint-text">建议：{repository.nextStep}</p>
    <details className="repo-requirements">
      <summary>需求匹配与证据（{repository.requirementCount}）</summary>
      {repository.requirementCount === 0 && <p className="muted">该仓库没有逐需求结果，不代表需求已满足。</p>}
      {repository.requirements.map(item => <details className="requirement-evidence" key={item.key}>
        <summary><span className="requirement-summary-text">{item.ref} · {item.text}</span><StatusBadge status={item.verdict} /></summary>
        <p className="muted">{item.modality.label} · {item.reason.label} · 硬约束 {item.hardCheck.label} · 引文 {item.citationCount} 条</p>
        {item.modelOmitted && <p className="hint-text">模型遗漏该需求，已按确定性规则补为信息不足。</p>}
        {item.rejectedCount > 0 && <p className="warning">有 {item.rejectedCount} 条引文未通过校验，已从结论中排除；原始原因见需求技术字段。</p>}
        {item.citations.length > 0
          ? item.citations.map(evidence => <EvidenceCard key={evidence.key} evidence={evidence} />)
          : <p className="muted">没有通过校验的 README 引文；该结论来自确定性规则，“信息不足”不等于项目不具备该功能。</p>}
        <AuditDetails title="需求技术字段" fields={item.auditFields} />
      </details>)}
    </details>
    <AuditDetails title="仓库技术审计与原始字段" fields={repository.auditFields} />
  </article>
}

function RankingView({ ranking, repositories, names = new Map(), onOpenDetail }: {
  ranking: NonNullable<ReviewReport['ranking']>
  repositories: ReviewReport['repositories']
  names?: Map<string, string | null>
  onOpenDetail?: (repositoryId: string) => void
}) {
  const summary = summarizeRanking(ranking, repositories, names)
  const nextStep = summary.counts.participated === 0
    ? '当前没有可参与排名的仓库；请回到深度筛选核查失败或未完成的原因，不能据此判定需求不满足。'
    : summary.counts.needsReview > 0
      ? `先核对首选仓库的推荐理由与引文，再逐项检查 ${summary.counts.needsReview} 个“待进一步核查”仓库的缺口；排名不能替代人工判断。`
      : '逐条抽查首选仓库的推荐理由与引文，并确认未参与排名的仓库不是被误排除。'
  return <section className="ranking-view" aria-labelledby="ranking-title">
    <h4 id="ranking-title">推荐结论与排名</h4>
    <SummaryCard
      eyebrow="最终推荐结论"
      title={summary.headline}
      statuses={[summary.rule]}
      description={`${summary.explanation}${summary.ruleNote}`}
    />
    <MetricGrid items={[
      { label: '参与排名', value: summary.counts.participated },
      { label: '推荐', value: summary.counts.recommended, hint: '首选为首位' },
      { label: '待进一步核查', value: summary.counts.needsReview, hint: '需人工确认' },
      { label: '不推荐', value: summary.counts.notRecommended, hint: '含硬约束冲突' },
      { label: '未参与排名', value: summary.counts.excluded, hint: '不等于不符合需求' },
    ]} />
    <p className="stage-boundary">排名是确定性规则下的相对顺序，不是需求满足的唯一证明；“推荐”仍需抽查引文，“不推荐”与“未参与”也不能直接等同于项目不符合需求。</p>
    <p className="hint-text">建议下一步：{nextStep}</p>
    {summary.rows.length === 0 && <EmptyState title="没有可参与排名的仓库" description="已发布报告中的仓库可能尚未完成核查；未完成或未参与排名不代表需求不满足，请查看技术审计中的原始状态。" />}
    <div className="ranking-list">
      {summary.rows.map(row => <RankingResultCard key={row.repositoryId} row={row} onOpenDetail={onOpenDetail} />)}
    </div>
    {summary.excluded.length > 0 && <section className="ranking-excluded">
      <h5>未参与排名的仓库（{summary.excluded.length}）</h5>
      <p className="muted">这些仓库未完成核查或未通过排名前置条件；排除不等于不符合需求，原始原因代码保留在各自的默认折叠技术审计中。</p>
      <div className="ranking-excluded-list">
        {summary.excluded.map(row => <article className="search-candidate display-repo-card" key={row.repositoryId}>
          <header className="display-repo-head">
            <div><span className="display-repo-ref">{row.reference}</span><h4>{row.name}</h4></div>
            <StatusBadge status={row.status} />
          </header>
          <p className="hint-text">未参与排名；这不代表项目不符合需求。</p>
          <AuditDetails title="未参与原因与技术字段" fields={row.auditFields} />
        </article>)}
      </div>
    </section>}
    <AuditDetails title="排名规则与原始状态（技术审计）" fields={summary.auditFields} />
  </section>
}

function RankingResultCard({ row, onOpenDetail }: { row: RankingRowViewModel; onOpenDetail?: (repositoryId: string) => void }) {
  const citationCount = row.reasons.reduce((total, reason) => total + reason.citations.length, 0)
  const passedUnits = row.hardUnits.filter(unit => unit.result.raw === 'pass').length
  const metricItems = [
    { label: '硬约束检查', value: row.hardUnits.length, hint: row.hardUnits.length > 0 ? `${passedUnits} 项通过` : '没有必需或禁止项' },
    { label: '推荐理由', value: row.reasons.length },
    { label: '可核对引文', value: citationCount },
    ...(row.totalScore === null ? [] : [{ label: '综合得分', value: row.totalScore.toFixed(2), hint: '不覆盖硬约束' }]),
  ]
  return <article className="search-candidate display-repo-card ranking-result-card">
    <header className="display-repo-head">
      <div>
        <span className="display-repo-ref">第 {row.rank} 名 · {row.reference}</span>
        <h4>{row.htmlUrl ? <a href={row.htmlUrl} target="_blank" rel="noreferrer">{row.name}</a> : row.name}</h4>
      </div>
      <div className="display-repo-badges">
        <StatusBadge status={row.tier} />
        <StatusBadge status={row.recommendation} />
      </div>
    </header>
    <p>{row.reasonSummary ?? '报告未提供推荐理由文本；请以需求匹配结果与原始字段为准。'}</p>
    <MetricGrid items={metricItems} />
    <p className="muted">偏好需求：{row.preferredStatus ? `${row.preferredStatus.label} · ` : ''}{row.preferredText}</p>
    {row.coverageText && <p className="muted">{row.coverageText}</p>}
    {row.rankingExplanation && <p className="muted">{row.rankingExplanation}</p>}
    <details className="repo-requirements">
      <summary>推荐依据与证据（{row.reasons.length}）</summary>
      {row.reasons.length === 0 && <p className="muted">报告没有给出可展示的推荐理由；这不代表仓库不符合需求，请回到深度筛选核对需求匹配结果。</p>}
      {row.reasons.map(reason => <article className="ranking-reason" key={reason.key}>
        <p><strong>{reason.text}</strong></p>
        {reason.requirementRef && <p className="muted">{reason.requirementRef}{reason.requirementText ? ` · ${reason.requirementText}` : ''}</p>}
        {reason.citations.length > 0
          ? reason.citations.map(evidence => <div className="ranking-citation" key={evidence.key}>
            <EvidenceCard evidence={evidence} />
            <AuditDetails title="引文技术字段" fields={citationAuditFields(evidence)} />
          </div>)
          : <p className="muted">该理由未附可跳转的 README 引文；请在深度筛选中核对原始证据，不能仅凭结论文本视为满足。</p>}
      </article>)}
      {row.limitations.length > 0 && <section className="repo-findings">
        <h5>限制与注意事项</h5>
        <ul>{row.limitations.map((limitation, index) => <li key={`${row.repositoryId}-limitation-${index}`}>{limitation}</li>)}</ul>
      </section>}
      <section className="repo-findings">
        <h5>硬约束检查（{row.hardUnits.length}）</h5>
        {row.hardUnits.length === 0
          ? <p className="muted">该仓库没有可核查的必需或禁止项；这不等于所有需求都已满足。</p>
          : <ul>{row.hardUnits.map(unit => <li key={unit.key}>
            {unit.kind.label} · {unitLabel(unit)} · <StatusBadge status={unit.result} />
          </li>)}</ul>}
      </section>
    </details>
    {row.scoreRows.length > 0 && <details className="search-source">
      <summary>评分明细（技术排名，默认折叠）</summary>
      <table className="ranking-breakdown">
        <thead><tr><th>维度</th><th>权重</th><th>得分</th><th>支持 / 反证 / 不足</th></tr></thead>
        <tbody>{row.scoreRows.map(dimension => <tr key={dimension.key}>
          <td>{dimension.label}{dimension.missingData ? '（元数据缺失）' : ''}</td>
          <td>{dimension.weightText}</td>
          <td>{dimension.scoreText}</td>
          <td>{dimension.countsText}</td>
        </tr>)}</tbody>
      </table>
      {row.scoreRows.some(dimension => dimension.detail) && <p className="muted">{row.scoreRows.filter(dimension => dimension.detail).map(dimension => `${dimension.label}：${dimension.detail}`).join('；')}</p>}
    </details>}
    {onOpenDetail && <div className="ranking-actions">
      <button type="button" className="secondary" onClick={() => onOpenDetail(row.repositoryId)}>查看项目详情</button>
    </div>}
    <AuditDetails title="排名技术字段与原始状态" fields={row.auditFields} />
  </article>
}

function runAuditFields(run: SearchRun, revisionId: string): { label: string; value: string }[] {
  return [
    { label: '绑定版本 ID', value: revisionId },
    { label: '运行 ID', value: run.run_id },
    { label: '任务 ID', value: run.task_id },
    { label: '确认版本 ID', value: run.confirmed_revision_id },
    { label: '状态（原始）', value: run.status },
    { label: '流水线状态（原始）', value: run.pipeline_status },
    { label: '规划 / 搜索状态（原始）', value: `${run.plan_status ?? '未提供'} / ${run.search_status ?? '未提供'}` },
    { label: '证据发布状态（原始）', value: run.evidence_status ?? '未提供' },
    { label: '检索状态（原始）', value: run.retrieval_status ?? '未提供' },
    { label: '检索错误代码', value: run.retrieval_error_code ?? '无' },
    { label: '外部派发关闭', value: run.dispatch_closed === undefined ? '未提供' : run.dispatch_closed ? 'true' : 'false' },
    { label: '错误代码', value: [run.error_code, run.pipeline_error_code, ...(run.search_stop_reasons ?? [])].filter(Boolean).join('、') || '无' },
    { label: '运行故障', value: run.operation_failures?.map(failure => `${failure.operation} · ${failure.status} · ${failure.error_code}`).join('；') || '无' },
    { label: '候选采集', value: run.candidates?.map(candidate => `${candidate.repository_id} · ${candidate.collection_status ?? '未提供'}`).join('；') || '无' },
  ]
}

type DetailTab = 'overview' | 'requirements' | 'evidence' | 'analysis' | 'audit'
const detailTabs: { key: DetailTab; label: string }[] = [
  { key: 'overview', label: '综合分析' },
  { key: 'requirements', label: '需求匹配' },
  { key: 'evidence', label: '证据核查' },
  { key: 'analysis', label: '技术分析' },
  { key: 'audit', label: '技术审计' },
]

function ProjectDetailWorkspace({ repository, ranking, screening, rankingAuditFields, onBack }: {
  repository: RepositoryReviewViewModel
  ranking: RankingRowViewModel
  screening: ScreeningRepositoryViewModel | null
  rankingAuditFields: { label: string; value: string }[]
  onBack: () => void
}) {
  const [tab, setTab] = useState<DetailTab>('overview')
  const [focusRequirement, setFocusRequirement] = useState<string | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const selectedTab = detailTabs.find(item => item.key === tab) ?? detailTabs[0]
  const citationCount = repository.requirements.reduce((total, item) => total + item.citationCount, 0)
  const semanticChunks = screening?.chunks.filter(chunk => chunk.methods.includes('dense')).length ?? 0
  const lexicalChunks = screening?.chunks.filter(chunk => chunk.methods.includes('lexical')).length ?? 0
  function selectTab(next: DetailTab) {
    setTab(next)
    setFocusRequirement(null)
  }
  function openEvidence(requirementId: string) {
    setFocusRequirement(requirementId)
    setTab('evidence')
  }
  useEffect(() => {
    const scroller = scrollRef.current
    if (!scroller) return
    if (tab === 'evidence' && focusRequirement) {
      const target = document.getElementById(`detail-evidence-${focusRequirement}`)
      if (target) {
        target.scrollIntoView({ block: 'start', behavior: 'smooth' })
        return
      }
    }
    scroller.scrollTo({ top: 0 })
  }, [tab, focusRequirement])
  return <div className="search-workspace result-workspace detail-workspace">
    <aside className="search-overview result-overview detail-overview card" aria-label="项目详情概览">
      <header className="overview-head">
        <span className="section-index">06 / PROJECT DETAIL</span>
        <h2 id="detail-title">项目详情</h2>
        <p className="overview-subtitle">从最终结果打开；只读取已发布深度核查报告与已保存 README 证据，不重新访问 GitHub 或调用模型。</p>
        <div className="overview-badges">
          <StatusBadge status={ranking.recommendation} />
          <StatusBadge status={ranking.tier} />
        </div>
      </header>
      <dl className="search-overview-status">
        <div><dt>仓库</dt><dd>{repository.displayName}</dd></div>
        <div><dt>语言 / Stars</dt><dd>{screening?.language || '未提供'}{screening && screening.stars !== null ? ` · ${screening.stars} stars` : ''}</dd></div>
        <div><dt>需求条目</dt><dd>{repository.requirementCount}</dd></div>
        <div><dt>支持 / 冲突 / 信息不足</dt><dd>{repository.supportCount} / {repository.conflictCount} / {repository.insufficientCount}</dd></div>
        <div><dt>可核对引文</dt><dd>{citationCount} 条</dd></div>
        <div><dt>检索片段</dt><dd>{screening ? `${screening.chunks.length} 个` : '未提供'}</dd></div>
        <div><dt>最终名次</dt><dd>第 {ranking.rank} 名</dd></div>
      </dl>
      <button type="button" className="secondary detail-back" onClick={onBack}>← 返回最终结果</button>
      <div className="search-budget">只读项目详情 · 不发起新请求 · 无证据不等于不满足</div>
      <div className="overview-feedback" aria-live="polite">
        <p className="muted">五个页签分别呈现结论、需求匹配、证据原文、技术分析与默认折叠的技术审计；需求页可直接跳到对应证据。</p>
      </div>
    </aside>
    <div className="search-editor detail-editor">
      <div className="search-editor-scroll detail-editor-scroll" ref={scrollRef}>
        <section className="search-section card detail-card" aria-labelledby="detail-name">
          <header className="search-section-head">
            <span className="group-index">PROJECT</span>
            <h3 id="detail-name">{repository.displayName}</h3>
            {ranking.htmlUrl && <a className="detail-repo-link" href={ranking.htmlUrl} target="_blank" rel="noreferrer">GitHub 仓库 ↗</a>}
          </header>
          <div className="detail-tabbar" role="tablist" aria-label="项目详情页签">
            {detailTabs.map(item => <button
              key={item.key}
              type="button"
              role="tab"
              aria-selected={tab === item.key}
              className={tab === item.key ? 'active' : ''}
              onClick={() => selectTab(item.key)}
            >{item.label}</button>)}
          </div>
          <div className="detail-tab-panel" role="tabpanel" aria-label={selectedTab.label}>
            {tab === 'overview' && <>
              <SummaryCard
                eyebrow="项目综合分析"
                title={`第 ${ranking.rank} 名 · ${ranking.recommendation.label}`}
                statuses={[ranking.tier, repository.coverage]}
                description={ranking.reasonSummary ?? '报告未提供推荐理由文本；这不代表项目不符合需求。'}
              />
              <MetricGrid items={[
                { label: '需求条目', value: repository.requirementCount },
                { label: '有证据支持', value: repository.supportCount },
                { label: '证据冲突', value: repository.conflictCount, hint: '需人工确认' },
                { label: '信息不足', value: repository.insufficientCount, hint: '不等于不满足' },
                { label: '可核对引文', value: citationCount },
              ]} />
              <p className="stage-boundary">推荐结论来自已发布报告的确定性规则；“信息不足”与“无引文”都不等于需求不满足，仍需查看逐需求证据。</p>
              {repository.strengths.length > 0 && <section className="repo-findings">
                <h5>优势与支持证据</h5>
                <ul>{repository.strengths.map((item, index) => <li key={`${repository.repositoryId}-detail-strength-${index}`}>{item}</li>)}</ul>
              </section>}
              {repository.gaps.length > 0 && <section className="repo-findings">
                <h5>待核查 / 缺口</h5>
                <ul>{repository.gaps.map((item, index) => <li key={`${repository.repositoryId}-detail-gap-${index}`}>{item}</li>)}</ul>
              </section>}
              {repository.gaps.length === 0 && repository.requirementCount > 0 && <p className="hint-text">没有待核查的必需或禁止项；支持结论仍建议抽查引文。</p>}
              <p className="hint-text">建议下一步：{repository.nextStep}</p>
              <section className="repo-findings">
                <h5>排名与偏好证据</h5>
                <p className="muted">偏好需求：{ranking.preferredStatus ? `${ranking.preferredStatus.label} · ` : ''}{ranking.preferredText}</p>
                {ranking.coverageText && <p className="muted">{ranking.coverageText}</p>}
                {ranking.rankingExplanation && <p className="muted">{ranking.rankingExplanation}</p>}
              </section>
              <div className="detail-actions">
                <button type="button" className="secondary" onClick={() => selectTab('requirements')}>查看逐需求匹配</button>
                <button type="button" className="secondary" onClick={() => selectTab('evidence')}>查看证据原文</button>
              </div>
            </>}

            {tab === 'requirements' && <>
              <p className="stage-boundary">逐需求结果来自已发布报告；“信息不足”不是“不满足”。点击“查看证据”会跳到证据核查页中的对应条目。</p>
              {repository.requirements.length === 0
                ? <EmptyState title="没有逐需求结果" description="该仓库没有可展示的需求匹配；这不代表需求已满足，请查看技术审计中的原始状态。" />
                : <div className="detail-requirement-list">
                  {repository.requirements.map(item => <article className="detail-requirement" key={item.key}>
                    <header>
                      <div><span className="display-repo-ref">{item.ref}</span><h4>{item.text}</h4></div>
                      <StatusBadge status={item.verdict} />
                    </header>
                    <p className="muted">{item.field.label} · {item.modality.label} · 硬约束 {item.hardCheck.label} · 引文 {item.citationCount} 条</p>
                    {item.modelOmitted && <p className="hint-text">模型遗漏该需求，已按确定性规则补为信息不足。</p>}
                    {item.rejectedCount > 0 && <p className="warning">有 {item.rejectedCount} 条引文未通过校验，已从结论中排除；原始原因见技术审计。</p>}
                    <div className="detail-actions">
                      <button type="button" className="secondary" onClick={() => openEvidence(item.id)}>{item.citationCount > 0 ? `查看证据（${item.citationCount}）` : '查看证据状态'}</button>
                    </div>
                  </article>)}
                </div>}
            </>}

            {tab === 'evidence' && <>
              <p className="stage-boundary">以下引文均来自已保存的 README，行号可回到原文核对；引文技术字段默认折叠。没有通过校验的引文不会被当作满足证据。</p>
              {repository.requirements.length === 0
                ? <EmptyState title="没有逐需求证据" description="报告没有提供可展示的逐需求结果；未知或无证据不代表项目不符合需求。" />
                : repository.requirements.map(item => <section
                  className={`detail-evidence-group${focusRequirement === item.id ? ' focus' : ''}`}
                  id={`detail-evidence-${item.id}`}
                  key={item.key}
                  tabIndex={-1}
                >
                  <header>
                    <div><span className="display-repo-ref">{item.ref}</span><h4>{item.text}</h4></div>
                    <StatusBadge status={item.verdict} />
                  </header>
                  <p className="muted">{item.modality.label} · 引文 {item.citationCount} 条 · 未通过校验 {item.rejectedCount} 条</p>
                  {item.citations.length > 0
                    ? item.citations.map(evidence => <div className="ranking-citation" key={evidence.key}>
                      <EvidenceCard evidence={evidence} />
                      <AuditDetails title="引文技术字段" fields={citationAuditFields(evidence)} />
                    </div>)
                    : <p className="muted">没有通过校验的 README 引文；“信息不足”不等于项目不具备该功能。</p>}
                </section>)}
            </>}

            {tab === 'analysis' && <>
              <p className="stage-boundary">技术分析只基于已保存 README 与确定性排名结果，不代表源码审计、代码运行验证或部署可用性。</p>
              <MetricGrid items={[
                { label: '综合得分', value: ranking.totalScore === null ? '—' : ranking.totalScore.toFixed(2), hint: ranking.totalScore === null ? '当前规则不产生百分制得分' : '不覆盖硬约束层级' },
                { label: '证据覆盖率', value: ranking.coveragePercent === null ? '—' : `${ranking.coveragePercent}%` },
                { label: '直接引文', value: citationCount },
                { label: '检索片段', value: screening ? screening.chunks.length : '未提供' },
              ]} />
              <section className="repo-findings">
                <h5>硬约束检查（{ranking.hardUnits.length}）</h5>
                {ranking.hardUnits.length === 0
                  ? <p className="muted">该仓库没有可核查的必需或禁止项；这不等于所有需求都已满足。</p>
                  : <ul>{ranking.hardUnits.map(unit => <li key={unit.key}>
                    {unit.kind.label} · {unitLabel(unit)} · <StatusBadge status={unit.result} />
                  </li>)}</ul>}
              </section>
              <section className="repo-findings">
                <h5>偏好需求与覆盖率</h5>
                <p className="muted">偏好需求：{ranking.preferredStatus ? `${ranking.preferredStatus.label} · ` : ''}{ranking.preferredText}</p>
                {ranking.coverageText ? <p className="muted">{ranking.coverageText}</p> : <p className="muted">报告未提供证据覆盖率；这不代表证据缺失。</p>}
              </section>
              {ranking.scoreRows.length > 0
                ? <details className="search-source" open>
                  <summary>评分明细（技术排名）</summary>
                  <table className="ranking-breakdown">
                    <thead><tr><th>维度</th><th>权重</th><th>得分</th><th>支持 / 反证 / 不足</th></tr></thead>
                    <tbody>{ranking.scoreRows.map(dimension => <tr key={dimension.key}>
                      <td>{dimension.label}{dimension.missingData ? '（元数据缺失）' : ''}</td>
                      <td>{dimension.weightText}</td>
                      <td>{dimension.scoreText}</td>
                      <td>{dimension.countsText}</td>
                    </tr>)}</tbody>
                  </table>
                </details>
                : <p className="hint-text">当前排名规则未提供百分制评分明细；这属于规则设计，不是数据缺失。</p>}
              <section className="repo-findings">
                <h5>检索证据分布</h5>
                {screening
                  ? <>
                    <MetricGrid items={[
                      { label: '检索片段', value: screening.chunks.length },
                      { label: '语义检索命中', value: semanticChunks },
                      { label: '关键词检索命中', value: lexicalChunks },
                      { label: '关联需求', value: new Set(screening.chunks.flatMap(chunk => chunk.associations)).size },
                    ]} />
                    <p className="muted">检索片段只表示与需求相关，不能直接视为满足；片段原文可在证据核查中查看。</p>
                  </>
                  : <p className="muted">没有已保存的检索片段；这不代表项目不符合需求。</p>}
              </section>
              <section className="repo-findings">
                <h5>README 快照</h5>
                <p className="muted">核查覆盖：<StatusBadge status={repository.coverage} /> · 仓库状态：<StatusBadge status={repository.status} /></p>
                <p>{repository.readmePath ? <a href={repository.readmePath} target="_blank" rel="noreferrer">查看已保存 README 原文</a> : '报告未提供 README 路径'}</p>
                <p className="muted">所有结论均来自该快照，不代表仓库当前最新版本。</p>
              </section>
            </>}

            {tab === 'audit' && <>
              <p className="stage-boundary">以下为项目详情的原始字段、快照标识与完整性信息，默认折叠、可复制；普通视图不会重复展示这些字段。</p>
              <AuditDetails title="仓库核查技术字段" fields={repository.auditFields} />
              <AuditDetails title="排名技术字段与原始状态" fields={ranking.auditFields} />
              {rankingAuditFields.length > 0 && <AuditDetails title="排名规则与原始状态" fields={rankingAuditFields} />}
              {screening && <AuditDetails title="检索快照技术字段" fields={screening.auditFields} />}
              <section className="repo-findings">
                <h5>逐需求技术字段</h5>
                {repository.requirements.length === 0
                  ? <p className="muted">没有逐需求原始字段。</p>
                  : repository.requirements.map(item => <AuditDetails key={item.key} title={`${item.ref} 技术字段`} fields={item.auditFields} />)}
              </section>
              <p className="muted">报告与授权的全局技术审计见最终结果页「报告与授权溯源」。</p>
            </>}
          </div>
        </section>
      </div>
    </div>
  </div>
}

function unitLabel(unit: { sourceLabel: string; memberLabels: string[] }) {
  const members = unit.memberLabels.join('、')
  return members && members !== unit.sourceLabel ? `${unit.sourceLabel}（${members}）` : unit.sourceLabel
}

function citationAuditFields(evidence: EvidenceViewModel): { label: string; value: string }[] {
  return [
    { label: '需求项 ID', value: evidence.requirementId || '未提供' },
    { label: 'Chunk ID', value: evidence.chunkId },
    { label: '行区间', value: `${evidence.lineStart}–${evidence.lineEnd}` },
    { label: 'UTF-8 字节', value: `[${evidence.byteStart}, ${evidence.byteEnd})` },
    { label: 'README 路径', value: evidence.readmePath || '未提供' },
  ]
}

function chunkAuditFields(chunk: ScreeningChunkViewModel): { label: string; value: string }[] {
  return [
    { label: 'Chunk ID', value: chunk.chunkId },
    { label: '行区间', value: `${chunk.lineStart}–${chunk.lineEnd}` },
    { label: 'UTF-8 字节区间', value: `[${chunk.byteStart}, ${chunk.byteEnd})` },
    { label: '检索方式', value: chunk.methods.join(' + ') || '未提供' },
    { label: '余弦距离', value: chunk.distance === null ? '未提供' : chunk.distance.toFixed(3) },
    { label: 'RRF 分数', value: chunk.rrf.toFixed(4) },
    { label: 'README 路径', value: chunk.readmePath ?? '未提供' },
  ]
}

function retrievalAuditFields(bundle: RetrievalBundle): { label: string; value: string }[] {
  return [
    { label: 'Bundle 版本', value: bundle.version },
    { label: 'Schema 版本', value: bundle.schema_version === undefined ? '未提供' : String(bundle.schema_version) },
    { label: '任务 ID', value: bundle.task_id },
    { label: '运行 ID', value: bundle.run_id },
    { label: '确认版本 ID', value: bundle.confirmed_revision_id },
    { label: '状态（原始）', value: bundle.status },
    { label: '生成时间（原始）', value: bundle.created_at },
    { label: '模型', value: bundle.model },
    { label: '向量维度', value: String(bundle.dimension) },
    { label: '空结果原因', value: bundle.empty_reason ?? '无' },
    { label: '查询用量', value: bundle.query_usage ? `tokens ${bundle.query_usage.tokens ?? '未提供'} · attempts ${bundle.query_usage.attempts ?? '未提供'}` : '未提供' },
  ]
}

function ScreeningRepositoryCard({ repository, evidencePublished }: { repository: ScreeningRepositoryViewModel; evidencePublished: boolean }) {
  const name = repository.name === '未命名仓库' ? repository.reference : repository.name
  return <article className="search-candidate display-repo-card">
    <header className="display-repo-head">
      <div>
        <span className="display-repo-ref">{repository.reference}</span>
        <h4>{repository.htmlUrl ? <a href={repository.htmlUrl} target="_blank" rel="noreferrer">{name}</a> : name}</h4>
        {repository.htmlUrl && <p className="display-repo-url">{repository.htmlUrl}</p>}
      </div>
      <div className="display-repo-badges">
        {repository.availability && <StatusBadge status={repository.availability} />}
        <StatusBadge status={repository.retrievalStatus} />
        {repository.coverage && <StatusBadge status={repository.coverage} />}
      </div>
    </header>
    {repository.description && <p>{repository.description}</p>}
    <p className="muted">{[repository.language, repository.stars !== null ? `${repository.stars} Stars` : ''].filter(Boolean).join(' · ') || '仓库元数据未提供'}</p>
    <MetricGrid items={[
      { label: '需求条目', value: evidencePublished ? repository.requirements.length : '—' },
      { label: '发现相关提及', value: evidencePublished ? repository.mentioned : '—', hint: '不是满足结论' },
      { label: '尚未核实', value: evidencePublished ? repository.unconfirmed : '—', hint: '不代表不满足' },
      { label: '检索片段', value: repository.retrievalChunkCount },
    ]} />
    <details className="repo-requirements">
      <summary><span>查看初筛依据</span><span className="repo-requirements-count">需求 {evidencePublished ? repository.requirements.length : '—'} · 提及 {evidencePublished ? repository.mentioned : '—'}</span></summary>
      {!evidencePublished && <p className="muted">逐需求证据尚未发布；这里的空状态不代表需求不满足。</p>}
      {evidencePublished && repository.requirements.length === 0 && <p className="muted">该仓库没有逐需求结果；不代表需求不满足。</p>}
      {repository.requirements.map(requirement => <details className="requirement-evidence" key={requirement.key}>
        <summary><span className="requirement-summary-text">{requirement.ref} · {requirement.text}</span><StatusBadge status={requirement.status} /></summary>
        <p className="muted">{requirement.modality.label} · {requirement.status.label} · 引文 {requirement.quotes.length} 条</p>
        {requirement.quotes.length > 0
          ? requirement.quotes.map(quote => <EvidenceCard key={quote.key} evidence={quote} />)
          : <p className="muted">没有精确 README 引文，尚未确认；这不代表需求不满足。</p>}
      </details>)}
    </details>
    {evidencePublished && <p className="hint-text">“发现相关提及”只表示 README 中出现了相关内容，不能覆盖否定语义、禁止项或 ALL / ANY 约束。</p>}
    <AuditDetails title="仓库快照与技术字段" fields={repository.auditFields} />
  </article>
}

function ScreeningRetrievalView({ repositories, bundle }: { repositories: ScreeningRepositoryViewModel[]; bundle: RetrievalBundle }) {
  const withChunks = repositories.filter(repository => repository.chunks.length > 0)
  return <div className="retrieval-report">
    {bundle.notice && <p className="stage-boundary">{bundle.notice}</p>}
    <p className="hint-text">召回只表示片段与需求可能相关（相关性待核查），不是“已找到满足证据”；禁止项未提及不代表符合。</p>
    {bundle.empty_reason && <p className="muted">没有可检索的已保存快照（{bundle.empty_reason}）。</p>}
    {withChunks.length === 0 && !bundle.empty_reason && <p className="muted">本次运行没有可展示的检索片段。</p>}
    {withChunks.map(repository => {
      const name = repository.name === '未命名仓库' ? repository.reference : repository.name
      return <article className="search-candidate display-repo-card" key={repository.repositoryId}>
        <header className="display-repo-head">
          <div>
            <span className="display-repo-ref">{repository.reference}</span>
            <h4>{repository.htmlUrl ? <a href={repository.htmlUrl} target="_blank" rel="noreferrer">{name}</a> : name}</h4>
          </div>
          <div className="display-repo-badges">
            <StatusBadge status={repository.retrievalStatus} />
            {repository.coverage && <StatusBadge status={repository.coverage} />}
          </div>
        </header>
        <MetricGrid items={[
          { label: '检索片段', value: repository.chunks.length },
          { label: '关联需求', value: new Set(repository.chunks.flatMap(chunk => chunk.associations)).size },
        ]} />
        {repository.chunks.map(chunk => <details className="requirement-evidence" key={chunk.key}>
          <summary><span className="requirement-summary-text">行 {chunk.lineStart}–{chunk.lineEnd} · 关联 {chunk.associations.length} 条需求</span></summary>
          <p className="muted">关联需求：{chunk.associations.join('；')}</p>
          {chunk.readmePath && <p><a href={chunk.readmePath} target="_blank" rel="noreferrer">查看 README 原文</a></p>}
          <pre className="source-quote">{chunk.quote}</pre>
          <AuditDetails title="片段技术字段" fields={chunkAuditFields(chunk)} />
        </details>)}
        <AuditDetails title="检索快照技术字段" fields={repository.auditFields} />
      </article>
    })}
    <AuditDetails title="检索 Bundle 技术字段" fields={retrievalAuditFields(bundle)} />
  </div>
}

function reviewAuditFields(review: DeepReviewOperation): { label: string; value: string }[] {
  return [
    { label: 'Operation ID', value: review.operation_id },
    { label: '核查状态（原始）', value: review.status },
    { label: '持久化状态（原始）', value: review.persisted_status },
    { label: '需求修订 ID', value: review.revision_id },
    { label: '修订哈希', value: review.revision_hash },
    { label: 'Bundle 哈希', value: review.bundle_hash },
    { label: '模型', value: review.model },
    { label: 'Prompt 版本', value: review.prompt_version },
    { label: '授权时间（原始）', value: review.authorized_at },
    { label: 'Deadline（原始）', value: review.deadline },
    { label: '报告对象键', value: review.report_key ?? '尚未发布' },
    { label: '报告 SHA-256', value: review.report_hash ?? '尚未发布' },
    { label: '错误代码', value: review.error_code ?? '无' },
  ]
}

function reportAuditFields(report: ReviewReport): { label: string; value: string }[] {
  const summary = report.summary
  return [
    { label: 'Schema 版本', value: String(report.schema_version) },
    { label: '报告版本', value: report.version },
    { label: 'Owner', value: report.owner },
    { label: '任务 ID', value: report.task_id },
    { label: '运行 ID', value: report.run_id },
    { label: '确认版本 ID', value: report.confirmed_revision_id },
    { label: 'Operation ID', value: report.operation_id },
    { label: '修订哈希', value: report.revision_hash },
    { label: 'Plan 哈希', value: report.plan_hash },
    { label: 'Bundle 哈希', value: report.bundle_hash },
    { label: '模型', value: report.model },
    { label: 'Prompt 版本', value: report.prompt_version },
    { label: '报告状态（原始）', value: report.status },
    { label: '生成时间（原始）', value: report.created_at },
    { label: '仓库统计', value: `总计 ${summary.repositories_total} · 完成 ${summary.repositories_completed} · 失败 ${summary.repositories_failed} · 未知 ${summary.repositories_unknown} · 跳过 ${summary.repositories_skipped}` },
    { label: '需求与判定统计', value: `需求条目 ${summary.requirements_total} · 支持 ${summary.supports} · 冲突 ${summary.contradicts} · 信息不足 ${summary.insufficient} · 模型遗漏 ${summary.model_omitted} · 未知项 ${summary.unknown_items} · 重复项 ${summary.duplicates}` },
    { label: '用量统计', value: `调用 ${summary.usage.calls} 次（未知用量 ${summary.usage.unknown_calls} 次）· 输入 ${summary.usage.prompt_tokens} · 输出 ${summary.usage.completion_tokens} · 合计 ${summary.usage.total_tokens}` },
    { label: '报告提示数', value: String(report.warnings.length) },
    { label: '排名规则', value: report.ranking ? `${report.ranking.ranking_config_version}${report.ranking.scored_at ? ` · ${report.ranking.scored_at}` : ''}` : '未生成' },
  ]
}

function deepCheckAuditFields(deep: NonNullable<SearchRun['deep_check']>): { label: string; value: string }[] {
  return [
    { label: '状态（原始）', value: deep.status },
    { label: '已发布', value: deep.published ? 'true' : 'false' },
    { label: '完整性阻断', value: deep.integrity_blocked ? 'true' : 'false' },
    { label: '错误代码', value: deep.error_code ?? '无' },
  ]
}

function DeepReportView({ report, names = new Map() }: { report: DeepReport; names?: Map<string, string | null> }) {
  const name = (repositoryId: string) => names.get(repositoryId) ?? '未命名仓库'
  return <div className="deep-report">
    <p className="warning">{report.notice}</p>
    {report.anomalies.length > 0 && <p className="warning">模型输出异常：{report.anomalies.map(anomaly => `${name(anomaly.repository_id)} · ${anomaly.message}`).join('；')}</p>}
    {report.candidates.length === 0 && <p>已发布报告没有可核对的候选。</p>}
    {report.candidates.map(candidate => <article className="search-candidate" key={candidate.repository_id}>
      <h4>第 {candidate.rank + 1} 名 · {name(candidate.repository_id)} · {candidate.recommendation.label}</h4>
      <p>{candidate.summary}</p>
      <p className="muted">{candidate.recommendation.reason}（{candidate.availability_label} · {candidate.input_coverage_label} · 输入 {candidate.input_bytes} 字节）</p>
      {candidate.missing_features.length > 0 && <section className="repo-findings">
        <h5>缺失功能清单</h5>
        <ul>{candidate.missing_features.map(finding => <li key={finding.requirement_item_id}>{finding.text} · {statusOf('modality', finding.modality).label} · {finding.reason}</li>)}</ul>
      </section>}
      {candidate.risk_notes.length > 0 && <section className="repo-findings">
        <h5>风险提示</h5>
        <ul>{candidate.risk_notes.map(finding => <li key={finding.requirement_item_id}>{finding.text} · {statusOf('modality', finding.modality).label} · {finding.reason}</li>)}</ul>
      </section>}
      <details className="repo-requirements">
        <summary>逐需求判定（{candidate.items.length}）</summary>
        {candidate.items.map(item => <details className="requirement-evidence" key={item.requirement_item_id}>
          <summary><span>{item.text}</span><StatusBadge status={statusOf('verdict', item.verdict)} /></summary>
          <p className="muted">依据：{item.basis_label} · 逻辑组 {item.logic_group_id ?? '无'} · 引文 {item.citations.length} 条</p>
          {item.rejections.length > 0 && <p className="warning">引文剔除：{item.rejections.join('、')}（越界行号不计入结论）。</p>}
          {item.citations.length > 0
            ? item.citations.map((quote, index) => <div className="evidence-card" key={`${item.requirement_item_id}-${index}`}>
              <p>行 {quote.line_start}–{quote.line_end} · UTF-8 字节 [{quote.byte_start}, {quote.byte_end})</p>
              <pre className="source-quote">{quote.text}</pre>
            </div>)
            : <p className="muted">没有通过复核的引文，结论仅由确定性规则给出。</p>}
          <AuditDetails title="需求技术字段" fields={[
            { label: '需求 ID', value: item.requirement_item_id },
            { label: '模态（原始）', value: item.modality },
            { label: '操作符（原始）', value: item.operator },
            { label: '逻辑组', value: item.logic_group_id ?? '无' },
            { label: '判定（原始）', value: item.verdict },
            { label: '判定标签', value: item.verdict_label },
            { label: '依据（原始）', value: item.basis },
            { label: '依据标签', value: item.basis_label },
            { label: '引文字节区间', value: item.citations.map(citation => `[${citation.byte_start}, ${citation.byte_end})`).join('、') || '无' },
            { label: '引文剔除', value: item.rejections.join('、') || '无' },
          ]} />
        </details>)}
      </details>
      <AuditDetails title="仓库与来源技术审计" fields={[
        { label: '仓库 ID', value: candidate.repository_id },
        { label: '推荐代码（原始）', value: candidate.recommendation.code },
        { label: '推荐原因代码', value: candidate.recommendation.reason_code },
        { label: '可用性（原始）', value: candidate.availability },
        { label: '可用性标签', value: candidate.availability_label },
        { label: '输入覆盖（原始）', value: candidate.input_coverage },
        { label: '输入覆盖标签', value: candidate.input_coverage_label },
        { label: '输入字节', value: String(candidate.input_bytes) },
        { label: '快照 ID', value: candidate.source?.snapshot_id ?? '无已保存快照' },
        { label: 'Commit SHA', value: candidate.source?.commit_sha ?? '未提供' },
        { label: 'Blob SHA', value: candidate.source?.blob_sha ?? '未提供' },
        { label: 'README 路径', value: candidate.source?.readme_path ?? '未提供' },
        { label: '内容哈希', value: candidate.source?.content_hash ?? '未提供' },
      ]} />
    </article>)}
  </div>
}

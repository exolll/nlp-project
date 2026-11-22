import { useEffect, useRef, useState } from 'react'
import { request } from './api'
import { deepPhase, deepPhaseLabel, readSearch, reserveDeep, reserveSearch, restoreDeep, restoreSearch, shouldPollDeep, shouldPollSearch, evidenceLabel } from './search'
import type { DeepAttempt, DeepReport, SearchAttempt, SearchEvidence, SearchRun } from './search'

export default function SearchPanel({ taskId, revisionId, enabled }: { taskId: string; revisionId: string; enabled: boolean }) {
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
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('未授权搜索；确认需求不会自动启动搜索。')
  const [deepNotice, setDeepNotice] = useState('深度核对需单独授权；未授权时不会调用模型。')
  const [stalled, setStalled] = useState(false)
  const scopeRef = useRef<AbortController | null>(null)
  const lock = useRef(false)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const polls = useRef(0)
  const active = (scope: AbortController) => scopeRef.current === scope && !scope.signal.aborted
  const cancelTimer = () => { clearTimeout(timer.current); timer.current = undefined }
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
      setError(result.evidenceError)
      setDeepError(result.deepError)
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
  useEffect(() => {
    const scope = new AbortController(); scopeRef.current = scope; lock.current = false
    if (attemptRef.current?.run_id) void refresh()
    return () => { scope.abort(); cancelTimer() }
    // Parent keys by task and immutable revision, so no requests or state cross tasks.
  }, [taskId, revisionId])
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
    if (!scope || !active(scope) || lock.current || !enabled || !saved?.run_id || deepStorageError || !canSend) return
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
  const deepResend = !!deepAttempt && (phase === 'not_started' || phase === 'interrupted' || (phase === 'running' && stalled))
  const deepActive = !busy && enabled && !!attempt?.run_id && !deepStorageError && snapshotsReady && phase !== 'ready' && phase !== 'failed' && phase !== 'blocked'
  const deepSendDisabled = !deepActive || (deepAttempt ? !deepResend : !deepConsent)
  return <section className="search-panel" aria-labelledby="search-title">
    <div className="card-title"><div><span className="section-index">04 / SEARCH & EVIDENCE</span><h2 id="search-title">候选搜索与 README 证据</h2></div><span className="badge">{run?.status ?? '等待授权'}</span></div>
    <p>绑定已确认版本：<code>{revisionId}</code>。只搜索这个已保存版本，不包含本地未保存编辑。</p>
    <div className="extraction-consent"><div><strong>明确授权一次真实外部调用</strong><p>点击下方按钮会向后端 POST 搜索运行，调用真实模型服务与 GitHub，可能产生费用并发送需求。测试必须拦截 API 使用模拟响应；刷新只执行 GET。</p><p>固定预算上限：模型 1 次、搜索 5 次（每次最多 5 条、1 页）、README 5 个候选；GitHub 共 15 次（含 commit / README 请求）。</p><label className="resolution-check"><input type="checkbox" checked={consent} disabled={busy || !!attempt?.run_id || !!storageError || !enabled} onChange={event => setConsent(event.target.checked)} />我同意将此已确认版本用于上述外部调用与预算</label></div></div>
    {!enabled && <p className="warning">请先保存并确认当前编辑，或退出编辑后选择已确认版本。搜索不会使用未保存内容。</p>}
    <div className="requirement-actions"><button className="primary" disabled={!enabled || !consent || busy || !!storageError || !!attempt?.run_id} onClick={() => void start()}>{busy && !run ? '正在提交…' : attempt ? '显式重试同一次搜索（沿用 UUID）' : '按此预算开始搜索'}</button><button className="secondary" disabled={busy || !attempt?.run_id} onClick={() => void refresh(true)}>刷新搜索与证据（仅 GET）</button></div>
    <div aria-live="polite"><p>{busy && <span className="spinner" />}{notice}</p>{storageError && <p className="warning" role="alert">{storageError}</p>}{error && <p className="error" role="alert">{error}</p>}{attempt && !attempt.run_id && <p className="warning">本次提交结果未知。没有运行 ID 时无法查询；仅允许按钮重发相同载荷和幂等键，不会另建请求。</p>}</div>
    {run && <><dl className="search-status"><dt>运行 ID</dt><dd>{run.run_id}</dd><dt>流水线</dt><dd>{run.pipeline_status}</dd><dt>规划 / 搜索 / 证据发布</dt><dd>{run.plan_status ?? '未提供'} / {run.search_status ?? '未提供'} / {run.evidence_status ?? 'not_ready'}</dd><dt>外部派发关闭</dt><dd>{run.dispatch_closed === undefined ? '未提供' : run.dispatch_closed ? '是' : '否'}</dd></dl>{run.search_status === 'partial' && <p className="warning">仅有部分采集结果；不丢弃已保存的 README 证据，也不把缺失视为不满足。</p>}{[run.error_code, run.pipeline_error_code, ...(run.search_stop_reasons ?? [])].filter(Boolean).map((code, i) => <p className="warning" key={i}>{code}</p>)}{run.operation_failures?.map((failure, i) => <p className="warning" key={i}>{failure.operation} · {failure.status} · {failure.error_code}</p>)}<h3>候选与 README 采集状态</h3>{!run.candidates?.length && <p className="muted">尚无候选记录，不代表需求不满足。</p>}<ul>{run.candidates?.map(candidate => <li key={candidate.repository_id}>仓库 ID {candidate.repository_id} · README：{candidate.collection_status ?? '未提供'}</li>)}</ul></>}
    <h3>逐需求证据核对</h3><p className="warning">字面提及不是满足结论；否定语义、禁止约束及 ALL / ANY 均需人工核对。未找到提及也不代表不满足。</p>
    {!evidence ? <p className="muted">证据报告尚未读取或尚未发布。候选采集完成不等于证据已发布；可稍后只读刷新。</p> : <><p>{evidence.warning}</p>{evidence.candidates.length === 0 && <p>已发布报告没有候选。</p>}{evidence.candidates.map(candidate => <article className="search-candidate" key={candidate.repository_id}><h4>仓库 ID {candidate.repository_id} · README {candidate.availability}</h4>{candidate.source ? <details><summary>精确快照来源</summary><dl>{Object.entries(candidate.source).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl></details> : <p>无已保存快照，无法确认。</p>}{evidence.requirement_items.map(requirement => {
      const item = candidate.items.find(value => value.requirement_item_id === requirement.requirement_item_id)
      return <details className="requirement-evidence" key={requirement.requirement_item_id}><summary>{requirement.text} · {evidenceLabel(item?.status ?? 'unconfirmed')}</summary><p>{requirement.requirement_item_id} · {requirement.modality} · {requirement.operator} · 逻辑组 {requirement.logic_group_id ?? '无'}</p>{item?.quotes.length ? item.quotes.map((quote, i) => <div key={i}><small>README 行 {quote.line_start}–{quote.line_end} · UTF-8 字节 [{quote.byte_start}, {quote.byte_end})</small><pre className="source-quote">{quote.text}</pre></div>) : <p>无精确引文，尚未确认。</p>}</details>
    })}</article>)}</>}
    <h3>深度核对与中文推荐</h3>
    <div className="extraction-consent"><div><strong>单独授权 1 次深度核对模型调用</strong><p>只读取本次运行已保存的 README 快照，不再访问 GitHub。模型逐条判定「有证据支持 / 有证据冲突 / 信息不足」并给出行号，后端逐条复核行号后确定性地生成中文推荐标签与缺失功能清单；引文越界一律剔除。刷新只执行 GET，失败与中断都不自动重试。</p><p>预算：每个搜索运行最多 1 次模型调用；一个任务最多 3 个运行，间接封顶 3 次。</p><label className="resolution-check"><input type="checkbox" checked={deepConsent} disabled={busy || !!deepStorageError || !!deepAttempt || !snapshotsReady || phase !== 'not_started' || !enabled} onChange={event => setDeepConsent(event.target.checked)} />我单独同意为本次已保存的搜索快照调用 1 次深度核对模型</label></div></div>
    <div className="requirement-actions"><button className="primary" disabled={deepSendDisabled} onClick={() => void startDeep()}>{busy && !deepAttempt ? '正在提交…' : deepResend ? '显式重发同一次授权（沿用 UUID）' : deepAttempt ? '本次运行已授权深度核对' : '开始深度核对（1 次模型调用）'}</button><button className="secondary" disabled={busy || !attempt?.run_id} onClick={() => void refresh(true)}>刷新深度核对（仅 GET）</button></div>
    <div aria-live="polite">{!attempt?.run_id && <p className="muted">请先完成一次搜索运行；深度核对只作用于已保存的搜索快照。</p>}{attempt?.run_id && !deepAttempt && !snapshotsReady && phase === 'not_started' && <p className="warning">候选派发尚未关闭或规划未发布，此时不会授权模型调用。</p>}{deepStorageError && <p className="warning" role="alert">{deepStorageError}</p>}{deepNotice && <p>{deepNotice}</p>}{deepError && <p className="error" role="alert">{deepError}</p>}</div>
    {run?.deep_check && <dl className="search-status"><dt>深度核对状态</dt><dd>{deepPhaseLabel(phase)}</dd><dt>错误代码</dt><dd>{run.deep_check.error_code ?? '无'}</dd></dl>}
    {!report && <p className="muted">尚无已发布的深度核对报告；报告未发布不代表需求不满足。</p>}
    {report && <DeepReportView report={report} />}
  </section>
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

export const searchBudget = { model_calls: 1, github_requests: 15, search_requests: 5, readme_candidates: 5, per_page: 5, max_pages: 1 } as const
export type SearchPayload = { revision_id: string; consent: true; budget: typeof searchBudget }
export type SearchAttempt = { key: string; payload: SearchPayload; run_id: string | null }
export interface SearchRun {
  run_id: string; task_id: string; confirmed_revision_id: string; status: string
  pipeline_status: string; pipeline_error_code?: string | null; error_code?: string | null
  search_status?: string; plan_status?: string; dispatch_closed?: boolean; evidence_status?: string
  search_stop_reasons?: string[]; operation_failures?: { operation: string; status: string; error_code: string }[]
  candidates?: { repository_id: string; collection_status: string | null }[]
  deep_check?: DeepState | null
}
export interface DeepState { status: string; published: boolean; integrity_blocked: boolean; error_code?: string | null }
export type DeepPayload = { revision_id: string; consent: true }
export type DeepAttempt = { key: string; payload: DeepPayload }
export type DeepPhase = 'not_started' | 'running' | 'ready' | 'failed' | 'interrupted' | 'blocked'
export interface DeepFinding { requirement_item_id: string; text: string; modality: string; reason: string }
export interface DeepReport {
  version: string; task_id: string; run_id: string; confirmed_revision_id: string; notice: string
  requirement_items: { requirement_item_id: string; text: string; modality: string; operator: string; logic_group_id: string | null }[]
  candidates: {
    repository_id: string; rank: number; availability: string; availability_label: string
    input_coverage: string; input_coverage_label: string; input_bytes: number
    source: { snapshot_id: string; commit_sha: string; readme_path: string; blob_sha: string; content_hash: string } | null
    recommendation: { code: string; label: string; reason_code: string; reason: string }
    summary: string; missing_features: DeepFinding[]; risk_notes: DeepFinding[]
    items: {
      requirement_item_id: string; text: string; modality: string; operator: string; logic_group_id: string | null
      verdict: string; basis: string; rejections: string[]; verdict_label: string; basis_label: string
      citations: { text: string; line_start: number; line_end: number; byte_start: number; byte_end: number }[]
    }[]
  }[]
  anomalies: { repository_id: string; code: string; message: string }[]
}
export interface EvidenceItem {
  requirement_item_id: string; text: string; modality: string; operator: string; logic_group_id: string | null
  status: string; quotes: { text: string; byte_start: number; byte_end: number; line_start: number; line_end: number }[]
}
export interface SearchEvidence {
  task_id: string; run_id: string; confirmed_revision_id: string; warning: string
  requirement_items: Omit<EvidenceItem, 'status' | 'quotes'>[]
  candidates: { repository_id: string; availability: string; source: { snapshot_id: string; commit_sha: string; readme_path: string; blob_sha: string; content_hash: string } | null; items: EvidenceItem[] }[]
}
export const searchPayload = (revision: string): SearchPayload => ({ revision_id: revision, consent: true, budget: { ...searchBudget } })
// Fail closed when storage is unavailable/corrupt: losing this key could charge twice.
export function restoreSearch(storage: Pick<Storage, 'getItem'>, key: string, revision: string): SearchAttempt | null {
  const raw = storage.getItem(key)
  if (!raw) return null
  const value = JSON.parse(raw) as SearchAttempt
  if (!value || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value.key) ||
      JSON.stringify(value.payload) !== JSON.stringify(searchPayload(revision)) ||
      !(value.run_id === null || typeof value.run_id === 'string')) throw new Error('搜索记录无效；为避免重复调用，已阻止提交。请恢复会话存储，不要删除记录重试。')
  return value
}
export function reserveSearch(storage: Pick<Storage, 'getItem' | 'setItem'>, key: string, revision: string): SearchAttempt {
  const previous = restoreSearch(storage, key, revision)
  if (previous) return previous
  const attempt = { key: crypto.randomUUID(), payload: searchPayload(revision), run_id: null }
  storage.setItem(key, JSON.stringify(attempt))
  return attempt
}
export function shouldPollSearch(run: SearchRun): boolean {
  if (['interrupted', 'failed', 'completed'].includes(run.pipeline_status)) return false
  // A manifest may be partial/completed while the pipeline still publishes saved evidence.
  if (['reserved', 'queued', 'running'].includes(run.pipeline_status)) return true
  return !['partial', 'completed', 'failed', 'interrupted'].includes(run.status)
}
export function evidenceLabel(status: string): string {
  return status === 'mention_found' ? '发现字面提及（mention_found），不代表满足' : '尚未确认（unconfirmed），不代表不满足'
}
export const deepPayload = (revision: string): DeepPayload => ({ revision_id: revision, consent: true })
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
// Same fail-closed rule as the search attempt: an unverifiable key could authorize a second paid call.
export function restoreDeep(storage: Pick<Storage, 'getItem'>, key: string, revision: string): DeepAttempt | null {
  const raw = storage.getItem(key)
  if (!raw) return null
  const value = JSON.parse(raw) as DeepAttempt
  if (!value || !UUID.test(value.key) || JSON.stringify(value.payload) !== JSON.stringify(deepPayload(revision))) {
    throw new Error('深度核对记录无效；为避免重复调用，已阻止提交。请恢复会话存储，不要删除记录重试。')
  }
  return value
}
export function reserveDeep(storage: Pick<Storage, 'getItem' | 'setItem'>, key: string, revision: string): DeepAttempt {
  const previous = restoreDeep(storage, key, revision)
  if (previous) return previous
  const attempt = { key: crypto.randomUUID(), payload: deepPayload(revision) }
  storage.setItem(key, JSON.stringify(attempt))
  return attempt
}
export function deepPhase(run: SearchRun): DeepPhase {
  const state = run.deep_check
  if (!state) return 'not_started'
  if (state.integrity_blocked) return 'blocked'
  if (state.published) return 'ready'
  if (state.status === 'failed') return 'failed'
  if (state.status === 'interrupted') return 'interrupted'
  if (state.status === 'not_started') return 'not_started'
  return 'running'
}
export const shouldPollDeep = (run: SearchRun): boolean => deepPhase(run) === 'running'
export function deepPhaseLabel(phase: DeepPhase): string {
  return { not_started: '未授权', running: '正在核对', ready: '已发布', failed: '未完成',
           interrupted: '已中断，需显式恢复', blocked: '完整性校验失败' }[phase]
}
export type SearchRequest = <T>(path: string, method?: string, signal?: AbortSignal, payload?: unknown, key?: string) => Promise<T>
export async function readSearch(request: SearchRequest, task: string, revision: string, runId: string, signal: AbortSignal) {
  const path = `/tasks/${encodeURIComponent(task)}/search-runs/${encodeURIComponent(runId)}`
  const run = await request<SearchRun>(path, 'GET', signal)
  if (signal.aborted) throw new Error('已停止读取')
  if (run.task_id !== task || run.confirmed_revision_id !== revision || run.run_id !== runId) throw new Error('搜索运行与当前任务或版本不匹配。')
  let evidence: SearchEvidence | null = null
  let evidenceError = ''
  if (run.evidence_status === 'published') {
    try {
      evidence = await request<SearchEvidence>(`${path}/evidence`, 'GET', signal)
      if (evidence.task_id !== task || evidence.confirmed_revision_id !== revision || evidence.run_id !== runId) throw new Error('证据与当前任务或版本不匹配。')
    } catch (error) {
      evidence = null
      evidenceError = error instanceof Error ? error.message : '证据尚未就绪'
    }
  }
  if (signal.aborted) throw new Error('已停止读取')
  let deep: DeepReport | null = null
  let deepError = ''
  if (deepPhase(run) === 'ready') {
    try {
      deep = await request<DeepReport>(`${path}/deep-check`, 'GET', signal)
      if (deep.task_id !== task || deep.confirmed_revision_id !== revision || deep.run_id !== runId) throw new Error('深度核对与当前任务或版本不匹配。')
    } catch (error) {
      deep = null
      deepError = error instanceof Error ? error.message : '深度核对结果尚未就绪'
    }
  }
  if (signal.aborted) throw new Error('已停止读取')
  return { run, evidence, evidenceError, deep, deepError }
}

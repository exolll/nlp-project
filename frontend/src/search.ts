export const searchBudget = { model_calls: 1, github_requests: 20, search_requests: 9, readme_candidates: 5, per_page: 5, max_pages: 1 } as const
export type SearchPayload = { revision_id: string; consent: true; budget: typeof searchBudget }
export type SearchAttempt = { key: string; payload: SearchPayload; run_id: string | null }
export interface SearchRun {
  run_id: string; task_id: string; confirmed_revision_id: string; status: string
  pipeline_status: string; pipeline_error_code?: string | null; error_code?: string | null
  search_status?: string; plan_status?: string; dispatch_closed?: boolean; evidence_status?: string
  search_stop_reasons?: string[]; operation_failures?: { operation: string; status: string; error_code: string }[]
  candidates?: { repository_id: string; collection_status: string | null }[]
  retrieval_status?: string; retrieval_error_code?: string | null
  plan_warnings?: { group: string; declared: string[]; used: string[] }[]
  deep_check?: DeepState | null
}
export interface RetrievalChunk {
  chunk_id: string; text: string; line_start: number; line_end: number
  byte_start: number; byte_end: number; methods: string[]
  ranks: Record<string, number>; dense_distance: number | null; rrf_score: number; score_direction: string
}
export interface RetrievalItem {
  requirement_item_id: string; text: string; status: string; relevance?: string
  empty_reason: string | null; chunks: RetrievalChunk[]
}
export interface RetrievalCandidate {
  repository_id: string; snapshot_id: string; coverage: string; readme_path: string | null
  commit_sha?: string | null; blob_sha?: string | null; content_hash?: string | null; collected_at?: string | null
  repository: { full_name: string | null; html_url: string | null; stars: number | null
    description: string | null; language: string | null } | null
  status: string; error_code: string | null; items: RetrievalItem[]
}
export interface RetrievalBundle {
  version: string; schema_version?: number; task_id: string; run_id: string; confirmed_revision_id: string; status: string
  warning: string; notice: string; created_at: string; model: string; dimension: number
  empty_reason: string | null; query_usage?: { tokens?: number; attempts?: number } | null
  requirements: { requirement_item_id: string; field: string; text: string; modality: string; operator: string; logic_group_id: string | null }[]
  candidates: RetrievalCandidate[]
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
export type DeepReviewPayload = { revision_id: string; consent: true }
export type DeepReviewStatus = 'running' | 'completed' | 'partial' | 'failed' | 'unknown'
export interface DeepReviewCitation {
  chunk_id: string; quote: string; line_start: number; line_end: number; byte_start: number; byte_end: number
}
export interface DeepReviewRequirement {
  requirement_item_id: string; field: string; text: string; modality: string; operator: string; logic_group_id: string | null
  verdict: 'supports' | 'contradicts' | 'insufficient'; reason_code: string; hard_constraint_check: string
  citations: DeepReviewCitation[]
  rejected_citations: { chunk_id: string; reason: string }[]; model_omitted: boolean
}
export interface DeepReviewRepository {
  repository_id: string; snapshot_id: string
  trace: { commit_sha: string; blob_sha: string; content_hash: string; readme_path: string; collected_at: string }
  coverage: string; status: 'completed' | 'failed' | 'unknown' | 'skipped'; error_code: string | null
  input: { chunk_ids: string[]; chunk_count: number; input_bytes_upper_bound: number
    budget: { max_chunks: number; max_input_bytes: number; max_output_tokens: number } }
  requirements: DeepReviewRequirement[]
  hard_constraint_summary: {
    required: { pass: number; fail: number; unknown: number; total: number }
    prohibited: { pass: number; fail: number; unknown: number; total: number }
    preferred: { evidence_found: number; unknown: number; total: number }
  }
  logic_group_results: { logic_group_id: string; operator: string; member_item_ids: string[]; result: string }[]
  usage: { known: boolean; prompt_tokens: number | null; completion_tokens: number | null; total_tokens: number | null }
  response_hash: string | null
}
export type HardConstraintTier = 'meets_requirements' | 'needs_review' | 'hard_conflict'
export type PreferredStatus = 'supports' | 'contradicts' | 'insufficient' | 'mixed' | null
export interface HardConstraintUnit {
  kind: 'required' | 'prohibited' | 'logic_group'; source_id: string; member_item_ids: string[]
  result: 'pass' | 'fail' | 'unknown'
}
export interface RecommendationReason {
  reason_code: string; requirement_item_id: string | null; text: string; citations: DeepReviewCitation[]
}
export type ScoreDimensionKey = 'required_features' | 'topic' | 'technology' | 'language'
  | 'project_type' | 'preferred' | 'activity'
export interface ScoreDimension {
  key: ScoreDimensionKey; label: string; weight: number; applicable: boolean
  score: number | null; units_total: number; supports: number; contradicts: number
  insufficient: number; missing_data: boolean; detail: string | null
}
export interface EvidenceCoverage {
  applicable_units: number; supported: number; contradicted: number; insufficient: number
  coverage: number; not_applicable_dimensions: string[]
}
export interface RepositoryRanking {
  repository_id: string; full_name: string | null; html_url: string | null; rank: number
  hard_constraint_tier: HardConstraintTier; hard_constraint_units: HardConstraintUnit[]
  preferred: { applicable: boolean; status: PreferredStatus; supports_count: number; contradicts_count: number
    insufficient_count: number; evidence_match_count: number }
  recommendation: { status: 'recommended' | 'needs_review' | 'not_recommended'; reason_code: string
    reasons: RecommendationReason[]; limitations: string[] }
  total_score?: number | null; score_breakdown?: ScoreDimension[]
  evidence_coverage?: EvidenceCoverage | null; ranking_tier?: HardConstraintTier | null
  ranking_explanation?: string | null
}
export interface RankingResults {
  ranking_config_version: 'hard-tier-preferred-v1' | 'hard-tier-score-v2'
  scored_at?: string | null
  repositories: RepositoryRanking[]
  excluded_repositories: { repository_id: string; status: string; error_code: string | null; reason_code: string }[]
}
const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
const isText = (value: unknown): value is string => typeof value === 'string' && value.length > 0
const isOptionalText = (value: unknown): value is string | null => value === null || isText(value)
const isCount = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0
const isOneOf = <T extends string>(value: unknown, values: readonly T[]): value is T => typeof value === 'string' && values.includes(value as T)
const isTextList = (value: unknown): value is string[] => Array.isArray(value) && value.every(isText)
const isCitation = (value: unknown): value is DeepReviewCitation => isRecord(value) && isText(value.chunk_id) && isText(value.quote)
  && isCount(value.line_start) && value.line_start >= 1 && isCount(value.line_end) && value.line_end >= value.line_start
  && isCount(value.byte_start) && isCount(value.byte_end) && value.byte_end > value.byte_start
const citationKey = (citation: DeepReviewCitation) => JSON.stringify([
  citation.chunk_id, citation.quote, citation.line_start, citation.line_end, citation.byte_start, citation.byte_end,
])
const isSafeLink = (value: string) => {
  try {
    const url = new URL(value)
    return url.protocol === 'https:' || url.protocol === 'http:'
  } catch { return false }
}
const isPreferredSummary = (value: unknown): value is RepositoryRanking['preferred'] => {
  if (!isRecord(value) || typeof value.applicable !== 'boolean' || !isCount(value.supports_count)
      || !isCount(value.contradicts_count) || !isCount(value.insufficient_count) || !isCount(value.evidence_match_count)) return false
  const counts = [value.supports_count, value.contradicts_count, value.insufficient_count]
  if (!value.applicable) return value.status === null && counts.every(count => count === 0) && value.evidence_match_count === 0
  if (!isOneOf(value.status, ['supports', 'contradicts', 'insufficient', 'mixed'] as const) || !counts.some(Boolean)) return false
  const expected = value.supports_count && value.contradicts_count ? 'mixed' : value.supports_count ? 'supports' : value.contradicts_count ? 'contradicts' : 'insufficient'
  return value.status === expected && value.evidence_match_count <= value.supports_count
}
const rankingTierOrder: Record<HardConstraintTier, number> = { meets_requirements: 0, needs_review: 1, hard_conflict: 2 }
const scoreKeys = ['required_features', 'topic', 'technology', 'language', 'project_type', 'preferred', 'activity'] as const
const isScoreDimension = (value: unknown): value is ScoreDimension => {
  if (!isRecord(value) || !isOneOf(value.key, scoreKeys) || !isText(value.label)
      || typeof value.weight !== 'number' || value.weight < 0 || value.weight > 1
      || typeof value.applicable !== 'boolean' || !isCount(value.units_total)
      || !isCount(value.supports) || !isCount(value.contradicts) || !isCount(value.insufficient)
      || typeof value.missing_data !== 'boolean' || !isOptionalText(value.detail)) return false
  if (value.units_total !== value.supports + value.contradicts + value.insufficient) return false
  if (value.applicable !== (typeof value.score === 'number')) return false
  return !value.applicable || (value.score as number) >= 0 && (value.score as number) <= 100
}
const isCoverage = (value: unknown): value is EvidenceCoverage => {
  if (!isRecord(value) || !isCount(value.applicable_units) || !isCount(value.supported)
      || !isCount(value.contradicted) || !isCount(value.insufficient)
      || typeof value.coverage !== 'number' || value.coverage < 0 || value.coverage > 1
      || !isTextList(value.not_applicable_dimensions)) return false
  if (value.applicable_units !== value.supported + value.contradicted + value.insufficient) return false
  const expected = value.applicable_units === 0 ? 0 : (value.supported + value.contradicted) / value.applicable_units
  return Math.abs(value.coverage - expected) <= 1e-9
}
const isRanking = (value: unknown, repositories: DeepReviewRepository[]): value is RankingResults => {
  if (!isRecord(value) || !isOneOf(value.ranking_config_version,
    ['hard-tier-preferred-v1', 'hard-tier-score-v2'] as const)
      || !Array.isArray(value.repositories) || !Array.isArray(value.excluded_repositories)) return false
  const scoredRanking = value.ranking_config_version === 'hard-tier-score-v2'
  const sourceById = new Map<string, DeepReviewRepository>()
  for (const source of repositories) {
    if (!isRecord(source) || !isText(source.repository_id) || sourceById.has(source.repository_id)
        || !isOneOf(source.status, ['completed', 'failed', 'unknown', 'skipped'] as const)
        || !Array.isArray(source.requirements) || !isRecord(source.trace) || !isText(source.trace.readme_path)
        || !isRecord(source.input) || !isTextList(source.input.chunk_ids)) return false
    sourceById.set(source.repository_id, source)
  }
  const rankedIds = new Set<string>()
  const excludedIds = new Set<string>()
  for (let index = 0; index < value.repositories.length; index += 1) {
    const row = value.repositories[index]
    if (!isRecord(row) || !isText(row.repository_id) || rankedIds.has(row.repository_id) || row.rank !== index + 1
        || !isOptionalText(row.full_name) || !isOptionalText(row.html_url) || (row.html_url !== null && !isSafeLink(row.html_url))
        || !isOneOf(row.hard_constraint_tier, ['meets_requirements', 'needs_review', 'hard_conflict'] as const)
        || !Array.isArray(row.hard_constraint_units) || !isPreferredSummary(row.preferred) || !isRecord(row.recommendation)) return false
    const source = sourceById.get(row.repository_id)
    if (!source || source.status !== 'completed') return false
    const requirements = new Map<string, DeepReviewRequirement>()
    const chunkIds = new Set(source.input.chunk_ids)
    for (const item of source.requirements) {
      if (!isRecord(item) || !isText(item.requirement_item_id) || requirements.has(item.requirement_item_id)
          || !isText(item.text) || !isText(item.modality) || !Array.isArray(item.citations)
          || !item.citations.every(citation => isCitation(citation) && chunkIds.has(citation.chunk_id))) return false
      requirements.set(item.requirement_item_id, item as unknown as DeepReviewRequirement)
    }
    const preferred = [...requirements.values()].filter(item => item.modality === 'preferred')
    const supports = preferred.filter(item => item.verdict === 'supports').length
    const contradicts = preferred.filter(item => item.verdict === 'contradicts').length
    const insufficient = preferred.filter(item => item.verdict === 'insufficient').length
    if (row.preferred.applicable !== (preferred.length > 0) || row.preferred.supports_count !== supports
        || row.preferred.contradicts_count !== contradicts || row.preferred.insufficient_count !== insufficient
        || row.preferred.evidence_match_count !== preferred.filter(item => item.verdict === 'supports'
          && item.citations.some(citation => chunkIds.has(citation.chunk_id))).length) return false
    for (const unit of row.hard_constraint_units) {
      if (!isRecord(unit) || !isOneOf(unit.kind, ['required', 'prohibited', 'logic_group'] as const)
          || !isText(unit.source_id) || !isTextList(unit.member_item_ids) || !isOneOf(unit.result, ['pass', 'fail', 'unknown'] as const)) return false
      if (unit.kind === 'logic_group') {
        if (!unit.member_item_ids.length || !unit.member_item_ids.every(id => {
          const item = requirements.get(id)
          return item && (item.modality === 'required' || item.modality === 'prohibited') && item.logic_group_id === unit.source_id
        })) return false
      } else if (unit.member_item_ids.length !== 1 || unit.member_item_ids[0] !== unit.source_id
                 || requirements.get(unit.source_id)?.modality !== unit.kind) return false
    }
    const hasFail = row.hard_constraint_units.some(unit => unit.result === 'fail')
    const hasUnknown = row.hard_constraint_units.some(unit => unit.result === 'unknown')
    if ((hasFail && row.hard_constraint_tier !== 'hard_conflict')
        || (row.hard_constraint_tier === 'hard_conflict' && !hasFail)
        || (row.hard_constraint_tier === 'meets_requirements'
          && (!row.hard_constraint_units.length || hasUnknown || source.coverage !== 'full'))
        || (!row.hard_constraint_units.length && row.hard_constraint_tier !== 'needs_review')) return false
    if (scoredRanking) {
      if (typeof row.total_score !== 'number' || row.total_score < 0 || row.total_score > 100
          || row.ranking_tier !== row.hard_constraint_tier || !isText(row.ranking_explanation)
          || !isCoverage(row.evidence_coverage) || !Array.isArray(row.score_breakdown)
          || row.score_breakdown.length !== 7 || !row.score_breakdown.every(isScoreDimension)
          || new Set(row.score_breakdown.map(dimension => dimension.key)).size !== 7) return false
    }
    if (index > 0) {
      const previous = value.repositories[index - 1] as RepositoryRanking
      const tierDelta = rankingTierOrder[previous.hard_constraint_tier] - rankingTierOrder[row.hard_constraint_tier]
      if (tierDelta > 0) return false
      if (tierDelta === 0 && scoredRanking) {
        const scoreDelta = (previous.total_score as number) - (row.total_score as number)
        const previousCoverage = previous.evidence_coverage as EvidenceCoverage
        const rowCoverage = row.evidence_coverage as EvidenceCoverage
        const coverageDelta = previousCoverage.coverage - rowCoverage.coverage
        if (scoreDelta < -1e-9) return false
        if (Math.abs(scoreDelta) <= 1e-9 && coverageDelta < -1e-9) return false
        if (Math.abs(scoreDelta) <= 1e-9 && Math.abs(coverageDelta) <= 1e-9
            && previous.repository_id > row.repository_id) return false
      } else if (tierDelta === 0) {
        const evidenceDelta = previous.preferred.evidence_match_count - row.preferred.evidence_match_count
        if (evidenceDelta < 0 || (evidenceDelta === 0 && previous.repository_id > row.repository_id)) return false
      }
    }
    const recommendation = row.recommendation
    const expectedStatus = { meets_requirements: 'recommended', needs_review: 'needs_review', hard_conflict: 'not_recommended' }[row.hard_constraint_tier]
    if (!isOneOf(recommendation.status, ['recommended', 'needs_review', 'not_recommended'] as const)
        || recommendation.status !== expectedStatus || !isText(recommendation.reason_code)
        || !Array.isArray(recommendation.reasons) || !isTextList(recommendation.limitations)
        || !recommendation.limitations.some(text => text.includes('仅基于已保存 README'))) return false
    for (const reason of recommendation.reasons) {
      if (!isRecord(reason) || !isText(reason.reason_code) || !isText(reason.text) || !isOptionalText(reason.requirement_item_id)
          || !Array.isArray(reason.citations) || reason.citations.length > 2 || !reason.citations.every(isCitation)
          || (reason.citations.length > 0 && reason.requirement_item_id === null)) return false
      const scopedReason = reason.reason_code.startsWith('PREFERRED_') || isOneOf(reason.reason_code,
        ['REQUIRED_SUPPORTED', 'PROHIBITED_CONFIRMED', 'HARD_CONSTRAINT_CONFLICT', 'HARD_CONSTRAINT_EVIDENCE_INSUFFICIENT'] as const)
      if (scopedReason && reason.requirement_item_id === null) return false
      if (reason.requirement_item_id !== null) {
        const item = requirements.get(reason.requirement_item_id)
        if (!item || (reason.reason_code.startsWith('PREFERRED_') ? item.modality !== 'preferred'
          : item.modality !== 'required' && item.modality !== 'prohibited')) return false
        const allowed = new Set(item.citations.map(citationKey))
        if (!reason.citations.every(citation => allowed.has(citationKey(citation)) && source.input.chunk_ids.includes(citation.chunk_id))) return false
      }
    }
    rankedIds.add(row.repository_id)
  }
  for (const excluded of value.excluded_repositories) {
    if (!isRecord(excluded) || !isText(excluded.repository_id) || excludedIds.has(excluded.repository_id) || rankedIds.has(excluded.repository_id)
        || !isOneOf(excluded.status, ['failed', 'unknown', 'skipped'] as const) || !isOptionalText(excluded.error_code)
        || !isText(excluded.reason_code)) return false
    const source = sourceById.get(excluded.repository_id)
    if (!source || source.status !== excluded.status) return false
    excludedIds.add(excluded.repository_id)
  }
  return rankedIds.size + excludedIds.size === sourceById.size
}

export interface DeepReviewReport {
  schema_version: number; version: string; owner: string; task_id: string; run_id: string; confirmed_revision_id: string
  operation_id: string; revision_hash: string; plan_hash: string; bundle_hash: string; model: string; prompt_version: string
  notice: string; created_at: string; status: Exclude<DeepReviewStatus, 'running'>
  repositories: DeepReviewRepository[]
  ranking?: RankingResults | null
  summary: { repositories_total: number; repositories_completed: number; repositories_failed: number
    repositories_unknown: number; repositories_skipped: number; requirements_total: number
    supports: number; contradicts: number; insufficient: number; model_omitted: number; duplicates: number; unknown_items: number
    usage: { calls: number; unknown_calls: number; prompt_tokens: number; completion_tokens: number; total_tokens: number } }
  warnings: { code: string; requirement_item_id: string | null; logic_group_id: string | null; message: string | null }[]
}
export interface DeepReviewOperation {
  operation_id: string; status: DeepReviewStatus; persisted_status: DeepReviewStatus
  revision_id: string; revision_hash: string; bundle_hash: string; model: string; prompt_version: string
  consent: true; authorized_at: string; deadline: string; report_key: string | null; report_hash: string | null
  error_code: string | null; report: DeepReviewReport | null
}
export const deepReviewPayload = (revision: string): DeepReviewPayload => ({ revision_id: revision, consent: true })
export function validateDeepReview(value: unknown, task: string, revision: string, runId: string): DeepReviewOperation {
  const operation = value as DeepReviewOperation | null
  const statuses: DeepReviewStatus[] = ['running', 'completed', 'partial', 'failed', 'unknown']
  if (!operation || typeof operation.operation_id !== 'string' || !statuses.includes(operation.status)
      || !statuses.includes(operation.persisted_status) || operation.revision_id !== revision
      || operation.consent !== true || typeof operation.bundle_hash !== 'string') {
    throw new Error('深度核查响应与当前授权不匹配。')
  }
  const report = operation.report
  if (!('report' in operation) || (report != null && typeof report !== 'object')) {
    throw new Error('深度核查报告格式无效。')
  }
  if (report && (report.task_id !== task || report.run_id !== runId
      || report.confirmed_revision_id !== revision || report.operation_id !== operation.operation_id
      || report.revision_hash !== operation.revision_hash || report.bundle_hash !== operation.bundle_hash
      || report.model !== operation.model || report.prompt_version !== operation.prompt_version
      || report.status !== operation.persisted_status || !Array.isArray(report.repositories)
      || !report.summary || !Array.isArray(report.warnings))) {
    throw new Error('深度核查报告与当前任务、版本或授权不匹配。')
  }
  if (report?.ranking != null && !isRanking(report.ranking, report.repositories)) {
    throw new Error('深度核查排名格式或引用归属无效。')
  }
  return operation
}
export const shouldPollDeepReview = (operation: DeepReviewOperation): boolean => operation.status === 'running'
export function deepReviewStatusLabel(status: DeepReviewStatus): string {
  return { running: '正在核查', completed: '已完成', partial: '部分完成', failed: '失败', unknown: '结果未知' }[status]
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
  let retrieval: RetrievalBundle | null = null
  let retrievalError = ''
  if (run.retrieval_status === 'ready' || run.retrieval_status === 'partial') {
    try {
      retrieval = await request<RetrievalBundle>(`${path}/retrieval-evidence`, 'GET', signal)
      if (retrieval.task_id !== task || retrieval.confirmed_revision_id !== revision || retrieval.run_id !== runId) throw new Error('检索证据与当前任务或版本不匹配。')
    } catch (error) {
      retrieval = null
      retrievalError = error instanceof Error ? error.message : '检索证据尚未就绪'
    }
  }
  if (signal.aborted) throw new Error('已停止读取')
  return { run, evidence, evidenceError, deep, deepError, retrieval, retrievalError }
}
export async function readDeepReview(request: SearchRequest, task: string, revision: string, runId: string, signal: AbortSignal) {
  const path = `/tasks/${encodeURIComponent(task)}/search-runs/${encodeURIComponent(runId)}/deep-review`
  const operation = await request<DeepReviewOperation>(path, 'GET', signal)
  if (signal.aborted) throw new Error('已停止读取深度核查')
  return validateDeepReview(operation, task, revision, runId)
}

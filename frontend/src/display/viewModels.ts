import type { DeepReviewReport, RetrievalBundle, RetrievalChunk, SearchEvidence, SearchRun } from '../search'
import { formatStamp } from './format'
import { statusOf, type StatusDisplay } from './status'

export interface ReferenceEntry {
  id: string
  label: string
  name: string
}

export function buildRequirementRefs(items: { requirement_item_id: string; text: string }[]): Map<string, ReferenceEntry> {
  const refs = new Map<string, ReferenceEntry>()
  for (const item of items) {
    if (refs.has(item.requirement_item_id)) continue
    refs.set(item.requirement_item_id, { id: item.requirement_item_id, label: `需求 ${refs.size + 1}`, name: item.text })
  }
  return refs
}

const letter = (index: number) => (index < 26 ? String.fromCharCode(65 + index) : `A${index + 1}`)

export function buildRepositoryRefs(items: { repository_id: string; full_name?: string | null }[]): Map<string, ReferenceEntry> {
  const refs = new Map<string, ReferenceEntry>()
  for (const item of items) {
    if (refs.has(item.repository_id)) continue
    refs.set(item.repository_id, {
      id: item.repository_id,
      label: `仓库 ${letter(refs.size)}`,
      name: item.full_name ?? '未命名仓库',
    })
  }
  return refs
}

export interface RankingReasonViewModel {
  key: string
  text: string
  requirementRef: string | null
  requirementText: string | null
  citations: EvidenceViewModel[]
}

export interface RankingUnitViewModel {
  key: string
  kind: StatusDisplay
  sourceLabel: string
  memberLabels: string[]
  result: StatusDisplay
}

export interface RankingScoreRowViewModel {
  key: string
  label: string
  weightText: string
  scoreText: string
  countsText: string
  detail: string | null
  missingData: boolean
}

export interface RankingRowViewModel {
  repositoryId: string
  reference: string
  rank: number
  name: string
  htmlUrl: string | null
  tier: StatusDisplay
  recommendation: StatusDisplay
  reasonSummary: string | null
  reasons: RankingReasonViewModel[]
  limitations: string[]
  preferredText: string
  preferredStatus: StatusDisplay | null
  hardUnits: RankingUnitViewModel[]
  totalScore: number | null
  coveragePercent: number | null
  coverageText: string | null
  rankingExplanation: string | null
  scoreRows: RankingScoreRowViewModel[]
  auditFields: { label: string; value: string }[]
}

export interface RankingExcludedViewModel {
  repositoryId: string
  reference: string
  name: string
  status: StatusDisplay
  auditFields: { label: string; value: string }[]
}

export interface RankingTopModel {
  repositoryId: string
  name: string
  htmlUrl: string | null
  tier: StatusDisplay
  recommendation: StatusDisplay
  reason: string | null
}

export interface RankingSummaryModel {
  top: RankingTopModel | null
  counts: { recommended: number; needsReview: number; notRecommended: number; participated: number; excluded: number }
  headline: string
  explanation: string
  rule: StatusDisplay
  ruleNote: string
  scoredAt: string | null
  rows: RankingRowViewModel[]
  excluded: RankingExcludedViewModel[]
  auditFields: { label: string; value: string }[]
}

export function summarizeRanking(
  ranking: NonNullable<DeepReviewReport['ranking']>,
  repositories: DeepReviewReport['repositories'] = [],
  repositoryNames: Map<string, string | null> = new Map(),
): RankingSummaryModel {
  const counts = { recommended: 0, needsReview: 0, notRecommended: 0, participated: ranking.repositories.length, excluded: ranking.excluded_repositories.length }
  for (const row of ranking.repositories) {
    if (row.recommendation.status === 'recommended') counts.recommended += 1
    else if (row.recommendation.status === 'needs_review') counts.needsReview += 1
    else counts.notRecommended += 1
  }
  const names = new Map<string, string>()
  for (const [id, name] of repositoryNames) if (name) names.set(id, name)
  for (const row of ranking.repositories) if (row.full_name) names.set(row.repository_id, row.full_name)
  const ids = [...ranking.repositories.map(row => row.repository_id), ...ranking.excluded_repositories.map(row => row.repository_id)]
  const refs = buildRepositoryRefs(ids.map(id => ({ repository_id: id, full_name: names.get(id) ?? null })))
  const requirementRefs = buildRequirementRefs(repositories.flatMap(repository => repository.requirements))
  const readmePath = (repositoryId: string) => repositories.find(repository => repository.repository_id === repositoryId)?.trace.readme_path ?? null
  const rows: RankingRowViewModel[] = ranking.repositories.map(row => {
    const repository = repositories.find(item => item.repository_id === row.repository_id)
    const ref = refs.get(row.repository_id)
    const reasons = row.recommendation.reasons.map((reason, reasonIndex) => ({
      key: `${row.repository_id}-reason-${reasonIndex}`,
      text: reason.text,
      requirementRef: reason.requirement_item_id ? requirementRefs.get(reason.requirement_item_id)?.label ?? '需求' : null,
      requirementText: reason.requirement_item_id ? requirementRefs.get(reason.requirement_item_id)?.name ?? null : null,
      citations: reason.citations.map((citation, citationIndex) => ({
        key: `${row.repository_id}-${reasonIndex}-${citationIndex}`,
        requirementId: reason.requirement_item_id ?? '',
        requirementRef: reason.requirement_item_id ? requirementRefs.get(reason.requirement_item_id)?.label ?? '需求' : '推荐依据',
        quote: citation.quote,
        lineStart: citation.line_start,
        lineEnd: citation.line_end,
        chunkId: citation.chunk_id,
        byteStart: citation.byte_start,
        byteEnd: citation.byte_end,
        readmePath: readmePath(row.repository_id) ?? '',
      })),
    }))
    const hardUnits = row.hard_constraint_units.map((unit, unitIndex) => ({
      key: `${row.repository_id}-unit-${unitIndex}`,
      kind: statusOf('unitKind', unit.kind),
      sourceLabel: unit.kind === 'logic_group' ? '逻辑组' : requirementRefs.get(unit.source_id)?.label ?? '需求',
      memberLabels: unit.member_item_ids.map(id => requirementRefs.get(id)?.label ?? '需求'),
      result: statusOf('unitResult', unit.result),
    }))
    const coverage = row.evidence_coverage
    const auditFields = [
      { label: '仓库 ID', value: row.repository_id },
      { label: '排名', value: String(row.rank) },
      { label: '硬约束层级（原始）', value: row.hard_constraint_tier },
      { label: '推荐状态（原始）', value: row.recommendation.status },
      { label: '推荐原因代码', value: row.recommendation.reason_code },
      { label: '推荐理由代码', value: row.recommendation.reasons.map(reason => `${reason.reason_code}${reason.requirement_item_id ? ` · ${reason.requirement_item_id}` : ''}`).join('；') || '无' },
      { label: '限制说明', value: row.recommendation.limitations.join('；') || '无' },
      { label: '偏好统计（原始）', value: row.preferred.applicable ? `support ${row.preferred.supports_count} · contradict ${row.preferred.contradicts_count} · insufficient ${row.preferred.insufficient_count} · evidence ${row.preferred.evidence_match_count} · status ${row.preferred.status ?? '未提供'}` : '不适用' },
      { label: '硬约束单元（原始）', value: row.hard_constraint_units.map(unit => `${unit.kind} ${unit.source_id} · ${unit.result} · 成员 ${unit.member_item_ids.join('、')}`).join('；') || '无' },
      { label: '总分（原始）', value: row.total_score === undefined || row.total_score === null ? '未提供' : row.total_score.toFixed(2) },
      { label: '证据覆盖率（原始）', value: coverage ? `${(coverage.coverage * 100).toFixed(0)}% · 适用单元 ${coverage.applicable_units} · 支持 ${coverage.supported} · 反证 ${coverage.contradicted} · 不足 ${coverage.insufficient} · 不适用维度 ${coverage.not_applicable_dimensions.join('、') || '无'}` : '未提供' },
      { label: '排名说明（原始）', value: row.ranking_explanation ?? '未提供' },
      { label: '评分明细（原始）', value: (row.score_breakdown ?? []).map(dimension => `${dimension.label} · 权重 ${(dimension.weight * 100).toFixed(0)}% · 得分 ${dimension.applicable && dimension.score !== null ? dimension.score.toFixed(2) : 'N/A'} · 支持 ${dimension.supports} / 反证 ${dimension.contradicts} / 不足 ${dimension.insufficient} · 单元 ${dimension.units_total}${dimension.missing_data ? ' · 元数据缺失' : ''}${dimension.detail ? ` · ${dimension.detail}` : ''}`).join('；') || '未提供' },
      { label: '报告内 README 快照', value: repository?.trace.readme_path ?? '未提供' },
    ]
    return {
      repositoryId: row.repository_id,
      reference: ref?.label ?? '仓库',
      rank: row.rank,
      name: row.full_name ?? ref?.name ?? '未命名仓库',
      htmlUrl: row.html_url,
      tier: statusOf('tier', row.hard_constraint_tier),
      recommendation: statusOf('recommendation', row.recommendation.status),
      reasonSummary: row.recommendation.reasons[0]?.text ?? null,
      reasons,
      limitations: row.recommendation.limitations,
      preferredText: row.preferred.applicable
        ? `支持 ${row.preferred.supports_count} · 反证 ${row.preferred.contradicts_count} · 信息不足 ${row.preferred.insufficient_count} · 有效证据 ${row.preferred.evidence_match_count} / ${row.preferred.supports_count + row.preferred.contradicts_count + row.preferred.insufficient_count}`
        : '不适用（没有偏好需求，不是证据不足）',
      preferredStatus: row.preferred.applicable && row.preferred.status ? statusOf('preferred', row.preferred.status) : null,
      hardUnits,
      totalScore: row.total_score === undefined ? null : row.total_score,
      coveragePercent: coverage ? Math.round(coverage.coverage * 100) : null,
      coverageText: coverage ? `证据覆盖率 ${(coverage.coverage * 100).toFixed(0)}%（支持 ${coverage.supported} · 反证 ${coverage.contradicted} · 不足 ${coverage.insufficient}）` : null,
      rankingExplanation: row.ranking_explanation ?? null,
      scoreRows: (row.score_breakdown ?? []).map(dimension => ({
        key: dimension.key,
        label: dimension.label,
        weightText: `${(dimension.weight * 100).toFixed(0)}%`,
        scoreText: dimension.applicable && dimension.score !== null ? dimension.score.toFixed(2) : '不适用',
        countsText: `支持 ${dimension.supports} / 反证 ${dimension.contradicts} / 不足 ${dimension.insufficient}`,
        detail: dimension.detail,
        missingData: dimension.missing_data,
      })),
      auditFields,
    }
  })
  const excluded: RankingExcludedViewModel[] = ranking.excluded_repositories.map(row => {
    const ref = refs.get(row.repository_id)
    return {
      repositoryId: row.repository_id,
      reference: ref?.label ?? '仓库',
      name: ref?.name ?? '未命名仓库',
      status: statusOf('repository', row.status),
      auditFields: [
        { label: '仓库 ID', value: row.repository_id },
        { label: '状态（原始）', value: row.status },
        { label: '错误代码', value: row.error_code ?? '无' },
        { label: '未参与原因代码', value: row.reason_code },
      ],
    }
  })
  const top = rows[0] ?? null
  const topModel = top ? {
    repositoryId: top.repositoryId,
    name: top.name,
    htmlUrl: top.htmlUrl,
    tier: top.tier,
    recommendation: top.recommendation,
    reason: top.reasonSummary,
  } : null
  const rule = statusOf('rankingRule', ranking.ranking_config_version)
  return {
    top: topModel,
    counts,
    headline: topModel
      ? `${topModel.recommendation.raw === 'recommended' ? '首选' : '排名第 1'}：${topModel.name} · ${topModel.tier.label}`
      : '没有可参与排名的仓库',
    explanation: `共 ${counts.participated} 个仓库参与排名：推荐 ${counts.recommended}，待进一步核查 ${counts.needsReview}，不推荐 ${counts.notRecommended}；另有 ${counts.excluded} 个未参与排名。`,
    rule,
    ruleNote: ranking.ranking_config_version === 'hard-tier-score-v2'
      ? '先比较硬约束层级，再比较同层综合得分与证据覆盖率，最后按仓库 ID 升序；分数不能覆盖硬约束层级。'
      : '先比较硬约束层级，再比较同层偏好需求的有效证据数量；同分按仓库 ID 升序。',
    scoredAt: ranking.scored_at ? formatStamp(ranking.scored_at) : null,
    rows,
    excluded,
    auditFields: [
      { label: '排名规则（原始）', value: ranking.ranking_config_version },
      { label: '评分时间（原始）', value: ranking.scored_at ?? '未提供' },
      { label: '参与排名（原始）', value: String(ranking.repositories.length) },
      { label: '未参与排名（原始）', value: ranking.excluded_repositories.map(row => `${row.repository_id} · ${row.status} · ${row.error_code ?? '无错误代码'} · ${row.reason_code}`).join('；') || '无' },
    ],
  }
}

export interface EvidenceViewModel {
  key: string
  requirementId: string
  requirementRef: string
  quote: string
  lineStart: number
  lineEnd: number
  chunkId: string
  byteStart: number
  byteEnd: number
  readmePath: string
}

export interface RequirementViewModel {
  key: string
  id: string
  ref: string
  text: string
  modality: StatusDisplay
  field: StatusDisplay
  verdict: StatusDisplay
  reason: StatusDisplay
  hardCheck: StatusDisplay
  citationCount: number
  rejectedCount: number
  modelOmitted: boolean
  citations: EvidenceViewModel[]
  auditFields: { label: string; value: string }[]
}

export interface RepositoryReviewViewModel {
  repositoryId: string
  displayName: string
  reference: string
  status: StatusDisplay
  coverage: StatusDisplay
  errorCode: string | null
  readmePath: string
  requirementCount: number
  supportCount: number
  conflictCount: number
  insufficientCount: number
  strengths: string[]
  gaps: string[]
  pending: RequirementViewModel[]
  modelOmitted: number
  rejectedCitations: number
  nextStep: string
  requirements: RequirementViewModel[]
  auditFields: { label: string; value: string }[]
}

export function summarizeReview(report: DeepReviewReport, repositoryNames: Map<string, string | null> = new Map()): RepositoryReviewViewModel[] {
  const names = new Map<string, string>()
  for (const [id, name] of repositoryNames) if (name) names.set(id, name)
  for (const row of report.ranking?.repositories ?? []) if (row.full_name) names.set(row.repository_id, row.full_name)
  const refs = buildRepositoryRefs(report.repositories.map(row => ({ repository_id: row.repository_id, full_name: names.get(row.repository_id) })))
  const requirementRefs = buildRequirementRefs(report.repositories.flatMap(row => row.requirements))
  return report.repositories.map(repository => {
    const ref = refs.get(repository.repository_id)
    const requirements: RequirementViewModel[] = repository.requirements.map((item, index) => {
      const requirementRef = requirementRefs.get(item.requirement_item_id)
      return {
        key: `${repository.repository_id}-${index}`,
        id: item.requirement_item_id,
        ref: requirementRef?.label ?? '需求',
        text: item.text,
        modality: statusOf('modality', item.modality),
        field: statusOf('field', item.field),
        verdict: statusOf('verdict', item.verdict),
        reason: statusOf('reason', item.reason_code),
        hardCheck: statusOf('hardCheck', item.hard_constraint_check),
        citationCount: item.citations.length,
        rejectedCount: item.rejected_citations.length,
        modelOmitted: item.model_omitted,
        citations: item.citations.map((citation, citationIndex) => ({
          key: `${item.requirement_item_id}-${citationIndex}`,
          requirementId: item.requirement_item_id,
          requirementRef: requirementRef?.label ?? '需求',
          quote: citation.quote,
          lineStart: citation.line_start,
          lineEnd: citation.line_end,
          chunkId: citation.chunk_id,
          byteStart: citation.byte_start,
          byteEnd: citation.byte_end,
          readmePath: repository.trace.readme_path,
        })),
        auditFields: [
          { label: '需求 ID', value: item.requirement_item_id },
          { label: '字段（原始）', value: item.field },
          { label: '模态（原始）', value: item.modality },
          { label: '操作符（原始）', value: item.operator },
          { label: '逻辑组', value: item.logic_group_id ?? '无' },
          { label: '判定（原始）', value: item.verdict },
          { label: '原因代码', value: item.reason_code },
          { label: '硬约束检查（原始）', value: item.hard_constraint_check },
          { label: '引文 Chunk', value: item.citations.map(citation => citation.chunk_id).join('、') || '无' },
          { label: '引文字节区间', value: item.citations.map(citation => `[${citation.byte_start}, ${citation.byte_end})`).join('、') || '无' },
          { label: '未通过校验引文', value: item.rejected_citations.length > 0 ? item.rejected_citations.map(citation => `${citation.chunk_id}（${citation.reason}）`).join('、') : '无' },
        ],
      }
    })
    const supportCount = requirements.filter(item => item.verdict.raw === 'supports').length
    const conflictCount = requirements.filter(item => item.verdict.raw === 'contradicts').length
    const insufficientCount = requirements.filter(item => item.verdict.raw === 'insufficient').length
    const strengths = requirements
      .filter(item => item.verdict.raw === 'supports' && item.citationCount > 0)
      .map(item => `${item.ref} · ${item.text}`)
    const pending = requirements.filter(item => item.verdict.raw !== 'supports')
    const gaps = pending
      .filter(item => item.modality.raw === 'required' || item.modality.raw === 'prohibited')
      .map(item => `${item.ref} · ${item.text}（${item.verdict.label}）`)
    const modelOmitted = requirements.filter(item => item.modelOmitted).length
    const rejectedCitations = requirements.reduce((total, item) => total + item.rejectedCount, 0)
    const nextStep = gaps.length > 0
      ? `建议人工复核 ${gaps.length} 项冲突或信息不足的约束，再决定是否采用。`
      : modelOmitted > 0
        ? `建议补充证据或调整需求表述，处理 ${modelOmitted} 项未覆盖条目。`
        : strengths.length > 0
          ? '关键支持证据已附引文，可逐条核对原文。'
          : '当前没有可展示的结论，请先确认核查是否完成。'
    const usage = repository.usage.known
      ? `输入 ${repository.usage.prompt_tokens} · 输出 ${repository.usage.completion_tokens} · 合计 ${repository.usage.total_tokens}`
      : 'Provider 未返回用量（未知）'
    const constraints = repository.hard_constraint_summary
    const auditFields = [
      { label: '仓库 ID', value: repository.repository_id },
      { label: '快照 ID', value: repository.snapshot_id },
      { label: '状态（原始）', value: repository.status },
      { label: '证据覆盖（原始）', value: repository.coverage },
      { label: '错误代码', value: repository.error_code ?? '无' },
      { label: 'Commit SHA', value: repository.trace.commit_sha },
      { label: 'Blob SHA', value: repository.trace.blob_sha },
      { label: '内容哈希', value: repository.trace.content_hash },
      { label: 'README 路径', value: repository.trace.readme_path },
      { label: '采集时间', value: repository.trace.collected_at },
      { label: '输入 Chunk 数', value: String(repository.input.chunk_count) },
      { label: '输入字节上界', value: String(repository.input.input_bytes_upper_bound) },
      { label: '预算上限', value: `${repository.input.budget.max_chunks} 个 Chunk / ${repository.input.budget.max_input_bytes} 字节 / ${repository.input.budget.max_output_tokens} 输出 Token` },
      { label: '响应哈希', value: repository.response_hash ?? '无已记录响应' },
      { label: '模型用量', value: usage },
      { label: '必需项统计', value: `通过 ${constraints.required.pass} / 失败 ${constraints.required.fail} / 未知 ${constraints.required.unknown} / 总计 ${constraints.required.total}` },
      { label: '禁止项统计', value: `通过 ${constraints.prohibited.pass} / 失败 ${constraints.prohibited.fail} / 未知 ${constraints.prohibited.unknown} / 总计 ${constraints.prohibited.total}` },
      { label: '偏好项统计', value: `发现证据 ${constraints.preferred.evidence_found} / 未知 ${constraints.preferred.unknown} / 总计 ${constraints.preferred.total}` },
      { label: '逻辑组结果', value: repository.logic_group_results.map(group => `${group.logic_group_id} · ${group.operator} · ${group.result} · 成员 ${group.member_item_ids.join('、')}`).join('；') || '无' },
      { label: '输入 Chunk', value: repository.input.chunk_ids.join('、') || '无' },
    ]
    return {
      repositoryId: repository.repository_id,
      displayName: ref?.name ?? '未命名仓库',
      reference: ref?.label ?? '仓库',
      status: statusOf('repository', repository.status),
      coverage: statusOf('coverage', repository.coverage),
      errorCode: repository.error_code,
      readmePath: repository.trace.readme_path,
      requirementCount: requirements.length,
      supportCount,
      conflictCount,
      insufficientCount,
      strengths,
      gaps,
      pending,
      modelOmitted,
      rejectedCitations,
      nextStep,
      requirements,
      auditFields,
    }
  })
}

export interface ScreeningQuoteViewModel {
  key: string
  requirementRef: string
  lineStart: number
  lineEnd: number
  byteStart: number
  byteEnd: number
  quote: string
  readmePath: string | null
}

export interface ScreeningRequirementViewModel {
  key: string
  id: string
  ref: string
  text: string
  modality: StatusDisplay
  status: StatusDisplay
  quotes: ScreeningQuoteViewModel[]
}

export interface ScreeningChunkViewModel {
  key: string
  chunkId: string
  lineStart: number
  lineEnd: number
  byteStart: number
  byteEnd: number
  quote: string
  associations: string[]
  methods: string[]
  distance: number | null
  rrf: number
  readmePath: string | null
}

export interface ScreeningRepositoryViewModel {
  repositoryId: string
  reference: string
  name: string
  htmlUrl: string | null
  description: string | null
  language: string | null
  stars: number | null
  retrievalStatus: StatusDisplay
  coverage: StatusDisplay | null
  retrievalChunkCount: number
  mentioned: number
  unconfirmed: number
  availability: StatusDisplay | null
  readmePath: string | null
  requirements: ScreeningRequirementViewModel[]
  chunks: ScreeningChunkViewModel[]
  source: { snapshotId: string; commitSha: string; blobSha: string; contentHash: string; readmePath: string } | null
  auditFields: { label: string; value: string }[]
}

export interface ScreeningSummaryModel {
  candidateCount: number
  requirementCount: number
  mentioned: number
  unconfirmed: number
  retrievalStatus: StatusDisplay
  evidencePublished: boolean
  chunkCount: number
  repositories: ScreeningRepositoryViewModel[]
}

export function summarizeScreening(run: SearchRun | null, evidence: SearchEvidence | null, retrieval: RetrievalBundle | null): ScreeningSummaryModel {
  const ids: string[] = []
  const add = (id: string) => { if (!ids.includes(id)) ids.push(id) }
  retrieval?.candidates.forEach(item => add(item.repository_id))
  evidence?.candidates.forEach(item => add(item.repository_id))
  run?.candidates?.forEach(item => add(item.repository_id))
  const refs = buildRepositoryRefs(ids.map(id => {
    const candidate = retrieval?.candidates.find(item => item.repository_id === id)
    return { repository_id: id, full_name: candidate?.repository?.full_name ?? null }
  }))
  const requirementItems = evidence?.requirement_items ?? []
  const requirementRefs = buildRequirementRefs(requirementItems)
  let chunkCount = 0
  const repositories = ids.map(id => {
    const retrievalCandidate = retrieval?.candidates.find(item => item.repository_id === id)
    const evidenceCandidate = evidence?.candidates.find(item => item.repository_id === id)
    const runCandidate = run?.candidates?.find(item => item.repository_id === id)
    const items = evidenceCandidate?.items ?? []
    const ref = refs.get(id)
    const readmePath = retrievalCandidate?.readme_path ?? evidenceCandidate?.source?.readme_path ?? null
    const source = evidenceCandidate?.source
      ? {
          snapshotId: evidenceCandidate.source.snapshot_id,
          commitSha: evidenceCandidate.source.commit_sha,
          blobSha: evidenceCandidate.source.blob_sha,
          contentHash: evidenceCandidate.source.content_hash,
          readmePath: evidenceCandidate.source.readme_path,
        }
      : retrievalCandidate
        ? {
            snapshotId: retrievalCandidate.snapshot_id,
            commitSha: retrievalCandidate.commit_sha ?? '未提供',
            blobSha: retrievalCandidate.blob_sha ?? '未提供',
            contentHash: retrievalCandidate.content_hash ?? '未提供',
            readmePath: retrievalCandidate.readme_path ?? '未提供',
          }
        : null
    const requirements: ScreeningRequirementViewModel[] = evidence
      ? requirementItems.map((requirement, index) => {
          const item = items.find(row => row.requirement_item_id === requirement.requirement_item_id)
          return {
            key: `${id}-${requirement.requirement_item_id}-${index}`,
            id: requirement.requirement_item_id,
            ref: requirementRefs.get(requirement.requirement_item_id)?.label ?? '需求',
            text: requirement.text,
            modality: statusOf('modality', requirement.modality),
            status: statusOf('evidenceItem', item?.status ?? 'unconfirmed'),
            quotes: (item?.quotes ?? []).map((quote, quoteIndex) => ({
              key: `${id}-${requirement.requirement_item_id}-${quoteIndex}`,
              requirementRef: requirementRefs.get(requirement.requirement_item_id)?.label ?? '需求',
              lineStart: quote.line_start,
              lineEnd: quote.line_end,
              byteStart: quote.byte_start,
              byteEnd: quote.byte_end,
              quote: quote.text,
              readmePath,
            })),
          }
        })
      : []
    const merged = new Map<string, { chunk: RetrievalChunk; associations: string[]; methods: Set<string>; distance: number | null; rrf: number }>()
    for (const item of retrievalCandidate?.items ?? []) {
      const requirement = retrieval?.requirements.find(row => row.requirement_item_id === item.requirement_item_id)
      const label = requirement?.field ? statusOf('field', requirement.field).label : '需求'
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
    const chunks: ScreeningChunkViewModel[] = [...merged.values()].map(entry => ({
      key: entry.chunk.chunk_id,
      chunkId: entry.chunk.chunk_id,
      lineStart: entry.chunk.line_start,
      lineEnd: entry.chunk.line_end,
      byteStart: entry.chunk.byte_start,
      byteEnd: entry.chunk.byte_end,
      quote: entry.chunk.text,
      associations: entry.associations,
      methods: [...entry.methods],
      distance: entry.distance,
      rrf: entry.rrf,
      readmePath,
    }))
    chunkCount += chunks.length
    return {
      repositoryId: id,
      reference: ref?.label ?? '仓库',
      name: retrievalCandidate?.repository?.full_name ?? '未命名仓库',
      htmlUrl: retrievalCandidate?.repository?.html_url ?? null,
      description: retrievalCandidate?.repository?.description ?? null,
      language: retrievalCandidate?.repository?.language ?? null,
      stars: retrievalCandidate?.repository?.stars ?? null,
      retrievalStatus: statusOf('retrieval', retrievalCandidate?.status ?? run?.retrieval_status ?? 'not_started'),
      coverage: retrievalCandidate ? statusOf('coverage', retrievalCandidate.coverage) : null,
      retrievalChunkCount: chunks.length,
      mentioned: items.filter(item => item.status === 'mention_found').length,
      unconfirmed: items.filter(item => item.status !== 'mention_found').length,
      availability: evidenceCandidate ? statusOf('availability', evidenceCandidate.availability) : null,
      readmePath,
      requirements,
      chunks,
      source,
      auditFields: [
        { label: '仓库 ID', value: id },
        { label: '快照 ID', value: source?.snapshotId ?? '未提供' },
        { label: '采集状态（原始）', value: runCandidate?.collection_status ?? '未提供' },
        { label: '证据可用性（原始）', value: evidenceCandidate?.availability ?? '未提供' },
        { label: '检索状态（原始）', value: retrievalCandidate?.status ?? '未提供' },
        { label: '检索覆盖（原始）', value: retrievalCandidate?.coverage ?? '未提供' },
        { label: '检索错误代码', value: retrievalCandidate?.error_code ?? '无' },
        { label: 'Commit SHA', value: retrievalCandidate?.commit_sha ?? source?.commitSha ?? '未提供' },
        { label: 'Blob SHA', value: retrievalCandidate?.blob_sha ?? source?.blobSha ?? '未提供' },
        { label: '内容哈希', value: retrievalCandidate?.content_hash ?? source?.contentHash ?? '未提供' },
        { label: 'README 路径', value: readmePath ?? '未提供' },
        { label: '采集时间', value: retrievalCandidate?.collected_at ?? '未提供' },
      ],
    }
  })
  let mentioned = 0
  let unconfirmed = 0
  if (evidence) {
    for (const requirement of evidence.requirement_items) {
      const statuses = evidence.candidates.flatMap(candidate => candidate.items.filter(item => item.requirement_item_id === requirement.requirement_item_id))
      if (statuses.some(item => item.status === 'mention_found')) mentioned += 1
      else unconfirmed += 1
    }
  }
  return {
    candidateCount: ids.length,
    requirementCount: evidence?.requirement_items.length ?? retrieval?.requirements.length ?? 0,
    mentioned,
    unconfirmed,
    retrievalStatus: statusOf('retrieval', run?.retrieval_status ?? retrieval?.status ?? 'not_started'),
    evidencePublished: Boolean(evidence),
    chunkCount,
    repositories,
  }
}
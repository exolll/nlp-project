// 演示数据：仅用于本地开发/预览。所有响应都带 X-ProjectPilot-Mock 响应头，
// 界面会显示「演示数据（Mock）」标识；这里的内容不代表真实后端记录或模型结论。
import { createHash } from 'node:crypto'

const points = value => Array.from(value)
const sha256 = value => createHash('sha256').update(value).digest('hex')
const itemId = index => `00000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`

export const REPO_A = '1001'
export const REPO_B = '1002'
export const CHUNK_A = 'chunk-required-search'
export const CHUNK_B = 'chunk-preferred-zh'
export const CHUNK_C = 'chunk-required-tech'

export const demoConfig = {
  max_file_bytes: 10_000_000,
  max_pdf_pages: 30,
  max_primary_characters: 30_000,
  max_supplement_characters: 2_000,
  storage_backend: 'local (mock)',
  environment: 'development (mock)',
}

const filePlaceholder = '（演示环境未实际解析上传文件；以下为占位文本，不代表文件真实内容。）'

export function demoDocument({ documentId, taskId, filename, text, mimeType = 'text/plain', fileSize = 0 }) {
  const hasFile = Boolean(filename)
  const segments = []
  if (hasFile) {
    const content = `${filePlaceholder}\n\n文件：${filename}`
    segments.push({
      segment_id: 'seg-file', source_type: 'file', source_kind: 'assignment_document',
      content, original_content: content, source_location: `file:${filename}`,
      location_detail: { demo: true, note: '占位文本，未解析真实文件' },
    })
  }
  if (text) {
    segments.push({
      segment_id: hasFile ? 'seg-supplement' : 'seg-primary',
      source_type: 'user_text', source_kind: hasFile ? 'user_supplement' : 'user_primary_text',
      content: text, original_content: text, source_location: 'text', location_detail: { demo: true },
    })
  }
  let offset = 0
  const placed = segments.map(segment => {
    const value = { ...segment, start_offset: offset, end_offset: offset + points(segment.content).length }
    offset = value.end_offset + 2
    return value
  })
  const normalized = placed.map(segment => segment.content).join('\n\n')
  const extension = filename?.toLowerCase().endsWith('.pdf') ? 'pdf' : hasFile ? 'docx' : null
  return {
    document_id: documentId, task_id: taskId,
    input_mode: extension ? (text ? `${extension}_text` : extension) : 'text',
    original_filename: filename ?? null,
    mime_type: mimeType, file_size: fileSize,
    total_pages: extension === 'pdf' ? 3 : null,
    extracted_text: hasFile ? placed[0].content : text,
    supplemental_text: hasFile && text ? text : null,
    normalized_text: normalized,
    source_segments: placed,
    content_hash: sha256(normalized),
    extraction_status: 'completed',
    warnings: hasFile ? ['演示环境未实际解析上传文件，标准化文本为占位内容。'] : [],
    created_at: new Date().toISOString(),
    character_count: points(normalized).length,
  }
}

const entries = [
  { field: 'project_topic', text: '面向学生的开源项目发现与证据核查', modality: 'informational' },
  { field: 'project_description', text: '输入需求后自动拆解，并在 GitHub 候选仓库的 README 中检索证据，给出带引用的推荐。', modality: 'informational' },
  { field: 'project_type', text: '课程项目', modality: 'informational' },
  { field: 'domain', text: '开发者工具', modality: 'informational' },
  { field: 'required_features', text: 'GitHub 仓库搜索', modality: 'required' },
  { field: 'preferred_features', text: '中文推荐说明', modality: 'preferred' },
  { field: 'programming_languages', text: 'Python', modality: 'informational' },
  { field: 'programming_languages', text: 'TypeScript', modality: 'informational' },
  { field: 'required_technologies', text: 'FastAPI', modality: 'required' },
  { field: 'preferred_technologies', text: 'MySQL', modality: 'preferred' },
]

function locate(document, needle) {
  for (const segment of document.source_segments) {
    const index = segment.original_content.indexOf(needle)
    if (index < 0) continue
    const start = points(segment.original_content.slice(0, index)).length
    return { segment, start, end: start + points(needle).length }
  }
  return null
}

export function demoRevision({ revisionId, taskId, document, revisionNumber = 1, parentRevisionId = null }) {
  const counters = new Map()
  const occurrences = new Map()
  const requirementItems = entries.map((entry, index) => {
    const occurrence = counters.get(entry.field) ?? 0
    counters.set(entry.field, occurrence + 1)
    const span = locate(document, entry.text)
    const valuePath = Array.isArray(requirementsFor(entry.field)) ? `${entry.field}[${occurrence}]` : entry.field
    return {
      requirement_item_id: itemId(index),
      field: entry.field, value_path: valuePath, text: entry.text, modality: entry.modality,
      source_kind: span ? span.segment.source_kind : 'user_primary_text',
      evidence_ids: [`ev-${index + 1}`],
      logic_group_id: null, operator: 'not_applicable', supersedes_item_id: null,
    }
  })
  function requirementsFor(field) {
    return ['project_topic', 'project_description', 'project_type', 'domain'].includes(field) ? null : []
  }
  const evidence = entries.map((entry, index) => {
    const span = locate(document, entry.text)
    return {
      evidence_id: `ev-${index + 1}`, field: entry.field, original_text: entry.text,
      source_kind: span ? span.segment.source_kind : 'user_primary_text',
      source_location: span ? span.segment.source_location : 'text',
      requirement_item_id: itemId(index),
      segment_id: span ? span.segment.segment_id : null,
      start_offset: span ? span.start : null,
      end_offset: span ? span.end : null,
    }
  })
  const requirements = {
    project_topic: entries[0].text,
    project_description: entries[1].text,
    project_type: entries[2].text,
    domain: entries[3].text,
    required_features: ['GitHub 仓库搜索'],
    preferred_features: ['中文推荐说明'],
    programming_languages: ['Python', 'TypeScript'],
    required_technologies: ['FastAPI'],
    preferred_technologies: ['MySQL'],
    missing_information: [],
    uncertain_information: null,
    source_evidence: evidence,
  }
  return {
    revision_id: revisionId, task_id: taskId, revision_number: revisionNumber,
    parent_revision_id: parentRevisionId, status: 'extracted',
    requirements, requirement_items: requirementItems,
    change_log: [], created_at: new Date().toISOString(), confirmed_at: null,
  }
}

const quote = (chunkId, text, lineStart, lineEnd) => ({
  chunk_id: chunkId, quote: text, line_start: lineStart, line_end: lineEnd,
  byte_start: 320, byte_end: 320 + Buffer.byteLength(text),
})

const deepCitation = (text, lineStart, lineEnd) => ({
  text, line_start: lineStart, line_end: lineEnd, byte_start: 320, byte_end: 320 + Buffer.byteLength(text),
})

const source = (name, path, sha) => ({
  snapshot_id: `snapshot-${name}`, commit_sha: sha, readme_path: `https://github.com/demo/${path}/blob/${sha}/README.md`,
  blob_sha: `${sha}-blob`, content_hash: `${sha}-hash`,
})

const requirementRows = revision => revision.requirement_items.map(item => ({
  requirement_item_id: item.requirement_item_id, text: item.text, modality: item.modality,
  operator: item.operator, logic_group_id: item.logic_group_id,
}))

const findItem = (revision, field) => revision.requirement_items.find(item => item.field === field) ?? null

const verdictMap = (revision, repositoryId) => {
  const rows = new Map()
  for (const item of revision.requirement_items) {
    const supported = repositoryId === REPO_A
      && ['required_features', 'required_technologies', 'preferred_features'].includes(item.field)
    rows.set(item.requirement_item_id, supported ? 'supports' : 'insufficient')
  }
  return rows
}

const citationsFor = (revision, item, repositoryId) => {
  if (repositoryId !== REPO_A) return []
  if (item.field === 'required_features' && item.text) return [quote(CHUNK_A, '支持 GitHub 仓库搜索，并保留 README 引用行号。', 12, 14)]
  if (item.field === 'required_technologies') return [quote(CHUNK_C, '技术栈：FastAPI + React，数据库偏好 MySQL。', 18, 20)]
  if (item.field === 'preferred_features') return [quote(CHUNK_B, '提供中文推荐说明与评分排名。', 24, 26)]
  return []
}

export function demoSearchEvidence({ taskId, runId, revision }) {
  const rows = requirementRows(revision)
  const evidenceItem = (row, mention, text, lineStart, lineEnd) => ({
    ...row, status: mention ? 'mention_found' : 'unconfirmed',
    quotes: mention ? [{ text, line_start: lineStart, line_end: lineEnd, byte_start: 320, byte_end: 320 + Buffer.byteLength(text) }] : [],
  })
  return {
    version: 'evidence-mock-1', task_id: taskId, run_id: runId, confirmed_revision_id: revision.revision_id,
    warning: '演示数据：字面提及不是满足结论；否定语义与禁止约束仍需人工核对。',
    requirement_items: rows,
    candidates: [
      {
        repository_id: REPO_A, availability: 'available', source: source('alpha', 'demo-alpha', 'a1b2c3d'),
        items: rows.map(row => evidenceItem(row, ['required_features', 'preferred_features', 'required_technologies'].includes(row_field(revision, row)), 'README 描述了检索、引用与中文说明能力。', 12, 26)),
      },
      {
        repository_id: REPO_B, availability: 'available', source: source('beta', 'demo-beta', 'd4e5f6a'),
        items: rows.map(row => evidenceItem(row, false)),
      },
    ],
  }
}

function row_field(revision, row) {
  return revision.requirement_items.find(item => item.requirement_item_id === row.requirement_item_id)?.field ?? ''
}

const chunk = (chunkId, text, lineStart, lineEnd) => ({
  chunk_id: chunkId, text, line_start: lineStart, line_end: lineEnd,
  byte_start: 320, byte_end: 320 + Buffer.byteLength(text),
  methods: ['dense', 'lexical'], ranks: { dense: 1, lexical: 2 }, dense_distance: 0.0821,
  rrf_score: 0.0328, score_direction: 'lower_is_closer',
})

export function demoRetrievalBundle({ taskId, runId, revision }) {
  const requirements = revision.requirement_items.map(item => ({
    requirement_item_id: item.requirement_item_id, field: item.field, text: item.text,
    modality: item.modality, operator: item.operator, logic_group_id: item.logic_group_id,
  }))
  const byField = new Map(requirements.map(row => [row.field, row]))
  const chunksFor = (field, repositoryId) => {
    if (repositoryId !== REPO_A) return []
    if (field === 'required_features') return [chunk(CHUNK_A, '支持 GitHub 仓库搜索，并保留 README 引用行号。', 12, 14)]
    if (field === 'required_technologies') return [chunk(CHUNK_C, '技术栈：FastAPI + React，数据库偏好 MySQL。', 18, 20)]
    if (field === 'preferred_features') return [chunk(CHUNK_B, '提供中文推荐说明与评分排名。', 24, 26)]
    return []
  }
  const candidate = (repositoryId, path, fullName, stars, language, description) => ({
    repository_id: repositoryId, snapshot_id: `snapshot-${path}`, coverage: 'full',
    readme_path: `https://github.com/demo/${path}/blob/a1b2c3d/README.md`,
    commit_sha: 'a1b2c3d', blob_sha: 'a1b2c3d-blob', content_hash: 'a1b2c3d-hash',
    collected_at: '2026-11-26T02:00:00+00:00',
    repository: { full_name: fullName, html_url: `https://github.com/${fullName}`, stars, description, language },
    status: 'ok', error_code: null,
    items: requirements.map(row => {
      const chunks = chunksFor(row.field, repositoryId)
      return {
        ...row, status: chunks.length ? 'ready' : 'empty', relevance: chunks.length ? 'candidate' : 'none',
        empty_reason: chunks.length ? null : '未召回相关片段',
        chunks: byField.has(row.field) ? chunks : [],
      }
    }),
  })
  return {
    version: 'retrieval-mock-1', schema_version: 2, task_id: taskId, run_id: runId, confirmed_revision_id: revision.revision_id,
    status: 'ready',
    warning: '演示数据：召回只表示片段与需求可能相关，不代表已找到满足证据。',
    notice: '演示数据：以下为已保存 README 中召回的候选片段；相关性尚未核查，不代表功能已满足；相似度距离与 RRF 排名不构成满足百分比。',
    created_at: new Date().toISOString(), model: 'BAAI/bge-m3', dimension: 1024,
    config: { top_k: 3, rrf_k: 60, dense_weight: 1, lexical_weight: 1 },
    empty_reason: null, query_usage: { tokens: 96, attempts: 2 },
    requirements, candidates: [
      candidate(REPO_A, 'demo-alpha', 'demo/demo-alpha', 4820, 'TypeScript', '带中文说明的 GitHub 项目检索演示仓库。'),
      candidate(REPO_B, 'demo-beta', 'demo/demo-beta', 1160, 'Python', '另一个候选仓库，用于演示证据不足的情况。'),
    ],
  }
}

export function demoDeepCheckReport({ taskId, runId, revision }) {
  const rows = requirementRows(revision)
  const candidate = (repositoryId, name, path, sha, rank, coverage) => {
    const verdicts = verdictMap(revision, repositoryId)
    const items = revision.requirement_items.map(item => {
      const verdict = verdicts.get(item.requirement_item_id)
      const citations = verdict === 'supports' ? citationsFor(revision, item, repositoryId).map(citation => deepCitation(citation.quote, citation.line_start, citation.line_end)) : []
      return {
        requirement_item_id: item.requirement_item_id, text: item.text, modality: item.modality,
        operator: item.operator, logic_group_id: item.logic_group_id,
        verdict, basis: 'model', rejections: [],
        verdict_label: verdict === 'supports' ? '有证据支持' : '信息不足',
        basis_label: '判定已通过行号复核', citations,
      }
    })
    const required = items.filter(item => item.modality === 'required')
    const supported = required.filter(item => item.verdict === 'supports').length
    const missing = required.filter(item => item.verdict !== 'supports')
      .map(item => ({ requirement_item_id: item.requirement_item_id, text: item.text, modality: item.modality, reason: '未见决定性证据' }))
    return {
      repository_id: repositoryId, rank, availability: 'available', availability_label: '快照可读',
      input_coverage: coverage, input_coverage_label: coverage === 'full' ? '全文输入' : '输入被截断',
      input_bytes: 8192, source: source(name, path, sha),
      recommendation: supported === required.length && required.length > 0
        ? { code: 'verify_further', label: '值得进一步核验', reason_code: 'all_hard_requirements_supported', reason: '每条硬性需求都找到了可复核的行号证据，仍需人工进一步核验。' }
        : { code: 'insufficient', label: '信息不足', reason_code: 'no_hard_requirement_evidence', reason: '硬性需求在已保存的 README 中没有决定性证据。' },
      summary: `硬性需求 ${supported}/${required.length} 项有证据支持，${required.length - supported} 项信息不足。`,
      missing_features: missing, risk_notes: [], items,
    }
  }
  return {
    version: 'deep-check-mock-1', task_id: taskId, run_id: runId, confirmed_revision_id: revision.revision_id,
    notice: '演示数据：deep-check 为辅助技术核查过程，最终推荐以逐仓库深度核查为准。',
    requirement_items: rows,
    candidates: [
      candidate(REPO_A, 'alpha', 'demo-alpha', 'a1b2c3d', 0, 'full'),
      candidate(REPO_B, 'beta', 'demo-beta', 'd4e5f6a', 1, 'truncated'),
    ],
    anomalies: [],
  }
}

export function demoDeepReviewReport({ taskId, runId, revision, operation }) {
  const preferredLimitations = ['仅基于已保存 README，不访问仓库其他文件。', '证据不足不等于项目不具备该功能。']
  const repository = (repositoryId, name, path, sha, coverage) => {
    const verdicts = verdictMap(revision, repositoryId)
    const requirements = revision.requirement_items.map(item => {
      const verdict = verdicts.get(item.requirement_item_id)
      const citations = citationsFor(revision, item, repositoryId)
      return {
        requirement_item_id: item.requirement_item_id, field: item.field, text: item.text,
        modality: item.modality, operator: item.operator, logic_group_id: item.logic_group_id,
        verdict,
        reason_code: verdict === 'supports' ? 'supported_by_citation' : 'not_found_in_provided_evidence',
        hard_constraint_check: item.modality === 'required' ? (verdict === 'supports' ? 'pass' : 'unknown') : 'not_applicable',
        citations, rejected_citations: [], model_omitted: false,
      }
    })
    const required = requirements.filter(item => item.modality === 'required')
    const preferred = requirements.filter(item => item.modality === 'preferred')
    return {
      repository_id: repositoryId, snapshot_id: `snapshot-${name}`,
      trace: { ...source(name, path, sha), collected_at: '2026-11-26T02:00:00+00:00' },
      coverage, status: 'completed', error_code: null,
      input: { chunk_ids: [CHUNK_A, CHUNK_B, CHUNK_C], chunk_count: 3, input_bytes_upper_bound: 8192,
        budget: { max_chunks: 8, max_input_bytes: 24000, max_output_tokens: 1200 } },
      requirements,
      hard_constraint_summary: {
        required: { pass: required.filter(item => item.verdict === 'supports').length,
          fail: required.filter(item => item.verdict === 'contradicts').length,
          unknown: required.filter(item => item.verdict === 'insufficient').length, total: required.length },
        prohibited: { pass: 0, fail: 0, unknown: 0, total: 0 },
        preferred: { evidence_found: preferred.filter(item => item.verdict === 'supports').length,
          unknown: preferred.filter(item => item.verdict !== 'supports').length, total: preferred.length },
      },
      logic_group_results: [],
      usage: { known: true, prompt_tokens: 820, completion_tokens: 160, total_tokens: 980 },
      response_hash: `response-${name}`,
    }
  }
  const sources = [
    repository(REPO_A, 'alpha', 'demo-alpha', 'a1b2c3d', 'full'),
    repository(REPO_B, 'beta', 'demo-beta', 'd4e5f6a', 'truncated'),
  ]
  const ranked = sources.map(row => {
    const required = row.requirements.filter(item => item.modality === 'required')
    const preferred = row.requirements.filter(item => item.modality === 'preferred')
    const units = required.map(item => ({
      kind: 'required', source_id: item.requirement_item_id, member_item_ids: [item.requirement_item_id],
      result: item.verdict === 'supports' ? 'pass' : item.verdict === 'contradicts' ? 'fail' : 'unknown',
    }))
    const hasFail = units.some(unit => unit.result === 'fail')
    const hasUnknown = units.some(unit => unit.result === 'unknown')
    const tier = hasFail ? 'hard_conflict'
      : units.length && !hasUnknown && row.coverage === 'full' ? 'meets_requirements' : 'needs_review'
    const supports = preferred.filter(item => item.verdict === 'supports')
    const contradicts = preferred.filter(item => item.verdict === 'contradicts')
    const status = supports.length && contradicts.length ? 'mixed' : supports.length ? 'supports' : contradicts.length ? 'contradicts' : 'insufficient'
    const reasons = tier === 'meets_requirements'
      ? required.filter(item => item.verdict === 'supports').slice(0, 2).map(item => ({
          reason_code: 'REQUIRED_SUPPORTED', requirement_item_id: item.requirement_item_id,
          text: `README 中有可复核的行号证据支持必需需求：${item.text}。`, citations: item.citations,
        }))
      : required.length
        ? [{ reason_code: 'HARD_CONSTRAINT_EVIDENCE_INSUFFICIENT', requirement_item_id: required[0].requirement_item_id,
             text: 'README 证据不足，需人工核查该必需需求。', citations: required[0].citations }]
        : [{ reason_code: 'NO_HARD_CONSTRAINTS', requirement_item_id: null,
             text: '当前版本没有可判定的必需约束，仅作参考。', citations: [] }]
    return {
      repository_id: row.repository_id,
      full_name: row.repository_id === REPO_A ? 'demo/demo-alpha' : 'demo/demo-beta',
      html_url: row.repository_id === REPO_A ? 'https://github.com/demo/demo-alpha' : 'https://github.com/demo/demo-beta',
      rank: row.repository_id === REPO_A ? 1 : 2, hard_constraint_tier: tier, hard_constraint_units: units,
      preferred: {
        applicable: preferred.length > 0, status: preferred.length ? status : null,
        supports_count: supports.length, contradicts_count: contradicts.length,
        insufficient_count: preferred.filter(item => item.verdict === 'insufficient').length,
        evidence_match_count: supports.filter(item => item.citations.length).length,
      },
      recommendation: {
        status: { meets_requirements: 'recommended', needs_review: 'needs_review', hard_conflict: 'not_recommended' }[tier],
        reason_code: tier === 'meets_requirements' ? 'MEETS_REQUIREMENTS' : 'HARD_CONSTRAINT_EVIDENCE_INSUFFICIENT',
        reasons, limitations: preferredLimitations,
      },
    }
  })
  const verdicts = sources.flatMap(row => row.requirements)
  return {
    schema_version: 1, version: 'deep-review-mock-1', task_id: taskId, run_id: runId,
    confirmed_revision_id: revision.revision_id, operation_id: operation.operation_id,
    revision_hash: operation.revision_hash, plan_hash: 'plan-hash-mock', bundle_hash: operation.bundle_hash,
    model: operation.model, prompt_version: operation.prompt_version,
    notice: '演示数据：由本地 Mock Server 生成，不代表真实模型结论。',
    created_at: new Date().toISOString(), status: operation.status,
    repositories: sources,
    ranking: { ranking_config_version: 'hard-tier-preferred-v1', scored_at: null, repositories: ranked, excluded_repositories: [] },
    summary: {
      repositories_total: sources.length, repositories_completed: sources.length, repositories_failed: 0,
      repositories_unknown: 0, repositories_skipped: 0, requirements_total: verdicts.length,
      supports: verdicts.filter(item => item.verdict === 'supports').length,
      contradicts: verdicts.filter(item => item.verdict === 'contradicts').length,
      insufficient: verdicts.filter(item => item.verdict === 'insufficient').length,
      model_omitted: 0, duplicates: 0, unknown_items: 0,
      usage: { calls: 2, unknown_calls: 0, prompt_tokens: 1640, completion_tokens: 320, total_tokens: 1960 },
    },
    warnings: [],
  }
}
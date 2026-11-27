import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const compile = source => `data:text/javascript;base64,${Buffer.from(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText).toString('base64')}`
const read = path => readFile(new URL(path, import.meta.url), 'utf8')
const bundle = async paths => (await Promise.all(paths.map(read)))
  .map(source => source.replace(/^import[^\n]*$/gm, ''))
  .join('\n')
const display = await import(compile(await bundle(['./src/display/status.ts', './src/display/format.ts', './src/display/viewModels.ts'])))
const { statusOf, shortRef, formatStamp, summarizeRanking, summarizeReview, summarizeScreening } = display

test('status mapping is Chinese, explicit, and never inverts insufficient evidence', () => {
  const mention = statusOf('evidenceItem', 'mention_found')
  assert.equal(mention.label, '发现相关提及')
  assert.ok(!mention.label.includes('满足'), 'mention must not read as satisfied')
  const unconfirmed = statusOf('evidenceItem', 'unconfirmed')
  assert.equal(unconfirmed.label, '尚未核实')
  assert.ok(!unconfirmed.label.includes('不符合'), 'unconfirmed must not read as not satisfied')
  const insufficient = statusOf('verdict', 'insufficient')
  assert.equal(insufficient.label, '信息不足')
  assert.ok(!insufficient.label.includes('不符合'), 'insufficient must not read as a hard failure')
  assert.equal(statusOf('reason', 'not_found_in_provided_evidence').tone, 'neutral')
  assert.equal(statusOf('coverage', 'truncated').label, '证据覆盖不完整')
  assert.equal(statusOf('preferred', 'mixed').label, '支持与反证并存')
  assert.equal(statusOf('rankingRule', 'hard-tier-preferred-v1').label, '硬约束优先 + 偏好证据（v1）')
  assert.equal(statusOf('tier', 'needs_review').label, '待进一步核查')
})

test('unknown and missing raw states fall back safely without leaking guesses', () => {
  const unknown = statusOf('verdict', 'brand_new_state')
  assert.equal(unknown.label, '未知状态')
  assert.equal(unknown.tone, 'neutral')
  assert.equal(unknown.raw, 'brand_new_state')
  assert.equal(statusOf('repository', null).label, '未提供')
  assert.equal(statusOf('repository', '').label, '未提供')
})

test('formatting helpers shorten references and never render invalid timestamps as dates', () => {
  assert.equal(shortRef('1234567890'), '#12345678')
  assert.equal(shortRef('abc', 12), '#abc')
  assert.equal(shortRef(null), '未提供')
  assert.equal(formatStamp(null), '未提供')
  assert.equal(formatStamp('not-a-date'), 'not-a-date')
  assert.match(formatStamp('2026-11-27T10:05:00Z'), /^\d{2}-\d{2} \d{2}:\d{2}$/)
})

const ranking = {
  ranking_config_version: 'hard-tier-preferred-v1',
  scored_at: '2026-11-27T10:00:00Z',
  repositories: [
    { repository_id: 'repo-a', full_name: 'demo/demo-alpha', html_url: 'https://github.com/demo/demo-alpha', rank: 1,
      hard_constraint_tier: 'meets_requirements', hard_constraint_units: [],
      preferred: { applicable: true, status: 'supports', supports_count: 1, contradicts_count: 0, insufficient_count: 0, evidence_match_count: 1 },
      recommendation: { status: 'recommended', reason_code: 'all_required_supported',
        reasons: [{ reason_code: 'all_required_supported', requirement_item_id: null, text: '必需项均有证据支持。', citations: [] }], limitations: [] } },
    { repository_id: 'repo-b', full_name: 'demo/demo-beta', html_url: 'https://github.com/demo/demo-beta', rank: 2,
      hard_constraint_tier: 'needs_review', hard_constraint_units: [],
      preferred: { applicable: false, status: null, supports_count: 0, contradicts_count: 0, insufficient_count: 0, evidence_match_count: 0 },
      recommendation: { status: 'needs_review', reason_code: 'required_evidence_insufficient',
        reasons: [{ reason_code: 'required_evidence_insufficient', requirement_item_id: null, text: '必需项证据不足。', citations: [] }], limitations: [] } },
  ],
  excluded_repositories: [],
}

test('ranking summary uses friendly names and counts without raw identifiers', () => {
  const summary = summarizeRanking(ranking)
  assert.equal(summary.top.name, 'demo/demo-alpha')
  assert.ok(summary.headline.includes('首选'))
  assert.ok(summary.headline.includes('符合要求'))
  assert.equal(summary.counts.recommended, 1)
  assert.equal(summary.counts.needsReview, 1)
  assert.equal(summary.counts.participated, 2)
  assert.equal(summary.counts.excluded, 0)
  assert.ok(!summary.headline.includes('repo-a'))
  assert.ok(summary.explanation.includes('推荐 1'))
  assert.ok(summary.rule.label.includes('硬约束优先'))
  assert.match(summary.scoredAt, /^\d{2}-\d{2} \d{2}:\d{2}$/)
})

const citation = { chunk_id: 'chunk-1', quote: 'python cli.py --help', line_start: 4, line_end: 4, byte_start: 10, byte_end: 32 }
const requirement = (id, text, modality, verdict, citations, extra = {}) => ({
  requirement_item_id: id, field: modality === 'prohibited' ? 'required_features' : 'required_features', text, modality,
  operator: modality === 'prohibited' ? 'ALL' : 'not_applicable', logic_group_id: null, verdict,
  reason_code: verdict === 'supports' ? 'supported_by_citation' : verdict === 'contradicts' ? 'conflicted_by_citation' : 'not_found_in_provided_evidence',
  hard_constraint_check: modality === 'preferred' ? 'not_applicable' : verdict === 'supports' ? 'pass' : verdict === 'contradicts' ? 'fail' : 'unknown',
  citations, rejected_citations: [], model_omitted: false, ...extra,
})
const report = {
  ranking,
  repositories: [{
    repository_id: 'repo-a', snapshot_id: 'snap-a',
    trace: { commit_sha: 'commit', blob_sha: 'blob', content_hash: 'hash', readme_path: 'https://example.com/demo/demo-alpha/README.md', collected_at: '2026-11-27T09:00:00Z' },
    coverage: 'full', status: 'completed', error_code: null,
    input: { chunk_ids: ['chunk-1'], chunk_count: 1, input_bytes_upper_bound: 120,
      budget: { max_chunks: 4, max_input_bytes: 400, max_output_tokens: 800 } },
    usage: { known: true, prompt_tokens: 20, completion_tokens: 10, total_tokens: 30 },
    response_hash: 'response-hash',
    hard_constraint_summary: {
      required: { pass: 1, fail: 0, unknown: 1, total: 2 },
      prohibited: { pass: 0, fail: 1, unknown: 0, total: 1 },
      preferred: { evidence_found: 0, unknown: 0, total: 0 },
    },
    logic_group_results: [],
    requirements: [
      requirement('req-1', '必须有命令行入口', 'required', 'supports', [citation]),
      requirement('req-2', '必须支持导出 CSV', 'required', 'insufficient', []),
      requirement('req-3', '不得包含任务管理', 'prohibited', 'contradicts', [citation]),
    ],
  }],
}

test('review summary derives strengths, gaps, and next step while preserving citations', () => {
  const [repository] = summarizeReview(report)
  assert.equal(repository.displayName, 'demo/demo-alpha')
  assert.equal(repository.reference, '仓库 A')
  assert.deepEqual(repository.requirements.map(item => item.ref), ['需求 1', '需求 2', '需求 3'])
  assert.equal(repository.supportCount, 1)
  assert.equal(repository.insufficientCount, 1)
  assert.equal(repository.conflictCount, 1)
  assert.deepEqual(repository.strengths, ['需求 1 · 必须有命令行入口'])
  assert.equal(repository.gaps.length, 2)
  assert.ok(repository.gaps.some(item => item.includes('信息不足')))
  assert.ok(repository.gaps.some(item => item.includes('有证据冲突')))
  assert.equal(repository.pending.length, 2)
  assert.ok(repository.nextStep.includes('人工复核 2'))
  assert.equal(repository.requirements[0].citations[0].readmePath, 'https://example.com/demo/demo-alpha/README.md')
  assert.equal(repository.requirements[0].citations[0].requirementRef, '需求 1')
  assert.ok(!repository.displayName.includes('repo-a'))
})

test('ranking view preserves raw technical fields and README citation positions for audit', () => {
  const detailed = structuredClone(ranking)
  detailed.repositories[0].hard_constraint_units = [{ kind: 'required', source_id: 'req-1', member_item_ids: ['req-1'], result: 'pass' }]
  detailed.repositories[0].recommendation.reasons[0].requirement_item_id = 'req-1'
  detailed.repositories[0].recommendation.reasons[0].citations = [citation]
  const summary = summarizeRanking(detailed, report.repositories)
  const row = summary.rows[0]
  assert.equal(row.reference, '仓库 A')
  assert.equal(row.rank, 1)
  assert.deepEqual(row.hardUnits.map(unit => [unit.sourceLabel, unit.memberLabels.join('、')]), [['需求 1', '需求 1']])
  assert.equal(row.reasons[0].requirementRef, '需求 1')
  assert.equal(row.reasons[0].citations[0].quote, citation.quote)
  assert.equal(row.reasons[0].citations[0].byteStart, citation.byte_start)
  assert.equal(row.reasons[0].citations[0].lineStart, citation.line_start)
  assert.equal(row.reasons[0].citations[0].readmePath, 'https://example.com/demo/demo-alpha/README.md')
  const audit = new Map(row.auditFields.map(field => [field.label, field.value]))
  assert.equal(audit.get('仓库 ID'), 'repo-a')
  assert.equal(audit.get('硬约束层级（原始）'), 'meets_requirements')
  assert.equal(audit.get('推荐状态（原始）'), 'recommended')
  assert.equal(audit.get('推荐原因代码'), 'all_required_supported')
  assert.equal(audit.get('偏好统计（原始）'), 'support 1 · contradict 0 · insufficient 0 · evidence 1 · status supports')
  assert.equal(audit.get('硬约束单元（原始）'), 'required req-1 · pass · 成员 req-1')
  assert.equal(audit.get('总分（原始）'), '未提供')
  assert.equal(audit.get('报告内 README 快照'), 'https://example.com/demo/demo-alpha/README.md')
  const summaryAudit = new Map(summary.auditFields.map(field => [field.label, field.value]))
  assert.equal(summaryAudit.get('排名规则（原始）'), 'hard-tier-preferred-v1')
  assert.equal(summaryAudit.get('评分时间（原始）'), '2026-11-27T10:00:00Z')
  assert.equal(summary.counts.participated, 2)
})

test('review view preserves snapshot hashes and original evidence coordinates', () => {
  const [repository] = summarizeReview(report)
  const audit = new Map(repository.auditFields.map(field => [field.label, field.value]))
  assert.equal(audit.get('仓库 ID'), 'repo-a')
  assert.equal(audit.get('快照 ID'), 'snap-a')
  assert.equal(audit.get('Commit SHA'), 'commit')
  assert.equal(audit.get('Blob SHA'), 'blob')
  assert.equal(audit.get('内容哈希'), 'hash')
  assert.equal(audit.get('响应哈希'), 'response-hash')
  assert.match(audit.get('预算上限'), /4 个 Chunk/)
  assert.equal(audit.get('输入 Chunk'), 'chunk-1')
  const [requirement] = repository.requirements
  assert.equal(requirement.citations[0].quote, citation.quote)
  assert.equal(requirement.citations[0].lineStart, 4)
  assert.equal(requirement.citations[0].byteStart, 10)
  assert.equal(requirement.auditFields.find(field => field.label === '需求 ID').value, 'req-1')
})

test('unknown verdict stays an unknown state and is not summarized as supported or failed', () => {
  const unknown = structuredClone(report)
  unknown.repositories[0].requirements[0].verdict = 'someday_state'
  const [repository] = summarizeReview(unknown)
  assert.equal(repository.requirements[0].verdict.label, '未知状态')
  assert.equal(repository.supportCount, 0)
  assert.ok(!repository.strengths.some(item => item.includes('需求 1')))
})

const retrieval = {
  version: 'v1', status: 'ready', task_id: 'task', run_id: 'run', confirmed_revision_id: 'revision',
  requirements: [{ requirement_item_id: 'req-1', field: 'required_features', text: '必须有命令行入口', modality: 'required', operator: 'ALL', logic_group_id: null }],
  candidates: [{
    repository_id: 'repo-a', snapshot_id: 'snap', coverage: 'full', readme_path: 'https://example.com/readme', status: 'ready', error_code: null,
    repository: { full_name: 'demo/demo-alpha', html_url: null, stars: 42, description: '示例项目', language: 'Python' },
    items: [{ requirement_item_id: 'req-1', text: '必须有命令行入口', status: 'ok', empty_reason: null,
      chunks: [
        { chunk_id: 'c1', text: 'CLI usage: run demo --help', line_start: 10, line_end: 12, byte_start: 100, byte_end: 180, methods: ['dense'], ranks: {}, dense_distance: 0.2, rrf_score: 0.5, score_direction: 'lower_is_better' },
        { chunk_id: 'c2', text: 'Command line entry point', line_start: 20, line_end: 21, byte_start: 200, byte_end: 240, methods: ['lexical', 'dense'], ranks: {}, dense_distance: 0.3, rrf_score: 0.4, score_direction: 'lower_is_better' },
      ] }],
  }],
}
const evidence = {
  task_id: 'task', run_id: 'run', confirmed_revision_id: 'revision', warning: '',
  requirement_items: [{ requirement_item_id: 'req-1', field: 'required_features', text: '必须有命令行入口', modality: 'required', operator: 'ALL', logic_group_id: null },
    { requirement_item_id: 'req-2', field: 'required_features', text: '必须支持导出 CSV', modality: 'required', operator: 'ALL', logic_group_id: null }],
  candidates: [{
    repository_id: 'repo-a', availability: 'available', source: { snapshot_id: 'snap', commit_sha: 'commit', readme_path: 'https://example.com/readme', blob_sha: 'blob', content_hash: 'hash' },
    items: [{ requirement_item_id: 'req-1', status: 'mention_found' }, { requirement_item_id: 'req-2', status: 'unconfirmed' }],
  }],
}
const run = { retrieval_status: 'ready', candidates: [{ repository_id: 'repo-a', collection_status: 'completed' }] }

test('screening summary reports mentions and unverified items without inventing pass/fail', () => {
  const summary = summarizeScreening(run, evidence, retrieval)
  assert.equal(summary.candidateCount, 1)
  assert.equal(summary.requirementCount, 2)
  assert.equal(summary.mentioned, 1)
  assert.equal(summary.unconfirmed, 1)
  assert.equal(summary.evidencePublished, true)
  assert.equal(summary.retrievalStatus.label, '已就绪')
  assert.deepEqual(summary.repositories.map(item => item.name), ['demo/demo-alpha'])
  assert.equal(summary.repositories[0].reference, '仓库 A')
  assert.equal(summary.repositories[0].retrievalChunkCount, 2)
  assert.equal(summary.repositories[0].mentioned, 1)
  assert.equal(summary.repositories[0].unconfirmed, 1)
  assert.equal(summary.repositories[0].availability.label, '已保存快照可读')
  assert.equal(summary.chunkCount, 2)
  assert.deepEqual(summary.repositories[0].requirements.map(item => item.ref), ['需求 1', '需求 2'])
  assert.equal(summary.repositories[0].requirements[0].status.label, '发现相关提及')
  assert.equal(summary.repositories[0].requirements[1].status.label, '尚未核实')
  assert.equal(summary.repositories[0].requirements[0].quotes.length, 0)
  assert.equal(summary.repositories[0].chunks[0].associations[0], '必需功能：必须有命令行入口')
  assert.deepEqual(summary.repositories[0].chunks[1].methods, ['lexical', 'dense'])
  assert.ok(summary.repositories[0].auditFields.some(field => field.label === '仓库 ID' && field.value === 'repo-a'))
  assert.equal(summary.repositories[0].source.snapshotId, 'snap')
})

test('screening view keeps quote text, line and byte ranges for evidence trace', () => {
  const withQuotes = structuredClone(evidence)
  withQuotes.candidates[0].items[0].quotes = [{ text: 'python cli.py --help', line_start: 4, line_end: 4, byte_start: 10, byte_end: 32 }]
  const summary = summarizeScreening(run, withQuotes, retrieval)
  const repository = summary.repositories[0]
  const item = repository.requirements[0]
  assert.equal(item.ref, '需求 1')
  assert.equal(item.quotes.length, 1)
  assert.deepEqual(
    { requirementRef: item.quotes[0].requirementRef, lineStart: item.quotes[0].lineStart, lineEnd: item.quotes[0].lineEnd,
      byteStart: item.quotes[0].byteStart, byteEnd: item.quotes[0].byteEnd, quote: item.quotes[0].quote, readmePath: item.quotes[0].readmePath },
    { requirementRef: '需求 1', lineStart: 4, lineEnd: 4, byteStart: 10, byteEnd: 32, quote: 'python cli.py --help', readmePath: 'https://example.com/readme' },
  )
  assert.equal(repository.source.commitSha, 'commit')
  assert.equal(repository.auditFields.find(field => field.label === '内容哈希').value, 'hash')
  assert.ok(!item.status.label.includes('满足'))
})

test('screening summary leaves counts empty before evidence is published', () => {
  const summary = summarizeScreening(run, null, retrieval)
  assert.equal(summary.mentioned, 0)
  assert.equal(summary.unconfirmed, 0)
  assert.equal(summary.evidencePublished, false)
  assert.equal(summary.requirementCount, 1)
})
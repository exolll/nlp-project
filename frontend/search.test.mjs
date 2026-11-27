import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import test from 'node:test'
import ts from 'typescript'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const require = createRequire(import.meta.url)
const compile = source => `data:text/javascript;base64,${Buffer.from(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText).toString('base64')}`
const searchUrl = compile(await readFile(new URL('./src/search.ts', import.meta.url), 'utf8'))
const displayStatusUrl = compile(await readFile(new URL('./src/display/status.ts', import.meta.url), 'utf8'))
const displayFormatUrl = compile(await readFile(new URL('./src/display/format.ts', import.meta.url), 'utf8'))
const displayViewModelsUrl = compile((await readFile(new URL('./src/display/viewModels.ts', import.meta.url), 'utf8'))
  .replace("from '../search'", `from '${searchUrl}'`)
  .replace("from './status'", `from '${displayStatusUrl}'`)
  .replace("from './format'", `from '${displayFormatUrl}'`))
const reactUrl = name => pathToFileURL(require.resolve(name)).href
let displayComponentSource = await readFile(new URL('./src/components/display.tsx', import.meta.url), 'utf8')
for (const [spec, url] of Object.entries({ '../display/status': displayStatusUrl, '../display/viewModels': displayViewModelsUrl, react: reactUrl('react') })) {
  displayComponentSource = displayComponentSource.replaceAll(`from '${spec}'`, `from '${url}'`).replaceAll(`from "${spec}"`, `from '${url}'`)
}
let displayComponentOutput = ts.transpileModule(displayComponentSource, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText
for (const name of ['react/jsx-runtime', 'react']) displayComponentOutput = displayComponentOutput.replaceAll(`from "${name}"`, `from '${reactUrl(name)}'`).replaceAll(`from '${name}'`, `from '${reactUrl(name)}'`)
const displayComponentsUrl = `data:text/javascript;base64,${Buffer.from(displayComponentOutput).toString('base64')}`
const panelApiUrl = compile('export class Failure extends Error { constructor(status) { super("failure"); this.status = status } } export function request() { throw Error("Unexpected request") }')
async function loadSearchPanel() {
  let source = await readFile(new URL('./src/SearchPanel.tsx', import.meta.url), 'utf8')
  const imports = {
    './search': searchUrl, './api': panelApiUrl,
    './display/status': displayStatusUrl, './display/format': displayFormatUrl,
    './display/viewModels': displayViewModelsUrl, './components/display': displayComponentsUrl,
  }
  for (const [spec, url] of Object.entries(imports)) source = source.replaceAll(`from '${spec}'`, `from '${url}'`)
  let compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText
  for (const name of ['react/jsx-runtime', 'react']) compiled = compiled.replaceAll(`from "${name}"`, `from '${reactUrl(name)}'`).replaceAll(`from '${name}'`, `from '${reactUrl(name)}'`)
  return import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
}
const helpers = await import(searchUrl)
const { searchBudget, searchPayload, reserveSearch, restoreSearch, shouldPollSearch, readSearch, evidenceLabel,
        deepPayload, reserveDeep, restoreDeep, deepPhase, shouldPollDeep, deepPhaseLabel,
        deepReviewPayload, readDeepReview, shouldPollDeepReview, deepReviewStatusLabel } = helpers
const memory = () => { const entries = new Map(); return { getItem: key => entries.get(key) ?? null, setItem: (key, value) => entries.set(key, value) } }
const run = { task_id: 'task', run_id: 'run', confirmed_revision_id: 'revision', status: 'failed', search_status: 'partial', pipeline_status: 'running', evidence_status: 'not_ready' }
const report = { task_id: 'task', run_id: 'run', confirmed_revision_id: 'revision', requirement_items: [], candidates: [], warning: 'mentions only' }
const deepBody = { version: 'deep-check-v1', task_id: 'task', run_id: 'run', confirmed_revision_id: 'revision', notice: '模型分析不代表需求已满足',
  requirement_items: [{ requirement_item_id: 'item', text: '必须有命令行入口', modality: 'must', operator: 'ALL', logic_group_id: null }],
  candidates: [{ repository_id: '1', rank: 0, availability: 'snapshot', availability_label: '已保存快照', input_coverage: 'full', input_coverage_label: '全文已送入', input_bytes: 21,
    source: { snapshot_id: 's', commit_sha: 'c', readme_path: 'README.md', blob_sha: 'b', content_hash: 'h' },
    recommendation: { code: 'verify_further', label: '值得进一步核验', reason_code: 'all_hard_requirements_supported', reason: '全部硬性需求均找到支持证据' },
    summary: '硬性需求 1/1 项有证据支持，0 项信息不足。', missing_features: [], risk_notes: [],
    items: [{ requirement_item_id: 'item', text: '必须有命令行入口', modality: 'must', operator: 'ALL', logic_group_id: null, verdict: 'supports', basis: 'model', rejections: [],
      verdict_label: '有证据支持', basis_label: '模型判定且引文通过复核',
      citations: [{ text: '# A real README fixture', line_start: 1, line_end: 1, byte_start: 0, byte_end: 21 }] }] }], anomalies: [] }
const reviewCitation = { chunk_id: 'chunk', quote: '# A real README fixture', line_start: 1, line_end: 1, byte_start: 0, byte_end: 21 }
const conflictQuote = 'This project prohibits task management.'
const conflictCitation = { chunk_id: 'chunk', quote: conflictQuote, line_start: 3, line_end: 3,
  byte_start: reviewCitation.byte_end + 2, byte_end: reviewCitation.byte_end + 2 + Buffer.byteLength(conflictQuote) }
const itemId = '11111111-1111-4111-8111-111111111111'
const otherItemId = '22222222-2222-4222-8222-222222222222'
const conflictItemId = '33333333-3333-4333-8333-333333333333'
const preferredItemId = (repositoryIndex, itemIndex) => `80000000-0000-4000-8000-${repositoryIndex.toString(16).padStart(6, '0')}${itemIndex.toString(16).padStart(6, '0')}`
const reviewRequirement = (requirement_item_id, modality, verdict, citations) => ({
  requirement_item_id, field: modality === 'preferred' ? 'preferred_features' : modality === 'prohibited' ? 'prohibited_features' : 'required_features',
  text: modality === 'preferred' ? `偏好需求 ${requirement_item_id}` : modality === 'prohibited' ? '禁止任务管理' : '必须有命令行入口', modality,
  operator: 'not_applicable', logic_group_id: null, verdict,
  reason_code: verdict === 'supports' ? 'supported_by_citation' : verdict === 'contradicts' ? 'conflicted_by_citation' : 'not_found_in_provided_evidence',
  hard_constraint_check: modality === 'preferred' ? 'not_applicable' : verdict === 'supports' ? 'pass' : verdict === 'contradicts' ? 'fail' : 'unknown',
  citations, rejected_citations: [], model_omitted: false,
})
const reviewRepository = (repository_id, status, requirements) => {
  const completed = status === 'completed'
  const count = (modality, result) => requirements.filter(item => item.modality === modality && item.hard_constraint_check === result).length
  const preferred = requirements.filter(item => item.modality === 'preferred')
  return { repository_id, snapshot_id: `snapshot-${repository_id}`,
    trace: { commit_sha: 'commit', blob_sha: 'blob', content_hash: 'content', readme_path: 'README.md', collected_at: '2026-01-01T00:00:00Z' },
    coverage: completed ? 'full' : 'empty', status, error_code: status === 'unknown' ? 'PROVIDER_OUTCOME_UNKNOWN' : null,
    input: { chunk_ids: completed ? ['chunk'] : [], chunk_count: completed ? 1 : 0,
      input_bytes_upper_bound: completed ? conflictCitation.byte_end + 1 : 0, budget: { max_chunks: 16, max_input_bytes: 24000, max_output_tokens: 2000 } },
    requirements,
    hard_constraint_summary: {
      required: { pass: count('required', 'pass'), fail: count('required', 'fail'), unknown: count('required', 'unknown'), total: requirements.filter(item => item.modality === 'required').length },
      prohibited: { pass: count('prohibited', 'pass'), fail: count('prohibited', 'fail'), unknown: count('prohibited', 'unknown'), total: requirements.filter(item => item.modality === 'prohibited').length },
      preferred: { evidence_found: preferred.filter(item => item.citations.length > 0 && item.verdict !== 'insufficient').length,
        unknown: preferred.filter(item => item.verdict === 'insufficient').length, total: preferred.length },
    },
    logic_group_results: [], usage: { known: completed, prompt_tokens: completed ? 7 : null, completion_tokens: completed ? 3 : null, total_tokens: completed ? 10 : null },
    response_hash: completed ? `response-${repository_id}` : null,
  }
}
const repo1Requirements = [reviewRequirement(itemId, 'required', 'supports', [reviewCitation]),
  reviewRequirement(preferredItemId(1, 1), 'preferred', 'supports', [reviewCitation]), reviewRequirement(preferredItemId(1, 2), 'preferred', 'supports', [reviewCitation]),
  reviewRequirement(preferredItemId(1, 3), 'preferred', 'contradicts', [conflictCitation]), reviewRequirement(preferredItemId(1, 4), 'preferred', 'insufficient', [])]
const repo3Requirements = [reviewRequirement(conflictItemId, 'prohibited', 'contradicts', [conflictCitation]),
  ...Array.from({ length: 9 }, (_, index) => reviewRequirement(preferredItemId(3, index + 1), 'preferred', 'supports', [reviewCitation]))]
const reviewRepositories = [reviewRepository('1', 'completed', repo1Requirements), reviewRepository('2', 'completed', [reviewRequirement(otherItemId, 'required', 'insufficient', [])]),
  reviewRepository('3', 'completed', repo3Requirements), reviewRepository('4', 'unknown', [])]
const reviewRequirements = reviewRepositories.flatMap(repository => repository.requirements)
const reviewReport = { schema_version: 1, version: 'deep-review-v1', owner: 'owner', task_id: 'task', run_id: 'run', confirmed_revision_id: 'revision',
  operation_id: 'operation', revision_hash: 'revision-hash', plan_hash: 'plan-hash', bundle_hash: 'bundle-hash', model: 'mock-model', prompt_version: 'deep-review-v1',
  notice: '本报告仅基于已保存的 README 文档证据，不代表源码审计、代码运行验证或部署可用性。', created_at: '2026-01-01T00:00:00Z', status: 'partial',
  repositories: reviewRepositories,
  summary: { repositories_total: 4, repositories_completed: 3, repositories_failed: 0, repositories_unknown: 1, repositories_skipped: 0,
    requirements_total: reviewRequirements.length, supports: reviewRequirements.filter(item => item.verdict === 'supports').length,
    contradicts: reviewRequirements.filter(item => item.verdict === 'contradicts').length, insufficient: reviewRequirements.filter(item => item.verdict === 'insufficient').length,
    model_omitted: 0, duplicates: 0, unknown_items: 1, usage: { calls: 4, unknown_calls: 1, prompt_tokens: 21, completion_tokens: 9, total_tokens: 30 } }, warnings: [] }
const ranking = { ranking_config_version: 'hard-tier-preferred-v1', repositories: [
  { repository_id: '1', full_name: 'example/supported', html_url: 'https://github.com/example/supported', rank: 1,
    hard_constraint_tier: 'meets_requirements', hard_constraint_units: [{ kind: 'required', source_id: itemId, member_item_ids: [itemId], result: 'pass' }],
    preferred: { applicable: true, status: 'mixed', supports_count: 2, contradicts_count: 1, insufficient_count: 1, evidence_match_count: 2 },
    recommendation: { status: 'recommended', reason_code: 'HARD_CONSTRAINTS_MET', reasons: [
      { reason_code: 'REQUIRED_SUPPORTED', requirement_item_id: itemId, text: 'README 中有有效证据支持该必需需求。', citations: [reviewCitation] }],
      limitations: ['仅基于已保存 README。'] } },
  { repository_id: '2', full_name: 'example/review', html_url: null, rank: 2,
    hard_constraint_tier: 'needs_review', hard_constraint_units: [{ kind: 'required', source_id: otherItemId, member_item_ids: [otherItemId], result: 'unknown' }],
    preferred: { applicable: false, status: null, supports_count: 0, contradicts_count: 0, insufficient_count: 0, evidence_match_count: 0 },
    recommendation: { status: 'needs_review', reason_code: 'HARD_CONSTRAINT_EVIDENCE_INSUFFICIENT', reasons: [
      { reason_code: 'HARD_CONSTRAINT_EVIDENCE_INSUFFICIENT', requirement_item_id: otherItemId, text: 'README 证据不足，无法确认该硬约束。', citations: [] }],
      limitations: ['证据不足不等于项目不具备该功能。', '仅基于已保存 README。'] } },
  { repository_id: '3', full_name: 'example/conflict', html_url: null, rank: 3,
    hard_constraint_tier: 'hard_conflict', hard_constraint_units: [{ kind: 'prohibited', source_id: conflictItemId, member_item_ids: [conflictItemId], result: 'fail' }],
    preferred: { applicable: true, status: 'supports', supports_count: 9, contradicts_count: 0, insufficient_count: 0, evidence_match_count: 9 },
    recommendation: { status: 'not_recommended', reason_code: 'HARD_CONSTRAINT_CONFLICT', reasons: [
      { reason_code: 'HARD_CONSTRAINT_CONFLICT', requirement_item_id: conflictItemId, text: '存在禁止项冲突证据，不推荐。', citations: [conflictCitation] }], limitations: ['仅基于已保存 README。'] } },
], excluded_repositories: [{ repository_id: '4', status: 'unknown', error_code: 'PROVIDER_OUTCOME_UNKNOWN', reason_code: 'REPOSITORY_OUTCOME_UNKNOWN' }] }
const reviewOperation = { operation_id: 'operation', status: 'running', persisted_status: 'running', revision_id: 'revision', revision_hash: 'revision-hash',
  bundle_hash: 'bundle-hash', model: 'mock-model', prompt_version: 'deep-review-v1', consent: true, authorized_at: '2026-01-01T00:00:00Z',
  deadline: '2026-01-01T00:10:00Z', report_key: null, report_hash: null, error_code: null, report: null }

test('explicit payload fixes the confirmed revision and caps every external budget', () => {
  assert.deepEqual(searchPayload('revision'), { revision_id: 'revision', consent: true, budget: { model_calls: 1, github_requests: 15, search_requests: 5, readme_candidates: 5, per_page: 5, max_pages: 1 } })
  assert.equal(searchBudget.github_requests, searchBudget.search_requests + 2 * searchBudget.readme_candidates)
})
test('unknown submission, duplicate clicks and remount reuse the same durable UUID and exact payload', () => {
  const storage = memory()
  const original = reserveSearch(storage, 'task.revision', 'revision')
  assert.match(original.key, /^[0-9a-f-]{36}$/)
  assert.deepEqual(reserveSearch(storage, 'task.revision', 'revision'), original)
  assert.deepEqual(restoreSearch(storage, 'task.revision', 'revision'), original)
  assert.notEqual(reserveSearch(storage, 'other.revision', 'other').key, original.key)
  assert.throws(() => reserveSearch(storage, 'task.revision', 'changed-revision'))
})
test('storage corruption or inability to persist fails closed before POST', () => {
  assert.throws(() => reserveSearch({ getItem: () => '{' }, 'key', 'revision'))
  assert.throws(() => reserveSearch({ getItem: () => null, setItem: () => { throw Error('blocked') } }, 'key', 'revision'))
})
test('partial/completed manifests keep polling while pipeline publishes; terminal and interrupted pipelines stop', () => {
  assert.equal(shouldPollSearch(run), true)
  assert.equal(shouldPollSearch({ ...run, status: 'completed' }), true)
  for (const pipeline_status of ['completed', 'interrupted', 'failed']) assert.equal(shouldPollSearch({ ...run, pipeline_status }), false)
})
test('reads only GET, delayed publication is not invented, interrupted partial still exposes saved evidence', async () => {
  const calls = []
  let status = run
  const mock = async (path, method) => { calls.push([path, method]); return path.endsWith('/evidence') ? report : status }
  const controller = new AbortController()
  assert.equal((await readSearch(mock, 'task', 'revision', 'run', controller.signal)).evidence, null)
  assert.equal(calls.length, 1)
  status = { ...run, pipeline_status: 'interrupted', evidence_status: 'published' }
  assert.deepEqual((await readSearch(mock, 'task', 'revision', 'run', controller.signal)).evidence, report)
  assert.ok(calls.every(([, method]) => method === 'GET'))
  assert.equal(calls.at(-1)[0], '/tasks/task/search-runs/run/evidence')
})
test('evidence publication races preserve run status without POST or retries', async () => {
  let count = 0
  const result = await readSearch(async path => { count++; if (path.endsWith('/evidence')) throw Error('EVIDENCE_NOT_READY'); return { ...run, evidence_status: 'published' } }, 'task', 'revision', 'run', new AbortController().signal)
  assert.equal(result.run.status, 'failed')
  assert.equal(result.run.search_status, 'partial')
  assert.equal(result.evidence, null)
  assert.match(result.evidenceError, /EVIDENCE_NOT_READY/)
  assert.equal(count, 2)
})
test('abort and task/revision mismatch prevent stale reads from being applied', async () => {
  const controller = new AbortController()
  await assert.rejects(readSearch(async () => { controller.abort(); return run }, 'task', 'revision', 'run', controller.signal))
  await assert.rejects(readSearch(async () => run, 'other-task', 'revision', 'run', new AbortController().signal))
  const result = await readSearch(async path => path.endsWith('/evidence') ? { ...report, confirmed_revision_id: 'other' } : { ...run, evidence_status: 'published' }, 'task', 'revision', 'run', new AbortController().signal)
  assert.equal(result.evidence, null)
  assert.match(result.evidenceError, /不匹配/)
})
test('mention_found and unconfirmed never become satisfied or failed verdicts', () => {
  assert.match(evidenceLabel('mention_found'), /不代表满足/)
  assert.match(evidenceLabel('unconfirmed'), /不代表不满足/)
  assert.match(evidenceLabel('unexpected'), /unconfirmed/)
})

test('deep check is a second independent consent under the same fail-closed UUID rules', () => {
  assert.deepEqual(deepPayload('revision'), { revision_id: 'revision', consent: true })
  const storage = memory()
  const original = reserveDeep(storage, 'task.revision.run', 'revision')
  assert.match(original.key, /^[0-9a-f-]{36}$/)
  assert.deepEqual(reserveDeep(storage, 'task.revision.run', 'revision'), original)
  assert.deepEqual(restoreDeep(storage, 'task.revision.run', 'revision'), original)
  assert.notEqual(reserveDeep(storage, 'task.revision.other', 'revision').key, original.key)
  assert.throws(() => reserveDeep(storage, 'task.revision.run', 'changed-revision'))
  assert.throws(() => reserveDeep({ getItem: () => '{' }, 'key', 'revision'))
  assert.throws(() => reserveDeep({ getItem: () => null, setItem: () => { throw Error('blocked') } }, 'key', 'revision'))
})
test('only an unpublished live check polls; publication, integrity block and terminals stop', () => {
  assert.equal(deepPhase(run), 'not_started')
  assert.equal(shouldPollDeep(run), false)
  assert.equal(deepPhaseLabel('not_started'), '未授权')
  for (const status of ['dispatching', 'dispatched', 'recorded', 'completed']) {
    const value = { ...run, deep_check: { status, published: false, integrity_blocked: false } }
    assert.equal(deepPhase(value), 'running')
    assert.equal(shouldPollDeep(value), true)
  }
  for (const [status, phase] of [['failed', 'failed'], ['interrupted', 'interrupted']]) {
    const value = { ...run, deep_check: { status, published: false, integrity_blocked: false } }
    assert.equal(deepPhase(value), phase)
    assert.equal(shouldPollDeep(value), false)
  }
  const published = { ...run, deep_check: { status: 'completed', published: true, integrity_blocked: false } }
  assert.equal(deepPhase(published), 'ready')
  assert.equal(shouldPollDeep(published), false)
  assert.equal(deepPhaseLabel(deepPhase(published)), '已发布')
  const blocked = { ...run, deep_check: { status: 'completed', published: true, integrity_blocked: true } }
  assert.equal(deepPhase(blocked), 'blocked')
  assert.match(deepPhaseLabel('blocked'), /完整性/)
  assert.match(deepPhaseLabel('interrupted'), /显式恢复/)
})
test('deep report is GET-only, never read before publication, and its failure keeps saved evidence', async () => {
  const calls = []
  const controller = new AbortController()
  let status = run
  const mock = async (path, method) => {
    calls.push([path, method])
    if (path.endsWith('/deep-check')) return deepBody
    if (path.endsWith('/evidence')) return report
    return status
  }
  const skipped = await readSearch(mock, 'task', 'revision', 'run', controller.signal)
  assert.equal(skipped.deep, null)
  assert.equal(skipped.deepError, '')
  assert.ok(!calls.some(([path]) => path.endsWith('/deep-check')), 'unpublished deep check must not be read')
  status = { ...run, deep_check: { status: 'completed', published: true, integrity_blocked: false } }
  const published = await readSearch(mock, 'task', 'revision', 'run', controller.signal)
  assert.deepEqual(published.deep, deepBody)
  assert.equal(calls.at(-1)[0], '/tasks/task/search-runs/run/deep-check')
  assert.equal(calls.filter(([path]) => path.endsWith('/deep-check')).length, 1)
  const failing = await readSearch(async path => {
    if (path.endsWith('/deep-check')) throw Error('DEEP_CHECK_UNAVAILABLE')
    if (path.endsWith('/evidence')) return report
    return { ...run, evidence_status: 'published', deep_check: { status: 'completed', published: true, integrity_blocked: false } }
  }, 'task', 'revision', 'run', controller.signal)
  assert.equal(failing.deep, null)
  assert.match(failing.deepError, /DEEP_CHECK_UNAVAILABLE/)
  assert.equal(failing.evidence, report)
  const retrievalBundle = { version: 'evidence-bundle-v1', task_id: 'task', run_id: 'run', confirmed_revision_id: 'revision',
    status: 'ready', warning: 'w', notice: 'n', created_at: 'x', model: 'BAAI/bge-m3', dimension: 1024,
    empty_reason: null, requirements: [], candidates: [] }
  const retrievalCalls = []
  const retrievalResult = await readSearch(async (path, method) => {
    retrievalCalls.push([path, method])
    if (path.endsWith('/retrieval-evidence')) return retrievalBundle
    if (path.endsWith('/evidence')) return report
    return { ...run, evidence_status: 'published', retrieval_status: 'ready' }
  }, 'task', 'revision', 'run', controller.signal)
  assert.deepEqual(retrievalResult.retrieval, retrievalBundle)
  assert.equal(retrievalCalls.filter(([path]) => path.endsWith('/retrieval-evidence')).length, 1)
  assert.ok(retrievalCalls.every(([, method]) => method === 'GET'))
  const notPublished = await readSearch(async path => path.endsWith('/evidence') ? report : run,
    'task', 'revision', 'run', controller.signal)
  assert.equal(notPublished.retrieval, null)
  const retrievalFailing = await readSearch(async path => {
    if (path.endsWith('/retrieval-evidence')) throw Error('RETRIEVAL_UNAVAILABLE')
    if (path.endsWith('/evidence')) return report
    return { ...run, evidence_status: 'published', retrieval_status: 'ready' }
  }, 'task', 'revision', 'run', controller.signal)
  assert.equal(retrievalFailing.retrieval, null)
  assert.match(retrievalFailing.retrievalError, /RETRIEVAL_UNAVAILABLE/)
  assert.equal(retrievalFailing.evidence, report)
  assert.equal(failing.evidenceError, '')
  const mismatched = await readSearch(async path => path.endsWith('/deep-check') ? { ...deepBody, confirmed_revision_id: 'other' } : status, 'task', 'revision', 'run', controller.signal)
  assert.equal(mismatched.deep, null)
  assert.match(mismatched.deepError, /不匹配/)
  assert.ok(calls.every(([, method]) => method === 'GET'))
})

test('deep review has an independent consent payload without an idempotency key', () => {
  assert.deepEqual(deepReviewPayload('revision'), { revision_id: 'revision', consent: true })
  assert.equal(deepReviewStatusLabel('unknown'), '结果未知')
})
test('deep review polling is GET-only and stops on every terminal or expired running response', async () => {
  const calls = []
  const mock = async (path, method) => { calls.push([path, method]); return reviewOperation }
  const running = await readDeepReview(mock, 'task', 'revision', 'run', new AbortController().signal)
  assert.equal(shouldPollDeepReview(running), true)
  assert.deepEqual(calls, [['/tasks/task/search-runs/run/deep-review', 'GET']])
  for (const status of ['completed', 'partial', 'failed', 'unknown']) {
    assert.equal(shouldPollDeepReview({ ...reviewOperation, status, persisted_status: status }), false)
  }
  assert.equal(shouldPollDeepReview({ ...reviewOperation, status: 'unknown', persisted_status: 'running' }), false)
})
test('deep review rejects stale task, revision, operation and report bindings', async () => {
  const signal = new AbortController().signal
  await assert.rejects(readDeepReview(async () => ({ ...reviewOperation, revision_id: 'other' }), 'task', 'revision', 'run', signal), /不匹配/)
  const published = { ...reviewOperation, status: 'partial', persisted_status: 'partial', report: reviewReport, report_key: 'deep-reviews/operation/report.json', report_hash: 'hash' }
  assert.deepEqual((await readDeepReview(async () => published, 'task', 'revision', 'run', signal)).report, reviewReport)
  for (const patch of [{ task_id: 'other' }, { run_id: 'other' }, { confirmed_revision_id: 'other' }, { operation_id: 'other' }, { bundle_hash: 'other' }, { status: 'completed' }]) {
    await assert.rejects(readDeepReview(async () => ({ ...published, report: { ...reviewReport, ...patch } }), 'task', 'revision', 'run', signal), /不匹配/)
  }
  const controller = new AbortController()
  await assert.rejects(readDeepReview(async () => { controller.abort(); return reviewOperation }, 'task', 'revision', 'run', controller.signal), /停止读取/)
})
test('ranking survives GET validation while reports without ranking remain compatible', async () => {
  const published = { ...reviewOperation, status: 'partial', persisted_status: 'partial', report: { ...reviewReport, ranking }, report_key: 'report.json', report_hash: 'hash' }
  assert.equal((await readDeepReview(async () => published, 'task', 'revision', 'run', new AbortController().signal)).report.ranking, ranking)
  const old = { ...published, report: reviewReport }
  assert.equal((await readDeepReview(async () => old, 'task', 'revision', 'run', new AbortController().signal)).report.ranking, undefined)
  const explicitNull = { ...published, report: { ...reviewReport, ranking: null } }
  assert.equal((await readDeepReview(async () => explicitNull, 'task', 'revision', 'run', new AbortController().signal)).report.ranking, null)
})
test('malformed ranking and cross-requirement citations fail closed before rendering', async () => {
  const signal = new AbortController().signal
  const published = { ...reviewOperation, status: 'partial', persisted_status: 'partial', report: { ...reviewReport, ranking }, report_key: 'report.json', report_hash: 'hash' }
  const rejectRanking = async value => assert.rejects(readDeepReview(async () => ({ ...published, report: { ...published.report, ranking: value } }), 'task', 'revision', 'run', signal), /排名格式或引用归属无效/)
  await rejectRanking({})
  const rankGap = structuredClone(ranking); rankGap.repositories[1].rank = 3
  const countMismatch = structuredClone(ranking); countMismatch.repositories[0].preferred.evidence_match_count = 1
  const unsafeLink = structuredClone(ranking); unsafeLink.repositories[0].html_url = 'javascript:alert(1)'
  const missingLimitation = structuredClone(ranking); missingLimitation.repositories[0].recommendation.limitations = []
  const crossRepository = structuredClone(ranking); crossRepository.repositories[0].recommendation.reasons[0].citations = [conflictCitation]
  const crossRequirement = structuredClone(ranking)
  crossRequirement.repositories[0].recommendation.reasons[0] = { reason_code: 'PREFERRED_SUPPORTED', requirement_item_id: preferredItemId(1, 1),
    text: 'README 中有有效证据支持该偏好需求。', citations: [conflictCitation] }
  const unknownChunk = structuredClone(ranking); unknownChunk.repositories[0].recommendation.reasons[0].citations[0].chunk_id = 'other-chunk'
  for (const value of [rankGap, countMismatch, unsafeLink, missingLimitation, crossRepository, crossRequirement, unknownChunk]) await rejectRanking(value)
})
test('ranking enforces hard-tier invariants, deterministic order and README chunk ownership', async () => {
  const signal = new AbortController().signal
  const published = { ...reviewOperation, status: 'partial', persisted_status: 'partial', report: { ...reviewReport, ranking }, report_key: 'report.json', report_hash: 'hash' }
  const rejectReport = async report => assert.rejects(
    readDeepReview(async () => ({ ...published, report }), 'task', 'revision', 'run', signal),
    /排名格式或引用归属无效/)
  const failTier = structuredClone(ranking)
  failTier.repositories[0].hard_constraint_units[0].result = 'fail'
  await rejectReport({ ...published.report, ranking: failTier })
  const wrongOrder = structuredClone(ranking)
  wrongOrder.repositories = [wrongOrder.repositories[1], wrongOrder.repositories[0], wrongOrder.repositories[2]]
  wrongOrder.repositories[0].rank = 1
  wrongOrder.repositories[1].rank = 2
  await rejectReport({ ...published.report, ranking: wrongOrder })
  const unknownSourceChunk = structuredClone(reviewReport)
  unknownSourceChunk.repositories[0].requirements[1].citations = [
    { ...unknownSourceChunk.repositories[0].requirements[1].citations[0], chunk_id: 'other-chunk' }]
  await rejectReport({ ...unknownSourceChunk, ranking })
})
test('ranking UI shows three tiers, preferred mixed/null, scoped citations, limitations and exclusions', async () => {
  const View = (await loadSearchPanel()).DeepReviewReportView
  const oldHtml = renderToStaticMarkup(React.createElement(View, { report: reviewReport }))
  assert.doesNotMatch(oldHtml, /class="ranking-view"/)
  const html = renderToStaticMarkup(React.createElement(View, { report: { ...reviewReport, ranking } }))
  for (const text of ['推荐结论与排名', '符合要求', '待进一步核查', '存在硬约束冲突', '支持与反证并存',
    '偏好需求：不适用', '有效证据 2 / 4', 'README 中有有效证据支持该必需需求。', conflictQuote, '仅基于已保存 README。',
    '证据不足不等于项目不具备该功能。', 'REPOSITORY_OUTCOME_UNKNOWN', '第 3 名', 'https://github.com/example/supported',
    '该理由未附可跳转的 README 引文', 'Chunk ID', '[0, 21)']) assert.ok(html.includes(text), text)
  assert.ok(html.includes('必需项 · 需求 1 ·'))
  assert.doesNotMatch(html, /需求 1（需求 1）/)
  assert.ok(html.indexOf('第 1 名') < html.indexOf('第 2 名') && html.indexOf('第 2 名') < html.indexOf('第 3 名'))
  assert.doesNotMatch(html, /满足率|综合得分|%/)
})

test('scored ranking validates sort key and renders 100-point breakdown', async () => {
  const weights = { required_features: .35, topic: .15, technology: .15, language: .1, project_type: .1, preferred: .1, activity: .05 }
  const labels = { required_features: 'Required Feature Match', topic: 'Topic Match', technology: 'Technology Match',
    language: 'Programming Language Match', project_type: 'Project Type Match', preferred: 'Preferred Match', activity: 'Activity / Popularity' }
  const scored = () => {
    const copy = structuredClone(ranking)
    copy.ranking_config_version = 'hard-tier-score-v2'
    copy.scored_at = '2026-11-25T00:00:00+00:00'
    copy.repositories.forEach((row, index) => {
      row.total_score = 90 - index * 20
      row.ranking_tier = row.hard_constraint_tier
      row.ranking_explanation = `综合得分 ${row.total_score}/100`
      row.evidence_coverage = { applicable_units: 1, supported: 1, contradicted: 0, insufficient: 0, coverage: 1, not_applicable_dimensions: [] }
      row.score_breakdown = Object.entries(weights).map(([key, weight]) => ({
        key, label: labels[key], weight, applicable: true, score: key === 'activity' ? 50 : 80,
        units_total: key === 'activity' ? 0 : 1, supports: key === 'activity' ? 0 : 1,
        contradicts: 0, insufficient: 0, missing_data: false, detail: null }))
    })
    return copy
  }
  const signal = new AbortController().signal
  const published = value => ({ ...reviewOperation, status: 'partial', persisted_status: 'partial',
    report: { ...reviewReport, ranking: value }, report_key: 'report.json', report_hash: 'hash' })
  const accepted = await readDeepReview(async () => published(scored()), 'task', 'revision', 'run', signal)
  assert.equal(accepted.report.ranking.ranking_config_version, 'hard-tier-score-v2')
  const wrongOrder = scored()
  wrongOrder.repositories[0].hard_constraint_tier = 'needs_review'
  wrongOrder.repositories[0].ranking_tier = 'needs_review'
  wrongOrder.repositories[0].recommendation.status = 'needs_review'
  wrongOrder.repositories[0].total_score = 10
  await assert.rejects(readDeepReview(async () => published(wrongOrder), 'task', 'revision', 'run', signal), /排名格式或引用归属无效/)

  const View = (await loadSearchPanel()).DeepReviewReportView
  const html = renderToStaticMarkup(React.createElement(View, { report: { ...reviewReport, ranking: scored() } }))
  for (const text of ['90.00', '证据覆盖率 100%', 'Required Feature Match', 'Activity / Popularity', '硬约束层级']) assert.ok(html.includes(text), text)
  assert.ok(html.includes('80.00'))
  const missingActivity = scored()
  const activity = missingActivity.repositories[0].score_breakdown.find(item => item.key === 'activity')
  Object.assign(activity, { applicable: false, score: null, missing_data: true })
  const missingHtml = renderToStaticMarkup(React.createElement(View, { report: { ...reviewReport, ranking: missingActivity } }))
  assert.ok(missingHtml.includes('N/A'))
  assert.ok(missingHtml.includes('Activity / Popularity（元数据缺失）'))
})

test('actual deep-review POST sends owner auth and consent but no Idempotency-Key', async () => {
  const apiSource = (await readFile(new URL('./src/api.ts', import.meta.url), 'utf8')).replaceAll('import.meta.env', '({ VITE_API_BASE_URL: undefined })')
  const api = await import(compile(apiSource))
  const originalFetch = globalThis.fetch
  const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  const storage = memory()
  storage.setItem('projectpilot.session', JSON.stringify({ access_token: 'mock-owner' }))
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: storage })
  const calls = []
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options })
    return new Response(JSON.stringify(reviewOperation), { status: 202, headers: { 'Content-Type': 'application/json' } })
  }
  try {
    await api.request('/tasks/task/search-runs/run/deep-review', 'POST', undefined, deepReviewPayload('revision'))
    assert.equal(calls.length, 1)
    assert.equal(calls[0].options.headers.Authorization, 'Bearer mock-owner')
    assert.equal(calls[0].options.headers['Idempotency-Key'], undefined)
    assert.deepEqual(JSON.parse(calls[0].options.body), deepReviewPayload('revision'))
  } finally { globalThis.fetch = originalFetch; if (originalStorage) Object.defineProperty(globalThis, 'sessionStorage', originalStorage); else delete globalThis.sessionStorage }
})

test('actual API uses owner token and never retries an unknown POST; explicit retry keeps UUID', async () => {
  const apiSource = (await readFile(new URL('./src/api.ts', import.meta.url), 'utf8')).replaceAll('import.meta.env', '({ VITE_API_BASE_URL: undefined })')
  const api = await import(compile(apiSource))
  const originalFetch = globalThis.fetch
  const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  const storage = memory()
  storage.setItem('projectpilot.session', JSON.stringify({ access_token: 'mock-owner' }))
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: storage })
  const calls = []
  globalThis.fetch = async (path, options) => { calls.push({ path, options }); throw Error('offline simulated disconnect') }
  try {
    for (let i = 0; i < 2; i++) {
      const saved = reserveSearch(storage, 'search', 'revision')
      await assert.rejects(api.request('/tasks/task/search-runs', 'POST', undefined, saved.payload, saved.key))
      assert.equal(calls.length, i + 1)
    }
    assert.equal(calls[0].options.headers.Authorization, 'Bearer mock-owner')
    assert.equal(calls[0].options.headers['Idempotency-Key'], calls[1].options.headers['Idempotency-Key'])
    assert.equal(calls[0].options.body, calls[1].options.body)
  } finally { globalThis.fetch = originalFetch; if (originalStorage) Object.defineProperty(globalThis, 'sessionStorage', originalStorage); else delete globalThis.sessionStorage }
})

test('initial rendered UI requires unchecked consent, disables POST, escapes revision text, and makes no requests', async () => {
  const source = await readFile(new URL('./src/SearchPanel.tsx', import.meta.url), 'utf8')
  const Panel = (await loadSearchPanel()).default
  const original = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: memory() })
  try {
    const html = renderToStaticMarkup(React.createElement(Panel, { taskId: 'task', revisionId: '<script>alert(1)</script>', enabled: true }))
    assert.match(html, /type="checkbox"/)
    assert.doesNotMatch(html, /checked=""/)
    assert.match(html, /disabled="">按此预算开始搜索/)
    assert.match(html, /&lt;script&gt;/)
    assert.doesNotMatch(html, /<script>/)
    assert.match(html, /真实模型服务与 GitHub/)
    assert.match(html, /我单独同意/)
    assert.match(html, /disabled="">开始深度核对/)
    assert.match(html, /不会直接上传原始 PDF\/Word 文件/)
    assert.match(html, /系统将按仓库最多发起 5 次模型调用/)
    assert.match(html, /disabled="">开始逐仓库深度核查/)
    assert.match(html, /请先完成一次搜索运行/)
    const seeded = memory()
    seeded.setItem('projectpilot.search.task.rev', JSON.stringify({ key: '9c1a2b3c-4d5e-4f60-8a7b-1c2d3e4f5a6b', payload: searchPayload('rev'), run_id: 'run' }))
    seeded.setItem('projectpilot.deep.task.rev.run', JSON.stringify({ key: '1c2d3e4f-5a6b-4789-9a0b-1c2d3e4f5a6b', payload: deepPayload('rev') }))
    Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: seeded })
    const again = renderToStaticMarkup(React.createElement(Panel, { taskId: 'task', revisionId: 'rev', enabled: true }))
    assert.match(again, /disabled="">显式重发同一次授权/)
    assert.doesNotMatch(again, /checked=""/)
    assert.doesNotMatch(again, /<script>/)
    assert.doesNotMatch(source, /dangerouslySetInnerHTML/)
  } finally { if (original) Object.defineProperty(globalThis, 'sessionStorage', original); else delete globalThis.sessionStorage }
})

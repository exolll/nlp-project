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
const helpers = await import(searchUrl)
const { searchBudget, searchPayload, reserveSearch, restoreSearch, shouldPollSearch, readSearch, evidenceLabel,
        deepPayload, reserveDeep, restoreDeep, deepPhase, shouldPollDeep, deepPhaseLabel } = helpers
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
  assert.equal(failing.evidenceError, '')
  const mismatched = await readSearch(async path => path.endsWith('/deep-check') ? { ...deepBody, confirmed_revision_id: 'other' } : status, 'task', 'revision', 'run', controller.signal)
  assert.equal(mismatched.deep, null)
  assert.match(mismatched.deepError, /不匹配/)
  assert.ok(calls.every(([, method]) => method === 'GET'))
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
  let source = await readFile(new URL('./src/SearchPanel.tsx', import.meta.url), 'utf8')
  source = source.replace("from './search'", `from '${searchUrl}'`).replace("from './api'", `from '${compile('export function request() { throw Error("Unexpected request") }')}'`)
  let compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText
  for (const name of ['react/jsx-runtime', 'react']) compiled = compiled.replaceAll(`from "${name}"`, `from '${pathToFileURL(require.resolve(name)).href}'`).replaceAll(`from '${name}'`, `from '${pathToFileURL(require.resolve(name)).href}'`)
  const Panel = (await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)).default
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

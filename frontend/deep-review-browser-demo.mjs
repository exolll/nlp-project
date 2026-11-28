import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { createServer as createNetServer } from 'node:net'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const frontend = fileURLToPath(new URL('.', import.meta.url))
const root = join(frontend, '..')
const artifactDirectory = join(root, 'data/acceptance/ranking')
const taskId = '11111111-1111-4111-8111-111111111111'
const revisionId = '22222222-2222-4222-8222-222222222222'
const runId = '33333333-3333-4333-8333-333333333333'
const documentId = '44444444-4444-4444-8444-444444444444'
const operationId = '55555555-5555-4555-8555-555555555555'
const itemId = '66666666-6666-4666-8666-666666666666'
const conflictItemId = '77777777-7777-4777-8777-777777777777'
const createdAt = '2026-11-24T00:00:00Z'
const quote = 'Task tracker with TypeScript and a command-line interface.'
const conflictQuote = 'This project does not support task management.'
const offlineReadme = `${quote}\n\n${conflictQuote}\n`
const citation = { chunk_id: 'offline-chunk-0', quote, line_start: 1, line_end: 1, byte_start: 0, byte_end: Buffer.byteLength(quote) }
const conflictCitation = { chunk_id: 'offline-chunk-0', quote: conflictQuote, line_start: 3, line_end: 3,
  byte_start: citation.byte_end + 2, byte_end: citation.byte_end + 2 + Buffer.byteLength(conflictQuote) }
const preferredItemId = (repositoryIndex, itemIndex) => `80000000-0000-4000-8000-${repositoryIndex.toString(16).padStart(6, '0')}${itemIndex.toString(16).padStart(6, '0')}`
const repository = (id, status = 'completed', verdict = 'supports', preferredVerdicts = []) => {
  const repositoryIndex = Number.parseInt(id.slice(-3), 10)
  const conflict = verdict === 'contradicts'
  const required = { requirement_item_id: conflict ? conflictItemId : itemId, field: 'required_features', text: '支持任务管理', modality: 'required',
    operator: 'not_applicable', logic_group_id: null, verdict,
    reason_code: verdict === 'supports' ? 'supported_by_citation' : conflict ? 'conflicted_by_citation' : 'not_found_in_provided_evidence',
    hard_constraint_check: verdict === 'supports' ? 'pass' : conflict ? 'fail' : 'unknown', citations: verdict === 'supports' ? [citation] : conflict ? [conflictCitation] : [],
    rejected_citations: [], model_omitted: false }
  const preferred = preferredVerdicts.map((preferredVerdict, index) => ({
    requirement_item_id: preferredItemId(repositoryIndex, index), field: 'preferred_features', text: `偏好功能 ${index + 1}`, modality: 'preferred',
    operator: 'not_applicable', logic_group_id: null, verdict: preferredVerdict,
    reason_code: preferredVerdict === 'supports' ? 'supported_by_citation' : preferredVerdict === 'contradicts' ? 'conflicted_by_citation' : 'not_found_in_provided_evidence',
    hard_constraint_check: 'not_applicable', citations: preferredVerdict === 'insufficient' ? [] : [preferredVerdict === 'supports' ? citation : conflictCitation],
    rejected_citations: [], model_omitted: false,
  }))
  return {
    repository_id: id, snapshot_id: `snapshot-${id}`,
    trace: { commit_sha: 'offline-commit', blob_sha: 'offline-blob', content_hash: 'offline-content', readme_path: 'README.md', collected_at: createdAt },
    coverage: status === 'completed' ? 'full' : 'empty', status, error_code: status === 'unknown' ? 'PROVIDER_OUTCOME_UNKNOWN' : null,
    input: { chunk_ids: status === 'completed' ? ['offline-chunk-0'] : [], chunk_count: status === 'completed' ? 1 : 0,
      input_bytes_upper_bound: status === 'completed' ? Buffer.byteLength(offlineReadme) : 0,
      budget: { max_chunks: 16, max_input_bytes: 24000, max_output_tokens: 2000 } },
    requirements: status === 'completed' ? [required, ...preferred] : [],
    hard_constraint_summary: { required: { pass: status === 'completed' && verdict === 'supports' ? 1 : 0,
      fail: status === 'completed' && conflict ? 1 : 0, unknown: status === 'completed' && verdict === 'insufficient' ? 1 : 0, total: status === 'completed' ? 1 : 0 },
      prohibited: { pass: 0, fail: 0, unknown: 0, total: 0 },
      preferred: { evidence_found: preferredVerdicts.filter(value => value !== 'insufficient').length,
        unknown: preferredVerdicts.filter(value => value === 'insufficient').length, total: preferredVerdicts.length } },
    logic_group_results: [], usage: { known: status === 'completed', prompt_tokens: status === 'completed' ? 7 : null,
      completion_tokens: status === 'completed' ? 3 : null, total_tokens: status === 'completed' ? 10 : null }, response_hash: status === 'completed' ? 'offline-response' : null,
  }
}
const recommendation = (status, code, text, citations = [], requirementItemId = itemId) => ({ status, reason_code: code,
  reasons: [{ reason_code: code, requirement_item_id: requirementItemId, text, citations }],
  limitations: ['仅基于已保存 README，不代表源码审计、代码运行验证或部署可用性。'] })
const ranking = { ranking_config_version: 'hard-tier-preferred-v1', repositories: [
  { repository_id: 'repo-100', full_name: 'example/task-tracker', html_url: 'https://github.com/example/task-tracker', rank: 1,
    hard_constraint_tier: 'meets_requirements', hard_constraint_units: [{ kind: 'required', source_id: itemId, member_item_ids: [itemId], result: 'pass' }],
    preferred: { applicable: true, status: 'mixed', supports_count: 2, contradicts_count: 1, insufficient_count: 1, evidence_match_count: 2 },
    recommendation: recommendation('recommended', 'HARD_CONSTRAINTS_MET', 'README 中有有效证据支持该必需需求。', [citation]) },
  { repository_id: 'repo-200', full_name: 'example/needs-review', html_url: 'https://github.com/example/needs-review', rank: 2,
    hard_constraint_tier: 'needs_review', hard_constraint_units: [{ kind: 'required', source_id: itemId, member_item_ids: [itemId], result: 'unknown' }],
    preferred: { applicable: false, status: null, supports_count: 0, contradicts_count: 0, insufficient_count: 0, evidence_match_count: 0 },
    recommendation: { ...recommendation('needs_review', 'HARD_CONSTRAINT_EVIDENCE_INSUFFICIENT', 'README 证据不足，无法确认该硬约束。'),
      limitations: ['证据不足不等于项目不具备该功能。', '仅基于已保存 README。'] } },
  { repository_id: 'repo-300', full_name: 'example/conflict', html_url: 'https://github.com/example/conflict', rank: 3,
    hard_constraint_tier: 'hard_conflict', hard_constraint_units: [{ kind: 'required', source_id: conflictItemId, member_item_ids: [conflictItemId], result: 'fail' }],
    preferred: { applicable: true, status: 'supports', supports_count: 1, contradicts_count: 0, insufficient_count: 0, evidence_match_count: 1 },
    recommendation: recommendation('not_recommended', 'HARD_CONSTRAINT_CONFLICT', 'README 明确说明不支持任务管理，构成硬约束冲突，不推荐。', [conflictCitation], conflictItemId) },
], excluded_repositories: [{ repository_id: 'repo-900', status: 'unknown', error_code: 'PROVIDER_OUTCOME_UNKNOWN', reason_code: 'REPOSITORY_OUTCOME_UNKNOWN' }] }
const reviewReport = { schema_version: 1, version: 'deep-review-v1', owner: 'offline-user', task_id: taskId, run_id: runId, confirmed_revision_id: revisionId,
  operation_id: operationId, revision_hash: 'offline-revision-hash', plan_hash: 'offline-plan-hash', bundle_hash: 'offline-bundle-hash',
  model: 'offline-mock', prompt_version: 'deep-review-v1', notice: '仅基于已保存 README 的离线排名演示。', created_at: createdAt, status: 'partial',
  repositories: [repository('repo-100', 'completed', 'supports', ['supports', 'supports', 'contradicts', 'insufficient']),
    repository('repo-200', 'completed', 'insufficient'), repository('repo-300', 'completed', 'contradicts', ['supports']), repository('repo-900', 'unknown')],
  ranking, summary: { repositories_total: 4, repositories_completed: 3, repositories_failed: 0, repositories_unknown: 1, repositories_skipped: 0,
    requirements_total: 8, supports: 4, contradicts: 2, insufficient: 2, model_omitted: 0, duplicates: 0, unknown_items: 1,
    usage: { calls: 4, unknown_calls: 1, prompt_tokens: 21, completion_tokens: 9, total_tokens: 30 } }, warnings: [] }
const operation = { operation_id: operationId, status: 'partial', persisted_status: 'partial', revision_id: revisionId,
  revision_hash: 'offline-revision-hash', bundle_hash: 'offline-bundle-hash', model: 'offline-mock', prompt_version: 'deep-review-v1', consent: true,
  authorized_at: createdAt, deadline: '2026-11-24T01:00:00Z', report_key: 'offline/report.json', report_hash: 'offline-report-hash', error_code: null, report: reviewReport }
const searchRun = { task_id: taskId, run_id: runId, confirmed_revision_id: revisionId, status: 'completed', pipeline_status: 'completed',
  dispatch_closed: true, plan_status: 'published', search_status: 'completed', evidence_status: 'not_ready', retrieval_status: 'ready',
  candidates: [{ repository_id: 'repo-100', collection_status: 'completed' }], deep_check: null }
const document = { document_id: documentId, task_id: taskId, input_mode: 'text', original_filename: null, mime_type: 'text/plain', file_size: 30,
  total_pages: null, extracted_text: '需要一个任务管理项目。', supplemental_text: null, normalized_text: '需要一个任务管理项目。',
  source_segments: [], content_hash: 'offline-content', extraction_status: 'completed', warnings: [], created_at: createdAt, character_count: 11 }
const requirements = { project_topic: '任务管理', project_description: '管理任务', project_type: 'Web 应用', domain: '效率',
  required_features: ['支持任务管理'], preferred_features: [], programming_languages: ['TypeScript'], required_technologies: [], preferred_technologies: [],
  missing_information: [], uncertain_information: [], source_evidence: [] }
const revision = { revision_id: revisionId, task_id: taskId, revision_number: 1, parent_revision_id: null, status: 'confirmed', requirements,
  requirement_items: [{ requirement_item_id: itemId, field: 'required_features', value_path: 'required_features[0]', text: '支持任务管理', modality: 'required',
    source_kind: 'assignment_document', evidence_ids: [], logic_group_id: null, operator: 'not_applicable', supersedes_item_id: null }],
  change_log: [], created_at: createdAt, confirmed_at: createdAt }
const responses = new Map([
  ['/api/v1/config', { max_file_bytes: 1000000, max_pdf_pages: 20, max_primary_characters: 10000, max_supplement_characters: 10000, storage_backend: 'offline', environment: 'test' }],
  ['/api/v1/tasks', { tasks: [{ task_id: taskId, created_at: createdAt, status: 'completed', document_id: documentId }] }],
  [`/api/v1/tasks/${taskId}/input/status`, { task_id: taskId, document_id: documentId, status: 'completed', error: null }],
  [`/api/v1/tasks/${taskId}/input`, document],
  [`/api/v1/documents/${documentId}/requirements`, { extraction: null, revisions: [revision], latest_revision_id: revisionId }],
  [`/api/v1/tasks/${taskId}/search-runs/${runId}`, searchRun],
  [`/api/v1/tasks/${taskId}/search-runs/${runId}/retrieval-evidence`, { version: 'evidence-bundle-v2', schema_version: 2, task_id: taskId,
    run_id: runId, confirmed_revision_id: revisionId, status: 'ready', warning: '离线片段', notice: '离线演示', created_at: createdAt, model: 'offline',
    dimension: 3, empty_reason: null, requirements: [], candidates: [] }],
  [`/api/v1/tasks/${taskId}/search-runs/${runId}/deep-review`, operation],
])
const requests = []
const apiServer = createServer(async (incoming, outgoing) => {
  const path = new URL(incoming.url, 'http://127.0.0.1').pathname
  requests.push({ method: incoming.method, path })
  if (incoming.method === 'OPTIONS') {
    outgoing.writeHead(204, { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Authorization, Content-Type, Idempotency-Key' })
    outgoing.end()
    return
  }
  const body = responses.get(path)
  outgoing.writeHead(body ? 200 : 404, { 'Content-Type': 'application/json; charset=utf-8', 'Access-Control-Allow-Origin': '*' })
  outgoing.end(JSON.stringify(body ?? { error_code: 'OFFLINE_NOT_FOUND', message: path }))
})
await new Promise(resolve => apiServer.listen(0, '127.0.0.1', resolve))
const apiOrigin = `http://127.0.0.1:${apiServer.address().port}`
const portServer = createNetServer()
await new Promise(resolve => portServer.listen(0, '127.0.0.1', resolve))
const vitePort = portServer.address().port
await new Promise(resolve => portServer.close(resolve))
const vite = spawn(process.execPath, [join(frontend, 'node_modules/vite/bin/vite.js'), '--host', '127.0.0.1', '--port', String(vitePort), '--strictPort'], {
  cwd: frontend, env: { ...process.env, VITE_API_BASE_URL: `${apiOrigin}/api/v1` }, stdio: ['ignore', 'pipe', 'pipe'] })
const browserProfile = await mkdtemp(join(tmpdir(), 'projectpilot-ranking-'))
const edgePaths = ['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', 'C:/Program Files/Microsoft/Edge/Application/msedge.exe']
let browser
let socket
let browserOutput = ''
let viteOutput = ''
vite.stdout.on('data', chunk => { viteOutput += String(chunk) })
vite.stderr.on('data', chunk => { viteOutput += String(chunk) })
try {
  const edge = edgePaths.find(path => {
    return existsSync(path)
  })
  assert.ok(edge, 'Microsoft Edge executable not found')
  browser = spawn(edge, ['--headless=new', '--disable-gpu', '--no-first-run', '--window-size=1440,1000',
    '--remote-debugging-port=0', `--user-data-dir=${browserProfile}`, 'about:blank'])
  browser.stderr.on('data', chunk => { browserOutput += String(chunk) })
  const endpoint = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(Error(`Browser startup timed out: ${browserOutput}`)), 20000)
    browser.on('error', reject)
    browser.stderr.on('data', chunk => {
      const match = String(chunk).match(/DevTools listening on (ws:\/\/[^\s]+)/)
      if (match) { clearTimeout(timeout); resolve(match[1]) }
    })
  })
  socket = new WebSocket(endpoint)
  await new Promise(resolve => socket.addEventListener('open', resolve, { once: true }))
  let sequence = 0
  const pending = new Map()
  const network = []
  const exceptions = []
  socket.addEventListener('message', event => {
    const data = JSON.parse(event.data)
    if (data.method === 'Network.requestWillBeSent') network.push(data.params.request)
    if (data.method === 'Runtime.exceptionThrown') exceptions.push(data.params.exceptionDetails)
    if (pending.has(data.id)) {
      const { resolve, reject } = pending.get(data.id)
      pending.delete(data.id)
      data.error ? reject(Error(JSON.stringify(data.error))) : resolve(data.result)
    }
  })
  const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
    const id = ++sequence
    pending.set(id, { resolve, reject })
    socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }))
  })
  const { targetId } = await send('Target.createTarget', { url: 'about:blank' })
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true })
  const command = (method, params) => send(method, params, sessionId)
  await command('Page.enable'); await command('Runtime.enable'); await command('Network.enable')
  await command('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false })
  const injection = `
    if (location.origin === ${JSON.stringify(`http://127.0.0.1:${vitePort}`)}) {
      sessionStorage.setItem('projectpilot.session', JSON.stringify({ access_token: 'offline-demo-token', expires_at: '2099-01-01T00:00:00Z' }));
      sessionStorage.setItem('projectpilot.search.${taskId}.${revisionId}', JSON.stringify({ key: '77777777-7777-4777-8777-777777777777',
        payload: { revision_id: '${revisionId}', consent: true, budget: { model_calls: 1, github_requests: 20, search_requests: 9, readme_candidates: 5, per_page: 5, max_pages: 1 } }, run_id: '${runId}' }));
    }
  `
  await command('Page.addScriptToEvaluateOnNewDocument', { source: injection })
  await command('Page.navigate', { url: `http://127.0.0.1:${vitePort}` })
  const evaluate = async expression => {
    const result = await command('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (result.exceptionDetails) throw Error(JSON.stringify(result.exceptionDetails))
    return result.result.value
  }
  const waitFor = async (expression, label, timeout = 45000) => {
    const deadline = Date.now() + timeout
    while (Date.now() < deadline) {
      if (await evaluate(expression)) return
      await new Promise(resolve => setTimeout(resolve, 250))
    }
    const diagnostic = await evaluate(`({ url: location.href, text: document.body.innerText.slice(0, 4000), storage: { ...sessionStorage } })`).catch(error => String(error))
    throw Error(`Timed out waiting for ${label}: ${viteOutput}\n${JSON.stringify({ diagnostic, requests, exceptions }, null, 2)}`)
  }
  await waitFor(`document.querySelectorAll('.history-row').length > 0`, 'offline task history')
  assert.ok(await evaluate(`(() => { const row = document.querySelector('.history-row'); if (!row) return false; row.click(); return true })()`))
  await waitFor(`!!document.querySelector('.ranking-view')`, 'ranking section')
  await waitFor(`document.querySelectorAll('.ranking-item').length === 3`, 'three ranked repositories')
  const beforeGets = requests.filter(row => row.method === 'GET' && row.path.endsWith('/deep-review')).length
  const refreshed = await evaluate(`(() => { const button = [...document.querySelectorAll('button')].find(node => node.textContent.includes('刷新深度核查（仅 GET）')); if (!button || button.disabled) return false; button.click(); return true })()`)
  assert.ok(refreshed, 'GET refresh button enabled')
  const refreshDeadline = Date.now() + 10000
  while (requests.filter(row => row.method === 'GET' && row.path.endsWith('/deep-review')).length <= beforeGets) {
    assert.ok(Date.now() < refreshDeadline, 'Timed out waiting for offline GET refresh')
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  const afterGets = requests.filter(row => row.method === 'GET' && row.path.endsWith('/deep-review')).length
  assert.ok(afterGets > beforeGets, 'refresh issued another GET')
  await evaluate(`window.__rankingReloadToken = 'old-document'`)
  await command('Page.reload', { ignoreCache: false })
  await waitFor(`window.__rankingReloadToken === undefined && document.querySelectorAll('.history-row').length > 0`, 'new document after page reload')
  assert.ok(await evaluate(`(() => { const row = document.querySelector('.history-row'); if (!row) return false; row.click(); return true })()`))
  await waitFor(`document.querySelectorAll('.ranking-item').length === 3`, 'ranking after page reload')
  const afterReloadGets = requests.filter(row => row.method === 'GET' && row.path.endsWith('/deep-review')).length
  assert.ok(afterReloadGets > afterGets, 'page reload restored the report with GET only')
  assert.equal(requests.filter(row => row.method === 'POST').length, 0, 'offline refresh and reload issued no POST')
  const text = await evaluate(`document.body.innerText`)
  for (const expected of ['推荐排名（确定性规则）', '符合要求', '待进一步核查', '存在硬约束冲突', '支持与反证并存',
    '偏好需求：不适用', '有效证据 2 / 4', '有效证据 1 / 1', 'README 中有有效证据支持该必需需求。',
    'README 明确说明不支持任务管理，构成硬约束冲突，不推荐。', 'This project does not support task management.',
    '证据不足不等于项目不具备该功能。', 'REPOSITORY_OUTCOME_UNKNOWN', '仅基于已保存 README']) assert.ok(text.includes(expected), expected)
  assert.ok(text.indexOf('第 1 名') < text.indexOf('第 2 名') && text.indexOf('第 2 名') < text.indexOf('第 3 名'))
  const counts = await evaluate(`({ ranking: document.querySelectorAll('.ranking-item').length,
    tiers: document.querySelectorAll('.ranking-tier').length, citations: document.querySelectorAll('.ranking-citation .source-quote').length,
    errors: document.querySelectorAll('.search-panel .error').length })`)
  assert.deepEqual(counts, { ranking: 3, tiers: 3, citations: 2, errors: 0 })
  assert.equal(exceptions.length, 0, 'browser runtime exceptions')
  const externalRequests = network.filter(row => !row.url.startsWith(apiOrigin)
    && !row.url.startsWith(`http://127.0.0.1:${vitePort}`) && !row.url.startsWith(`ws://127.0.0.1:${vitePort}`))
  assert.deepEqual(externalRequests, [], 'no external network request')
  await evaluate(`(() => { const report = document.querySelector('.ranking-view'); if (report) report.scrollIntoView({ block: 'start' }); return true })()`)
  await new Promise(resolve => setTimeout(resolve, 300))
  await mkdir(artifactDirectory, { recursive: true })
  const viewport = await command('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true })
  const screenshot = join(artifactDirectory, 'ranking-demo-full.png')
  await writeFile(screenshot, Buffer.from(viewport.data, 'base64'))
  const box = await evaluate(`(() => { const node = document.querySelector('.ranking-view'); if (!node) return null;
    const rect = node.getBoundingClientRect(); return { x: rect.left + scrollX, y: rect.top + scrollY, width: rect.width, height: rect.height, scale: 1 } })()`)
  let sectionScreenshot = null
  if (box) {
    const section = await command('Page.captureScreenshot', { format: 'png', clip: box, captureBeyondViewport: true })
    sectionScreenshot = join(artifactDirectory, 'ranking-demo-section.png')
    await writeFile(sectionScreenshot, Buffer.from(section.data, 'base64'))
  }
  const result = { passed: true, mode: 'offline-mock', external_api_calls: 0, api_requests: requests,
    ranking_rows: counts.ranking, tiers: counts.tiers, citations: counts.citations, browser_review_gets: afterReloadGets - beforeGets,
    browser_button_gets: afterGets - beforeGets, browser_reload_gets: afterReloadGets - afterGets,
    browser_posts: 0, runtime_exceptions: exceptions.length, screenshot, section_screenshot: sectionScreenshot }
  await writeFile(join(artifactDirectory, 'ranking-browser.json'), JSON.stringify(result, null, 2))
  console.log(JSON.stringify(result))
  await send('Browser.close')
} finally {
  socket?.close()
  if (browser?.exitCode === null) await new Promise(resolve => { browser.once('exit', resolve); setTimeout(() => { browser.kill(); resolve() }, 3000).unref() })
  vite.kill()
  await new Promise(resolve => apiServer.close(resolve))
  await rm(browserProfile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 })
}

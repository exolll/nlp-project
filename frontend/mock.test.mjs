import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

import { startMock } from './mock/server.mjs'

const compile = source => `data:text/javascript;base64,${Buffer.from(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText).toString('base64')}`
const load = async path => import(compile(await readFile(new URL(path, import.meta.url), 'utf8')))
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
const search = await load('./src/search.ts')

let mock
test.before(async () => { mock = await startMock(0) })
test.after(async () => { await mock.close() })

async function api(path, { method = 'GET', token, body, raw, headers = {}, port = mock.port } = {}) {
  const response = await fetch(`http://127.0.0.1:${port}/api/v1${path}`, {
    method,
    headers: {
      ...(raw ? { 'Content-Type': raw.contentType } : body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...headers,
    },
    ...(raw ? { body: raw.body } : body !== undefined ? { body: JSON.stringify(body) } : {}),
  })
  return { status: response.status, body: await response.json(), headers: response.headers }
}

function form(text) {
  const boundary = `----mock${Math.random().toString(16).slice(2)}`
  return {
    body: `--${boundary}\r\nContent-Disposition: form-data; name="text"\r\n\r\n${text}\r\n--${boundary}--\r\n`,
    contentType: `multipart/form-data; boundary=${boundary}`,
  }
}

async function session() {
  const created = await api('/sessions', { method: 'POST', body: {} })
  assert.equal(created.status, 201)
  assert.equal(created.headers.get('x-projectpilot-mock'), '1')
  return created.body.access_token
}

async function readyTask(token, text) {
  const task = await api('/tasks', { method: 'POST', token, body: {} })
  assert.equal(task.status, 201)
  const input = await api(`/tasks/${task.body.task_id}/input`, {
    method: 'POST', token, raw: form(text), headers: { 'Idempotency-Key': crypto.randomUUID() },
  })
  assert.equal(input.status, 202)
  assert.equal(input.body.status, 'processing')
  await sleep(1300)
  const status = await api(`/tasks/${task.body.task_id}/input/status`, { token })
  assert.equal(status.body.status, 'completed')
  return task.body.task_id
}

test('mock mirrors auth, errors, and never implements the pending endpoints', async () => {
  const unauthorized = await api('/tasks')
  assert.equal(unauthorized.status, 401)
  assert.equal(unauthorized.body.error_code, 'UNAUTHORIZED')
  assert.equal(unauthorized.body.retryable, false)
  assert.ok(unauthorized.body.request_id)
  assert.equal(unauthorized.headers.get('x-projectpilot-mock'), '1')

  const token = await session()
  const config = await api('/config', { token })
  assert.equal(config.status, 200)
  assert.deepEqual(Object.keys(config.body).sort(), [
    'environment', 'max_file_bytes', 'max_pdf_pages', 'max_primary_characters',
    'max_supplement_characters', 'storage_backend',
  ])

  const task = await api('/tasks', { method: 'POST', token, body: {} })
  const pending = [
    api(`/tasks/${task.body.task_id}`, { method: 'PATCH', token, body: { title: 'x' } }),
    api(`/tasks/${task.body.task_id}`, { method: 'DELETE', token }),
    api(`/tasks/${task.body.task_id}/search-runs`, { token }),
    api('/settings', { token }),
  ]
  for (const response of await Promise.all(pending)) {
    assert.equal(response.status, 404)
    assert.equal(response.body.error_code, 'INVALID_REQUEST')
    assert.equal(response.headers.get('x-projectpilot-mock'), '1')
  }
})

test('text input normalizes with code-point spans and is idempotent per key', async () => {
  const token = await session()
  const text = '我要做一个面向学生的开源项目发现与证据核查系统，需要 GitHub 仓库搜索与 README 证据引用，技术栈 FastAPI，偏好中文推荐说明。'
  const task = await api('/tasks', { method: 'POST', token, body: {} })
  const key = crypto.randomUUID()
  const first = await api(`/tasks/${task.body.task_id}/input`, { method: 'POST', token, raw: form(text), headers: { 'Idempotency-Key': key } })
  assert.equal(first.status, 202)
  assert.equal(first.body.error, null)

  const repeat = await api(`/tasks/${task.body.task_id}/input`, { method: 'POST', token, raw: form(text), headers: { 'Idempotency-Key': key } })
  assert.equal(repeat.status, 202)
  assert.equal(repeat.body.document_id, first.body.document_id)

  const changed = await api(`/tasks/${task.body.task_id}/input`, { method: 'POST', token, raw: form(`${text}（改）`), headers: { 'Idempotency-Key': crypto.randomUUID() } })
  assert.equal(changed.status, 409)
  assert.equal(changed.body.error_code, 'INPUT_CONFLICT')

  const notReady = await api(`/tasks/${task.body.task_id}/input`, { token })
  assert.equal(notReady.status, 409)
  assert.equal(notReady.body.retryable, true)

  await sleep(1300)
  const document = await api(`/tasks/${task.body.task_id}/input`, { token })
  assert.equal(document.status, 200)
  assert.equal(document.body.extraction_status, 'completed')
  assert.equal(document.body.input_mode, 'text')
  assert.equal(document.body.character_count, Array.from(text).length)
  for (const segment of document.body.source_segments) {
    const sliced = Array.from(document.body.normalized_text).slice(segment.start_offset, segment.end_offset).join('')
    assert.equal(sliced, segment.content)
  }
})

test('requirement, search, evidence and deep-review flows satisfy the frontend validators', async () => {
  const token = await session()
  const text = '课程项目：面向学生的开源项目发现与证据核查。需要 GitHub 仓库搜索与 README 证据引用。'
  const taskId = await readyTask(token, text)

  const extractionKey = crypto.randomUUID()
  const extraction = await api(`/documents/${(await api(`/tasks/${taskId}/input`, { token })).body.document_id}/requirement-extractions`, {
    method: 'POST', token, body: {}, headers: { 'Idempotency-Key': extractionKey },
  })
  assert.equal(extraction.status, 202)
  assert.equal(extraction.body.status, 'queued')
  assert.equal(extraction.body.model, 'deepseek-flash')

  const again = await api(`/documents/${(await api(`/tasks/${taskId}/input`, { token })).body.document_id}/requirement-extractions`, {
    method: 'POST', token, body: {}, headers: { 'Idempotency-Key': extractionKey },
  })
  assert.equal(again.body.extraction_id, extraction.body.extraction_id)

  await sleep(1600)
  const documentId = (await api(`/tasks/${taskId}/input`, { token })).body.document_id
  const requirements = await api(`/documents/${documentId}/requirements`, { token })
  assert.equal(requirements.status, 200)
  assert.equal(requirements.body.extraction.status, 'completed')
  assert.equal(requirements.body.revisions.length, 1)
  const revision = requirements.body.revisions[0]
  assert.equal(revision.status, 'extracted')
  assert.equal(requirements.body.latest_revision_id, revision.revision_id)

  const confirmed = await api(`/requirement-revisions/${revision.revision_id}/confirm`, { method: 'POST', token, body: {} })
  assert.equal(confirmed.status, 200)
  assert.equal(confirmed.body.status, 'confirmed')

  const payload = search.searchPayload(revision.revision_id)
  const runKey = crypto.randomUUID()
  const created = await api(`/tasks/${taskId}/search-runs`, { method: 'POST', token, body: payload, headers: { 'Idempotency-Key': runKey } })
  assert.equal(created.status, 202)
  assert.equal(created.body.status, 'reserved')
  const retried = await api(`/tasks/${taskId}/search-runs`, { method: 'POST', token, body: payload, headers: { 'Idempotency-Key': runKey } })
  assert.equal(retried.body.run_id, created.body.run_id)
  const runId = created.body.run_id

  const earlyReview = await api(`/tasks/${taskId}/search-runs/${runId}/deep-review`, { token })
  assert.equal(earlyReview.status, 404)
  assert.equal(earlyReview.body.error_code, 'DEEP_REVIEW_NOT_FOUND')

  await sleep(1000)
  const run = await api(`/tasks/${taskId}/search-runs/${runId}`, { token })
  assert.equal(run.body.status, 'completed')
  assert.equal(run.body.evidence_status, 'published')
  assert.equal(run.body.retrieval_status, 'ready')
  assert.equal(run.body.dispatch_closed, true)

  const evidence = await api(`/tasks/${taskId}/search-runs/${runId}/evidence`, { token })
  assert.equal(evidence.status, 200)
  assert.equal(evidence.body.candidates.length, 2)

  const retrieval = await api(`/tasks/${taskId}/search-runs/${runId}/retrieval-evidence`, { token })
  assert.equal(retrieval.status, 200)
  assert.equal(retrieval.body.candidates.length, 2)
  assert.ok(retrieval.body.candidates[0].items.some(item => item.chunks.length > 0))
  const filtered = await api(`/tasks/${taskId}/search-runs/${runId}/retrieval-evidence?repository_id=1002`, { token })
  assert.deepEqual(filtered.body.candidates.map(row => row.repository_id), ['1002'])
  assert.equal(filtered.body.filters.repository_id, '1002')

  const deepPost = await api(`/tasks/${taskId}/search-runs/${runId}/deep-check`, {
    method: 'POST', token, body: { revision_id: revision.revision_id, consent: true }, headers: { 'Idempotency-Key': crypto.randomUUID() },
  })
  assert.equal(deepPost.status, 202)
  assert.equal(deepPost.body.deep_check.published, false)
  await sleep(950)
  const deep = await api(`/tasks/${taskId}/search-runs/${runId}/deep-check`, { token })
  assert.equal(deep.status, 200)
  assert.equal(deep.body.candidates.length, 2)

  const reviewPost = await api(`/tasks/${taskId}/search-runs/${runId}/deep-review`, {
    method: 'POST', token, body: { revision_id: revision.revision_id, consent: true },
  })
  assert.equal(reviewPost.status, 202)
  assert.equal(reviewPost.body.status, 'running')

  await sleep(1700)
  const review = await api(`/tasks/${taskId}/search-runs/${runId}/deep-review`, { token })
  assert.equal(review.status, 200)
  const operation = search.validateDeepReview(review.body, taskId, revision.revision_id, runId)
  assert.equal(operation.status, 'completed')
  assert.ok(operation.report)
  assert.equal(operation.report.repositories.length, 2)
  assert.equal(operation.report.ranking.ranking_config_version, 'hard-tier-preferred-v1')
  assert.equal(operation.report.ranking.repositories[0].hard_constraint_tier, 'meets_requirements')
  assert.equal(operation.report.ranking.repositories[0].recommendation.status, 'recommended')
})

test('opt-in failure scenarios are one-time per session and recover on retry', async () => {
  const scenarioMock = await startMock(0, { scenarios: '1' })
  const call = (path, options = {}) => api(path, { ...options, port: scenarioMock.port })
  try {
    const token = (await call('/sessions', { method: 'POST', body: {} })).body.access_token

    const first = await call('/tasks', { method: 'POST', token, body: {} })
    const key = crypto.randomUUID()
    const material = form('演示场景：第一次上传应收到 503。')
    const failedUpload = await call(`/tasks/${first.body.task_id}/input`, {
      method: 'POST', token, raw: material, headers: { 'Idempotency-Key': key },
    })
    assert.equal(failedUpload.status, 503)
    assert.equal(failedUpload.body.error_code, 'UPLOAD_UNAVAILABLE')
    assert.equal(failedUpload.body.retryable, true)
    const retried = await call(`/tasks/${first.body.task_id}/input`, {
      method: 'POST', token, raw: material, headers: { 'Idempotency-Key': key },
    })
    assert.equal(retried.status, 202)

    await sleep(1300)
    const parseStatus = await call(`/tasks/${first.body.task_id}/input/status`, { token })
    assert.equal(parseStatus.body.status, 'failed')
    assert.equal(parseStatus.body.error.error_code, 'INPUT_PARSE_FAILED')
    assert.equal(parseStatus.body.error.retryable, false)
    const readFailed = await call(`/tasks/${first.body.task_id}/input`, { token })
    assert.equal(readFailed.status, 422)
    assert.equal(readFailed.body.error_code, 'INPUT_PARSE_FAILED')

    const second = await call('/tasks', { method: 'POST', token, body: {} })
    const upload = await call(`/tasks/${second.body.task_id}/input`, {
      method: 'POST', token, raw: form('第二次材料应能正常解析，并允许重新提取。'), headers: { 'Idempotency-Key': crypto.randomUUID() },
    })
    assert.equal(upload.status, 202)
    await sleep(1300)
    const documentId = (await call(`/tasks/${second.body.task_id}/input`, { token })).body.document_id

    const firstExtraction = await call(`/documents/${documentId}/requirement-extractions`, {
      method: 'POST', token, body: {}, headers: { 'Idempotency-Key': crypto.randomUUID() },
    })
    assert.equal(firstExtraction.status, 202)
    await sleep(1600)
    const failedExtraction = await call(`/documents/${documentId}/requirements`, { token })
    assert.equal(failedExtraction.body.extraction.status, 'failed')
    assert.equal(failedExtraction.body.extraction.error.error_code, 'EXTRACTION_FAILED')
    assert.equal(failedExtraction.body.extraction.error.retryable, true)
    assert.equal(failedExtraction.body.revisions.length, 0)

    const retryExtraction = await call(`/documents/${documentId}/requirement-extractions`, {
      method: 'POST', token, body: {}, headers: { 'Idempotency-Key': crypto.randomUUID() },
    })
    assert.equal(retryExtraction.status, 202)
    assert.notEqual(retryExtraction.body.extraction_id, firstExtraction.body.extraction_id)
    await sleep(1600)
    const recovered = await call(`/documents/${documentId}/requirements`, { token })
    assert.equal(recovered.body.extraction.status, 'completed')
    assert.equal(recovered.body.extraction.error, null)
    assert.equal(recovered.body.revisions.length, 1)
  } finally {
    await scenarioMock.close()
  }
})

test('revision edits append a draft and stale parents are rejected', async () => {
  const token = await session()
  const taskId = await readyTask(token, '需要一个 GitHub 仓库搜索工具。')
  const documentId = (await api(`/tasks/${taskId}/input`, { token })).body.document_id
  await api(`/documents/${documentId}/requirement-extractions`, {
    method: 'POST', token, body: {}, headers: { 'Idempotency-Key': crypto.randomUUID() },
  })
  await sleep(1600)
  const requirements = await api(`/documents/${documentId}/requirements`, { token })
  const parent = requirements.body.revisions[0]
  const payload = {
    requirements: {
      ...parent.requirements,
      missing_information: ['部署环境'],
    },
    requirement_items: parent.requirement_items,
    change_reason: '补充部署环境的缺失信息',
  }
  const child = await api(`/requirement-revisions/${parent.revision_id}/revisions`, { method: 'POST', token, body: payload })
  assert.equal(child.status, 201)
  assert.equal(child.body.status, 'draft')
  assert.equal(child.body.revision_number, 2)
  assert.equal(child.body.parent_revision_id, parent.revision_id)
  assert.deepEqual(child.body.requirements.missing_information, ['部署环境'])

  const stale = await api(`/requirement-revisions/${parent.revision_id}/revisions`, { method: 'POST', token, body: payload })
  assert.equal(stale.status, 409)
  assert.equal(stale.body.error_code, 'STALE_REVISION')

  const emptyReason = await api(`/requirement-revisions/${child.body.revision_id}/revisions`, {
    method: 'POST', token, body: { ...payload, change_reason: '   ' },
  })
  assert.equal(emptyReason.status, 422)
  assert.equal(emptyReason.body.error_code, 'INVALID_REVISION')
})
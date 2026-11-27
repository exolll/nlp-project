#!/usr/bin/env node
// 仅开发/测试使用的本地 Mock Server：严格镜像现有 19 条后端路由与响应结构，
// 不实现任何尚未存在的接口（改名 / 删除任务 / 运行列表 / 会话持久化 / 模型配置 / 连通性测试 / 配置详情）。
// 所有响应都带 X-ProjectPilot-Mock: 1，仅用于前端独立联调与预览。
import { createHash, randomBytes, randomUUID } from 'node:crypto'
import { createServer } from 'node:http'
import { pathToFileURL } from 'node:url'

import {
  REPO_A, REPO_B, demoConfig, demoDeepCheckReport, demoDeepReviewReport, demoDocument,
  demoRetrievalBundle, demoRevision, demoSearchEvidence,
} from './fixtures.mjs'

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const sessionHours = 24
const maxRequestBytes = 12 * 1024 * 1024
const sha256 = value => createHash('sha256').update(value).digest('hex')
const iso = ms => new Date(ms).toISOString()

class MockError extends Error {
  constructor(status, code, message, retryable = false) {
    super(message)
    this.status = status
    this.code = code
    this.retryable = retryable
  }
}

const fail = (status, code, message, retryable = false) => { throw new MockError(status, code, message, retryable) }

const sessions = new Map()

// 仅当 MOCK_SCENARIOS=1（或列出 upload503/parse_fail/extract_fail）时启用演示失败场景；
// 每个会话各触发一次，便于验证重试路径。默认关闭，不影响正常演示。
function normalizeScenarios(value) {
  const text = Array.isArray(value) ? value.join(',') : String(value ?? '').trim()
  const all = text === '1'
  const list = text.split(/[,\s]+/).filter(Boolean)
  return {
    upload: all || list.includes('upload503'),
    parse: all || list.includes('parse_fail'),
    extract: all || list.includes('extract_fail'),
  }
}

function owner(req) {
  const match = /^Bearer\s+(.+)$/i.exec(req.headers.authorization || '')
  if (!match) fail(401, 'UNAUTHORIZED', '请先创建有效会话。')
  const key = sha256(match[1])
  const session = sessions.get(key)
  if (!session || session.expires_at <= Date.now()) {
    sessions.delete(key)
    fail(401, 'UNAUTHORIZED', '会话无效或已过期。')
  }
  return session
}

function findTask(session, taskId) {
  const task = session.tasks.get(taskId)
  if (!task) fail(404, 'NOT_FOUND', '任务不存在。')
  return task
}

function findDocumentTask(session, documentId) {
  for (const task of session.tasks.values()) {
    if (task.document?.id === documentId) return task
  }
  fail(404, 'NOT_FOUND', '文档不存在。')
}

function findRevision(session, revisionId) {
  for (const task of session.tasks.values()) {
    const revision = task.requirements?.revisions.find(row => row.revision_id === revisionId)
    if (revision) return { task, revision }
  }
  return null
}

const taskStatus = task => !task.document ? 'empty'
  : task.document.parse_failed && Date.now() >= task.document.ready_at ? 'failed'
  : Date.now() >= task.document.ready_at ? 'completed' : 'processing'

const taskResponse = task => ({
  task_id: task.id, created_at: task.created_at, status: taskStatus(task),
  document_id: task.document?.id ?? null,
})

const inputStatus = (task, requestId = '') => {
  const status = taskStatus(task)
  return {
    task_id: task.id, document_id: task.document?.id ?? null, status,
    error: status === 'failed' ? {
      error_code: 'INPUT_PARSE_FAILED', message: '演示场景：材料解析失败，无法读取文档内容。请新建任务重试（仅 Mock）。',
      retryable: false, details: null, request_id: requestId,
    } : null,
  }
}

const requirementsOf = task => (task.requirements ??= { extractions: [], revisions: [] })

function jobStatus(record) {
  const elapsed = Date.now() - record.created_at
  if (record.fail_extraction) return elapsed < 500 ? 'queued' : elapsed < 1100 ? 'extracting' : elapsed < 1500 ? 'validating' : 'failed'
  return elapsed < 500 ? 'queued' : elapsed < 1100 ? 'extracting' : elapsed < 1500 ? 'validating' : 'completed'
}

function jobView(record) {
  const status = jobStatus(record)
  return {
    extraction_id: record.extraction_id, status,
    error: status === 'failed' ? {
      error_code: 'EXTRACTION_FAILED', message: '演示场景：首次提取失败（仅 Mock）。可显式重新提取。',
      retryable: true, details: null, request_id: '',
    } : null,
    model: record.model, prompt_version: record.prompt_version,
    usage: status === 'completed' ? record.usage : null, created_at: iso(record.created_at),
  }
}

function syncExtraction(task) {
  const aggregate = requirementsOf(task)
  if (aggregate.revisions.length) return
  const record = aggregate.extractions.at(-1)
  if (!record || record.fail_extraction || Date.now() < record.ready_at) return
  aggregate.revisions.push(demoRevision({
    revisionId: randomUUID(), taskId: task.id, document: task.document.result,
  }))
  record.usage = { prompt_tokens: 1200, completion_tokens: 420, total_tokens: 1620 }
}

function runPhase(run) {
  return Date.now() - run.created_at < 900 ? 'running' : 'completed'
}

function deepState(run) {
  const record = run.deep_check
  if (!record) return { status: 'not_started', published: false, integrity_blocked: false, error_code: null }
  const done = Date.now() - record.authorized_at >= 900
  return { status: done ? 'completed' : 'dispatching', published: done, integrity_blocked: false, error_code: null }
}

function runView(run, fresh = false) {
  const base = {
    run_id: run.run_id, task_id: run.task_id,
    confirmed_revision_id: run.confirmed_revision_id, retryable: false,
  }
  if (fresh) return { ...base, status: 'reserved', pipeline_status: 'reserved', pipeline_error_code: null, deep_check: deepState(run) }
  const completed = runPhase(run) === 'completed'
  return {
    ...base,
    status: completed ? 'completed' : 'running',
    pipeline_status: completed ? 'completed' : 'running',
    pipeline_error_code: null,
    search_status: completed ? 'completed' : 'running',
    search_stop_reasons: [],
    error_code: null,
    operation_failures: [],
    plan_status: 'published',
    dispatch_closed: completed,
    evidence_status: completed ? 'published' : 'not_ready',
    retrieval_status: completed ? 'ready' : 'running',
    retrieval_error_code: null,
    screening: null,
    candidates: [
      { repository_id: REPO_A, collection_status: completed ? 'saved' : 'pending' },
      { repository_id: REPO_B, collection_status: completed ? 'saved' : 'pending' },
    ],
    deep_check: deepState(run),
  }
}

function operationView(session, run) {
  const operation = run.deep_review
  const completed = Date.now() - operation.authorized_at >= 1600
  const status = completed ? 'completed' : 'running'
  const found = findRevision(session, operation.revision_id)
  if (completed && !found) fail(409, 'DEEP_REVIEW_INTEGRITY', '深度核查报告与已授权运行不一致。')
  const report = completed ? demoDeepReviewReport({
    taskId: run.task_id, runId: run.run_id, revision: found.revision,
    operation: { ...operation, status },
  }) : null
  return {
    operation_id: operation.operation_id, status, persisted_status: status,
    revision_id: operation.revision_id, revision_hash: operation.revision_hash,
    bundle_hash: operation.bundle_hash, model: operation.model, prompt_version: operation.prompt_version,
    consent: true, authorized_at: iso(operation.authorized_at), deadline: iso(operation.deadline),
    report_key: completed ? `deep-reviews/${operation.operation_id}/report.json` : null,
    report_hash: completed ? sha256(`${operation.operation_id}:report`) : null,
    error_code: null, report,
  }
}

function idempotencyKey(req) {
  const value = String(req.headers['idempotency-key'] ?? '')
  if (!UUID.test(value)) fail(422, 'INVALID_IDEMPOTENCY_KEY', '必须提供 UUID 格式的 Idempotency-Key。')
  return value
}

async function readBody(req) {
  const chunks = []
  let size = 0
  for await (const chunk of req) {
    size += chunk.length
    if (size > maxRequestBytes) fail(413, 'BODY_TOO_LARGE', '请求体超过允许上限。')
    chunks.push(chunk)
  }
  return Buffer.concat(chunks)
}

async function readJson(req) {
  const raw = await readBody(req)
  if (!raw.length) return {}
  try {
    return JSON.parse(raw.toString('utf8'))
  } catch {
    fail(422, 'INVALID_REQUEST', '请求字段或格式不正确。')
  }
}

function parseMultipart(buffer, contentType) {
  const header = /boundary=(?:"([^"]+)"|([^;]+))/i.exec(contentType || '')
  if (!header) fail(422, 'INVALID_MULTIPART', '表单格式无效，只允许一个文件和一个文本字段。')
  const delimiter = `--${(header[1] || header[2]).trim()}`
  const parts = buffer.toString('latin1').split(delimiter).slice(1)
  const fields = []
  for (const part of parts) {
    if (part.startsWith('--')) break
    const trimmed = part.startsWith('\r\n') ? part.slice(2) : part
    const split = trimmed.indexOf('\r\n\r\n')
    if (split < 0) continue
    let body = trimmed.slice(split + 4)
    if (body.endsWith('\r\n')) body = body.slice(0, -2)
    const name = /name="([^"]*)"/i.exec(trimmed.slice(0, split))
    const filename = /filename="([^"]*)"/i.exec(trimmed.slice(0, split))
    if (!name) continue
    fields.push({ name: name[1], filename: filename ? filename[1] : null, data: Buffer.from(body, 'latin1') })
  }
  return fields
}

function validBudget(budget) {
  if (!budget || typeof budget !== 'object') return false
  const { model_calls, github_requests, search_requests = 5, readme_candidates, per_page = 5, max_pages = 1 } = budget
  const int = (value, low, high) => Number.isInteger(value) && value >= low && value <= high
  return model_calls === 1 && int(github_requests, 1, 20) && int(search_requests, 1, 5)
    && int(readme_candidates, 0, 5) && int(per_page, 1, 5) && max_pages === 1
    && github_requests >= search_requests + 2 * readme_candidates
}

function sameSignature(left, right) {
  return left.field === right.field && left.text === right.text && left.modality === right.modality
    && left.operator === right.operator && left.logic_group_id === right.logic_group_id
}

function buildRevision(task, parent, body, revisionId, createdAt) {
  const aggregate = requirementsOf(task)
  if (aggregate.revisions.at(-1)?.revision_id !== parent.revision_id) fail(409, 'STALE_REVISION', '版本已更新，请刷新后基于最新版本编辑。')
  if (aggregate.revisions.length >= 50) fail(409, 'REVISION_LIMIT', '需求版本数已达上限，不会截断历史。')
  if (!body || typeof body.requirements !== 'object' || !Array.isArray(body.requirement_items)) {
    fail(422, 'INVALID_REVISION', '需求项覆盖、身份、属性或引用无效。')
  }
  const oldById = new Map(parent.requirement_items.map(item => [item.requirement_item_id, item]))
  const oldEvidence = new Map(parent.requirements.source_evidence.map(item => [item.evidence_id, item]))
  const items = []
  const evidence = new Map()
  for (const proposed of body.requirement_items) {
    const previous = proposed.requirement_item_id ? oldById.get(proposed.requirement_item_id) : null
    if (proposed.requirement_item_id && !previous) fail(422, 'INVALID_REVISION', '不能伪造已有需求项 ID。')
    if (previous && sameSignature(previous, proposed) && previous.value_path === proposed.value_path) {
      items.push({ ...previous, value_path: proposed.value_path })
      for (const id of previous.evidence_ids) {
        const row = oldEvidence.get(id)
        if (row) evidence.set(id, row)
      }
      continue
    }
    const itemId = randomUUID()
    const evidenceId = randomUUID()
    items.push({
      ...proposed, requirement_item_id: itemId,
      supersedes_item_id: previous ? previous.requirement_item_id : null,
      source_kind: 'user_revision', evidence_ids: [evidenceId],
    })
    evidence.set(evidenceId, {
      evidence_id: evidenceId, field: proposed.field, original_text: proposed.text,
      source_kind: 'user_revision', source_location: `revision:${revisionId}:${proposed.value_path}`,
      requirement_item_id: itemId, segment_id: null, start_offset: null, end_offset: null,
    })
  }
  const requirementIds = items.map(item => item.requirement_item_id)
  return {
    revision_id: revisionId, task_id: task.id, revision_number: parent.revision_number + 1,
    parent_revision_id: parent.revision_id, status: 'draft',
    requirements: { ...body.requirements, source_evidence: [...evidence.values()] },
    requirement_items: items,
    change_log: [{
      field: 'revision', old_value: { revision_number: parent.revision_number }, new_value: { revision_number: parent.revision_number + 1 },
      reason: body.change_reason.trim(), original_text: JSON.stringify({ change_reason: body.change_reason }),
      created_at: createdAt, requirement_item_ids: requirementIds,
    }],
    created_at: createdAt, confirmed_at: null,
  }
}

function unresolved(revision) {
  if (revision.requirements.uncertain_information?.length) return true
  const languages = revision.requirement_items.filter(item => item.field === 'programming_languages' && item.modality === 'required')
  if (languages.length < 2) return false
  const first = languages[0]
  return languages.some(item => !item.logic_group_id || item.operator === 'not_applicable'
    || item.logic_group_id !== first.logic_group_id || item.operator !== first.operator)
}

function route(req, res, url, requestId) {
  const send = (status, body) => {
    const payload = JSON.stringify(body)
    res.writeHead(status, {
      'content-type': 'application/json; charset=utf-8',
      'content-length': Buffer.byteLength(payload),
      'x-request-id': requestId,
      'x-projectpilot-mock': '1',
      'access-control-allow-origin': '*',
    })
    res.end(payload)
  }
  return { send }
}

async function handle(req, res, requestId, scenario) {
  const url = new URL(req.url, `http://${req.headers.host || '127.0.0.1'}`)
  const method = req.method
  const { send } = route(req, res, url, requestId)
  if (method === 'OPTIONS') {
    res.writeHead(204, {
      'x-projectpilot-mock': '1', 'x-request-id': requestId, 'access-control-allow-origin': '*',
      'access-control-allow-methods': 'GET, POST, OPTIONS',
      'access-control-allow-headers': 'Authorization, Content-Type, Idempotency-Key',
    })
    return res.end()
  }
  const segment = url.pathname.replace(/\/+$/, '').split('/').filter(Boolean)
  if (segment[0] !== 'api' || segment[1] !== 'v1') fail(404, 'INVALID_REQUEST', '请求无效或资源不存在。')
  const [, , first, second, third, fourth, fifth] = segment
  const length = segment.length

  if (first === 'config' && length === 3) {
    if (method !== 'GET') fail(404, 'INVALID_REQUEST', '请求无效或资源不存在。')
    return send(200, demoConfig)
  }

  if (first === 'sessions' && length === 3) {
    if (method !== 'POST') fail(404, 'INVALID_REQUEST', '请求无效或资源不存在。')
    await readBody(req)
    const accessToken = randomBytes(32).toString('base64url')
    const expiresAt = Date.now() + sessionHours * 3600 * 1000
    sessions.set(sha256(accessToken), {
      expires_at: expiresAt, tasks: new Map(), scenarios: { upload: 0, parse: 0, extract: 0 },
    })
    return send(201, { access_token: accessToken, expires_at: iso(expiresAt) })
  }

  if (first === 'tasks' && length === 3) {
    const session = owner(req)
    if (method === 'GET') {
      const rows = [...session.tasks.values()]
        .sort((left, right) => right.created_at.localeCompare(left.created_at) || right.id.localeCompare(left.id))
      return send(200, { tasks: rows.map(taskResponse) })
    }
    if (method === 'POST') {
      await readJson(req)
      if (session.tasks.size >= 100) fail(429, 'TASK_LIMIT', '会话任务数已达上限，请创建新会话。')
      const task = { id: randomUUID(), created_at: iso(Date.now()), document: null, requirements: null, search_runs: [] }
      session.tasks.set(task.id, task)
      return send(201, taskResponse(task))
    }
    fail(404, 'INVALID_REQUEST', '请求无效或资源不存在。')
  }

  if (first === 'tasks' && length === 5 && third === 'input' && method === 'POST') {
    const session = owner(req)
    const task = findTask(session, second)
    if (!String(req.headers['content-type'] ?? '').toLowerCase().startsWith('multipart/form-data')) {
      fail(415, 'INVALID_CONTENT_TYPE', '请使用 multipart/form-data 提交。')
    }
    const key = idempotencyKey(req)
    const fields = parseMultipart(await readBody(req), req.headers['content-type'])
    if (!fields.length || fields.some(field => !['file', 'text'].includes(field.name))
        || new Set(fields.map(field => field.name)).size !== fields.length) {
      fail(422, 'INVALID_FIELDS', '只允许 file 和 text 字段，且不得重复。')
    }
    const upload = fields.find(field => field.name === 'file')
    const textField = fields.find(field => field.name === 'text')
    if (upload && upload.filename === null) fail(422, 'INVALID_FIELDS', '文件或文本字段类型错误。')
    if (upload?.filename && upload.filename.length > 255) fail(422, 'INVALID_FILENAME', '文件名过长。')
    const text = textField ? textField.data.toString('utf8') : ''
    const filename = upload?.filename ?? null
    if (upload && upload.data.length > demoConfig.max_file_bytes) fail(413, 'FILE_SIZE_LIMIT', '文件大小超过允许上限。')
    if (!upload && !text.trim()) fail(422, 'INVALID_REQUEST', '材料不能为空，请提交文件或文本。')
    const limit = upload ? demoConfig.max_supplement_characters : demoConfig.max_primary_characters
    if (Array.from(text).length > limit) fail(422, 'INVALID_REQUEST', '文本超过允许长度。')
    const digest = sha256(JSON.stringify({
      file_sha256: upload ? sha256(upload.data) : null, text, filename,
    }))
    if (task.document) {
      if (task.document.idempotency_key !== key || task.document.request_hash !== digest) {
        fail(409, 'INPUT_CONFLICT', '任务已有不同的输入，请新建任务。')
      }
      return send(202, inputStatus(task, requestId))
    }
    if (scenario.upload && session.scenarios.upload < 1) {
      session.scenarios.upload = 1
      fail(503, 'UPLOAD_UNAVAILABLE', '演示场景：上传服务暂时不可用（仅 Mock），未保存任何材料；重试同一份材料即可恢复。', true)
    }
    const extension = filename?.toLowerCase().endsWith('.pdf') ? 'pdf' : filename ? 'docx' : null
    const mimeType = extension === 'pdf' ? 'application/pdf'
      : extension === 'docx' ? 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' : 'text/plain'
    const documentId = randomUUID()
    const parseFailed = scenario.parse && session.scenarios.parse < 1
    if (parseFailed) session.scenarios.parse = 1
    task.document = {
      id: documentId, idempotency_key: key, request_hash: digest,
      original_filename: filename, created_at: Date.now(), ready_at: Date.now() + 1200,
      parse_failed: parseFailed,
      result: demoDocument({
        documentId, taskId: task.id, filename, text, mimeType,
        fileSize: upload ? upload.data.length : Buffer.byteLength(text),
      }),
    }
    return send(202, inputStatus(task, requestId))
  }

  if (first === 'tasks' && length === 6 && third === 'input' && fourth === 'status') {
    const session = owner(req)
    if (method !== 'GET') fail(404, 'INVALID_REQUEST', '请求无效或资源不存在。')
    return send(200, inputStatus(findTask(session, second), requestId))
  }

  if (first === 'documents' && length === 5 && third === 'requirement-extractions') {
    const session = owner(req)
    if (method !== 'POST') fail(404, 'INVALID_REQUEST', '请求无效或资源不存在。')
    const key = idempotencyKey(req)
    await readBody(req)
    const task = findDocumentTask(session, second)
    const aggregate = requirementsOf(task)
    const existing = aggregate.extractions.find(record => record.idempotency_key === key)
    if (existing) return send(202, jobView(existing))
    if (aggregate.revisions.length || aggregate.extractions.some(record => {
      const status = jobStatus(record)
      return status !== 'completed' && !(status === 'failed' && record.fail_extraction)
    })) {
      fail(409, 'EXTRACTION_CONFLICT', '已有有效提取或正在提取；请编辑版本或等待结果。')
    }
    if (aggregate.extractions.length >= 3) fail(429, 'TASK_EXTRACTION_LIMIT', '此任务提取次数已达上限。')
    if (taskStatus(task) !== 'completed') fail(409, 'INPUT_NOT_READY', '文档尚未完成处理。')
    const createdAt = Date.now()
    const failExtraction = scenario.extract && session.scenarios.extract < 1
    if (failExtraction) session.scenarios.extract = 1
    const record = {
      extraction_id: randomUUID(), idempotency_key: key, created_at: createdAt,
      fail_extraction: failExtraction,
      ready_at: createdAt + 1500, model: 'deepseek-flash', prompt_version: 'mock-prompt-v1', usage: null,
    }
    aggregate.extractions.push(record)
    return send(202, jobView(record))
  }

  if (first === 'documents' && length === 5 && third === 'requirements') {
    const session = owner(req)
    if (method !== 'GET') fail(404, 'INVALID_REQUEST', '请求无效或资源不存在。')
    const task = findDocumentTask(session, second)
    syncExtraction(task)
    const aggregate = requirementsOf(task)
    const latest = aggregate.extractions.at(-1)
    return send(200, {
      extraction: latest ? jobView(latest) : null,
      revisions: aggregate.revisions,
      latest_revision_id: aggregate.revisions.at(-1)?.revision_id ?? null,
    })
  }

  if (first === 'requirement-revisions' && length === 5 && (third === 'revisions' || third === 'confirm')) {
    const session = owner(req)
    const found = findRevision(session, second)
    if (!found) fail(404, 'NOT_FOUND', '需求版本不存在。')
    if (method !== 'POST') fail(404, 'INVALID_REQUEST', '请求无效或资源不存在。')
    const { task, revision } = found
    if (third === 'confirm') {
      await readBody(req)
      if (unresolved(revision)) fail(409, 'REQUIREMENTS_UNRESOLVED', '请先编辑消除歧义，并明确多语言硬要求的同组 ALL / ANY 关系。')
      if (requirementsOf(task).revisions.at(-1)?.revision_id !== revision.revision_id) {
        fail(409, 'STALE_REVISION', '只能确认最新需求版本。')
      }
      revision.status = 'confirmed'
      revision.confirmed_at = iso(Date.now())
      return send(200, revision)
    }
    const body = await readJson(req)
    if (typeof body.change_reason !== 'string' || !body.change_reason.trim()) {
      fail(422, 'INVALID_REVISION', '请填写具体修改原因。')
    }
    const createdAt = iso(Date.now())
    const child = buildRevision(task, revision, body, randomUUID(), createdAt)
    requirementsOf(task).revisions.push(child)
    return send(201, child)
  }

  if (first === 'tasks' && second && third === 'search-runs') {
    const session = owner(req)
    const task = findTask(session, second)

    if (length === 5 && method === 'POST') {
      const body = await readJson(req)
      if (body.consent !== true) fail(422, 'CONSENT_REQUIRED', '必须明确同意本次模型与 GitHub 外部调用及预算。')
      const key = idempotencyKey(req)
      if (!validBudget(body.budget)) fail(422, 'INVALID_REQUEST', '请求字段或格式不正确。')
      const revision = task.requirements?.revisions.find(row => row.revision_id === body.revision_id)
      if (!revision) fail(404, 'NOT_FOUND', '需求版本不存在。')
      if (revision.status !== 'confirmed') fail(409, 'REVISION_UNCONFIRMED', '必须显式选择已确认的需求版本。')
      const digest = sha256(JSON.stringify(body))
      const existing = task.search_runs.find(row => row.key === key)
      if (existing) {
        if (existing.request_hash !== digest) fail(409, 'IDEMPOTENCY_CONFLICT', '同一幂等键不能用于不同请求。')
        return send(202, runView(existing))
      }
      if (task.search_runs.length >= 3) fail(429, 'SEARCH_RUN_LIMIT', '每个任务最多允许三次搜索运行。')
      const run = {
        key, run_id: randomUUID(), task_id: task.id, confirmed_revision_id: revision.revision_id,
        request_hash: digest, created_at: Date.now(), deep_check: null, deep_review: null,
      }
      task.search_runs.push(run)
      return send(202, runView(run, true))
    }

    if (!fourth) fail(404, 'INVALID_REQUEST', '请求无效或资源不存在。')
    const run = task.search_runs.find(row => row.run_id === fourth)
    if (!run) fail(404, 'NOT_FOUND', '搜索运行不存在。')

    if (length === 6 && method === 'GET') return send(200, runView(run))

    const revision = task.requirements?.revisions.find(row => row.revision_id === run.confirmed_revision_id)

    if (length === 7 && fifth === 'evidence') {
      if (method === 'GET') {
        if (runPhase(run) !== 'completed') fail(409, 'EVIDENCE_NOT_READY', '证据报告尚未就绪。')
        return send(200, demoSearchEvidence({ taskId: task.id, runId: run.run_id, revision }))
      }
      if (method === 'POST') {
        await readBody(req)
        if (runPhase(run) !== 'completed') fail(409, 'EVIDENCE_NOT_READY', '正式快照尚未稳定，不会续跑外部调用。')
        return send(200, demoSearchEvidence({ taskId: task.id, runId: run.run_id, revision }))
      }
    }

    if (length === 7 && fifth === 'retrieval-evidence' && method === 'GET') {
      if (runPhase(run) !== 'completed') fail(409, 'RETRIEVAL_NOT_READY', '检索证据尚未生成，请稍后查询状态。')
      const bundle = demoRetrievalBundle({ taskId: task.id, runId: run.run_id, revision })
      const repositoryId = url.searchParams.get('repository_id')
      const requirementItemId = url.searchParams.get('requirement_item_id')
      if (requirementItemId !== null) {
        if (!UUID.test(requirementItemId) || !bundle.requirements.some(row => row.requirement_item_id === requirementItemId)) {
          fail(404, 'RETRIEVAL_FILTER_NOT_FOUND', '该需求条目不在本次运行中。')
        }
        bundle.requirements = bundle.requirements.filter(row => row.requirement_item_id === requirementItemId)
        bundle.candidates = bundle.candidates.map(row => ({
          ...row, items: row.items.filter(item => item.requirement_item_id === requirementItemId),
        }))
      }
      if (repositoryId !== null) {
        if (!bundle.candidates.some(row => row.repository_id === repositoryId)) {
          fail(404, 'RETRIEVAL_FILTER_NOT_FOUND', '该仓库不在本次运行中。')
        }
        bundle.candidates = bundle.candidates.filter(row => row.repository_id === repositoryId)
      }
      bundle.filters = {
        repository_id: repositoryId === null ? null : repositoryId,
        requirement_item_id: requirementItemId === null ? null : requirementItemId,
      }
      return send(200, bundle)
    }

    if (length === 7 && fifth === 'deep-check') {
      if (method === 'GET') {
        if (!deepState(run).published) fail(409, 'DEEP_CHECK_NOT_READY', '深度核对结果尚未就绪。')
        return send(200, demoDeepCheckReport({ taskId: task.id, runId: run.run_id, revision }))
      }
      if (method === 'POST') {
        const body = await readJson(req)
        if (body.consent !== true) fail(422, 'CONSENT_REQUIRED', '必须单独明确同意本次深度核对的模型调用。')
        const key = idempotencyKey(req)
        const digest = sha256(JSON.stringify(body))
        if (run.deep_check) {
          if (run.deep_check.idempotency_key !== key || run.deep_check.request_hash !== digest) {
            fail(409, 'IDEMPOTENCY_CONFLICT', '一个搜索运行只允许一次深度核对授权。')
          }
          return send(202, runView(run))
        }
        if (runPhase(run) !== 'completed') fail(409, 'DEEP_CHECK_NOT_READY', '正式快照尚未稳定，不会发起模型调用。')
        run.deep_check = { idempotency_key: key, request_hash: digest, authorized_at: Date.now() }
        return send(202, runView(run))
      }
    }

    if (length === 7 && fifth === 'deep-review') {
      if (method === 'GET') {
        if (!run.deep_review) fail(404, 'DEEP_REVIEW_NOT_FOUND', '该搜索运行尚未授权深度核查。')
        return send(200, operationView(session, run))
      }
      if (method === 'POST') {
        const body = await readJson(req)
        if (body.consent !== true) fail(422, 'CONSENT_REQUIRED', '必须单独明确同意本次深度核查的模型调用与费用。')
        if (body.revision_id !== run.confirmed_revision_id) {
          fail(409, 'DEEP_REVIEW_REVISION_CONFLICT', '深度核查必须绑定搜索运行已确认的需求版本。')
        }
        if (run.deep_review) return send(202, operationView(session, run))
        if (runPhase(run) !== 'completed') fail(409, 'DEEP_REVIEW_NOT_READY', '检索 Bundle 尚未稳定，不能授权深度核查。')
        const authorized = Date.now()
        run.deep_review = {
          operation_id: `op-${randomUUID()}`, revision_id: body.revision_id,
          revision_hash: `revision-hash-${body.revision_id}`,
          bundle_hash: `bundle-hash-${run.run_id}`,
          model: 'deepseek-flash', prompt_version: 'mock-prompt-v1',
          authorized_at: authorized, deadline: authorized + 570_000,
        }
        return send(202, operationView(session, run))
      }
    }
  }

  if (first === 'tasks' && length === 5 && third === 'input' && method === 'GET') {
    const session = owner(req)
    const task = findTask(session, second)
    if (taskStatus(task) === 'failed') fail(422, 'INPUT_PARSE_FAILED', '演示场景：材料解析失败，无法读取文档内容。请新建任务重试（仅 Mock）。')
    if (taskStatus(task) !== 'completed') fail(409, 'INPUT_NOT_READY', '文档尚未完成处理，请查询状态。', true)
    return send(200, task.document.result)
  }

  fail(404, 'INVALID_REQUEST', '请求无效或资源不存在。')
}

export function startMock(port = Number(process.env.MOCK_PORT || 8787), options = {}) {
  const scenario = normalizeScenarios(options.scenarios ?? process.env.MOCK_SCENARIOS)
  const server = createServer((req, res) => {
    const requestId = randomUUID()
    handle(req, res, requestId, scenario).catch(error => {
      const status = error instanceof MockError ? error.status : 500
      const body = {
        error_code: error instanceof MockError ? error.code : 'INTERNAL_ERROR',
        message: error instanceof MockError ? error.message : '服务暂时无法完成请求。',
        retryable: error instanceof MockError ? error.retryable : true,
        details: null, request_id: requestId,
      }
      if (!res.headersSent) {
        const payload = JSON.stringify(body)
        res.writeHead(status, {
          'content-type': 'application/json; charset=utf-8',
          'content-length': Buffer.byteLength(payload),
          'x-request-id': requestId, 'x-projectpilot-mock': '1', 'access-control-allow-origin': '*',
        })
        res.end(payload)
      } else {
        res.end()
      }
      if (!(error instanceof MockError)) console.error('[mock] unexpected', error)
    })
  })
  return new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(port, '127.0.0.1', () => {
      const actual = server.address().port
      const enabled = Object.entries(scenario).filter(([, value]) => value).map(([name]) => name)
      if (enabled.length) console.log(`[mock] 失败场景已启用：${enabled.join(' / ')}（每个会话各触发一次，重试即恢复）`)
      console.log(`[mock] ProjectPilot 演示数据服务 http://127.0.0.1:${actual}/api/v1 （仅开发/测试；响应带 X-ProjectPilot-Mock）`)
      resolve({
        server, port: actual,
        close: () => new Promise(done => server.close(() => done())),
      })
    })
  })
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const mock = await startMock()
  const stop = () => mock.close().then(() => process.exit(0))
  process.on('SIGINT', stop)
  process.on('SIGTERM', stop)
}
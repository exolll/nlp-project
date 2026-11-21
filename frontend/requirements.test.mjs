import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

// Exercise the actual pure TS helpers without adding a test dependency or emitting files.
const source = await readFile(new URL('./src/requirements.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const { reconcileItems, createDraft, updateField, revisionPayload, contentChanged, meaningChanged, draftIssues, languageGroupIssue, evidenceSlice, settleAttempt, mayStartExtraction } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
const item = (text, index, extra = {}) => ({
  requirement_item_id: `item-${index}`, field: 'programming_languages', value_path: `programming_languages[${index}]`, text,
  modality: 'required', source_kind: 'assignment_document', evidence_ids: [`evidence-${index}`], logic_group_id: 'languages', operator: 'ANY', supersedes_item_id: null, ...extra,
})
const evidence = { evidence_id: 'evidence-0', field: 'programming_languages', original_text: '😀Python', source_kind: 'assignment_document', source_location: '第 1 段', requirement_item_id: 'item-0', segment_id: 'segment-1', start_offset: 1, end_offset: 8 }
const revision = {
  revision_id: 'revision-1', task_id: 'task-1', revision_number: 1, parent_revision_id: null, status: 'extracted',
  requirements: {
    project_topic: '课程项目', project_description: null, project_type: null, domain: null,
    required_features: [], preferred_features: null, programming_languages: ['Python', 'Java'], required_technologies: null, preferred_technologies: null,
    missing_information: ['部署方式'], uncertain_information: [{ field: 'programming_languages', requirement_item_ids: ['item-0', 'item-1'], reason: '语言关系不明确', question: '任选还是全部？', evidence_ids: ['evidence-0'] }], source_evidence: [evidence],
  },
  requirement_items: [item('Python', 0), item('Java', 1)], change_log: [], created_at: '2026-01-01', confirmed_at: null,
}

test('reorders by old text and preserves duplicate identities stably', () => {
  const previous = [item('Python', 0), item('Java', 1), item('Python', 2)]
  const result = reconcileItems('programming_languages', ['Java', 'Python', 'Python'], previous)
  assert.deepEqual(result.map(value => value.requirement_item_id), ['item-1', 'item-0', 'item-2'])
  assert.deepEqual(result.map(value => value.value_path), ['programming_languages[0]', 'programming_languages[1]', 'programming_languages[2]'])
  assert.equal(previous[1].value_path, 'programming_languages[1]')
  const duplicateReplacement = reconcileItems('programming_languages', ['Python', 'Python', 'Python'], previous)
  assert.deepEqual(duplicateReplacement.map(value => value.requirement_item_id), ['item-0', 'item-1', 'item-2'])
  assert.deepEqual(duplicateReplacement[2], previous[2])
})
test('changed rows copy original metadata; added rows have no forged IDs or evidence', () => {
  const result = reconcileItems('programming_languages', ['TypeScript', 'Java', 'Rust'], revision.requirement_items)
  assert.deepEqual(result[0], { ...revision.requirement_items[0], text: 'TypeScript' })
  assert.deepEqual(result[2], item('Rust', 2, { requirement_item_id: null, modality: 'informational', source_kind: 'user_revision', evidence_ids: [], logic_group_id: null, operator: 'not_applicable' }))
  const inserted = reconcileItems('programming_languages', ['Rust', 'Python', 'Java'], revision.requirement_items)
  assert.equal(inserted[0].requirement_item_id, null)
  assert.deepEqual(inserted.slice(1).map(value => value.requirement_item_id), ['item-0', 'item-1'])
})
test('deleting rows removes only corresponding items and keeps other fields', () => {
  const other = item('登录', 0, { field: 'required_features', value_path: 'required_features[0]', requirement_item_id: 'feature-0' })
  const result = reconcileItems('programming_languages', ['Java'], [...revision.requirement_items, other])
  assert.deepEqual(result, [other, { ...revision.requirement_items[1], value_path: 'programming_languages[0]' }])
})
test('null, empty list, and empty scalar remain distinct; toggling null restores metadata', () => {
  const draft = createDraft(revision)
  const absent = updateField(draft, 'programming_languages', draft.text.programming_languages, true)
  assert.equal(absent.requirements.programming_languages, null)
  assert.equal(absent.items.length, 0)
  const restored = updateField(absent, 'programming_languages', absent.text.programming_languages, false)
  assert.deepEqual(restored.items, draft.items)
  assert.equal(contentChanged(restored, revision), false)
  const empty = updateField(draft, 'programming_languages', '\n  \n', false)
  assert.deepEqual(empty.requirements.programming_languages, [])
  assert.equal(empty.items.length, 0)
  assert.equal(updateField(draft, 'project_topic', '', false).requirements.project_topic, '')
})
test('scalar items use field without index and synchronize text', () => {
  const previous = [item('旧主题', 0, { field: 'project_topic', value_path: 'project_topic' })]
  assert.deepEqual(reconcileItems('project_topic', '新主题', previous), [{ ...previous[0], text: '新主题' }])
})
test('twelve-field payload retains original evidence and requires explicit ambiguity resolution', () => {
  const before = JSON.stringify(revision)
  let draft = createDraft(revision)
  draft.reason = '  明确语言要求  '
  assert.equal(contentChanged(draft, revision), false)
  draft.resolved = true
  assert.deepEqual(revisionPayload(draft, revision).requirements.uncertain_information, revision.requirements.uncertain_information)
  draft = updateField(draft, 'programming_languages', 'Python\nTypeScript', false)
  assert.equal(draft.resolved, false)
  assert.equal(meaningChanged(draft, revision), true)
  assert.deepEqual(revisionPayload(draft, revision).requirements.uncertain_information, revision.requirements.uncertain_information)
  draft.resolved = true
  draft.missingText = '部署方式\n\n上线时间 '
  draft.requirements.source_evidence = []
  const payload = revisionPayload(draft, revision)
  assert.equal(Object.keys(payload.requirements).length, 12)
  assert.equal(payload.requirements.uncertain_information, null)
  assert.deepEqual(payload.requirements.missing_information, ['部署方式', '上线时间'])
  assert.equal(payload.requirements.source_evidence, revision.requirements.source_evidence)
  assert.equal(payload.change_reason, '明确语言要求')
  assert.equal(JSON.stringify(revision), before)
})
test('multi-language groups require explicit common group and consistent ALL or ANY', () => {
  assert.equal(languageGroupIssue(revision.requirement_items), null)
  assert.ok(languageGroupIssue(revision.requirement_items.map(value => ({ ...value, logic_group_id: null, operator: 'not_applicable' }))))
  assert.ok(languageGroupIssue([revision.requirement_items[0], { ...revision.requirement_items[1], operator: 'ALL' }]))
  assert.ok(languageGroupIssue(revision.requirement_items.map((value, index) => ({ ...value, logic_group_id: `single-${index}` }))))
  assert.equal(languageGroupIssue([revision.requirement_items[0]]), null)
})
test('field-specific metadata defaults match server coverage without guessing language logic', () => {
  assert.equal(reconcileItems('required_features', ['登录'], [])[0].modality, 'required')
  assert.equal(reconcileItems('preferred_technologies', ['Redis'], [])[0].modality, 'preferred')
  const language = reconcileItems('programming_languages', ['Python'], [])[0]
  assert.equal(language.modality, 'informational')
  assert.equal(language.operator, 'not_applicable')
  assert.equal(language.logic_group_id, null)
  assert.equal(languageGroupIssue([item('Python', 0, { logic_group_id: null, operator: 'not_applicable' }), item('Java', 1, { modality: 'preferred', logic_group_id: null, operator: 'not_applicable' })]), null)
})
test('server validation constraints and deleted uncertainty links are explained before save', () => {
  const draft = createDraft(revision)
  assert.deepEqual(draftIssues(draft, revision), [])
  assert.ok(draftIssues(updateField(draft, 'project_topic', '', false), revision).some(issue => issue.includes('非空文本')))
  const deleted = updateField(draft, 'programming_languages', 'Python', false)
  assert.ok(draftIssues(deleted, revision).some(issue => issue.includes('歧义引用')))
  deleted.resolved = true
  assert.deepEqual(draftIssues(deleted, revision), [])
  const incompatible = updateField(draft, 'required_features', '登录', false)
  incompatible.items.find(value => value.field === 'required_features').modality = 'preferred'
  assert.ok(draftIssues(incompatible, revision).some(issue => issue.includes('必须')))
})

test('evidence uses original Unicode code points, not UTF-16 or normalized text', () => {
  const document = { normalized_text: 'wrong text', source_segments: [{ segment_id: 'segment-1', original_content: '前😀Python后', content: 'different normalized content' }] }
  const verified = evidenceSlice(evidence, document)
  assert.equal(verified.quote, '😀Python')
  assert.equal(verified.matches, true)
  assert.equal(evidenceSlice({ ...evidence, original_text: '伪造引文' }, document).matches, false)
  assert.equal(evidenceSlice({ ...evidence, start_offset: -1 }, document).quote, null)
  assert.equal(evidenceSlice({ ...evidence, end_offset: 999 }, document).quote, null)
  assert.equal(evidenceSlice({ ...evidence, start_offset: 1.5 }, document).quote, null)
  assert.equal(evidenceSlice({ ...evidence, segment_id: null }, document).quote, null)
})
test('unknown POST cannot be settled by GET returning no job or the previous failed job', () => {
  const saved = { key: 'kept-key', state: 'unknown', baseline_id: 'old-job', extraction_id: null }
  const oldJob = { extraction_id: 'old-job', status: 'failed' }
  assert.equal(settleAttempt(saved, null), saved)
  assert.equal(settleAttempt(saved, oldJob), saved)
  assert.equal(mayStartExtraction(null, saved), false)
  assert.equal(mayStartExtraction(oldJob, saved), false)
  const restored = settleAttempt(saved, { extraction_id: 'new-job', status: 'extracting' })
  assert.equal(restored.key, saved.key)
  assert.equal(restored.state, 'accepted')
  assert.equal(mayStartExtraction(null, restored), false)
})
test('request remains backward compatible and supports JSON payload plus idempotency key', async () => {
  const apiSource = (await readFile(new URL('./src/api.ts', import.meta.url), 'utf8')).replaceAll('import.meta.env', '({ VITE_API_BASE_URL: undefined })')
  const apiCompiled = ts.transpileModule(apiSource, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
  const api = await import(`data:text/javascript;base64,${Buffer.from(apiCompiled).toString('base64')}`)
  const originalFetch = globalThis.fetch
  const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  const requests = []
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: { getItem: () => null } })
  globalThis.fetch = async (url, options) => { requests.push({ url, options }); return new Response(JSON.stringify({ ok: true }), { status: 200 }) }
  try {
    const controller = new AbortController()
    await api.request('/tasks', 'POST', controller.signal)
    await api.request('/requirement-revisions/r1/revisions', 'POST', controller.signal, { requirements: { project_topic: '主题' } }, 'test-uuid')
    await api.request('/documents/d1/requirements', 'GET', controller.signal)
    assert.equal(requests[0].options.body, '{}')
    assert.equal(requests[0].options.signal, controller.signal)
    assert.equal(requests[0].options.headers['Idempotency-Key'], undefined)
    assert.equal(requests[1].options.headers['Idempotency-Key'], 'test-uuid')
    assert.equal(requests[1].options.headers['Content-Type'], 'application/json')
    assert.deepEqual(JSON.parse(requests[1].options.body), { requirements: { project_topic: '主题' } })
    assert.equal(requests[2].options.body, undefined)
    assert.equal(requests[2].options.method, 'GET')
    assert.equal(requests[2].url, '/api/v1/documents/d1/requirements')
    globalThis.fetch = async () => new Response(JSON.stringify({ error_code: 'REVISION_CONFLICT', message: '冲突' }), { status: 409 })
    await assert.rejects(api.request('/requirement-revisions/r1/confirm', 'POST'), error => error instanceof api.Failure && error.status === 409 && error.info.error_code === 'REVISION_CONFLICT')
  } finally {
    globalThis.fetch = originalFetch
    if (originalStorage) Object.defineProperty(globalThis, 'sessionStorage', originalStorage)
    else delete globalThis.sessionStorage
  }
})

test('known failures permit explicit new extraction; completed and pending jobs do not', () => {
  const saved = { key: 'kept-key', state: 'accepted', baseline_id: null, extraction_id: 'new-job' }
  const failed = { extraction_id: 'new-job', status: 'failed' }
  assert.equal(mayStartExtraction(failed, settleAttempt(saved, failed)), true)
  for (const status of ['queued', 'extracting', 'validating', 'completed']) {
    const job = { extraction_id: 'new-job', status }
    assert.equal(mayStartExtraction(job, settleAttempt(saved, job)), false)
  }
  const completed = settleAttempt(saved, { extraction_id: 'new-job', status: 'completed' })
  assert.equal(mayStartExtraction(null, completed), false)
  assert.equal(mayStartExtraction(null, null), true)
})

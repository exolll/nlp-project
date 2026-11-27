import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const compile = source => `data:text/javascript;base64,${Buffer.from(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText).toString('base64')}`
const load = async path => import(compile(await readFile(new URL(path, import.meta.url), 'utf8')))

const rules = await load('./src/inputRules.ts')
const draft = await load('./src/draft.ts')
const history = await load('./src/history.ts')
const task = await load('./src/adapters/task.ts')
const requirements = await load('./src/requirements.ts')

const memory = () => {
  const data = new Map()
  return {
    data,
    getItem: key => data.has(key) ? data.get(key) : null,
    setItem: (key, value) => { data.set(key, String(value)) },
    removeItem: key => { data.delete(key) },
  }
}

const TASK = '8f1c2b3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d'

test('inputRules classifies files, labels sizes, and validates a single upload', () => {
  assert.equal(rules.fileKind('brief.PDF'), 'PDF')
  assert.equal(rules.fileKind('notes.docx'), 'DOCX')
  assert.equal(rules.fileKind('notes.doc'), null)
  assert.equal(rules.fileKind('notes.txt'), null)
  assert.equal(rules.fileLabel({ name: 'a.pdf', size: 2048 }), 'PDF · 2.0 KB')
  assert.equal(rules.fileLabel({ name: 'a.txt', size: 0 }), '文档 · 0.0 KB')

  const config = { max_primary_characters: 30_000, max_supplement_characters: 2_000, max_file_bytes: 10_000_000 }
  assert.equal(rules.inputLimit(config, false), 30_000)
  assert.equal(rules.inputLimit(config, true), 2_000)
  assert.equal(rules.inputLimit(null, false), 0)

  assert.match(rules.validateFileChoice([], config), /仅支持一个文件/)
  assert.match(rules.validateFileChoice([{ name: 'a.pdf', size: 1 }, { name: 'b.pdf', size: 1 }], config), /仅支持一个文件/)
  assert.match(rules.validateFileChoice([{ name: 'a.txt', size: 10 }], config), /PDF 或 DOCX/)
  assert.match(rules.validateFileChoice([{ name: 'a.pdf', size: 0 }], config), /空文件/)
  assert.match(rules.validateFileChoice([{ name: 'a.pdf', size: 10 }], null), /服务配置/)
  assert.match(rules.validateFileChoice([{ name: 'a.pdf', size: 10_000_001 }], config), /超过服务允许的大小/)
  assert.equal(rules.validateFileChoice([{ name: 'a.PDF', size: 1024 }], config), '')
})

test('draft round-trips, clears on empty, and ignores corrupt storage safely', () => {
  const storage = memory()
  assert.deepEqual(draft.readDraft(storage), { draft: null, error: '' })

  draft.writeDraft(storage, { draftId: 'd-1', text: '需求文本', fileHint: { name: 'a.pdf', size: 1234 } })
  const restored = draft.readDraft(storage)
  assert.equal(restored.error, '')
  assert.equal(restored.draft.draftId, 'd-1')
  assert.equal(restored.draft.text, '需求文本')
  assert.deepEqual(restored.draft.fileHint, { name: 'a.pdf', size: 1234 })
  assert.equal(restored.draft.version, 1)
  assert.ok(!Number.isNaN(Date.parse(restored.draft.updatedAt)))

  draft.writeDraft(storage, { draftId: 'd-1', text: '', fileHint: null })
  assert.equal(storage.getItem(draft.draftStorageKey), null)
  assert.deepEqual(draft.readDraft(storage), { draft: null, error: '' })

  storage.setItem(draft.draftStorageKey, '{broken json')
  const corrupt = draft.readDraft(storage)
  assert.equal(corrupt.draft, null)
  assert.match(corrupt.error, /无法解析/)

  storage.setItem(draft.draftStorageKey, JSON.stringify({ version: 2, draftId: 'x', text: 't', updatedAt: 'now' }))
  assert.equal(draft.readDraft(storage).draft, null)

  storage.setItem(draft.draftStorageKey, JSON.stringify({
    version: 1, draftId: 'x', text: 't', updatedAt: 'now', fileHint: { name: 1, size: 'big' },
  }))
  assert.match(draft.readDraft(storage).error, /无法解析/)

  storage.setItem(draft.draftStorageKey, JSON.stringify({ version: 1, draftId: 'x', text: '', updatedAt: 'now', fileHint: { name: 'a.pdf', size: 9 } }))
  assert.equal(draft.readDraft(storage).draft.fileHint.name, 'a.pdf')

  draft.clearDraft(storage)
  assert.equal(storage.getItem(draft.draftStorageKey), null)

  const blocked = { getItem: () => { throw new Error('denied') } }
  assert.deepEqual(draft.readDraft(blocked), { draft: null, error: '' })
})

test('preview preference defaults to hidden and only trusts explicit v1 records', () => {
  const storage = memory()
  assert.equal(history.readPreviewOpen(storage), false)
  history.writePreviewOpen(storage, true)
  assert.equal(history.readPreviewOpen(storage), true)
  history.writePreviewOpen(storage, false)
  assert.equal(history.readPreviewOpen(storage), false)

  storage.setItem(history.previewStorageKey, '{oops')
  assert.equal(history.readPreviewOpen(storage), false)
  storage.setItem(history.previewStorageKey, JSON.stringify({ version: 2, open: true }))
  assert.equal(history.readPreviewOpen(storage), false)
  storage.setItem(history.previewStorageKey, JSON.stringify({ version: 1, open: 'yes' }))
  assert.equal(history.readPreviewOpen(storage), false)
  storage.setItem(history.previewStorageKey, JSON.stringify({ version: 1, open: true }))
  assert.equal(history.readPreviewOpen(storage), true)
})

test('title helpers follow auto topic until the user edits the title', () => {
  assert.equal(task.generatedTitle(TASK), '任务 8f1c2b3d')
  assert.equal(task.titleFor(undefined, TASK), '任务 8f1c2b3d')
  assert.equal(task.titleFor({ title: '手动标题', autoTitle: '自动标题' }, TASK), '手动标题')
  assert.equal(task.titleFor({ title: '  ', autoTitle: '自动标题' }, TASK), '自动标题')
  assert.equal(task.titleFor({ title: '', autoTitle: '' }, TASK), '任务 8f1c2b3d')

  assert.equal(task.titleEditedFor(undefined, TASK, ''), false)
  assert.equal(task.titleEditedFor(undefined, TASK, '任务 8f1c2b3d'), false)
  assert.equal(task.titleEditedFor({ autoTitle: '自动标题' }, TASK, '自动标题'), false)
  assert.equal(task.titleEditedFor(undefined, TASK, '我的标题'), true)

  assert.deepEqual(task.topicPatch(undefined, '项目主题'), { title: '项目主题', autoTitle: '项目主题', titleEdited: false })
  assert.equal(task.topicPatch({ title: '项目主题', autoTitle: '项目主题' }, '项目主题'), null)
  assert.deepEqual(task.topicPatch({ title: '手动标题', autoTitle: '旧主题', titleEdited: true }, '新主题'), { autoTitle: '新主题' })
  assert.equal(task.topicPatch({ title: '手动标题', autoTitle: '新主题', titleEdited: true }, '新主题'), null)
  assert.equal(task.topicPatch(undefined, '   '), null)
  assert.equal(task.topicPatch(undefined, 'x'.repeat(120)).title.length, 80)
})

test('latestProjectTopic prefers the pointed revision and falls back to the highest number', () => {
  const revision = (revisionId, revisionNumber, topic) => ({
    revision_id: revisionId, revision_number: revisionNumber, requirements: { project_topic: topic },
  })
  const rows = [revision('r1', 1, '旧主题'), revision('r2', 2, '新主题')]
  assert.equal(requirements.latestProjectTopic(rows, 'r1'), '旧主题')
  assert.equal(requirements.latestProjectTopic(rows, null), '新主题')
  assert.equal(requirements.latestProjectTopic(rows, 'missing'), '新主题')
  assert.equal(requirements.latestProjectTopic([revision('r1', 1, '  ') ], 'r1'), null)
  assert.equal(requirements.latestProjectTopic([], 'r1'), null)
})
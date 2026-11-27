import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const compile = source => `data:text/javascript;base64,${Buffer.from(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText).toString('base64')}`
const load = async path => import(compile(await readFile(new URL(path, import.meta.url), 'utf8')))
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))

const router = await load('./src/router.ts')
const history = await load('./src/history.ts')
const stages = await load('./src/adapters/stages.ts')
const { createAutosave } = await load('./src/autosave.ts')

const TASK = '8f1c2b3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d'

test('router parses task stage routes and rejects unknown input', () => {
  assert.deepEqual(router.parseRoute(''), { kind: 'home' })
  assert.deepEqual(router.parseRoute('#/'), { kind: 'home' })
  assert.deepEqual(router.parseRoute(`#/task/${TASK}`), { kind: 'task', taskId: TASK, stage: null })
  assert.deepEqual(router.parseRoute(`#/task/${TASK}/stage/3`), { kind: 'task', taskId: TASK, stage: 3 })
  assert.deepEqual(router.parseRoute(`#/task/${TASK}/stage/9`), { kind: 'task', taskId: TASK, stage: null })
  assert.deepEqual(router.parseRoute('#/task/not-a-uuid/stage/2'), { kind: 'home' })
  assert.deepEqual(router.parseRoute('#/settings/models'), { kind: 'settings', tab: 'models' })
  assert.deepEqual(router.parseRoute('#/settings/unknown'), { kind: 'settings', tab: 'api' })
})

test('router round-trips every route it can produce', () => {
  for (const route of [{ kind: 'home' }, { kind: 'task', taskId: TASK, stage: 5 },
                       { kind: 'task', taskId: TASK, stage: null }, { kind: 'settings', tab: 'ui' }]) {
    assert.deepEqual(router.parseRoute(router.routeHash(route)), route)
  }
  assert.equal(router.sameRoute(router.parseRoute(`#/task/${TASK}`), { kind: 'task', taskId: TASK, stage: null }), true)
  assert.equal(router.sameRoute({ kind: 'home' }, { kind: 'settings', tab: 'api' }), false)
})

test('history groups tasks by local calendar day and keeps server order inside each group', () => {
  const now = new Date(2026, 10, 26, 10, 0, 0)
  const iso = (...args) => new Date(...args).toISOString()
  const rows = [
    { id: 'a', created_at: iso(2026, 10, 26, 9, 0) },
    { id: 'b', created_at: iso(2026, 10, 26, 8, 0) },
    { id: 'c', created_at: iso(2026, 10, 25, 23, 30) },
    { id: 'd', created_at: iso(2026, 10, 22, 12, 0) },
    { id: 'e', created_at: iso(2026, 10, 1, 12, 0) },
  ]
  const groups = history.groupTasks(rows, now)
  assert.deepEqual(groups.map(group => group.group), ['today', 'yesterday', 'week', 'earlier'])
  assert.deepEqual(groups.map(group => group.tasks.map(task => task.id)), [['a', 'b'], ['c'], ['d'], ['e']])
  assert.equal(groups[0].label, '今天')
  assert.equal(groups[3].label, '更早时间')
  assert.equal(history.taskGroup('not-a-date', now), 'earlier')
})

test('ui state survives corruption, keeps ids only, and removes records safely', () => {
  const store = { value: '{broken', getItem() { return this.value }, setItem(_k, v) { this.value = v } }
  const broken = history.readUi(store)
  assert.deepEqual(broken.state, history.emptyUi())
  assert.ok(broken.error.includes('无法解析'))

  store.value = JSON.stringify({ version: 1, tasks: { [TASK]: { title: '论文检索', titleEdited: true, stage: 9, runId: 'r', revisionId: 'v' } }, last: { taskId: TASK, at: 't' } })
  const parsed = history.readUi(store)
  assert.equal(parsed.error, '')
  assert.equal(parsed.state.tasks[TASK].title, '论文检索')
  assert.equal(parsed.state.tasks[TASK].stage, null)
  assert.equal(parsed.state.tasks[TASK].runId, 'r')

  const patched = history.patchTask(history.emptyUi(), TASK, { title: '新标题', titleEdited: true })
  assert.equal(patched.last.taskId, TASK)
  assert.equal(history.removeTask(patched, TASK).tasks[TASK], undefined)
  assert.equal(history.removeTask(patched, TASK).last, null)
})

test('stage derivation is cumulative, marks stale downstream results, and only exposes completed stages', () => {
  const none = stages.deriveStages({ hasTask: true, inputCompleted: false, extractionCompleted: false, confirmedRevisionId: null, run: null })
  assert.deepEqual(none.map(stage => stage.done), [false, false, false, false, false, false])
  assert.equal(none[0].current, true)
  assert.deepEqual(none.map(stage => stage.reachable), [false, false, false, false, false, false])

  const skip = stages.deriveStages({ hasTask: true, inputCompleted: false, extractionCompleted: true, confirmedRevisionId: 'v2', run: null })
  assert.deepEqual(skip.map(stage => stage.done), [false, false, false, false, false, false])
  assert.equal(skip[0].current, true)

  const partial = stages.deriveStages({ hasTask: true, inputCompleted: true, extractionCompleted: true, confirmedRevisionId: 'v2', run: null })
  assert.deepEqual(partial.map(stage => stage.done), [true, true, true, false, false, false])
  assert.equal(partial[3].current, true)
  assert.deepEqual(partial.map(stage => stage.reachable), [true, true, true, false, false, false])

  const full = stages.deriveStages({
    hasTask: true, inputCompleted: true, extractionCompleted: true, confirmedRevisionId: 'v2',
    run: { confirmedRevisionId: 'v2', evidencePublished: true, deepReviewPublished: true, rankingAvailable: true },
  })
  assert.deepEqual(full.map(stage => stage.done), [true, true, true, true, true, true])
  assert.deepEqual(full.map(stage => stage.stale), [false, false, false, false, false, false])
  assert.equal(full[5].current, true)

  const stale = stages.deriveStages({
    hasTask: true, inputCompleted: true, extractionCompleted: true, confirmedRevisionId: 'v3',
    run: { confirmedRevisionId: 'v2', evidencePublished: true, deepReviewPublished: true, rankingAvailable: true },
  })
  assert.deepEqual(stale.map(stage => stage.stale), [false, false, false, true, true, true])
})

test('autosave debounces repeated input into one real save and reports only real outcomes', async () => {
  const saves = []
  const seen = []
  const controller = createAutosave({ delay: 15, save: value => { saves.push(value) }, onChange: status => seen.push(status) })
  assert.equal(controller.status(), 'idle')
  controller.schedule('a')
  controller.schedule('ab')
  controller.schedule('abc')
  await sleep(45)
  assert.deepEqual(saves, ['abc'])
  assert.equal(controller.status(), 'saved')
  assert.deepEqual(seen, ['saving', 'saved'])
})

test('autosave keeps the newest value when an older save is still in flight', async () => {
  const saves = []
  let release
  const gate = new Promise(resolve => { release = resolve })
  const controller = createAutosave({
    delay: 5,
    save: async value => { saves.push(value); if (value === 'first') await gate },
    onChange: () => {},
  })
  controller.schedule('first')
  await sleep(15)
  controller.schedule('second')
  assert.equal(controller.status(), 'saving')
  release()
  await sleep(25)
  assert.deepEqual(saves, ['first', 'second'])
  assert.equal(controller.status(), 'saved')
})

test('autosave never reports success on failure, keeps the value, and retries on demand', async () => {
  let failing = true
  const saved = []
  const controller = createAutosave({
    delay: 5,
    save: value => { if (failing) throw new DOMException('quota', 'QuotaExceededError'); saved.push(value) },
    onChange: () => {},
  })
  controller.schedule('draft')
  await sleep(20)
  assert.equal(controller.status(), 'failed')
  assert.equal(controller.hasUnsaved(), true)
  assert.deepEqual(saved, [])

  failing = false
  assert.equal(await controller.retry(), true)
  assert.equal(controller.status(), 'saved')
  assert.deepEqual(saved, ['draft'])
  assert.equal(controller.hasUnsaved(), false)
})

test('flush writes without waiting for the debounce and reports the real result', async () => {
  const saved = []
  const controller = createAutosave({ delay: 5000, save: value => { saved.push(value) }, onChange: () => {} })
  controller.schedule('switch')
  assert.equal(await controller.flush(), true)
  assert.deepEqual(saved, ['switch'])
  assert.deepEqual(await controller.flush(), true)
  assert.deepEqual(saved, ['switch'])
})
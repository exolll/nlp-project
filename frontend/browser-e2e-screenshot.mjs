import { spawn } from 'node:child_process'
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import assert from 'node:assert/strict'

const root = 'D:/作业/NLP/git学习/nlp-project'
const acceptDir = join(root, 'data/acceptance/3a-ui')
const file = process.argv[2] || (await readdir(acceptDir)).filter(name => name.endsWith('.json')).sort().at(-1)
const control = JSON.parse(await readFile(join(acceptDir, file), 'utf8'))
const api = 'http://127.0.0.1:8000/api/v1'
const headers = { Authorization: `Bearer ${control.token_a}` }
const bundle = await (await fetch(`${api}/tasks/${control.task_id}/search-runs/${control.run_id}/retrieval-evidence`, { headers })).json()
const expectedName = bundle.candidates[0]?.repository?.full_name
if (!expectedName) throw Error('Bundle has no repository metadata')

const profile = await mkdtemp(join(tmpdir(), 'projectpilot-shot-'))
const browser = spawn('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', [
  '--headless=new', '--disable-gpu', '--no-first-run', '--window-size=1440,1000',
  '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'])
let socket
try {
  const endpoint = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(Error('Browser startup timed out')), 20000)
    browser.stderr.on('data', chunk => {
      const match = String(chunk).match(/DevTools listening on (ws:\/\/[^\s]+)/)
      if (match) { clearTimeout(timeout); resolve(match[1]) }
    })
    browser.on('error', reject)
  })
  socket = new WebSocket(endpoint)
  await new Promise(resolve => socket.addEventListener('open', resolve, { once: true }))
  let sequence = 0
  const pending = new Map()
  socket.addEventListener('message', event => {
    const data = JSON.parse(event.data)
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
  await command('Page.enable')
  await command('Runtime.enable')
  await command('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false })
  const attempt = { key: crypto.randomUUID(), payload: { revision_id: control.revision_id, consent: true,
    budget: { model_calls: 1, github_requests: 20, search_requests: 9, readme_candidates: 5, per_page: 5, max_pages: 1 } },
    run_id: control.run_id }
  const source = `
    try {
      if (location.origin === 'http://127.0.0.1:5173') {
        sessionStorage.setItem('projectpilot.session', JSON.stringify({ access_token: ${JSON.stringify(control.token_a)}, expires_at: '2099-01-01T00:00:00Z' }));
        sessionStorage.setItem(${JSON.stringify('projectpilot.search.' + control.task_id + '.' + control.revision_id)}, ${JSON.stringify(JSON.stringify(attempt))});
      }
    } catch (error) {}
  `
  await command('Page.addScriptToEvaluateOnNewDocument', { source })
  await command('Page.navigate', { url: 'http://127.0.0.1:5173' })
  const evaluate = async expression => {
    const result = await command('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (result.exceptionDetails) throw Error(JSON.stringify(result.exceptionDetails))
    return result.result.value
  }
  const waitFor = async (expression, label, timeout = 45000) => {
    const deadline = Date.now() + timeout
    while (Date.now() < deadline) {
      if (await evaluate(expression)) return
      await new Promise(resolve => setTimeout(resolve, 300))
    }
    throw Error(`Timed out waiting for ${label}`)
  }
  await waitFor(`document.querySelectorAll('.history-row').length > 0`, 'task history', 30000)
  const clicked = await evaluate(`(() => {
    const row = [...document.querySelectorAll('.history-row')].find(node => node.textContent.includes(${JSON.stringify(control.task_id)}));
    if (!row) return false; row.click(); return true;
  })()`)
  if (!clicked) throw Error('Task row not found')
  await waitFor(`!!document.querySelector('.search-panel')`, 'search panel', 30000)
  await waitFor(`document.body.innerText.includes(${JSON.stringify(expectedName)})`, 'retrieval evidence', 45000)
  await waitFor(`document.body.innerText.includes('相关性待核查')`, 'retrieval relevance label', 30000)
  await evaluate(`(() => {
    document.querySelectorAll('.retrieval-report details.requirement-evidence').forEach((node, index) => { if (index < 2) node.open = true });
    const section = document.querySelector('.retrieval-report');
    if (section) section.scrollIntoView({ block: 'start' });
    window.scrollBy(0, -60); return true;
  })()`)
  await new Promise(resolve => setTimeout(resolve, 400))
  const text = await evaluate(`document.body.innerText`)
  const content = await evaluate(`document.body.textContent`)
  assert.ok(text.includes(expectedName), 'repository name visible')
  assert.ok(text.includes('README.md'), 'real README path visible')
  assert.ok(text.includes('相关性待核查'), 'retrieved is not shown as satisfaction')
  assert.ok(content.includes('历史记录未保存完整快照标识'), 'v1 trace fallback visible')
  assert.ok(text.includes('行 ') && text.includes('余弦距离'), 'line numbers and distance visible')
  assert.ok(!text.includes('找到相关片段'), 'legacy satisfaction wording removed')
  assert.ok(!/[满足度]\s*\d+%/.test(text), 'no satisfaction percentage')
  await new Promise(resolve => setTimeout(resolve, 300))
  const full = await command('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true })
  await writeFile(join(acceptDir, 'retrieval-evidence-full.png'), Buffer.from(full.data, 'base64'))
  const box = await evaluate(`(() => {
    const section = document.querySelector('.retrieval-report');
    if (!section) return null;
    const rect = section.getBoundingClientRect();
    return { x: rect.left + window.scrollX, y: rect.top + window.scrollY, width: rect.width, height: Math.min(rect.height, 2400) };
  })()`)
  if (box && box.width > 0 && box.height > 0) {
    const clip = await command('Page.captureScreenshot', { format: 'png', clip: { ...box, scale: 1 }, captureBeyondViewport: true })
    await writeFile(join(acceptDir, 'retrieval-evidence-section.png'), Buffer.from(clip.data, 'base64'))
  }
  console.log(JSON.stringify({ control: file, run_id: control.run_id, repository: expectedName,
                               screenshot: 'retrieval-evidence-full.png', section: 'retrieval-evidence-section.png' }))
  await send('Browser.close')
} finally {
  socket?.close()
  if (browser.exitCode === null) await new Promise(resolve => { browser.once('exit', resolve); setTimeout(() => { browser.kill(); resolve() }, 3000).unref() })
  await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 })
}

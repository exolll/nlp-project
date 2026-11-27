#!/usr/bin/env node
// 一条命令启动「演示数据 Mock Server + Vite」：npm run dev:mock
// 只启动本地进程，不写入任何后端或共享环境。
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

import { startMock } from './server.mjs'

let mock
try {
  mock = await startMock()
} catch (error) {
  console.error(`[dev:mock] 无法启动 Mock Server（${error.code || error.message}）。请检查端口是否被占用，或设置 MOCK_PORT。`)
  process.exit(1)
}

const vite = spawn(process.execPath, [fileURLToPath(new URL('../node_modules/vite/bin/vite.js', import.meta.url))], {
  stdio: 'inherit',
  env: { ...process.env, VITE_MOCK: '1' },
})

let stopping = false
async function stop(code = 0) {
  if (stopping) return
  stopping = true
  if (!vite.killed) vite.kill()
  await mock.close()
  process.exit(code)
}

process.on('SIGINT', () => void stop(0))
process.on('SIGTERM', () => void stop(0))
vite.on('exit', code => void stop(code ?? 0))
vite.on('error', error => {
  console.error('[dev:mock] 无法启动 Vite：', error.message)
  void stop(1)
})
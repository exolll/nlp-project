import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  // VITE_MOCK=1（由 npm run dev:mock 设置）把 /api 代理到本地演示数据服务。
  const mock = loadEnv(mode, '.', '').VITE_MOCK === '1'
  return {
    plugins: [react()],
    server: {
      host: '127.0.0.1', port: 5173, strictPort: true,
      proxy: { '/api': mock ? 'http://127.0.0.1:8787' : 'http://127.0.0.1:8000' },
    },
  }
})

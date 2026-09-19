import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// 开发代理：前端统一以相对路径 /api 请求，代理到后端（默认本机 8000）。
// 生产环境由反向代理（Nginx / B 静态挂载）承担同样的 /api 转发。
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const target = env.VITE_PROXY_TARGET || 'http://localhost:8000'
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        '/api': { target, changeOrigin: true },
      },
    },
  }
})

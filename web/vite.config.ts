import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const webTarget = process.env.VIGIL_WEB_TARGET ?? 'http://127.0.0.1:8080'
// 开发入口与生产页面一样禁止被其他网站嵌入，否则配置 VIGIL_WEB_DEV_ORIGIN 后可被诱导点击审批。
export const frameHeaders = {
  'Content-Security-Policy': "frame-ancestors 'none'",
  'X-Frame-Options': 'DENY',
}

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    headers: frameHeaders,
    proxy: {
      '/api': { target: webTarget, changeOrigin: true },
    },
  },
  preview: { host: '127.0.0.1', headers: frameHeaders },
})

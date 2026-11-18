import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const webTarget = process.env.VIGIL_WEB_TARGET ?? 'http://127.0.0.1:8080'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': { target: webTarget, changeOrigin: true },
    },
  },
})

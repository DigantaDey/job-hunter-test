import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Default proxy target for local dev; override per environment (e.g. Docker
// sets VITE_API_PROXY_TARGET=http://backend:8000).
const apiTarget = process.env.VITE_API_PROXY_TARGET || 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true
      }
    }
  },
  preview: {
    host: '0.0.0.0',
    port: 5173
  }
})

import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// `npm run dev` serves the app on :5173 and forwards the API and MCP to the Python server on :8080
export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': 'http://127.0.0.1:8080', '/mcp': 'http://127.0.0.1:8080' } },
  build: { outDir: 'dist', emptyOutDir: true, chunkSizeWarningLimit: 800 },
})

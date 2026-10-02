import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // During `npm run dev`, proxy API calls to the FastAPI backend (run
    // separately via `uvicorn knock.api.app:app --reload`) so the SPA can
    // call relative paths like `/api/auth/login` in both dev and the
    // built-and-served-by-FastAPI production setup.
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/respond': 'http://127.0.0.1:8000',
      '/sessions': 'http://127.0.0.1:8000',
    },
  },
  build: {
    outDir: 'dist',
  },
})

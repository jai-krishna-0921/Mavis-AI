import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // npm run dev with VITE_MOCK=0 talks to a local Mavis api (same origin in production, through Caddy)
  server: { proxy: { '/api': 'http://localhost:8000', '/oauth': 'http://localhost:8000' } },
  build: { sourcemap: false },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    include: ['src/**/*.test.{ts,tsx}'],
  },
})

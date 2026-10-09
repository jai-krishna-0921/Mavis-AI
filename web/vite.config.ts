import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // npm run dev with VITE_MOCK=0 talks to a local Mavis api (same origin in production, through Caddy)
  server: { proxy: { '/api': 'http://localhost:8000', '/oauth': 'http://localhost:8000' } },
  build: {
    sourcemap: false,
    // Keep long-lived libraries in their own files so the app code can change without re-downloading them.
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            { name: 'gsap', test: /node_modules[\\/]gsap/ },
            { name: 'react', test: /node_modules[\\/](react|react-dom|react-router|react-router-dom|scheduler)[\\/]/ },
            { name: 'query', test: /node_modules[\\/]@tanstack/ },
          ],
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    include: ['src/**/*.test.{ts,tsx}'],
  },
})

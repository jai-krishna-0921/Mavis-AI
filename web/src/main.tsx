import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App } from './App'
import { api } from './api/client'
import './styles/global.css'

const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false, retry: 1 } },
})

async function boot() {
  // Mock API for `npm run dev`. Set VITE_MOCK=0 to use a real backend. Tree-shaken from production builds.
  if (import.meta.env.DEV && import.meta.env.VITE_MOCK !== '0') {
    const { worker } = await import('./mocks/browser')
    await worker.start({ onUnhandledFrame: 'bypass', quiet: true })
  }
  // Load the public config (bot link, sign-in flags) at startup; components share this query.
  void queryClient.prefetchQuery({ queryKey: ['config'], queryFn: api.config, staleTime: 5 * 60_000 })
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </QueryClientProvider>
    </StrictMode>,
  )
}
void boot()

import { lazy, Suspense } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { RequireAuth } from './components/RequireAuth'
import { Landing } from './pages/Landing'
import { ToastProvider } from './components/Toast'
import { Logo } from './components/Logo'

// The landing page ships on its own. Everything behind it loads on demand.
const Layout = lazy(() => import('./components/Layout').then((m) => ({ default: m.Layout })))
const Login = lazy(() => import('./pages/Login').then((m) => ({ default: m.Login })))
const LinkTelegram = lazy(() => import('./pages/LinkTelegram').then((m) => ({ default: m.LinkTelegram })))
const Workspace = lazy(() => import('./pages/Workspace').then((m) => ({ default: m.Workspace })))
const Vault = lazy(() => import('./pages/Vault').then((m) => ({ default: m.Vault })))
const Preferences = lazy(() => import('./pages/Preferences').then((m) => ({ default: m.Preferences })))
const PrivacyPolicy = lazy(() => import('./pages/Legal').then((m) => ({ default: m.PrivacyPolicy })))
const TermsOfService = lazy(() => import('./pages/Legal').then((m) => ({ default: m.TermsOfService })))

function Loading() {
  return (
    <div className="grid min-h-[100dvh] place-items-center" role="status">
      <Logo size={48} live />
      <span className="sr-only">Loading</span>
    </div>
  )
}

export function App({ pollIntervalMs }: { pollIntervalMs?: number }) {
  return (
    <ToastProvider>
      <Suspense fallback={<Loading />}>
        <Routes>
          <Route path="/" element={<Landing />} />
          <Route path="/privacy" element={<PrivacyPolicy />} />
          <Route path="/terms" element={<TermsOfService />} />
          <Route path="/login" element={<Login pollIntervalMs={pollIntervalMs} />} />
          <Route path="/link-telegram" element={<LinkTelegram pollIntervalMs={pollIntervalMs} />} />
          <Route element={<RequireAuth><Layout /></RequireAuth>}>
            <Route path="/workspace" element={<Workspace />} />
            <Route path="/vault" element={<Vault />} />
            <Route path="/preferences" element={<Preferences />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Suspense>
    </ToastProvider>
  )
}

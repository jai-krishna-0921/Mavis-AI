import { Navigate, Route, Routes } from 'react-router-dom'
import { Layout } from './components/Layout'
import { RequireAuth } from './components/RequireAuth'
import { Landing } from './pages/Landing'
import { Login } from './pages/Login'
import { LinkTelegram } from './pages/LinkTelegram'
import { Workspace } from './pages/Workspace'
import { Vault } from './pages/Vault'
import { Preferences } from './pages/Preferences'
import { ToastProvider } from './components/Toast'

export function App({ pollIntervalMs }: { pollIntervalMs?: number }) {
  return (
    <ToastProvider>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/login" element={<Login pollIntervalMs={pollIntervalMs} />} />
        <Route path="/link-telegram" element={<LinkTelegram pollIntervalMs={pollIntervalMs} />} />
        <Route element={<RequireAuth><Layout /></RequireAuth>}>
          <Route path="/workspace" element={<Workspace />} />
          <Route path="/vault" element={<Vault />} />
          <Route path="/preferences" element={<Preferences />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </ToastProvider>
  )
}

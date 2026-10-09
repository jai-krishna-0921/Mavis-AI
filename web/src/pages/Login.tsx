import { Navigate, useSearchParams } from 'react-router-dom'
import { GoogleLogo, WarningCircle } from '@phosphor-icons/react'
import { useConfig, useMe } from '../api/hooks'
import { TelegramSignIn } from '../components/TelegramSignIn'
import { AuthShell } from '../components/AuthShell'
import { API_BASE } from '../api/client'

export function Login({ pollIntervalMs }: { pollIntervalMs?: number }) {
  const [params] = useSearchParams()
  const invite = params.get('invite') ?? undefined
  const problem = params.get('error')
  const me = useMe({ publicPage: true })
  const cfg = useConfig()
  if (me.data) return <Navigate to="/workspace" replace />
  const googleOff = cfg.data?.google_signin_enabled === false
  return (
    <AuthShell title="Sign in to Mavis AI" lede="Mavis lives in chat, so you sign in with Telegram. Nothing to remember, no password.">
      {problem?.startsWith('google_') && (
        <p className="mb-6 flex items-start gap-2 rounded-2xl border border-danger/30 bg-danger-soft p-3 text-sm text-danger" role="alert">
          <WarningCircle size={20} weight="light" className="mt-0.5 shrink-0" aria-hidden="true" />
          {problem === 'google_unavailable' ? 'Google sign in is not available right now.' : 'Google sign in did not work. Try again, or use Telegram.'}
        </p>
      )}
      <section aria-labelledby="tg">
        <h2 id="tg" className="mb-5 text-lg font-bold tracking-tight">Telegram</h2>
        <TelegramSignIn invite={invite} pollIntervalMs={pollIntervalMs} />
      </section>
      {!googleOff && (
        <>
          <div className="my-8 flex items-center gap-4 text-[13px] text-muted" aria-hidden="true">
            <span className="h-px flex-1 bg-line" />or<span className="h-px flex-1 bg-line" />
          </div>
          <section aria-labelledby="g">
            <h2 id="g" className="text-lg font-bold tracking-tight">Already linked?</h2>
            <p className="mt-1 text-sm text-muted">If you connected Google to Mavis, you can sign in with the same account.</p>
            <a className="btn mt-4 !min-h-12 !px-5" href={`${API_BASE}/auth/google/start`}>
              <GoogleLogo size={20} weight="bold" aria-hidden="true" /> Sign in with Google
            </a>
          </section>
        </>
      )}
    </AuthShell>
  )
}

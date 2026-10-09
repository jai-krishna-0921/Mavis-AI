import { Navigate, useSearchParams } from 'react-router-dom'
import { useMe } from '../api/hooks'
import { TelegramSignIn } from '../components/TelegramSignIn'
import { Weave } from '../components/Weave'
import { API_BASE } from '../api/client'
import ui from '../styles/ui.module.css'
import styles from './Login.module.css'

export function Login({ pollIntervalMs }: { pollIntervalMs?: number }) {
  const [params] = useSearchParams()
  const invite = params.get('invite') ?? undefined
  const problem = params.get('error')
  const me = useMe()
  if (me.data) return <Navigate to="/workspace" replace />
  return (
    <main className={styles.page}>
      <Weave size={44} title="Mavis AI" />
      <h1 className={styles.h1}>Sign in to Mavis AI</h1>
      <p className={ui.lede}>Mavis lives in chat, so you sign in with Telegram.</p>
      {problem?.startsWith('google_') && (
        <p className={ui.error} role="alert">
          {problem === 'google_unavailable' ? 'Google sign in is not available right now.' : 'Google sign in did not work. Try again, or use Telegram.'}
        </p>
      )}
      <section className={styles.block} aria-labelledby="tg">
        <h2 id="tg" className={styles.h2}>Telegram</h2>
        <TelegramSignIn invite={invite} pollIntervalMs={pollIntervalMs} />
      </section>
      <hr className={ui.rule} style={{ opacity: 0.15 }} />
      <section className={styles.block} aria-labelledby="g">
        <h2 id="g" className={styles.h2}>Already linked?</h2>
        <p className={ui.lede}>If you connected Google to Mavis, you can sign in with the same account.</p>
        <a className={ui.btn} style={{ marginTop: 12 }} href={`${API_BASE}/auth/google/start`}>Sign in with Google</a>
      </section>
    </main>
  )
}

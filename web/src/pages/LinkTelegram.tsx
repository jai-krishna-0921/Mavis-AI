import { Navigate, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { useMe } from '../api/hooks'
import { TelegramSignIn } from '../components/TelegramSignIn'
import { Weave } from '../components/Weave'
import ui from '../styles/ui.module.css'
import styles from './Login.module.css'

export function LinkTelegram({ pollIntervalMs }: { pollIntervalMs?: number }) {
  const [params] = useSearchParams()
  const me = useMe()
  const linkId = params.get('i') ?? undefined
  const pending = useQuery({ queryKey: ['pending-link', linkId], queryFn: () => api.pendingLink(linkId!), enabled: !!linkId, retry: false })
  if (me.data) return <Navigate to="/workspace" replace />
  return (
    <main className={styles.page}>
      <Weave size={44} title="Mavis AI" />
      <h1 className={styles.h1}>Link your Telegram first</h1>
      <p className={ui.lede}>
        We could not find a Mavis account for that Google address. Mavis lives in chat, so start there. Once you are in,
        your Google address is confirmed on your account and you can use either way to sign in next time.
      </p>
      {pending.data && <p className={ui.status}>Google address to link: {pending.data.email}. Telegram will ask you to approve this.</p>}
      <section className={styles.block} aria-label="Telegram sign in">
        <TelegramSignIn invite={params.get('invite') ?? undefined} linkId={linkId} pollIntervalMs={pollIntervalMs} />
      </section>
    </main>
  )
}

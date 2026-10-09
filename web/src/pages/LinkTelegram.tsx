import { Navigate, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { EnvelopeSimple } from '@phosphor-icons/react'
import { api } from '../api/client'
import { useMe } from '../api/hooks'
import { TelegramSignIn } from '../components/TelegramSignIn'
import { AuthShell } from '../components/AuthShell'

export function LinkTelegram({ pollIntervalMs }: { pollIntervalMs?: number }) {
  const [params] = useSearchParams()
  const me = useMe({ publicPage: true })
  const linkId = params.get('i') ?? undefined
  const pending = useQuery({ queryKey: ['pending-link', linkId], queryFn: () => api.pendingLink(linkId!), enabled: !!linkId, retry: false })
  if (me.data) return <Navigate to="/workspace" replace />
  return (
    <AuthShell
      title="Link your Telegram first"
      lede="We could not find a Mavis account for that Google address. Mavis lives in chat, so start there. Once you are in, your Google address is confirmed on your account and you can use either way to sign in next time."
    >
      {pending.data && (
        <p className="status mb-6 flex items-start gap-2 rounded-2xl border border-line-strong bg-sunken p-3">
          <EnvelopeSimple size={18} weight="light" className="mt-0.5 shrink-0" aria-hidden="true" />
          <span>Google address to link: {pending.data.email}. Telegram will ask you to approve this.</span>
        </p>
      )}
      <section aria-label="Telegram sign in">
        <TelegramSignIn invite={params.get('invite') ?? undefined} linkId={linkId} pollIntervalMs={pollIntervalMs} />
      </section>
    </AuthShell>
  )
}

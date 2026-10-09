import type { ChannelInfo, Me } from '../api/types'

const BOT = import.meta.env.VITE_BOT_USERNAME || 'MavisAIBot'
export const BOT_URL = `https://t.me/${BOT}`

// The contract leaves the channel shape open; accept a flag, a handle or an object.
export function normalizeChannel(
  kind: 'telegram' | 'slack',
  raw: Me['channels']['telegram'],
): ChannelInfo & { open_url: string } {
  const fallback = kind === 'telegram' ? BOT_URL : import.meta.env.VITE_SLACK_URL || 'https://slack.com/app_redirect'
  if (raw && typeof raw === 'object') return { ...raw, open_url: raw.open_url ?? fallback }
  if (typeof raw === 'string') return { connected: true, label: raw, open_url: fallback }
  return { connected: Boolean(raw), open_url: fallback }
}

export const fmtDate = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : 'never'

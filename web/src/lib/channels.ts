import type { ChannelInfo, Me } from '../api/types'

// The contract leaves the channel shape open; accept a flag, a handle or an object.
// `botUrl` comes from GET /config; with neither it nor the server's open_url there is no Telegram link at all.
export function normalizeChannel(
  kind: 'telegram' | 'slack',
  raw: Me['channels']['telegram'],
  botUrl?: string | null,
): Omit<ChannelInfo, 'open_url'> & { open_url: string | null } {
  const fallback = kind === 'telegram' ? (botUrl ?? null) : 'https://slack.com/app_redirect'
  if (raw && typeof raw === 'object') return { ...raw, open_url: raw.open_url ?? fallback }
  if (typeof raw === 'string') return { connected: true, label: raw, open_url: fallback }
  return { connected: Boolean(raw), open_url: fallback }
}

export const fmtDate = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : 'never'

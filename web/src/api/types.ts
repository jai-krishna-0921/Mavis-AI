// Types mirror section 3 of the dashboard design spec (/api/v1).

export type ChannelInfo = { connected: boolean; label?: string; open_url?: string }

export type Me = {
  user_id: string
  name: string
  timezone: string
  currency: string
  channels: { telegram: ChannelInfo | boolean | string | null; slack: ChannelInfo | boolean | string | null }
  email: string | null
  csrf: string
  invites_left: number | null
}

export type ConnectorId = 'google' | 'slack'
export type ConnectorStatus = 'active' | 'failed' | 'none'

export type Connector = {
  id: ConnectorId
  name: string
  description: string
  status: ConnectorStatus
  account: string | null
  scopes_granted: string[]
  missing_scopes: string[]
  connected_at: string | null
}

export type VaultKind = 'person' | 'organisation' | 'project' | 'fact' | 'preference'
export type VaultSource = 'you' | 'gmail' | 'slack' | 'calendar' | 'dashboard'

export type VaultItem = {
  id: string
  kind: VaultKind
  title: string
  detail?: string | null
  source: VaultSource
  trust: 'user' | 'high' | 'medium' | 'low'
  updated_at: string
}

export type VaultPage = { items: VaultItem[]; next_cursor: string | null }

export type VaultSummary = {
  profile: Record<string, string>
  counts: { people: number; organisations: number; projects: number; facts: number; sources: number }
}

export type SourceInfo = { source: VaultSource; count: number; last_sync: string | null }

export type Preferences = {
  name: string
  timezone: string
  quiet_hours: { start: string; end: string } | null
  proactive_channel: 'telegram' | 'slack' | 'both'
  morning_checkin_time: string | null
  language_register_opt_out: boolean
}

export type Invite = { code: string; link: string; name?: string | null; uses: number; max_uses: number | null }

export type TelegramStart = { nonce: string; deep_link: string; expires_at: string }
export type PollStatus = { status: 'pending' | 'ok' | 'expired' }

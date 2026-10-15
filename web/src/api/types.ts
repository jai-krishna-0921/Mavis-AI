// Copyright (c) 2026 Jai Krishna. All rights reserved.
// Proprietary. Use is governed by LICENSE in the repository root.
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

export type ConnectorId = string
export type ConnectorStatus = 'active' | 'failed' | 'none'

export type Connector = {
  id: ConnectorId
  name: string
  description: string
  icon: string
  services: string[]
  status: ConnectorStatus
  account: string | null
  scopes_granted: string[]
  missing_scopes: string[]
  connected_at: string | null
  notice?: string | null
}

export type McpStatus = 'ACTIVE' | 'NEEDS_AUTH' | 'ERROR'
export type McpAuth = 'none' | 'bearer' | 'oauth'
export type McpTool = { name: string; kind: 'read' | 'act'; description: string }

// A tool server the user connected (GET /mcp/servers). `host` has no path or query; secrets never appear.
export type McpServer = {
  id: number
  slug: string
  name: string
  host: string
  catalog_id: string | null
  auth: McpAuth
  status: McpStatus
  error: string | null
  error_text: string | null
  tool_count: number
  tools: McpTool[]
  tools_fetched_at: string | null
}

export type McpCatalogEntry = { id: string; name: string; description: string; auth: 'none' | 'oauth'; url: string }
export type McpCreated = { server: McpServer; authorize_url?: string }
export type McpCreateBody = { catalog_id: string } | { name: string; url: string; bearer_token?: string }

export type VaultKind = 'person' | 'organisation' | 'project' | 'fact' | 'preference'
export type VaultSource = 'you' | 'gmail' | 'slack' | 'calendar' | 'dashboard'

export type VaultItem = {
  id: string
  kind: VaultKind
  title: string
  detail?: string | null
  source: VaultSource
  trust: 'user' | 'high' | 'medium' | 'low'
  updated_at: string | null
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

// `link` is only returned when the invite is created: the server keeps a hash of the code, so the list gives `hint`.
export type Invite = { code: string; link: string | null; hint?: string; name?: string | null; uses: number; max_uses: number | null }

export type TelegramStart = { nonce: string; deep_link: string; expires_at: string; code: string; link_email: string | null }
export type PollStatus = { status: 'pending' | 'ok' | 'expired' }

// Public, unauthenticated settings (GET /config). Every Telegram link on the site comes from here.
export type PublicConfig = {
  bot_username: string | null
  bot_url: string | null
  slack_enabled: boolean
  google_signin_enabled: boolean
  mcp_enabled?: boolean
}

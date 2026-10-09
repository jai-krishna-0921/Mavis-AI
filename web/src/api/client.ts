import type {
  Connector, ConnectorId, Invite, Me, PollStatus, Preferences, SourceInfo, TelegramStart,
  VaultItem, VaultKind, VaultPage, VaultSummary,
} from './types'

export const API_BASE = '/api/v1'

export class ApiError extends Error {
  status: number
  code: string
  constructor(status: number, code: string, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

// The session cookie is HttpOnly: this module never reads it. The CSRF token
// comes from GET /me and rides on every mutating request.
let csrfToken: string | null = null
export const setCsrf = (t: string | null) => { csrfToken = t }
export const getCsrf = () => csrfToken

let unauthorized: () => void = () => { window.location.assign('/login') }
export const setUnauthorizedHandler = (fn: () => void) => { unauthorized = fn }

type Opts = { method?: string; body?: unknown; silent401?: boolean }

export async function request<T>(path: string, opts: Opts = {}): Promise<T> {
  const method = opts.method ?? 'GET'
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json'
  if (method !== 'GET' && method !== 'HEAD' && csrfToken) headers['X-Mavis-CSRF'] = csrfToken
  let res: Response
  try {
    res = await fetch(API_BASE + path, {
      method,
      headers,
      credentials: 'same-origin',
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    })
  } catch {
    throw new ApiError(0, 'network', 'Could not reach Mavis. Check your connection and try again.')
  }
  if (res.status === 401 && !opts.silent401) unauthorized()
  if (!res.ok) {
    let code = 'error'
    let message = 'Something went wrong. Please try again.'
    try {
      const j = (await res.json()) as { error?: string; message?: string }
      code = j.error ?? code
      message = j.message ?? message
    } catch { /* keep defaults */ }
    throw new ApiError(res.status, code, message)
  }
  if (res.status === 204) return undefined as T
  const text = await res.text()
  return (text ? JSON.parse(text) : undefined) as T
}

const q = (params: Record<string, string | undefined>) => {
  const s = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v) s.set(k, v)
  const out = s.toString()
  return out ? `?${out}` : ''
}

export const api = {
  async me(): Promise<Me> {
    const me = await request<Me>('/me', { silent401: true })
    setCsrf(me.csrf)
    return me
  },
  telegramStart: (invite?: string, linkId?: string) =>
    request<TelegramStart>('/auth/telegram/start', {
      method: 'POST', body: { ...(invite ? { invite } : {}), ...(linkId ? { link_id: linkId } : {}) },
    }),
  pendingLink: (linkId: string) => request<{ email: string }>(`/auth/telegram/link${q({ i: linkId })}`, { silent401: true }),
  telegramPoll: (nonce: string) =>
    request<PollStatus>(`/auth/telegram/poll${q({ nonce })}`, { silent401: true }),
  logout: (all = false) => request<void>('/auth/logout', { method: 'POST', body: { all } }),
  connectors: () => request<Connector[]>('/connectors'),
  connect: (id: ConnectorId) => request<{ url: string }>(`/connectors/${id}/connect`, { method: 'POST', body: {} }),
  disconnect: (id: ConnectorId, forget: boolean) =>
    request<void>(`/connectors/${id}/disconnect`, { method: 'POST', body: { forget } }),
  vaultSummary: () => request<VaultSummary>('/vault/summary'),
  vaultItems: (p: { kind: VaultKind; q?: string; cursor?: string }) =>
    request<VaultPage>(`/vault/items${q({ kind: p.kind, q: p.q, cursor: p.cursor })}`),
  vaultPatch: (id: string, patch: { title?: string; detail?: string }) =>
    request<VaultItem>(`/vault/items/${encodeURIComponent(id)}`, { method: 'PATCH', body: patch }),
  vaultForget: (id: string, alsoSuppress: boolean) =>
    request<void>(`/vault/items/${encodeURIComponent(id)}`, { method: 'DELETE', body: { also_suppress: alsoSuppress } }),
  vaultSources: () => request<SourceInfo[]>('/vault/sources'),
  vaultForgetSource: (source: string) =>
    request<void>(`/vault/sources/${encodeURIComponent(source)}/forget`, { method: 'POST', body: {} }),
  preferences: () => request<Preferences>('/preferences'),
  patchPreferences: (patch: Partial<Preferences>) =>
    request<Preferences>('/preferences', { method: 'PATCH', body: patch }),
  invites: () => request<Invite[]>('/invites'),
  createInvite: (name?: string) => request<Invite>('/invites', { method: 'POST', body: name ? { name } : {} }),
  revokeInvite: (code: string) => request<void>(`/invites/${encodeURIComponent(code)}`, { method: 'DELETE' }),
  deleteAccount: () => request<void>('/account/delete', { method: 'POST', body: { confirm: 'DELETE' } }),
}

// Copyright (c) 2026 Jai Krishna. All rights reserved.
// Proprietary. Use is governed by LICENSE in the repository root.
import { http, HttpResponse } from 'msw'
import { db } from './db'
import type { McpServer, Preferences, VaultItem, VaultKind } from '../api/types'

const B = '/api/v1'
const err = (status: number, error: string, message: string) => HttpResponse.json({ error, message }, { status })

const unauth = () => err(401, 'unauthorized', 'Please sign in to continue.')
const needCsrf = (req: Request) =>
  req.headers.get('X-Mavis-CSRF') !== db().me.csrf ? err(403, 'csrf', 'Your session could not be verified. Reload the page and try again.') : null

const PAGE = 8

export const MCP_CATALOG = [
  { id: 'linear', name: 'Linear', description: 'Issues, projects and comments in your Linear workspace.', auth: 'oauth' as const, url: 'https://mcp.linear.app/mcp' },
  { id: 'context7', name: 'Context7', description: 'Current documentation and code examples for libraries.', auth: 'none' as const, url: 'https://mcp.context7.com/mcp' },
]

export const mcpServer = (id: number, over: Partial<McpServer> = {}): McpServer => ({
  id, slug: `server_${id}`, name: `Server ${id}`, host: 'mcp.example.com', catalog_id: null, auth: 'none', status: 'ACTIVE',
  error: null, error_text: null, tool_count: 2, tools_fetched_at: '2026-10-14T09:00:00Z',
  tools: [
    { name: 'get_note', kind: 'read', description: 'Read a note.' },
    { name: 'create_note', kind: 'act', description: 'Make a note.' },
  ],
  ...over,
})

export const handlers = [
  http.get(`${B}/config`, () => HttpResponse.json({
    bot_username: 'Mavis247_bot', bot_url: 'https://telegram.me/Mavis247_bot', slack_enabled: true, google_signin_enabled: true, mcp_enabled: true,
  })),
  http.post(`${B}/auth/telegram/start`, async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as { invite?: string }
    db().resetPoll()
    const nonce = Math.random().toString(16).slice(2).padEnd(32, '0')
    const tail = body.invite ? `_${body.invite}` : ''
    return HttpResponse.json({
      nonce, code: '4827', link_email: null, deep_link: `https://telegram.me/Mavis247_bot?start=login_${nonce}${tail}`,
      expires_at: new Date(Date.now() + 600_000).toISOString(),
    })
  }),
  http.get(`${B}/auth/telegram/poll`, () => {
    const d = db()
    if (d.bumpPoll() >= d.pollsUntilOk) { d.loggedIn = true; return HttpResponse.json({ status: 'ok' }) }
    return HttpResponse.json({ status: 'pending' })
  }),
  http.get(`${B}/auth/google/start`, () => HttpResponse.redirect('/workspace', 302)),
  http.post(`${B}/auth/logout`, ({ request }) => {
    const bad = needCsrf(request); if (bad) return bad
    db().loggedIn = false
    return new HttpResponse(null, { status: 204 })
  }),
  http.get(`${B}/me`, () => (db().loggedIn && !db().deleted ? HttpResponse.json(db().me) : unauth())),

  http.get(`${B}/connectors`, () => HttpResponse.json(db().connectors)),
  http.post(`${B}/connectors/:id/connect`, ({ request, params }) => {
    const bad = needCsrf(request); if (bad) return bad
    return HttpResponse.json({ url: `/oauth/mock/${String(params.id)}` })
  }),
  http.post(`${B}/connectors/:id/disconnect`, async ({ request, params }) => {
    const bad = needCsrf(request); if (bad) return bad
    const body = (await request.json()) as { forget?: boolean }
    const c = db().connectors.find((x) => x.id === params.id)
    if (!c) return err(404, 'not_found', 'That connector does not exist.')
    c.status = 'none'; c.account = null; c.scopes_granted = []; c.missing_scopes = []; c.connected_at = null
    if (body.forget !== false) db().items = db().items.filter((i) => i.source !== (c.id === 'google' ? 'gmail' : 'slack'))
    return new HttpResponse(null, { status: 204 })
  }),

  http.get(`${B}/mcp/catalog`, () => HttpResponse.json(MCP_CATALOG)),
  http.get(`${B}/mcp/servers`, () => HttpResponse.json(db().mcpServers)),
  http.post(`${B}/mcp/servers`, async ({ request }) => {
    const bad = needCsrf(request); if (bad) return bad
    const body = (await request.json()) as { catalog_id?: string; name?: string; url?: string; bearer_token?: string }
    const d = db()
    if (body.catalog_id) {
      const entry = MCP_CATALOG.find((e) => e.id === body.catalog_id)
      if (!entry) return err(422, 'invalid_catalog_id', 'That server is not in the list.')
      const oauth = entry.auth === 'oauth'
      const server = mcpServer(d.newMcpId(), { slug: entry.id, name: entry.name, catalog_id: entry.id, auth: entry.auth, status: oauth ? 'NEEDS_AUTH' : 'ACTIVE', tool_count: oauth ? 0 : 2, tools: oauth ? [] : mcpServer(0).tools })
      d.mcpServers.push(server)
      return HttpResponse.json(oauth ? { server, authorize_url: `https://auth.example.com/oauth/mock/mcp/${entry.id}` } : { server }, { status: 201 })
    }
    if (!body.url?.startsWith('https://')) return err(422, 'invalid_url', 'That address is not allowed. Use a public https address.')
    const server = mcpServer(d.newMcpId(), { name: body.name ?? 'Custom', auth: body.bearer_token ? 'bearer' : 'none', host: new URL(body.url).host })
    d.mcpServers.push(server)
    return HttpResponse.json({ server }, { status: 201 })
  }),
  http.post(`${B}/mcp/servers/:id/connect`, async ({ request, params }) => {
    const bad = needCsrf(request); if (bad) return bad
    const server = db().mcpServers.find((s) => s.id === Number(params.id))
    if (!server) return err(404, 'not_found', 'That server was not found.')
    if (server.auth === 'oauth') return HttpResponse.json({ server, authorize_url: `https://auth.example.com/oauth/mock/mcp/${server.slug}` })
    const body = (await request.json().catch(() => ({}))) as { bearer_token?: string }
    if (!body.bearer_token) return err(422, 'invalid_bearer_token', 'Paste the server\'s token to reconnect it.')
    server.status = 'ACTIVE'; server.error = null; server.error_text = null
    return HttpResponse.json({ server })
  }),
  http.post(`${B}/mcp/servers/:id/refresh`, ({ request, params }) => {
    const bad = needCsrf(request); if (bad) return bad
    const server = db().mcpServers.find((s) => s.id === Number(params.id))
    if (!server) return err(404, 'not_found', 'That server was not found.')
    server.tools_fetched_at = '2026-10-14T10:00:00Z'
    return HttpResponse.json(server)
  }),
  http.delete(`${B}/mcp/servers/:id`, ({ request, params }) => {
    const bad = needCsrf(request); if (bad) return bad
    const d = db()
    const n = d.mcpServers.length
    d.mcpServers.splice(0, n, ...d.mcpServers.filter((s) => s.id !== Number(params.id)))
    return d.mcpServers.length < n ? new HttpResponse(null, { status: 204 }) : err(404, 'not_found', 'That server was not found.')
  }),

  http.get(`${B}/vault/summary`, () => {
    const items = db().items
    const n = (k: VaultKind) => items.filter((i) => i.kind === k).length
    return HttpResponse.json({
      profile: { name: db().me.name, timezone: db().me.timezone, currency: db().me.currency },
      counts: { people: n('person'), organisations: n('organisation'), projects: n('project'), facts: n('fact') + n('preference'), sources: db().sources().length },
    })
  }),
  http.get(`${B}/vault/items`, ({ request }) => {
    const u = new URL(request.url)
    const kind = u.searchParams.get('kind')
    const q = (u.searchParams.get('q') ?? '').toLowerCase()
    const start = Number(u.searchParams.get('cursor') ?? 0)
    const all = db().items.filter((i) => i.kind === kind && (!q || `${i.title} ${i.detail ?? ''}`.toLowerCase().includes(q)))
    const items = all.slice(start, start + PAGE)
    return HttpResponse.json({ items, next_cursor: start + PAGE < all.length ? String(start + PAGE) : null })
  }),
  http.patch(`${B}/vault/items/:id`, async ({ request, params }) => {
    const bad = needCsrf(request); if (bad) return bad
    const patch = (await request.json()) as Partial<VaultItem>
    const id = decodeURIComponent(String(params.id))
    if (id.startsWith('profile.')) {
      const key = id.slice(8) as 'name' | 'timezone' | 'currency'
      if (key === 'name' || key === 'timezone' || key === 'currency') { db().me[key] = patch.title ?? ''; if (key !== 'currency') db().preferences[key] = patch.title ?? '' }
      return HttpResponse.json({ id, kind: 'fact', title: patch.title ?? '', source: 'dashboard', trust: 'user', updated_at: new Date().toISOString() })
    }
    const it = db().items.find((i) => i.id === id)
    if (!it) return err(404, 'not_found', 'That item no longer exists.')
    if (patch.title !== undefined) it.title = patch.title
    if (patch.detail !== undefined) it.detail = patch.detail
    it.source = 'dashboard'; it.trust = 'user'; it.updated_at = new Date().toISOString()
    return HttpResponse.json(it)
  }),
  http.delete(`${B}/vault/items/:id`, async ({ request, params }) => {
    const bad = needCsrf(request); if (bad) return bad
    const body = (await request.json().catch(() => ({}))) as { also_suppress?: boolean }
    const id = decodeURIComponent(String(params.id))
    if (body.also_suppress) db().suppressed.push(id)
    db().items = db().items.filter((i) => i.id !== id)
    return new HttpResponse(null, { status: 204 })
  }),
  http.get(`${B}/vault/sources`, () => HttpResponse.json(db().sources())),
  http.post(`${B}/vault/sources/:source/forget`, ({ request, params }) => {
    const bad = needCsrf(request); if (bad) return bad
    db().items = db().items.filter((i) => i.source !== params.source)
    return new HttpResponse(null, { status: 204 })
  }),

  http.get(`${B}/preferences`, () => HttpResponse.json(db().preferences)),
  http.patch(`${B}/preferences`, async ({ request }) => {
    const bad = needCsrf(request); if (bad) return bad
    const patch = (await request.json()) as Partial<Preferences>
    Object.assign(db().preferences, patch)
    if (patch.name) db().me.name = patch.name
    if (patch.timezone) db().me.timezone = patch.timezone
    return HttpResponse.json(db().preferences)
  }),

  // like the real API: the list never returns a full link (only a hint); the full link comes once, from create
  http.get(`${B}/invites`, () => HttpResponse.json(db().invites.map((i) => ({ ...i, link: null, hint: i.hint ?? String(i.code).slice(-4) })))),
  http.post(`${B}/invites`, async ({ request }) => {
    const bad = needCsrf(request); if (bad) return bad
    const body = (await request.json().catch(() => ({}))) as { name?: string }
    const d = db()
    if (d.me.invites_left !== null && d.me.invites_left <= 0) return err(409, 'invite_cap', 'You have used all your invite links. Revoke one to make another.')
    const code = Math.random().toString(36).slice(2, 8)
    const inv = { code, link: `${location.origin}/login?invite=${code}`, name: body.name ?? null, uses: 0, max_uses: 5 }
    d.invites.push(inv)
    if (d.me.invites_left !== null) d.me.invites_left -= 1
    return HttpResponse.json(inv, { status: 201 })
  }),
  http.delete(`${B}/invites/:code`, ({ request, params }) => {
    const bad = needCsrf(request); if (bad) return bad
    const d = db()
    const n = d.invites.length
    d.invites = d.invites.filter((i) => i.code !== params.code)
    if (d.invites.length < n && d.me.invites_left !== null) d.me.invites_left += 1
    return new HttpResponse(null, { status: 204 })
  }),

  http.post(`${B}/account/delete`, async ({ request }) => {
    const bad = needCsrf(request); if (bad) return bad
    const body = (await request.json()) as { confirm?: string }
    if (body.confirm !== 'DELETE') return err(400, 'confirm', 'Type DELETE to confirm.')
    db().deleted = true
    return new HttpResponse(null, { status: 202 })
  }),
]

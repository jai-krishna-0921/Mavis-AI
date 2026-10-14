// Copyright (c) 2026 Jai Krishna. All rights reserved.
// Proprietary. Use is governed by LICENSE in the repository root.
import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ArrowsClockwise, CheckCircle, Toolbox, WarningCircle } from '@phosphor-icons/react'
import {
  useConnectMcp, useCreateMcpServer, useDeleteMcp, useMcpCatalog, useMcpServers, useRefreshMcp,
} from '../api/hooks'
import type { McpCatalogEntry, McpServer, McpStatus } from '../api/types'
import { httpsUrl, redirectTo } from '../lib/nav'
import { Modal } from '../components/Modal'
import { useToast } from '../components/Toast'
import { EmptyState, IconTile, Panel, SkeletonRows } from '../components/Ui'

const STATUS_LABEL: Record<McpStatus, string> = { ACTIVE: 'Ready', NEEDS_AUTH: 'Needs sign-in', ERROR: 'Not working' }
const STATUS_TAG: Record<McpStatus, string> = { ACTIVE: 'tag-ok', NEEDS_AUTH: 'tag-warn', ERROR: 'tag-warn' }

const SIGN_IN_FAILED = 'That sign-in link could not be used. Please try again.'

const toolsLabel = (n: number) => `${n} ${n === 1 ? 'tool' : 'tools'}`

// Only an https address is a place to send the browser; anything else shows the generic error.
function signIn(url: string, toast: (msg: string, tone?: 'ok' | 'error') => void) {
  const safe = httpsUrl(url)
  if (safe) redirectTo(safe)
  else toast(SIGN_IN_FAILED, 'error')
}

function ResultNote() {
  const [params] = useSearchParams()
  const result = params.get('mcp')
  if (result === 'connected') {
    return <p className="mb-4 flex items-center gap-2 rounded-2xl border border-ok/30 bg-ok-soft p-3 text-sm" role="status"><CheckCircle size={20} weight="light" className="text-ok" aria-hidden="true" />Signed in. Mavis can use that app now.</p>
  }
  if (result === 'error') {
    return <p className="mb-4 flex items-center gap-2 rounded-2xl border border-danger/30 bg-danger-soft p-3 text-sm text-danger" role="alert"><WarningCircle size={20} weight="light" aria-hidden="true" />That sign-in did not finish. You can try again.</p>
  }
  return null
}

function CatalogCard({ entry }: { entry: McpCatalogEntry }) {
  const create = useCreateMcpServer()
  const toast = useToast()
  const connect = () =>
    create.mutate({ catalog_id: entry.id }, {
      onSuccess: (r) => { if (r.authorize_url) signIn(r.authorize_url, toast); else toast(`${entry.name} is ready`) },
    })
  return (
    <li className="row flex-wrap items-start">
      <IconTile><Toolbox size={24} weight="light" aria-hidden="true" /></IconTile>
      <div className="min-w-0 flex-1">
        <div className="row-title">{entry.name}</div>
        <div className="row-sub">{entry.description}</div>
        {create.error && <p className="error" role="alert">{create.error.message}</p>}
      </div>
      <div className="row-actions">
        <button type="button" className="btn btn-sm" disabled={create.isPending} onClick={connect} aria-label={`Connect ${entry.name}`}>
          {entry.auth === 'oauth' ? 'Sign in' : 'Connect'}
        </button>
      </div>
    </li>
  )
}

function TokenDialog({ server, onClose }: { server: McpServer; onClose: () => void }) {
  const [token, setToken] = useState('')
  const connect = useConnectMcp()
  const toast = useToast()
  return (
    <Modal title={`Reconnect ${server.name}`} onClose={onClose}>
      <p className="text-muted">Paste a new token for {server.name}. It is stored encrypted and never shown again.</p>
      <div className="field mt-4">
        <label htmlFor={`tok-${server.id}`}>Token</label>
        <input id={`tok-${server.id}`} className="input" type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} />
      </div>
      {connect.error && <p className="error" role="alert">{connect.error.message}</p>}
      <div className="mt-6 flex flex-wrap justify-end gap-2">
        <button type="button" className="btn" onClick={onClose}>Cancel</button>
        <button type="button" className="btn btn-primary" disabled={connect.isPending || !token.trim()}
          onClick={() => connect.mutate({ id: server.id, bearerToken: token.trim() }, { onSettled: () => setToken(''), onSuccess: () => { toast(`${server.name} reconnected`); onClose() } })}>
          Reconnect
        </button>
      </div>
    </Modal>
  )
}

function RemoveDialog({ server, onClose }: { server: McpServer; onClose: () => void }) {
  const del = useDeleteMcp()
  const toast = useToast()
  return (
    <Modal title={`Remove ${server.name}`} onClose={onClose}>
      <p className="text-muted">Mavis will stop using {server.name} and delete the sign-in it holds. Nothing in {server.name} itself is changed.</p>
      {del.error && <p className="error" role="alert">{del.error.message}</p>}
      <div className="mt-6 flex flex-wrap justify-end gap-2">
        <button type="button" className="btn" onClick={onClose}>Cancel</button>
        <button type="button" className="btn btn-danger-solid" disabled={del.isPending}
          onClick={() => del.mutate(server.id, { onSuccess: () => { toast(`${server.name} removed`); onClose() } })}>
          Remove
        </button>
      </div>
    </Modal>
  )
}

function ServerRow({ server }: { server: McpServer }) {
  const [dialog, setDialog] = useState<'remove' | 'token' | null>(null)
  const refresh = useRefreshMcp()
  const connect = useConnectMcp()
  const toast = useToast()
  const reconnect = () => {
    if (server.auth === 'bearer') { setDialog('token'); return }
    connect.mutate({ id: server.id }, { onSuccess: (r) => { if (r.authorize_url) signIn(r.authorize_url, toast) } })
  }
  const err = refresh.error ?? connect.error
  return (
    <li className="row flex-wrap items-start">
      <IconTile tone={server.status === 'ACTIVE' ? 'accent' : 'plain'}><Toolbox size={24} weight="light" aria-hidden="true" /></IconTile>
      <div className="min-w-0 flex-1">
        <div className="row-title flex flex-wrap items-center gap-2">
          {server.name}
          <span className={`tag tag-dot ${STATUS_TAG[server.status]}`}>{STATUS_LABEL[server.status]}</span>
        </div>
        <div className="row-sub">{server.host}, {toolsLabel(server.tool_count)}</div>
        {server.error_text && <div className="row-sub mt-0.5" data-testid={`mcp-error-${server.id}`}>{server.error_text}</div>}
        {server.tools.length > 0 && (
          <details className="mt-3">
            <summary className="cursor-pointer text-sm text-muted">Show {toolsLabel(server.tool_count)}</summary>
            <ul className="m-0 mt-2 grid list-none gap-2 p-0" aria-label={`${server.name} tools`}>
              {server.tools.map((t) => (
                <li key={t.name} className="text-sm">
                  <span className="font-medium">{t.name}</span>{' '}
                  <span className={`tag ${t.kind === 'read' ? 'tag-ok' : 'tag-warn'}`}>{t.kind === 'read' ? 'reads' : 'asks first'}</span>
                  {t.description && <div className="row-sub">{t.description}</div>}
                </li>
              ))}
            </ul>
          </details>
        )}
        {err && <p className="error" role="alert">{err.message}</p>}
      </div>
      <div className="row-actions">
        <button type="button" className="btn btn-sm" disabled={refresh.isPending} onClick={() => refresh.mutate(server.id, { onSuccess: () => toast(`${server.name} refreshed`) })} aria-label={`Refresh ${server.name}`}>
          <ArrowsClockwise size={14} weight="light" aria-hidden="true" /> Refresh
        </button>
        {server.status === 'NEEDS_AUTH' && (
          <button type="button" className="btn btn-sm" disabled={connect.isPending} onClick={reconnect} aria-label={`Reconnect ${server.name}`}>Reconnect</button>
        )}
        <button type="button" className="btn btn-sm btn-danger" onClick={() => setDialog('remove')} aria-label={`Remove ${server.name}`}>Remove</button>
      </div>
      {dialog === 'remove' && <RemoveDialog server={server} onClose={() => setDialog(null)} />}
      {dialog === 'token' && <TokenDialog server={server} onClose={() => setDialog(null)} />}
    </li>
  )
}

function CustomForm() {
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [token, setToken] = useState('')
  const create = useCreateMcpServer()
  const toast = useToast()
  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    create.mutate({ name: name.trim(), url: url.trim(), ...(token.trim() ? { bearer_token: token.trim() } : {}) }, {
      onSettled: () => setToken(''),
      onSuccess: (r) => {
        if (r.authorize_url) { signIn(r.authorize_url, toast); return }
        toast(`${r.server.name} is ready`)
        setName(''); setUrl(''); setToken('')
      },
    })
  }
  return (
    <form onSubmit={submit} className="mt-6" aria-label="Add a custom server">
      <h3 className="text-base font-bold">Add a custom server</h3>
      <p className="mt-1 text-sm text-muted">Any MCP server reachable over https. Mavis asks you before it changes anything there.</p>
      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        <div className="field !mb-0"><label htmlFor="mcp-name">Name</label><input id="mcp-name" className="input" value={name} onChange={(e) => setName(e.target.value)} maxLength={80} required /></div>
        <div className="field !mb-0"><label htmlFor="mcp-url">Address (https)</label><input id="mcp-url" className="input" type="url" placeholder="https://mcp.example.com/mcp" value={url} onChange={(e) => setUrl(e.target.value)} required /></div>
        <div className="field !mb-0 sm:col-span-2"><label htmlFor="mcp-token">Token (only if the server gives you one)</label><input id="mcp-token" className="input" type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} /></div>
      </div>
      {create.error && <p className="error mt-3" role="alert">{create.error.message}</p>}
      <div className="mt-4"><button type="submit" className="btn btn-primary" disabled={create.isPending || !name.trim() || !url.trim()}>Add server</button></div>
    </form>
  )
}

export function McpSection() {
  const catalog = useMcpCatalog(true)
  const servers = useMcpServers(true)
  const connected = new Set((servers.data ?? []).map((s) => s.catalog_id).filter(Boolean))
  const available = (catalog.data ?? []).filter((e) => !connected.has(e.id))
  return (
    <Panel id="mcp" title="Tools from other apps (MCP)" lede="Connect apps that speak MCP. Mavis reads from them on its own and asks you before it changes anything.">
      <ResultNote />
      {servers.isPending && <SkeletonRows rows={1} label="Loading connected tools" />}
      {servers.error && <p className="error" role="alert">{servers.error.message}</p>}
      {servers.data && servers.data.length > 0 && (
        <ul className="m-0 mb-6 list-none p-0" aria-label="Connected tool servers">
          {servers.data.map((s) => <ServerRow key={s.id} server={s} />)}
        </ul>
      )}
      {servers.data?.length === 0 && (
        <EmptyState icon={<Toolbox size={24} weight="light" />} title="No apps connected yet">Pick one below, or add your own server.</EmptyState>
      )}
      {available.length > 0 && (
        <>
          <h3 className="mb-1 mt-6 text-base font-bold">Add an app</h3>
          <ul className="m-0 list-none p-0" aria-label="Apps you can connect">
            {available.map((e) => <CatalogCard key={e.id} entry={e} />)}
          </ul>
        </>
      )}
      <CustomForm />
    </Panel>
  )
}

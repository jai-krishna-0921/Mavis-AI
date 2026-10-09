import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ArrowSquareOut, GoogleLogo, Plugs, SlackLogo, TelegramLogo, WarningCircle, CheckCircle } from '@phosphor-icons/react'
import { useConfig, useConnectors, useDisconnect, useMe } from '../api/hooks'
import { api } from '../api/client'
import type { Connector } from '../api/types'
import { normalizeChannel, fmtDate } from '../lib/channels'
import { redirectTo } from '../lib/nav'
import { CopyButton } from '../components/CopyButton'
import { Modal } from '../components/Modal'
import { SERVICES, ServiceIcon } from '../components/ServiceIcon'
import { useToast } from '../components/Toast'
import { EmptyState, IconTile, PageHead, Panel, SkeletonRows } from '../components/Ui'

function Contact() {
  const me = useMe()
  const cfg = useConfig()
  if (!me.data) return null
  const rows = (['telegram', 'slack'] as const).map((k) => ({
    k, name: k === 'telegram' ? 'Telegram' : 'Slack',
    Icon: k === 'telegram' ? TelegramLogo : SlackLogo,
    ch: normalizeChannel(k, me.data.channels[k], cfg.data?.bot_url),
  }))
  return (
    <Panel id="contact" title="Contact" lede="Where to reach Mavis.">
      <ul className="m-0 list-none p-0">
        {rows.map(({ k, name, Icon, ch }) => (
          <li key={k} className="row flex-wrap">
            <IconTile tone={ch.connected ? 'accent' : 'plain'}><Icon size={24} weight="light" aria-hidden="true" /></IconTile>
            <div className="min-w-0 flex-1">
              <div className="row-title">{name}</div>
              <div className="row-sub">{ch.connected ? (ch.label ?? 'Connected') : 'Not linked yet'}</div>
            </div>
            <div className="row-actions">
              {ch.open_url ? (
                <>
                  <CopyButton text={ch.open_url} label="Copy link" what={`to ${name}`} />
                  <a className="btn btn-sm" href={ch.open_url} target="_blank" rel="noopener noreferrer" aria-label={`Open ${name}`}>
                    Open <ArrowSquareOut size={14} weight="light" aria-hidden="true" />
                  </a>
                </>
              ) : (
                <span className="status">Link unavailable</span>
              )}
            </div>
          </li>
        ))}
      </ul>
    </Panel>
  )
}

function DisconnectDialog({ c, onClose }: { c: Connector; onClose: () => void }) {
  const [forget, setForget] = useState(true)
  const dis = useDisconnect()
  const toast = useToast()
  return (
    <Modal title={`Disconnect ${c.name}`} onClose={onClose}>
      <p className="text-muted">Mavis will stop reading {c.account ?? c.name}. What should happen to what it learned from there?</p>
      <fieldset className="m-0 mt-5 grid gap-3 border-0 p-0">
        <legend className="sr-only">What to do with learned data</legend>
        <label className="check rounded-2xl border border-white/10 p-4 has-[:checked]:border-accent/50 has-[:checked]:bg-accent/[0.06]">
          <input type="radio" name="forget" checked={forget} onChange={() => setForget(true)} />
          <span><strong>Disconnect and forget.</strong> Remove people, facts and projects learned from {c.name}.</span>
        </label>
        <label className="check rounded-2xl border border-white/10 p-4 has-[:checked]:border-accent/50 has-[:checked]:bg-accent/[0.06]">
          <input type="radio" name="forget" checked={!forget} onChange={() => setForget(false)} />
          <span><strong>Disconnect, keep what was learned.</strong> You can forget it later in the Vault.</span>
        </label>
      </fieldset>
      {dis.error && <p className="error" role="alert">{dis.error.message}</p>}
      <div className="mt-6 flex flex-wrap justify-end gap-2">
        <button type="button" className="btn" onClick={onClose}>Cancel</button>
        <button type="button" className="btn btn-danger-solid" disabled={dis.isPending}
          onClick={() => dis.mutate({ id: c.id, forget }, { onSuccess: () => { toast(`${c.name} disconnected`); onClose() } })}>
          Disconnect
        </button>
      </div>
    </Modal>
  )
}

function ConnectorRow({ c }: { c: Connector }) {
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const connect = async () => {
    setBusy(true); setErr('')
    try { redirectTo((await api.connect(c.id)).url) } catch (e) { setErr((e as Error).message); setBusy(false) }
  }
  const active = c.status === 'active'
  const Icon = c.id === 'google' ? GoogleLogo : SlackLogo
  return (
    <li className="row flex-wrap items-start">
      <IconTile tone={active ? 'accent' : 'plain'}><Icon size={24} weight={c.id === 'google' ? 'bold' : 'light'} aria-hidden="true" /></IconTile>
      <div className="min-w-0 flex-1">
        <div className="row-title flex flex-wrap items-center gap-2">
          {c.name}
          <span className={`tag tag-dot ${active ? 'tag-ok' : c.status === 'failed' ? 'tag-warn' : ''}`}>
            {active ? 'Connected' : c.status === 'failed' ? 'Needs attention' : 'Not connected'}
          </span>
        </div>
        <div className="row-sub">{c.description}</div>
        {c.account && <div className="row-sub mt-0.5">{c.account}{c.connected_at ? `, since ${fmtDate(c.connected_at)}` : ''}</div>}
        {c.id === 'google' && (active || c.status === 'failed') && (
          <ul className="m-0 mt-4 flex list-none flex-wrap gap-2 p-0" aria-label="Google services">
            {SERVICES.map((s) => {
              const granted = c.scopes_granted.includes(s)
              return (
                <li key={s} className={`flex items-center gap-2 rounded-full border px-3 py-1.5 text-[13px] capitalize ${granted ? 'border-ok/25 bg-ok/[0.07] text-ink' : 'border-white/10 text-muted'}`}>
                  <ServiceIcon name={s} size={18} />
                  <span>{s}</span>
                  <span className="sr-only">{granted ? 'allowed' : 'not allowed'}</span>
                </li>
              )
            })}
          </ul>
        )}
        {c.missing_scopes.length > 0 && <div className="row-sub mt-3">Not allowed yet: {c.missing_scopes.join(', ')}.</div>}
        {err && <p className="error" role="alert">{err}</p>}
      </div>
      <div className="row-actions">
        <button type="button" className="btn btn-sm" disabled={busy} onClick={() => void connect()}>
          {active ? 'Add account' : c.status === 'failed' ? 'Reconnect' : 'Connect'}
        </button>
        {(active || c.status === 'failed') && (
          <button type="button" className="btn btn-sm btn-danger" onClick={() => setConfirm(true)}>Disconnect</button>
        )}
      </div>
      {confirm && <DisconnectDialog c={c} onClose={() => setConfirm(false)} />}
    </li>
  )
}

function ConnectNote() {
  const [params] = useSearchParams()
  if (params.get('connected')) {
    return <p className="mb-4 flex items-center gap-2 rounded-2xl border border-ok/25 bg-ok/[0.07] p-3 text-sm" role="status"><CheckCircle size={20} weight="light" className="text-ok" aria-hidden="true" />Connected. Mavis is reading it now.</p>
  }
  if (params.get('error')) {
    return <p className="mb-4 flex items-center gap-2 rounded-2xl border border-danger/30 bg-danger/10 p-3 text-sm text-danger" role="alert"><WarningCircle size={20} weight="light" aria-hidden="true" />That account was not connected. You can try again.</p>
  }
  return null
}

export function Workspace() {
  const conns = useConnectors()
  return (
    <div>
      <PageHead title="Workspace" lede="Where Mavis lives and what it can see." />
      <Contact />
      <Panel id="connectors" title="Connectors" lede="Accounts Mavis can read and act on, only with your permission.">
        <ConnectNote />
        {conns.isPending && <SkeletonRows rows={2} />}
        {conns.error && <p className="error" role="alert">{conns.error.message}</p>}
        {conns.data?.length === 0 && <EmptyState icon={<Plugs size={24} weight="light" />} title="Nothing to connect yet">Connectors appear here once they are available.</EmptyState>}
        <ul className="m-0 list-none p-0">
          {conns.data?.map((c) => <ConnectorRow key={c.id} c={c} />)}
        </ul>
      </Panel>
    </div>
  )
}

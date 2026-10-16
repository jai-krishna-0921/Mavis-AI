// Copyright (c) 2026 Jai Krishna. All rights reserved.
// Proprietary. Use is governed by LICENSE in the repository root.
import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ArrowSquareOut, Plugs, SlackLogo, TelegramLogo, WarningCircle, CheckCircle, Circle } from '@phosphor-icons/react'
import { useConfig, useConnectors, useDisconnect, useMe } from '../api/hooks'
import { api } from '../api/client'
import type { Connector } from '../api/types'
import { normalizeChannel, fmtDate } from '../lib/channels'
import { httpsUrl, redirectTo } from '../lib/nav'
import { CopyButton } from '../components/CopyButton'
import { Modal } from '../components/Modal'
import { ConnectorIcon } from '../components/ConnectorIcon'
import { ServiceIcon } from '../components/ServiceIcon'
import { useToast } from '../components/Toast'
import { EmptyState, IconTile, PageHead, Panel, SkeletonRows } from '../components/Ui'
import { McpSection } from './McpSection'

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
        <label className="check rounded-2xl border border-line-strong p-4 has-[:checked]:border-accent-hi/50 has-[:checked]:bg-accent-soft">
          <input type="radio" name="forget" checked={forget} onChange={() => setForget(true)} />
          <span><strong>Disconnect and forget.</strong> Remove people, facts and projects learned from {c.name}.</span>
        </label>
        <label className="check rounded-2xl border border-line-strong p-4 has-[:checked]:border-accent-hi/50 has-[:checked]:bg-accent-soft">
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

function useStartConnect(id: Connector['id']) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const connect = async () => {
    setBusy(true); setErr('')
    try { redirectTo((await api.connect(id)).url) } catch (e) { setErr((e as Error).message); setBusy(false) }
  }
  return { busy, err, connect }
}

const START_HIDDEN = 'mavis.start.hidden'

function readHidden(): boolean {
  try { return localStorage.getItem(START_HIDDEN) === '1' } catch { return false }
}

function StartConnect({ c }: { c: Connector }) {
  const { busy, err, connect } = useStartConnect(c.id)
  return (
    <>
      <button type="button" className="btn btn-sm" disabled={busy} onClick={() => void connect()} aria-label={`Connect ${c.name}`}>Connect</button>
      {err && <p className="error w-full" role="alert">{err}</p>}
    </>
  )
}

// First-run checklist: three steps read from what is actually linked; it goes away once they are done.
function GettingStarted() {
  const me = useMe()
  const cfg = useConfig()
  const conns = useConnectors()
  const [hidden, setHidden] = useState(readHidden)
  if (!me.data || !conns.data || hidden) return null
  const tg = normalizeChannel('telegram', me.data.channels.telegram, cfg.data?.bot_url)
  const google = conns.data.find((c) => c.id === 'google')
  const slack = conns.data.find((c) => c.id === 'slack')
  const microsoft = conns.data.find((c) => c.id === 'microsoft')
  const steps = [
    {
      key: 'telegram', title: 'Say hi to Mavis on Telegram', done: tg.connected,
      sub: 'Mavis lives in your chat. Tell it what is on your plate this week.',
      action: tg.open_url ? (
        <a className="btn btn-sm" href={tg.open_url} target="_blank" rel="noopener noreferrer" aria-label="Open Telegram">
          Open <ArrowSquareOut size={14} weight="light" aria-hidden="true" />
        </a>
      ) : null,
    },
    ...(google ? [{
      key: 'google', title: 'Connect Google', done: google.status === 'active',
      sub: 'Gmail, Calendar and Drive, so Mavis knows your day.', action: <StartConnect c={google} />,
    }] : []),
    ...(slack ? [{
      key: 'slack', title: 'Connect Slack (optional)', done: slack.status === 'active',
      sub: 'Message Mavis from Slack too, in any workspace.', action: <StartConnect c={slack} />,
    }] : []),
    ...(microsoft ? [{
      key: 'microsoft', title: 'Connect Microsoft 365 (optional)', done: microsoft.status === 'active',
      sub: 'Outlook mail and calendar, Teams chats and OneDrive.', action: <StartConnect c={microsoft} />,
    }] : []),
  ]
  const done = steps.filter((st) => st.done).length
  if (done === steps.length) return null
  const hide = () => {
    try { localStorage.setItem(START_HIDDEN, '1') } catch { /* private mode: hide for this visit only */ }
    setHidden(true)
  }
  return (
    <Panel id="start" title="Get started" lede={`${done} of ${steps.length} done. It takes about two minutes.`}
      action={<button type="button" className="btn btn-sm" onClick={hide}>Hide</button>}>
      <ol className="m-0 list-none p-0" aria-label="Getting started steps">
        {steps.map((st) => (
          <li key={st.key} className="row flex-wrap">
            <IconTile tone={st.done ? 'accent' : 'plain'}>
              {st.done
                ? <CheckCircle size={22} weight="light" aria-label="Done" />
                : <Circle size={22} weight="light" aria-label="To do" />}
            </IconTile>
            <div className="min-w-0 flex-1">
              <div className={`row-title ${st.done ? 'text-muted line-through' : ''}`}>{st.title}</div>
              {!st.done && <div className="row-sub">{st.sub}</div>}
            </div>
            {!st.done && st.action && <div className="row-actions">{st.action}</div>}
          </li>
        ))}
      </ol>
    </Panel>
  )
}

function ConnectorRow({ c }: { c: Connector }) {
  const [confirm, setConfirm] = useState(false)
  const { busy, err, connect } = useStartConnect(c.id)
  const active = c.status === 'active'
  return (
    <li className="row flex-wrap items-start">
      <IconTile tone={active ? 'accent' : 'plain'}><ConnectorIcon icon={c.icon} /></IconTile>
      <div className="min-w-0 flex-1">
        <div className="row-title flex flex-wrap items-center gap-2">
          {c.name}
          <span className={`tag tag-dot ${active ? 'tag-ok' : c.status === 'failed' ? 'tag-warn' : ''}`}>
            {active ? 'Connected' : c.status === 'failed' ? 'Needs attention' : 'Not connected'}
          </span>
        </div>
        <div className="row-sub">{c.description}</div>
        {c.account && <div className="row-sub mt-0.5">{c.account}{c.connected_at ? `, since ${fmtDate(c.connected_at)}` : ''}</div>}
        {c.services.length > 0 && (active || c.status === 'failed') && (
          <ul className="m-0 mt-4 flex list-none flex-wrap gap-2 p-0" aria-label={`${c.name} services`}>
            {c.services.map((s) => {
              const never = c.unavailable?.includes(s) ?? false
              const granted = !never && c.scopes_granted.includes(s)
              return (
                <li key={s} className={`flex items-center gap-2 rounded-full border px-3 py-1.5 text-[13px] capitalize ${granted ? 'border-ok/30 bg-ok-soft text-ink' : 'border-line-strong text-muted'}`}>
                  <ServiceIcon name={s} size={18} />
                  <span>{s}</span>
                  <span className="sr-only">{never ? 'not available' : granted ? 'allowed' : 'not allowed'}</span>
                </li>
              )
            })}
          </ul>
        )}
        {c.note && <p className="row-sub mt-3">{c.note}</p>}
        {c.missing_scopes.length > 0 && <div className="row-sub mt-3">Not allowed yet: {c.missing_scopes.join(', ')}.</div>}
        {c.notice && <div className="row-sub mt-3" data-testid={`${c.id}-notice`}>{c.notice}</div>}
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

function AdminConsentNote() {
  const conns = useConnectors()
  const link = httpsUrl(conns.data?.find((c) => c.id === 'microsoft')?.admin_link ?? '')
  return (
    <div className="mb-4 flex items-start gap-2 rounded-2xl border border-danger/30 bg-danger-soft p-3 text-sm text-danger" role="alert">
      <WarningCircle size={20} weight="light" className="shrink-0" aria-hidden="true" />
      <p className="m-0 min-w-0">
        {link ? (
          <>
            Your organisation needs an admin to approve Mavis AI first. Send this link to your IT admin, then connect again:{' '}
            <a className="break-all underline" href={link} target="_blank" rel="noopener noreferrer">{link}</a>
          </>
        ) : (
          'Your organisation needs an admin to approve Mavis AI first. Ask your IT admin to approve it, then connect again.'
        )}
      </p>
    </div>
  )
}

function ConnectNote() {
  const [params] = useSearchParams()
  if (params.get('connected')) {
    return <p className="mb-4 flex items-center gap-2 rounded-2xl border border-ok/30 bg-ok-soft p-3 text-sm" role="status"><CheckCircle size={20} weight="light" className="text-ok" aria-hidden="true" />Connected. Mavis is reading it now.</p>
  }
  if (params.get('error') === 'microsoft_admin_consent') return <AdminConsentNote />
  if (params.get('error')) {
    return <p className="mb-4 flex items-center gap-2 rounded-2xl border border-danger/30 bg-danger-soft p-3 text-sm text-danger" role="alert"><WarningCircle size={20} weight="light" aria-hidden="true" />That account was not connected. You can try again.</p>
  }
  return null
}

export function Workspace() {
  const conns = useConnectors()
  const cfg = useConfig()
  return (
    <div>
      <PageHead title="Workspace" lede="Where Mavis lives and what it can see." />
      <GettingStarted />
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
      {cfg.data?.mcp_enabled && <McpSection />}
    </div>
  )
}

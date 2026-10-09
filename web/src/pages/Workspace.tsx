import { useState } from 'react'
import { useConnectors, useDisconnect, useMe } from '../api/hooks'
import { api } from '../api/client'
import type { Connector } from '../api/types'
import { normalizeChannel, fmtDate } from '../lib/channels'
import { redirectTo } from '../lib/nav'
import { CopyButton } from '../components/CopyButton'
import { Modal } from '../components/Modal'
import { SERVICES, ServiceIcon } from '../components/ServiceIcon'
import ui from '../styles/ui.module.css'
import styles from './Workspace.module.css'

function Contact() {
  const me = useMe()
  if (!me.data) return null
  const rows = (['telegram', 'slack'] as const).map((k) => ({ k, name: k === 'telegram' ? 'Telegram' : 'Slack', ch: normalizeChannel(k, me.data.channels[k]) }))
  return (
    <section className={ui.section} aria-labelledby="contact">
      <header><h2 id="contact">Contact</h2><p>Where to reach Mavis.</p></header>
      <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
        {rows.map(({ k, name, ch }) => (
          <li key={k} className={ui.row}>
            <div className={ui.rowMain}>
              <div className={ui.rowTitle}>{name}</div>
              <div className={ui.rowSub}>{ch.connected ? (ch.label ?? 'Connected') : 'Not linked yet'}</div>
            </div>
            <div className={ui.rowActions}>
              <CopyButton text={ch.open_url} label="Copy link" what={`to ${name}`} />
              <a className={ui.btn} href={ch.open_url} target="_blank" rel="noopener noreferrer" aria-label={`Open ${name}`}>Open</a>
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}

function DisconnectDialog({ c, onClose }: { c: Connector; onClose: () => void }) {
  const [forget, setForget] = useState(true)
  const dis = useDisconnect()
  return (
    <Modal title={`Disconnect ${c.name}`} onClose={onClose}>
      <p className={ui.lede}>Mavis will stop reading {c.account ?? c.name}. What should happen to what it learned from there?</p>
      <fieldset style={{ border: 0, padding: 0, margin: '16px 0 0', display: 'grid', gap: 12 }}>
        <legend className="sr-only">What to do with learned data</legend>
        <label className={ui.check}>
          <input type="radio" name="forget" checked={forget} onChange={() => setForget(true)} />
          <span><strong>Disconnect and forget.</strong> Remove people, facts and projects learned from {c.name}.</span>
        </label>
        <label className={ui.check}>
          <input type="radio" name="forget" checked={!forget} onChange={() => setForget(false)} />
          <span><strong>Disconnect, keep what was learned.</strong> You can forget it later in the Vault.</span>
        </label>
      </fieldset>
      {dis.error && <p className={ui.error} role="alert">{dis.error.message}</p>}
      <div className="actions" style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 20 }}>
        <button type="button" className={ui.btn} onClick={onClose}>Cancel</button>
        <button type="button" className={`${ui.btn} ${ui.btnDangerSolid}`} disabled={dis.isPending}
          onClick={() => dis.mutate({ id: c.id, forget }, { onSuccess: onClose })}>
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
  return (
    <li className={ui.row} style={{ alignItems: 'flex-start' }}>
      <div className={ui.rowMain}>
        <div className={ui.rowTitle}>
          {c.name}{' '}
          <span className={ui.tag}>{active ? 'Connected' : c.status === 'failed' ? 'Needs attention' : 'Not connected'}</span>
        </div>
        <div className={ui.rowSub}>{c.description}</div>
        {c.account && <div className={ui.rowSub}>{c.account}{c.connected_at ? `, since ${fmtDate(c.connected_at)}` : ''}</div>}
        {c.id === 'google' && (active || c.status === 'failed') && (
          <ul className={styles.services} aria-label="Google services">
            {SERVICES.map((s) => {
              const granted = c.scopes_granted.includes(s)
              return (
                <li key={s} className={granted ? styles.on : styles.off}>
                  <ServiceIcon name={s} />
                  <span>{s}</span>
                  <span className="sr-only">{granted ? 'allowed' : 'not allowed'}</span>
                </li>
              )
            })}
          </ul>
        )}
        {c.missing_scopes.length > 0 && <div className={ui.rowSub}>Not allowed yet: {c.missing_scopes.join(', ')}.</div>}
        {err && <p className={ui.error} role="alert">{err}</p>}
      </div>
      <div className={ui.rowActions}>
        <button type="button" className={ui.btn} disabled={busy} onClick={() => void connect()}>
          {active ? 'Add account' : c.status === 'failed' ? 'Reconnect' : 'Connect'}
        </button>
        {(active || c.status === 'failed') && (
          <button type="button" className={`${ui.btn} ${ui.btnDanger}`} onClick={() => setConfirm(true)}>Disconnect</button>
        )}
      </div>
      {confirm && <DisconnectDialog c={c} onClose={() => setConfirm(false)} />}
    </li>
  )
}

export function Workspace() {
  const conns = useConnectors()
  return (
    <div className={ui.page}>
      <div className={ui.pageHead}><h1>Workspace</h1><p className={ui.lede}>Where Mavis lives and what it can see.</p></div>
      <Contact />
      <section className={ui.section} aria-labelledby="connectors">
        <header><h2 id="connectors">Connectors</h2><p>Accounts Mavis can read and act on, only with your permission.</p></header>
        {conns.isPending && <p className={ui.status} role="status">Loading</p>}
        {conns.error && <p className={ui.error} role="alert">{conns.error.message}</p>}
        <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
          {conns.data?.map((c) => <ConnectorRow key={c.id} c={c} />)}
        </ul>
      </section>
    </div>
  )
}

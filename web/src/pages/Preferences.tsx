import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { API_BASE, api, setCsrf } from '../api/client'
import { useMe, usePatchPreferences, usePreferences } from '../api/hooks'
import type { Preferences as Prefs } from '../api/types'
import { Modal } from '../components/Modal'
import ui from '../styles/ui.module.css'

const zones = (() => {
  try { return Intl.supportedValuesOf('timeZone') } catch { return ['UTC'] }
})()

function DeleteAccount() {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const navigate = useNavigate()
  const qc = useQueryClient()
  const go = async () => {
    setBusy(true); setErr('')
    try {
      await api.deleteAccount()
      setCsrf(null); qc.clear()
      navigate('/', { replace: true })
    } catch (e) { setErr((e as Error).message); setBusy(false) }
  }
  return (
    <>
      <button type="button" className={`${ui.btn} ${ui.btnDanger}`} onClick={() => setOpen(true)}>Delete account</button>
      {open && (
        <Modal title="Delete your account" onClose={() => { setOpen(false); setText('') }}>
          <p className={ui.lede}>
            This starts deleting your account, your connected accounts and everything Mavis remembers. It ends all your sessions and cannot be undone.
          </p>
          <div className={ui.field} style={{ marginTop: 16 }}>
            <label htmlFor="del-confirm">Type DELETE to confirm</label>
            <input id="del-confirm" className={ui.input} value={text} onChange={(e) => setText(e.target.value)} autoComplete="off" />
          </div>
          {err && <p className={ui.error} role="alert">{err}</p>}
          <div className="actions" style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 12 }}>
            <button type="button" className={ui.btn} onClick={() => { setOpen(false); setText('') }}>Cancel</button>
            <button type="button" className={`${ui.btn} ${ui.btnDangerSolid}`} disabled={text !== 'DELETE' || busy} onClick={() => void go()}>
              Delete everything
            </button>
          </div>
        </Modal>
      )}
    </>
  )
}

function PrefsForm({ initial }: { initial: Prefs }) {
  const [f, setF] = useState<Prefs>(initial)
  const patch = usePatchPreferences()
  const [saved, setSaved] = useState(false)
  const set = <K extends keyof Prefs>(k: K, v: Prefs[K]) => { setSaved(false); setF((p) => ({ ...p, [k]: v })) }
  const quiet = f.quiet_hours
  const tzList = zones.includes(f.timezone) ? zones : [f.timezone, ...zones]

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    patch.mutate(f, { onSuccess: () => setSaved(true) })
  }

  return (
    <form onSubmit={submit} aria-label="Preferences">
      <div className={ui.field}>
        <label htmlFor="pf-name">Name</label>
        <input id="pf-name" className={ui.input} value={f.name} onChange={(e) => set('name', e.target.value)} required maxLength={80} />
      </div>
      <div className={ui.field}>
        <label htmlFor="pf-tz">Timezone</label>
        <select id="pf-tz" className={ui.input} value={f.timezone} onChange={(e) => set('timezone', e.target.value)}>
          {tzList.map((z) => <option key={z} value={z}>{z}</option>)}
        </select>
      </div>

      <fieldset style={{ border: 0, padding: 0, margin: '0 0 18px' }}>
        <legend className={ui.label}>Quiet hours</legend>
        <label className={ui.check} style={{ margin: '8px 0' }}>
          <input type="checkbox" checked={quiet !== null} onChange={(e) => set('quiet_hours', e.target.checked ? { start: '22:00', end: '07:00' } : null)} />
          <span>Do not message me during quiet hours</span>
        </label>
        {quiet && (
          <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
            <div className={ui.field} style={{ margin: 0 }}>
              <label htmlFor="qh-start">From</label>
              <input id="qh-start" type="time" className={ui.input} value={quiet.start} onChange={(e) => set('quiet_hours', { ...quiet, start: e.target.value })} />
            </div>
            <div className={ui.field} style={{ margin: 0 }}>
              <label htmlFor="qh-end">Until</label>
              <input id="qh-end" type="time" className={ui.input} value={quiet.end} onChange={(e) => set('quiet_hours', { ...quiet, end: e.target.value })} />
            </div>
          </div>
        )}
      </fieldset>

      <fieldset style={{ border: 0, padding: 0, margin: '0 0 18px' }}>
        <legend className={ui.label}>Where Mavis reaches out first</legend>
        <div style={{ display: 'flex', gap: 20, marginTop: 8, flexWrap: 'wrap' }}>
          {(['telegram', 'slack', 'both'] as const).map((c) => (
            <label key={c} className={ui.check}>
              <input type="radio" name="channel" value={c} checked={f.proactive_channel === c} onChange={() => set('proactive_channel', c)} />
              <span style={{ textTransform: 'capitalize' }}>{c}</span>
            </label>
          ))}
        </div>
      </fieldset>

      <div className={ui.field}>
        <label htmlFor="pf-morning">Morning check in time</label>
        <input id="pf-morning" type="time" className={ui.input} style={{ maxWidth: 160 }} value={f.morning_checkin_time ?? ''}
          onChange={(e) => set('morning_checkin_time', e.target.value || null)} />
      </div>
      <label className={ui.check} style={{ marginBottom: 20 }}>
        <input type="checkbox" checked={f.language_register_opt_out} onChange={(e) => set('language_register_opt_out', e.target.checked)} />
        <span>Do not match my tone and language register</span>
      </label>

      <button type="submit" className={`${ui.btn} ${ui.btnPrimary}`} disabled={patch.isPending}>Save changes</button>
      {saved && <span className={ui.ok} role="status" style={{ marginLeft: 12 }}>Saved</span>}
      {patch.error && <p className={ui.error} role="alert">{patch.error.message}</p>}
    </form>
  )
}

const EMAIL_NOTE: Record<string, string> = {
  confirmed: 'That Google address is now confirmed. You can use it to sign in.',
  taken: 'That Google address already belongs to another Mavis account.',
}

function SignInEmail() {
  const me = useMe()
  const [params] = useSearchParams()
  const note = EMAIL_NOTE[params.get('email') ?? '']
  return (
    <div className={ui.row}>
      <div className={ui.rowMain}>
        <div className={ui.rowTitle}>Google sign in</div>
        <div className={ui.rowSub}>{me.data?.email ? `Confirmed: ${me.data.email}` : 'No Google address confirmed yet.'}</div>
        {note && <p className={ui.status} role="status">{note}</p>}
      </div>
      <a className={ui.btn} href={`${API_BASE}/auth/google/start?confirm=1`}>{me.data?.email ? 'Confirm another' : 'Confirm with Google'}</a>
    </div>
  )
}

export function Preferences() {
  const prefs = usePreferences()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const logoutAll = async () => {
    try { await api.logout(true) } finally { setCsrf(null); qc.clear(); navigate('/login', { replace: true }) }
  }
  return (
    <div className={ui.page}>
      <div className={ui.pageHead}><h1>Preferences</h1><p className={ui.lede}>How Mavis should work with you.</p></div>
      <section className={ui.section} aria-labelledby="general">
        <header><h2 id="general">General</h2></header>
        <hr className={ui.rule} style={{ marginBottom: 20 }} />
        {prefs.isPending && <p className={ui.status} role="status">Loading</p>}
        {prefs.error && <p className={ui.error} role="alert">{prefs.error.message}</p>}
        {prefs.data && <PrefsForm initial={prefs.data} />}
        <SignInEmail />
      </section>

      <section className={ui.section} aria-labelledby="data">
        <header><h2 id="data">Data controls</h2><p>You decide what Mavis keeps.</p></header>
        <div className={ui.row}>
          <div className={ui.rowMain}>
            <div className={ui.rowTitle}>What Mavis remembers</div>
            <div className={ui.rowSub}>Review, correct or forget anything in the Vault.</div>
          </div>
          <Link className={ui.btn} to="/vault">Open Vault</Link>
        </div>
        <div className={ui.row}>
          <div className={ui.rowMain}>
            <div className={ui.rowTitle}>Sessions</div>
            <div className={ui.rowSub}>Sign out of this browser and every other one.</div>
          </div>
          <button type="button" className={ui.btn} onClick={() => void logoutAll()}>Log out everywhere</button>
        </div>
        <div className={ui.row}>
          <div className={ui.rowMain}>
            <div className={ui.rowTitle}>Delete account</div>
            <div className={ui.rowSub}>Removes your account and everything Mavis remembers.</div>
          </div>
          <DeleteAccount />
        </div>
      </section>
    </div>
  )
}

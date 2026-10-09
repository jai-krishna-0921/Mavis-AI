import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { GoogleLogo, SignOut, Trash, Vault as VaultIcon } from '@phosphor-icons/react'
import { API_BASE, api, setCsrf } from '../api/client'
import { useMe, usePatchPreferences, usePreferences } from '../api/hooks'
import type { Preferences as Prefs } from '../api/types'
import { Modal } from '../components/Modal'
import { useToast } from '../components/Toast'
import { IconTile, PageHead, Panel, Skeleton } from '../components/Ui'

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
      <button type="button" className="btn btn-sm btn-danger" onClick={() => setOpen(true)}>Delete account</button>
      {open && (
        <Modal title="Delete your account" onClose={() => { setOpen(false); setText('') }}>
          <p className="text-muted">
            This starts deleting your account, your connected accounts and everything Mavis remembers. It ends all your sessions and cannot be undone.
          </p>
          <div className="field mt-5">
            <label htmlFor="del-confirm">Type DELETE to confirm</label>
            <input id="del-confirm" className="input" value={text} onChange={(e) => setText(e.target.value)} autoComplete="off" />
          </div>
          {err && <p className="error" role="alert">{err}</p>}
          <div className="mt-2 flex flex-wrap justify-end gap-2">
            <button type="button" className="btn" onClick={() => { setOpen(false); setText('') }}>Cancel</button>
            <button type="button" className="btn btn-danger-solid" disabled={text !== 'DELETE' || busy} onClick={() => void go()}>
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
  const toast = useToast()
  const [saved, setSaved] = useState(false)
  const set = <K extends keyof Prefs>(k: K, v: Prefs[K]) => { setSaved(false); setF((p) => ({ ...p, [k]: v })) }
  const quiet = f.quiet_hours
  const tzList = zones.includes(f.timezone) ? zones : [f.timezone, ...zones]

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    patch.mutate(f, { onSuccess: () => { setSaved(true); toast('Preferences saved') } })
  }

  return (
    <form onSubmit={submit} aria-label="Preferences">
      <div className="grid gap-x-6 sm:grid-cols-2">
        <div className="field">
          <label htmlFor="pf-name">Name</label>
          <input id="pf-name" className="input" value={f.name} onChange={(e) => set('name', e.target.value)} required maxLength={80} />
        </div>
        <div className="field">
          <label htmlFor="pf-tz">Timezone</label>
          <select id="pf-tz" className="input" value={f.timezone} onChange={(e) => set('timezone', e.target.value)}>
            {tzList.map((z) => <option key={z} value={z}>{z}</option>)}
          </select>
        </div>
      </div>

      <fieldset className="m-0 mb-6 rounded-3xl border border-line-strong p-5">
        <legend className="label px-2">Quiet hours</legend>
        <label className="check">
          <input type="checkbox" checked={quiet !== null} onChange={(e) => set('quiet_hours', e.target.checked ? { start: '22:00', end: '07:00' } : null)} />
          <span>Do not message me during quiet hours</span>
        </label>
        {quiet && (
          <div className="mt-4 flex flex-wrap gap-4">
            <div className="field !mb-0">
              <label htmlFor="qh-start">From</label>
              <input id="qh-start" type="time" className="input tnum" value={quiet.start} onChange={(e) => set('quiet_hours', { ...quiet, start: e.target.value })} />
            </div>
            <div className="field !mb-0">
              <label htmlFor="qh-end">Until</label>
              <input id="qh-end" type="time" className="input tnum" value={quiet.end} onChange={(e) => set('quiet_hours', { ...quiet, end: e.target.value })} />
            </div>
          </div>
        )}
      </fieldset>

      <fieldset className="m-0 mb-6 border-0 p-0">
        <legend className="label mb-3">Where Mavis reaches out first</legend>
        <div className="grid grid-cols-3 gap-2">
          {(['telegram', 'slack', 'both'] as const).map((c) => (
            <label key={c} className="check cursor-pointer justify-center rounded-2xl border border-line-strong px-3 py-3 transition-colors duration-300 hover:border-ink/30 has-[:checked]:border-accent-hi/60 has-[:checked]:bg-accent-soft has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-navy-hi">
              <input className="sr-only" type="radio" name="channel" value={c} checked={f.proactive_channel === c} onChange={() => set('proactive_channel', c)} />
              <span className="capitalize">{c}</span>
            </label>
          ))}
        </div>
      </fieldset>

      <div className="field">
        <label htmlFor="pf-morning">Morning check in time</label>
        <input id="pf-morning" type="time" className="input tnum max-w-44" value={f.morning_checkin_time ?? ''}
          onChange={(e) => set('morning_checkin_time', e.target.value || null)} />
      </div>
      <label className="check mb-8">
        <input type="checkbox" checked={f.language_register_opt_out} onChange={(e) => set('language_register_opt_out', e.target.checked)} />
        <span>Do not match my tone and language register</span>
      </label>

      <div className="flex flex-wrap items-center gap-4">
        <button type="submit" className="btn btn-primary !px-6" disabled={patch.isPending}>Save changes</button>
        {saved && <span className="tag tag-ok" role="status">Saved</span>}
      </div>
      {patch.error && <p className="error" role="alert">{patch.error.message}</p>}
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
    <div className="row mt-8 !border-t !border-line">
      <IconTile><GoogleLogo size={22} weight="bold" aria-hidden="true" /></IconTile>
      <div className="min-w-0 flex-1">
        <div className="row-title">Google sign in</div>
        <div className="row-sub">{me.data?.email ? `Confirmed: ${me.data.email}` : 'No Google address confirmed yet.'}</div>
        {note && <p className="status mt-1" role="status">{note}</p>}
      </div>
      <a className="btn btn-sm" href={`${API_BASE}/auth/google/start?confirm=1`}>{me.data?.email ? 'Confirm another' : 'Confirm with Google'}</a>
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
    <div>
      <PageHead title="Preferences" lede="How Mavis should work with you." />
      <Panel id="general" title="General">
        {prefs.isPending && (
          <div role="status" aria-label="Loading preferences" className="grid gap-5">
            <Skeleton className="h-11 w-full" /><Skeleton className="h-11 w-full" /><Skeleton className="h-28 w-full !rounded-3xl" />
            <span className="sr-only">Loading</span>
          </div>
        )}
        {prefs.error && <p className="error" role="alert">{prefs.error.message}</p>}
        {prefs.data && <PrefsForm initial={prefs.data} />}
        <SignInEmail />
      </Panel>

      <Panel id="data" title="Data controls" lede="You decide what Mavis keeps.">
        <div className="row">
          <IconTile><VaultIcon size={22} weight="light" aria-hidden="true" /></IconTile>
          <div className="min-w-0 flex-1">
            <div className="row-title">What Mavis remembers</div>
            <div className="row-sub">Review, correct or forget anything in the Vault.</div>
          </div>
          <Link className="btn btn-sm" to="/vault">Open Vault</Link>
        </div>
        <div className="row">
          <IconTile><SignOut size={22} weight="light" aria-hidden="true" /></IconTile>
          <div className="min-w-0 flex-1">
            <div className="row-title">Sessions</div>
            <div className="row-sub">Sign out of this browser and every other one.</div>
          </div>
          <button type="button" className="btn btn-sm" onClick={() => void logoutAll()}>Log out everywhere</button>
        </div>
        <div className="row">
          <IconTile><Trash size={22} weight="light" aria-hidden="true" /></IconTile>
          <div className="min-w-0 flex-1">
            <div className="row-title">Delete account</div>
            <div className="row-sub">Removes your account and everything Mavis remembers.</div>
          </div>
          <DeleteAccount />
        </div>
      </Panel>
    </div>
  )
}

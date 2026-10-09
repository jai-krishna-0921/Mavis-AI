import { useEffect, useId, useMemo, useState } from 'react'
import {
  useForgetItem, useForgetSource, usePatchItem, useVaultItems, useVaultSources, useVaultSummary,
} from '../api/hooks'
import type { VaultItem, VaultKind, VaultSource } from '../api/types'
import { Modal } from '../components/Modal'
import { fmtDate } from '../lib/channels'
import { useQueryClient } from '@tanstack/react-query'
import ui from '../styles/ui.module.css'
import styles from './Vault.module.css'

const TABS: { kind: VaultKind; label: string; count: 'people' | 'organisations' | 'projects' | 'facts' | null }[] = [
  { kind: 'person', label: 'People', count: 'people' },
  { kind: 'organisation', label: 'Organisations', count: 'organisations' },
  { kind: 'project', label: 'Projects', count: 'projects' },
  { kind: 'fact', label: 'Facts', count: 'facts' },
  { kind: 'preference', label: 'Preferences', count: null },
]

const SOURCE_LABEL: Record<string, string> = { you: 'You', gmail: 'Gmail', slack: 'Slack', calendar: 'Calendar', dashboard: 'Dashboard' }
const srcLabel = (s: string) => SOURCE_LABEL[s] ?? s

function useDebounced<T>(v: T, ms = 250) {
  const [d, setD] = useState(v)
  useEffect(() => { const t = setTimeout(() => setD(v), ms); return () => clearTimeout(t) }, [v, ms])
  return d
}

function ProfileCard() {
  const summary = useVaultSummary()
  const patch = usePatchItem()
  const [editing, setEditing] = useState<string | null>(null)
  const [val, setVal] = useState('')
  if (!summary.data) return null
  const entries = Object.entries(summary.data.profile)
  const save = (key: string) => patch.mutate({ id: `profile.${key}`, title: val.trim() }, { onSuccess: () => setEditing(null) })
  return (
    <section className={styles.profile} aria-labelledby="profile-h">
      <h2 id="profile-h" className="sr-only">Profile</h2>
      <dl>
        {entries.map(([k, v]) => (
          <div key={k} className={styles.pf}>
            <dt>{k}</dt>
            {editing === k ? (
              <dd>
                <form onSubmit={(e) => { e.preventDefault(); save(k) }} className={styles.inline}>
                  <label className="sr-only" htmlFor={`pf-${k}`}>Edit {k}</label>
                  <input id={`pf-${k}`} className={ui.input} value={val} onChange={(e) => setVal(e.target.value)} autoFocus />
                  <button type="submit" className={`${ui.btn} ${ui.btnPrimary}`} disabled={patch.isPending}>Save</button>
                  <button type="button" className={ui.btn} onClick={() => setEditing(null)}>Cancel</button>
                </form>
              </dd>
            ) : (
              <dd>
                <span>{v}</span>{' '}
                <button type="button" className={ui.btnLink} aria-label={`Edit ${k}`} onClick={() => { setEditing(k); setVal(v) }}>Edit</button>
              </dd>
            )}
          </div>
        ))}
      </dl>
      {patch.error && <p className={ui.error} role="alert">{patch.error.message}</p>}
    </section>
  )
}

function ItemRow({ item }: { item: VaultItem }) {
  const [mode, setMode] = useState<'view' | 'edit' | 'forget'>('view')
  const [title, setTitle] = useState(item.title)
  const [detail, setDetail] = useState(item.detail ?? '')
  const [suppress, setSuppress] = useState(false)
  const patch = usePatchItem()
  const forget = useForgetItem()
  const tid = useId()
  const did = useId()

  if (mode === 'edit') {
    return (
      <li className={styles.item}>
        <form onSubmit={(e) => { e.preventDefault(); patch.mutate({ id: item.id, title: title.trim(), detail }, { onSuccess: () => setMode('view') }) }}>
          <div className={ui.field}>
            <label htmlFor={tid}>Title</label>
            <input id={tid} className={ui.input} value={title} onChange={(e) => setTitle(e.target.value)} required autoFocus />
          </div>
          <div className={ui.field}>
            <label htmlFor={did}>Details</label>
            <textarea id={did} className={ui.input} value={detail} onChange={(e) => setDetail(e.target.value)} />
          </div>
          <p className={ui.status}>Your correction is saved with your own trust and marked as from the dashboard.</p>
          {patch.error && <p className={ui.error} role="alert">{patch.error.message}</p>}
          <div className={ui.rowActions} style={{ justifyContent: 'flex-start', marginTop: 10 }}>
            <button type="submit" className={`${ui.btn} ${ui.btnPrimary}`} disabled={patch.isPending || !title.trim()}>Save</button>
            <button type="button" className={ui.btn} onClick={() => { setMode('view'); setTitle(item.title); setDetail(item.detail ?? '') }}>Cancel</button>
          </div>
        </form>
      </li>
    )
  }

  return (
    <li className={styles.item}>
      <div className={styles.itemTop}>
        <div className={ui.rowMain}>
          {/* Third-party text: plain text only, escaped by React. */}
          <div className={ui.rowTitle}>{item.title}</div>
          {item.detail && <div className={ui.rowSub}>{item.detail}</div>}
          <div className={styles.meta}>
            <span className={ui.tag}>{srcLabel(item.source)}</span>
            <span>trust {item.trust}</span>
            <span>updated {fmtDate(item.updated_at)}</span>
          </div>
        </div>
        {mode === 'view' && (
          <div className={ui.rowActions}>
            <button type="button" className={ui.btn} aria-label={`Edit ${item.title}`} onClick={() => setMode('edit')}>Edit</button>
            <button type="button" className={`${ui.btn} ${ui.btnDanger}`} aria-label={`Forget ${item.title}`} onClick={() => setMode('forget')}>Forget</button>
          </div>
        )}
      </div>
      {mode === 'forget' && (
        <div className={styles.confirm} role="group" aria-label={`Forget ${item.title}`}>
          <p>Mavis will delete this and its search vectors.</p>
          <label className={ui.check}>
            <input type="checkbox" checked={suppress} onChange={(e) => setSuppress(e.target.checked)} />
            <span>Also stop Mavis learning this again</span>
          </label>
          {forget.error && <p className={ui.error} role="alert">{forget.error.message}</p>}
          <div className={ui.rowActions} style={{ justifyContent: 'flex-start', marginTop: 10 }}>
            <button type="button" className={`${ui.btn} ${ui.btnDangerSolid}`} disabled={forget.isPending}
              onClick={() => forget.mutate({ id: item.id, alsoSuppress: suppress })}>Forget it</button>
            <button type="button" className={ui.btn} onClick={() => setMode('view')}>Keep</button>
          </div>
        </div>
      )}
    </li>
  )
}

function Sources() {
  const sources = useVaultSources()
  const forget = useForgetSource()
  const qc = useQueryClient()
  const [target, setTarget] = useState<VaultSource | null>(null)
  if (!sources.data?.length) return null
  return (
    <details className={styles.sources}>
      <summary>Sources</summary>
      <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
        {sources.data.map((s) => (
          <li key={s.source} className={ui.row}>
            <div className={ui.rowMain}>
              <div className={ui.rowTitle}>{srcLabel(s.source)}</div>
              <div className={ui.rowSub}>{s.count} items{s.last_sync ? `, last sync ${fmtDate(s.last_sync)}` : ''}</div>
            </div>
            <button type="button" className={`${ui.btn} ${ui.btnDanger}`} onClick={() => setTarget(s.source)}>
              Forget everything from {srcLabel(s.source)}
            </button>
          </li>
        ))}
      </ul>
      {target && (
        <Modal title={`Forget everything from ${srcLabel(target)}`} onClose={() => setTarget(null)}>
          <p className={ui.lede}>This deletes every item Mavis learned from {srcLabel(target)}. It cannot be undone.</p>
          {forget.error && <p className={ui.error} role="alert">{forget.error.message}</p>}
          <div className="actions" style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 20 }}>
            <button type="button" className={ui.btn} onClick={() => setTarget(null)}>Cancel</button>
            <button type="button" className={`${ui.btn} ${ui.btnDangerSolid}`} disabled={forget.isPending}
              onClick={() => forget.mutate(target, { onSuccess: () => { setTarget(null); void qc.invalidateQueries({ queryKey: ['vault'] }) } })}>
              Forget everything
            </button>
          </div>
        </Modal>
      )}
    </details>
  )
}

export function Vault() {
  const [kind, setKind] = useState<VaultKind>('person')
  const [search, setSearch] = useState('')
  const [source, setSource] = useState<string | null>(null)
  const q = useDebounced(search.trim())
  const summary = useVaultSummary()
  const items = useVaultItems(kind, q)

  const all = useMemo(() => items.data?.pages.flatMap((p) => p.items) ?? [], [items.data])
  const present = useMemo(() => [...new Set(all.map((i) => i.source))], [all])
  const shown = source ? all.filter((i) => i.source === source) : all
  const panelId = 'vault-panel'

  return (
    <div className={ui.page}>
      <div className={ui.pageHead}><h1>Vault</h1><p className={ui.lede}>Everything Mavis knows about you. Correct it or make it forget.</p></div>
      <ProfileCard />

      <div role="tablist" aria-label="Vault sections" className={styles.tabs}>
        {TABS.map((t) => (
          <button key={t.kind} role="tab" type="button" id={`tab-${t.kind}`} aria-selected={kind === t.kind} aria-controls={panelId}
            tabIndex={kind === t.kind ? 0 : -1} className={styles.tab}
            onClick={() => { setKind(t.kind); setSource(null) }}
            onKeyDown={(e) => {
              const i = TABS.findIndex((x) => x.kind === kind)
              const n = e.key === 'ArrowRight' ? i + 1 : e.key === 'ArrowLeft' ? i - 1 : null
              if (n === null) return
              const next = TABS[(n + TABS.length) % TABS.length]
              setKind(next.kind); setSource(null)
              requestAnimationFrame(() => document.getElementById(`tab-${next.kind}`)?.focus())
            }}>
            {t.label}{t.count && summary.data ? <span className={styles.count}> {summary.data.counts[t.count]}</span> : null}
          </button>
        ))}
      </div>

      <div id={panelId} role="tabpanel" aria-labelledby={`tab-${kind}`}>
        <div className={styles.tools}>
          <div className={ui.field} style={{ margin: 0, flex: 1, minWidth: 200 }}>
            <label htmlFor="vault-search" className="sr-only">Search the vault</label>
            <input id="vault-search" type="search" className={ui.input} placeholder="Search" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
          <div className={ui.chips} role="group" aria-label="Filter by source">
            {present.map((s) => (
              <button key={s} type="button" className={ui.chip} aria-pressed={source === s} onClick={() => setSource(source === s ? null : s)}>
                {srcLabel(s)}
              </button>
            ))}
          </div>
        </div>

        {items.isPending && <p className={ui.status} role="status">Loading</p>}
        {items.error && <p className={ui.error} role="alert">{items.error.message}</p>}
        {items.data && shown.length === 0 && (
          <p className={ui.empty}>{q ? 'Nothing matches that search.' : 'Nothing here yet. Mavis learns as you talk.'}</p>
        )}
        <ul className={styles.list}>
          {shown.map((i) => <ItemRow key={i.id} item={i} />)}
        </ul>
        {items.hasNextPage && (
          <button type="button" className={ui.btn} disabled={items.isFetchingNextPage} onClick={() => void items.fetchNextPage()}>
            Load more
          </button>
        )}
      </div>

      <Sources />
    </div>
  )
}


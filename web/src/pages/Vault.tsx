import { useEffect, useId, useMemo, useState } from 'react'
import {
  useForgetItem, useForgetSource, usePatchItem, useVaultItems, useVaultSources, useVaultSummary,
} from '../api/hooks'
import type { VaultItem, VaultKind, VaultSource } from '../api/types'
import { Modal } from '../components/Modal'
import { fmtDate } from '../lib/channels'
import { useQueryClient } from '@tanstack/react-query'
import { Brain, Buildings, Funnel, MagnifyingGlass, PencilSimple, Trash, UserCircle, Stack } from '@phosphor-icons/react'
import { useToast } from '../components/Toast'
import { EmptyState, PageHead, SkeletonRows } from '../components/Ui'

const TABS: { kind: VaultKind; label: string; count: 'people' | 'organisations' | 'projects' | 'facts' | null }[] = [
  { kind: 'person', label: 'People', count: 'people' },
  { kind: 'organisation', label: 'Organisations', count: 'organisations' },
  { kind: 'project', label: 'Projects', count: 'projects' },
  { kind: 'fact', label: 'Facts', count: 'facts' },
  { kind: 'preference', label: 'Preferences', count: null },
]

const SOURCE_LABEL: Record<string, string> = { you: 'You', gmail: 'Gmail', slack: 'Slack', calendar: 'Calendar', dashboard: 'Dashboard' }
const srcLabel = (s: string) => SOURCE_LABEL[s] ?? s

// Plain provenance for an item: where Mavis learned it, in the user's terms.
const PROVENANCE: Record<string, string> = {
  you: 'You told Mavis',
  gmail: 'From your mail',
  slack: 'From Slack',
  calendar: 'From your calendar',
  dashboard: 'You corrected this',
}
const provenance = (s: string) => PROVENANCE[s] ?? srcLabel(s)
// Only third-party sources are unconfirmed until the user says otherwise.
const THIRD_PARTY = new Set(['gmail', 'slack', 'calendar'])

function useDebounced<T>(v: T, ms = 250) {
  const [d, setD] = useState(v)
  useEffect(() => { const t = setTimeout(() => setD(v), ms); return () => clearTimeout(t) }, [v, ms])
  return d
}

function ProfileCard() {
  const summary = useVaultSummary()
  const patch = usePatchItem()
  const toast = useToast()
  const [editing, setEditing] = useState<string | null>(null)
  const [val, setVal] = useState('')
  if (summary.isPending) return <div className="panel mb-8" aria-hidden="true"><div className="panel-core grid gap-3 sm:grid-cols-3"><div className="skeleton h-14" /><div className="skeleton h-14" /><div className="skeleton h-14" /></div></div>
  if (!summary.data) return null
  const entries = Object.entries(summary.data.profile)
  const save = (key: string) => patch.mutate({ id: `profile.${key}`, title: val.trim() }, { onSuccess: () => { setEditing(null); toast('Profile updated') } })
  return (
    <section className="panel mb-8" aria-labelledby="profile-h">
      <div className="panel-core !py-4">
        <h2 id="profile-h" className="sr-only">Profile</h2>
        <dl className="m-0 grid gap-x-8 sm:grid-cols-[repeat(auto-fit,minmax(220px,1fr))]">
          {entries.map(([k, v]) => (
            <div key={k} className="min-w-0 border-b border-line py-3 last:border-b-0 sm:[&:nth-last-child(-n+1)]:border-b-0">
              <dt className="text-xs font-medium capitalize text-muted">{k}</dt>
              {editing === k ? (
                <dd className="m-0 mt-1.5">
                  <form onSubmit={(e) => { e.preventDefault(); save(k) }} className="flex flex-wrap gap-2">
                    <label className="sr-only" htmlFor={`pf-${k}`}>Edit {k}</label>
                    <input id={`pf-${k}`} className="input min-w-32 flex-1" value={val} onChange={(e) => setVal(e.target.value)} autoFocus />
                    <button type="submit" className="btn btn-sm btn-primary" disabled={patch.isPending}>Save</button>
                    <button type="button" className="btn btn-sm" onClick={() => setEditing(null)}>Cancel</button>
                  </form>
                </dd>
              ) : (
                <dd className="m-0 mt-0.5 flex items-center justify-between gap-3 [overflow-wrap:anywhere]">
                  <span className="font-semibold">{v}</span>
                  <button type="button" className="btn btn-sm shrink-0" aria-label={`Edit ${k}`} onClick={() => { setEditing(k); setVal(v) }}>Edit</button>
                </dd>
              )}
            </div>
          ))}
        </dl>
        {patch.error && <p className="error" role="alert">{patch.error.message}</p>}
      </div>
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
  const toast = useToast()
  const tid = useId()
  const did = useId()

  if (mode === 'edit') {
    return (
      <li className="border-t border-line px-1 py-5 first:border-t-0">
        <form onSubmit={(e) => { e.preventDefault(); patch.mutate({ id: item.id, title: title.trim(), detail }, { onSuccess: () => { setMode('view'); toast('Saved your correction') } }) }}>
          <div className="field">
            <label htmlFor={tid}>Title</label>
            <input id={tid} className="input" value={title} onChange={(e) => setTitle(e.target.value)} required autoFocus />
          </div>
          <div className="field">
            <label htmlFor={did}>Details</label>
            <textarea id={did} className="input" value={detail} onChange={(e) => setDetail(e.target.value)} />
          </div>
          <p className="status">Saving marks this as something you corrected.</p>
          {patch.error && <p className="error" role="alert">{patch.error.message}</p>}
          <div className="mt-4 flex gap-2">
            <button type="submit" className="btn btn-sm btn-primary" disabled={patch.isPending || !title.trim()}>Save</button>
            <button type="button" className="btn btn-sm" onClick={() => { setMode('view'); setTitle(item.title); setDetail(item.detail ?? '') }}>Cancel</button>
          </div>
        </form>
      </li>
    )
  }

  return (
    <li className="border-t border-line px-1 py-5 first:border-t-0">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start">
        <div className="min-w-0 flex-1">
          {/* Third-party text: plain text only, escaped by React. */}
          <div className="row-title">{item.title}</div>
          {item.detail && <div className="row-sub mt-0.5">{item.detail}</div>}
          <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted">
            <span className="tag">{provenance(item.source)}</span>
            {THIRD_PARTY.has(item.source) && <span className="rounded-full border border-dashed border-white/20 px-2.5 py-0.5 italic">Not confirmed</span>}
            {item.updated_at && <span className="tnum">updated {fmtDate(item.updated_at)}</span>}
          </div>
        </div>
        <div className="row-actions">
          <button type="button" className="btn btn-sm" aria-label={`Edit ${item.title}`} onClick={() => setMode('edit')}>
            <PencilSimple size={14} weight="light" aria-hidden="true" /> Edit
          </button>
          <button type="button" className="btn btn-sm btn-danger" aria-label={`Forget ${item.title}`} onClick={() => setMode('forget')}>
            <Trash size={14} weight="light" aria-hidden="true" /> Forget
          </button>
        </div>
      </div>
      {mode === 'forget' && (
        <div className="mt-4 grid gap-3 rounded-3xl border border-white/10 bg-black/25 p-5" role="group" aria-label={`Forget ${item.title}`}>
          <p>Mavis will delete this and its search vectors.</p>
          <label className="check">
            <input type="checkbox" checked={suppress} onChange={(e) => setSuppress(e.target.checked)} />
            <span>Also stop Mavis learning this again</span>
          </label>
          {forget.error && <p className="error" role="alert">{forget.error.message}</p>}
          <div className="flex gap-2">
            <button type="button" className="btn btn-sm btn-danger-solid" disabled={forget.isPending}
              onClick={() => forget.mutate({ id: item.id, alsoSuppress: suppress }, { onSuccess: () => toast('Forgotten') })}>Forget it</button>
            <button type="button" className="btn btn-sm" onClick={() => setMode('view')}>Keep</button>
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
  const toast = useToast()
  const [target, setTarget] = useState<VaultSource | null>(null)
  if (!sources.data?.length) return null
  return (
    <details className="panel group mt-10">
      <summary className="panel-core flex cursor-pointer list-none items-center justify-between !py-4 text-lg font-bold tracking-tight [&::-webkit-details-marker]:hidden">
        Sources
        <span className="text-sm font-normal text-muted transition-transform duration-500 ease-fluid group-open:rotate-45" aria-hidden="true">+</span>
      </summary>
      <ul className="m-0 mt-1.5 list-none rounded-[calc(2rem-0.375rem)] bg-surface px-6 py-2">
        {sources.data.map((s) => (
          <li key={s.source} className="row flex-wrap">
            <div className="min-w-0 flex-1">
              <div className="row-title">{srcLabel(s.source)}</div>
              <div className="row-sub tnum">{s.count} items{s.last_sync ? `, last sync ${fmtDate(s.last_sync)}` : ''}</div>
            </div>
            <button type="button" className="btn btn-sm btn-danger" onClick={() => setTarget(s.source)}>
              Forget everything from {srcLabel(s.source)}
            </button>
          </li>
        ))}
      </ul>
      {target && (
        <Modal title={`Forget everything from ${srcLabel(target)}`} onClose={() => setTarget(null)}>
          <p className="text-muted">This deletes every item Mavis learned from {srcLabel(target)}. It cannot be undone.</p>
          {forget.error && <p className="error" role="alert">{forget.error.message}</p>}
          <div className="mt-6 flex flex-wrap justify-end gap-2">
            <button type="button" className="btn" onClick={() => setTarget(null)}>Cancel</button>
            <button type="button" className="btn btn-danger-solid" disabled={forget.isPending}
              onClick={() => forget.mutate(target, { onSuccess: () => { setTarget(null); toast('Forgotten'); void qc.invalidateQueries({ queryKey: ['vault'] }) } })}>
              Forget everything
            </button>
          </div>
        </Modal>
      )}
    </details>
  )
}

const TAB_ICON = { person: UserCircle, organisation: Buildings, project: Stack, fact: Brain, preference: Funnel } as const

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
  const EmptyIcon = TAB_ICON[kind]

  return (
    <div>
      <PageHead title="Vault" lede="Everything Mavis knows about you. Correct it or make it forget." />
      <ProfileCard />

      <div role="tablist" aria-label="Vault sections" className="no-scrollbar mb-6 flex gap-1 overflow-x-auto rounded-full border border-white/[0.07] bg-white/[0.025] p-1.5">
        {TABS.map((t) => {
          const Icon = TAB_ICON[t.kind]
          return (
            <button key={t.kind} role="tab" type="button" id={`tab-${t.kind}`} aria-selected={kind === t.kind} aria-controls={panelId}
              tabIndex={kind === t.kind ? 0 : -1}
              className="flex min-h-10 shrink-0 items-center gap-2 rounded-full px-4 text-[14px] font-medium text-muted transition-[background-color,color] duration-500 ease-fluid hover:text-ink aria-selected:bg-ink aria-selected:text-bg"
              onClick={() => { setKind(t.kind); setSource(null) }}
              onKeyDown={(e) => {
                const i = TABS.findIndex((x) => x.kind === kind)
                const n = e.key === 'ArrowRight' ? i + 1 : e.key === 'ArrowLeft' ? i - 1 : null
                if (n === null) return
                const next = TABS[(n + TABS.length) % TABS.length]
                setKind(next.kind); setSource(null)
                requestAnimationFrame(() => document.getElementById(`tab-${next.kind}`)?.focus())
              }}>
              <Icon size={18} weight="light" aria-hidden="true" />
              {t.label}{t.count && summary.data ? <span className="tnum text-xs opacity-70"> {summary.data.counts[t.count]}</span> : null}
            </button>
          )
        })}
      </div>

      <div id={panelId} role="tabpanel" aria-labelledby={`tab-${kind}`}>
        <div className="mb-5 flex flex-wrap items-center gap-x-5 gap-y-3">
          <div className="relative min-w-52 flex-1">
            <label htmlFor="vault-search" className="sr-only">Search the vault</label>
            <MagnifyingGlass size={18} weight="light" className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-muted" aria-hidden="true" />
            <input id="vault-search" type="search" className="input !pl-10" placeholder="Search" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
          <div className="flex flex-wrap gap-2" role="group" aria-label="Filter by source">
            {present.map((s) => (
              <button key={s} type="button" className="chip" aria-pressed={source === s} onClick={() => setSource(source === s ? null : s)}>
                {srcLabel(s)}
              </button>
            ))}
          </div>
        </div>

        <div className="panel">
          <div className="panel-core !py-3">
            {items.isPending && <div className="py-4"><SkeletonRows rows={4} /></div>}
            {items.error && <p className="error" role="alert">{items.error.message}</p>}
            {items.data && shown.length === 0 && (
              <div className="py-4">
                <EmptyState icon={<EmptyIcon size={24} weight="light" />} title={q ? 'Nothing matches that search' : 'Nothing here yet'}>
                  {q ? 'Try a different word, or clear the search.' : 'Mavis learns as you talk. Anything it picks up shows up here for you to review.'}
                </EmptyState>
              </div>
            )}
            <ul className="m-0 list-none p-0">
              {shown.map((i) => <ItemRow key={i.id} item={i} />)}
            </ul>
          </div>
        </div>
        {items.hasNextPage && (
          <button type="button" className="btn mt-5" disabled={items.isFetchingNextPage} onClick={() => void items.fetchNextPage()}>
            Load more
          </button>
        )}
      </div>

      <Sources />
    </div>
  )
}

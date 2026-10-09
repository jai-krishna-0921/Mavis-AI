import { Brain, CalendarDots, Check, Clock, EnvelopeSimple, FileText, Table } from '@phosphor-icons/react'

// Four columns by three rows, no gaps: Inbox 2x2, Calendar 2x1, Reminders 1x1, Memory 1x1, Docs 4x1.
// On small screens the cards simply stack. `grid-flow-dense` guarantees no hole if the order changes.
const card = 'group relative overflow-hidden rounded-[1.75rem] border border-line bg-surface p-6 shadow-card transition-[box-shadow,transform] duration-700 ease-fluid hover:-translate-y-0.5 hover:shadow-lift sm:p-8'

const MAIL = [
  { from: 'Daniel Okafor', subject: 'Invoice 2291, due on the 5th', tag: 'Reply drafted', tone: 'accent' },
  { from: 'Northside Dental', subject: 'Your visit moved to Tuesday, 4:30', tag: 'Added to calendar', tone: 'ok' },
  { from: 'Priya Raman', subject: 'Can we shift Thursday?', tag: 'Needs you', tone: 'navy' },
  { from: 'Weekly digest', subject: 'Ten product updates you did not ask for', tag: 'Skipped', tone: 'plain' },
] as const
const TONE = {
  accent: 'bg-accent-soft text-accent-hi', ok: 'bg-ok-soft text-ok', navy: 'bg-navy-soft text-navy-hi', plain: 'bg-sunken text-muted',
}

export function Bento() {
  return (
    <div className="grid grid-flow-dense auto-rows-auto gap-4 md:grid-cols-4 md:grid-rows-[repeat(3,minmax(0,auto))]" data-bento>
      <article className={`${card} flex flex-col md:col-span-2 md:row-span-2`} data-reveal>
        <div className="flex items-center gap-3"><span className="grid size-11 place-items-center rounded-2xl bg-accent-soft text-accent-hi"><EnvelopeSimple size={24} weight="light" aria-hidden="true" /></span>
          <h3 className="text-2xl font-bold tracking-[-0.02em]">An inbox that sorts itself</h3></div>
        <p className="mt-3 max-w-[44ch] text-muted">Mavis reads what you allow, sorts what needs a human and drafts the rest. Nothing is sent until you say so.</p>
        <ul className="m-0 mt-8 grid list-none gap-2.5 p-0" aria-hidden="true">
          {MAIL.map((m) => (
            <li key={m.from} className="flex items-center justify-between gap-3 rounded-2xl border border-line bg-bg px-4 py-3 transition-transform duration-700 ease-fluid group-hover:translate-x-1">
              <div className="min-w-0 flex-1 text-sm leading-snug"><div className="truncate font-semibold">{m.from}</div><div className="truncate text-muted">{m.subject}</div></div>
              <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-medium ${TONE[m.tone]}`}>{m.tag}</span>
            </li>
          ))}
        </ul>
        <div className="mt-auto flex items-center gap-3 pt-8 text-sm text-muted" aria-hidden="true">
          <span className="grid size-6 place-items-center rounded-full bg-ok-soft text-ok"><Check size={14} weight="bold" /></span>
          Three handled. One waiting for you.
        </div>
      </article>

      <article className={`${card} md:col-span-2`} data-reveal>
        <div className="flex items-center gap-3"><span className="grid size-11 place-items-center rounded-2xl bg-navy-soft text-navy-hi"><CalendarDots size={24} weight="light" aria-hidden="true" /></span>
          <h3 className="text-2xl font-bold tracking-[-0.02em]">A calendar that makes room</h3></div>
        <p className="mt-3 max-w-[44ch] text-muted">It finds the slot that works for everyone, books it and reminds you before it starts.</p>
        <div className="mt-6 grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm" aria-hidden="true">
          <span className="tnum pt-2 text-muted">11:00</span><span className="rounded-xl bg-sunken px-3 py-2 text-muted">Focus time</span>
          <span className="tnum pt-2 text-muted">14:00</span><span className="rounded-xl border border-dashed border-accent-hi/50 bg-accent-soft px-3 py-2 font-medium text-accent-hi">Priya, moved from 3pm</span>
          <span className="tnum pt-2 text-muted">16:30</span><span className="rounded-xl bg-navy px-3 py-2 font-medium text-white">Priya, 45 min, confirmed</span>
        </div>
      </article>

      <article className={`${card} md:col-span-1`} data-reveal>
        <span className="grid size-11 place-items-center rounded-2xl bg-accent-soft text-accent-hi"><Clock size={24} weight="light" aria-hidden="true" /></span>
        <h3 className="mt-5 text-xl font-bold tracking-[-0.02em]">Nudges on time</h3>
        <p className="mt-2 text-[15px] text-muted">Ask once. Mavis texts you before it is due.</p>
      </article>

      <article className={`${card} md:col-span-1`} data-reveal>
        <span className="grid size-11 place-items-center rounded-2xl bg-navy-soft text-navy-hi"><Brain size={24} weight="light" aria-hidden="true" /></span>
        <h3 className="mt-5 text-xl font-bold tracking-[-0.02em]">Remembers, then forgets</h3>
        <p className="mt-2 text-[15px] text-muted">Everything it knows lives in your Vault. Edit or delete any of it.</p>
      </article>

      <article className={`${card} md:col-span-4`} data-reveal>
        <div className="grid gap-8 md:grid-cols-[minmax(0,5fr)_minmax(0,7fr)] md:items-center">
          <div>
            <div className="flex items-center gap-3"><span className="grid size-11 place-items-center rounded-2xl bg-accent-soft text-accent-hi"><FileText size={24} weight="light" aria-hidden="true" /></span>
              <span className="grid size-11 place-items-center rounded-2xl bg-ok-soft text-ok"><Table size={24} weight="light" aria-hidden="true" /></span></div>
            <h3 className="mt-5 text-2xl font-bold tracking-[-0.02em]">Documents and sheets, answered in a line</h3>
            <p className="mt-3 max-w-[44ch] text-muted">Ask about a contract or a budget. Mavis reads it, quotes what matters and tells you where it came from.</p>
          </div>
          <div className="rounded-2xl border border-line bg-bg p-4 text-sm" aria-hidden="true">
            <div className="ml-auto w-fit max-w-[90%] rounded-2xl rounded-br-md bg-navy px-4 py-2 text-white">What did we spend on travel in Q3?</div>
            <div className="mt-2 w-fit max-w-[92%] rounded-2xl rounded-bl-md bg-surface px-4 py-2.5 shadow-[0_1px_2px_rgb(26_23_20/0.06)]">
              Travel came to 4,180 for Q3, up 12 percent on Q2. The biggest line is the Lisbon offsite.
              <div className="mt-2 flex items-center gap-1.5 text-xs text-muted"><Check size={13} weight="bold" className="text-ok" /> From Budget 2026, sheet Q3</div>
            </div>
          </div>
        </div>
      </article>
    </div>
  )
}

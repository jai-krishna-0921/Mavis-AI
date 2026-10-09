import { Bell, CalendarDots, EnvelopeSimple, PencilSimpleLine, SlackLogo, UsersThree, type Icon } from '@phosphor-icons/react'

// Four cards on a four column grid: 2x2 + 2x1 + 1x1 + 1x1 = 8 cells in 4 columns and 2 rows, packed with
// grid-auto-flow: dense so nothing is left empty. Below lg it is 2 columns (A spans 2x2, B 2x1, C and D 1x1)
// and below sm it is one column.
const SHELL = 'group relative rounded-[2rem] border border-white/[0.07] bg-white/[0.025] p-1.5 transition-transform duration-700 ease-fluid hover:-translate-y-1'
const CORE = 'relative flex h-full flex-col overflow-hidden rounded-[calc(2rem-0.375rem)] bg-surface p-6 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)] sm:p-8'

function Head({ Icon, title, children, big = false }: { Icon: Icon; title: string; children: string; big?: boolean }) {
  return (
    <div className="relative z-10">
      <div className="mb-5 grid size-11 place-items-center rounded-2xl border border-accent/30 bg-accent/10 text-accent-hi">
        <Icon size={22} weight="light" aria-hidden="true" />
      </div>
      <h3 className={`font-bold tracking-[-0.02em] ${big ? 'text-3xl sm:text-4xl' : 'text-2xl'}`}>{title}</h3>
      <p className={`mt-2 max-w-[42ch] text-muted ${big ? 'text-base sm:text-lg' : 'text-[15px]'}`}>{children}</p>
    </div>
  )
}

function MailRow({ who, subject, chip, tone, delay }: { who: string; subject: string; chip: string; tone: 'hot' | 'calm'; delay: string }) {
  return (
    <div
      className="flex items-center gap-3 rounded-2xl border border-white/[0.07] bg-white/[0.03] px-3.5 py-3 transition-transform duration-700 ease-fluid group-hover:translate-x-2"
      style={{ transitionDelay: delay }}
    >
      <div className="hidden size-9 shrink-0 place-items-center rounded-full bg-navy-hi/25 sm:grid text-[13px] font-bold text-ink">{who.charAt(0)}</div>
      <div className="min-w-0 flex-1 leading-tight">
        <div className="truncate text-sm font-semibold">{who}</div>
        <div className="truncate text-[13px] text-muted">{subject}</div>
      </div>
      <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-medium ${tone === 'hot' ? 'bg-accent/15 text-accent-hi' : 'bg-white/[0.06] text-muted'}`}>{chip}</span>
    </div>
  )
}

function InboxVisual() {
  return (
    <div className="relative mt-8 grid flex-1 grid-cols-[minmax(0,1fr)] content-end gap-2.5" aria-hidden="true">
      <MailRow who="Daniel, Lark & Finch" subject="Invoice 2291 for September" chip="Needs a reply" tone="hot" delay="0ms" />
      <MailRow who="Meera Iyer" subject="Rent receipt, as promised" chip="Filed" tone="calm" delay="60ms" />
      <MailRow who="Northwind Studio" subject="Thursday sync moved to 4pm" chip="Calendar updated" tone="calm" delay="120ms" />
      <MailRow who="Dr Rao's clinic" subject="Appointment moved to Tuesday 4:30" chip="Reminder set" tone="calm" delay="180ms" />
      <div className="mt-2 rounded-2xl border border-accent/25 bg-accent/[0.06] p-4 transition-transform duration-700 ease-fluid group-hover:scale-[1.02]">
        <div className="flex items-center gap-2 text-xs font-medium text-accent-hi"><PencilSimpleLine size={16} weight="light" /> Draft reply to Daniel</div>
        <p className="mt-2 text-sm leading-relaxed text-ink/90">Thanks Daniel. I will pay this on the 3rd. Could you resend the PDF with the PO number on it?</p>
        <div className="mt-3 flex gap-2 text-xs font-semibold">
          <span className="rounded-full bg-accent px-3 py-1 text-bg">Send</span>
          <span className="rounded-full border border-white/15 px-3 py-1 text-muted">Change</span>
        </div>
      </div>
    </div>
  )
}

const DAYS = ['Tue', 'Wed', 'Thu', 'Fri']

function CalendarVisual() {
  return (
    <div className="relative mt-6 flex flex-1 items-end gap-3 sm:mt-0 lg:absolute lg:inset-y-8 lg:right-8 lg:w-[50%] lg:flex-none lg:items-stretch" aria-hidden="true">
      <div className="grid w-full grid-cols-4 gap-2 transition-transform duration-700 ease-fluid group-hover:scale-[1.04]">
        {DAYS.map((d, i) => (
          <div key={d} className="relative min-h-36 rounded-2xl border border-white/[0.07] bg-white/[0.025] p-2">
            <div className="text-center text-[11px] font-medium text-muted">{d}</div>
            {i === 0 && <><div className="mt-2 h-6 rounded-lg bg-navy-hi/30" /><div className="mt-1.5 h-9 rounded-lg bg-white/[0.07]" /></>}
            {i === 1 && <div className="mt-9 h-9 rounded-lg bg-white/[0.07]" />}
            {i === 2 && (
              <>
                <div className="mt-2 h-6 rounded-lg bg-navy-hi/30" />
                <div className="mt-2 grid h-14 place-items-center rounded-lg border border-dashed border-accent bg-accent/10 px-1 text-center text-[11px] font-semibold leading-tight text-accent-hi">45 min<br />free</div>
              </>
            )}
            {i === 3 && <div className="mt-6 h-14 rounded-lg bg-navy-hi/30" />}
          </div>
        ))}
      </div>
    </div>
  )
}

function PeopleVisual() {
  const nodes = [
    { x: 24, y: 28, t: 'P' }, { x: 126, y: 18, t: 'D' }, { x: 132, y: 100, t: 'M' }, { x: 80, y: 148, t: 'S' }, { x: 16, y: 102, t: 'N' },
  ]
  return (
    <svg className="mt-auto w-full max-w-[150px] self-center pt-4 transition-transform duration-1000 ease-fluid group-hover:scale-110" viewBox="0 0 164 170" fill="none" aria-hidden="true">
      {nodes.map((n) => <line key={n.t} x1="82" y1="85" x2={n.x + 14} y2={n.y + 14} stroke="rgb(255 255 255 / 0.14)" />)}
      <circle cx="82" cy="85" r="22" fill="#f26b3a" fillOpacity="0.16" stroke="#f26b3a" strokeOpacity="0.6" />
      <circle cx="82" cy="85" r="7" fill="#f26b3a" />
      {nodes.map((n) => (
        <g key={n.t}>
          <circle cx={n.x + 14} cy={n.y + 14} r="14" fill="#17171d" stroke="rgb(111 136 171 / 0.55)" />
          <text x={n.x + 14} y={n.y + 19} textAnchor="middle" fontSize="13" fontWeight="700" fill="#f4f1ec" className="font-sans">{n.t}</text>
        </g>
      ))}
    </svg>
  )
}

function SlackVisual() {
  return (
    <div className="mt-auto grid gap-2 self-stretch pt-4" aria-hidden="true">
      <div className="flex items-center justify-between rounded-xl bg-white/[0.04] px-3 py-2 text-[13px] text-muted transition-transform duration-700 ease-fluid group-hover:-translate-x-1"><span># launch</span><span className="tnum font-semibold text-ink">12</span></div>
      <div className="flex items-center justify-between rounded-xl bg-white/[0.04] px-3 py-2 text-[13px] text-muted transition-transform duration-700 ease-fluid group-hover:-translate-x-1" style={{ transitionDelay: '60ms' }}><span># brand</span><span className="tnum font-semibold text-ink">7</span></div>
      <div className="rounded-xl border border-accent/25 bg-accent/[0.07] px-3 py-2 text-[13px] leading-snug text-ink/90">19 messages. Two need you: the logo files and the pricing copy.</div>
    </div>
  )
}

export function Bento() {
  return (
    <div className="grid grid-flow-dense grid-cols-1 gap-4 sm:grid-cols-2 lg:auto-rows-[minmax(300px,auto)] lg:grid-cols-4">
      <article className={`${SHELL} sm:col-span-2 sm:row-span-2`} data-reveal>
        <div className={CORE}>
          <div className="pointer-events-none absolute -right-24 -top-24 size-80 rounded-full bg-accent/[0.09] blur-3xl" aria-hidden="true" />
          <Head Icon={EnvelopeSimple} title="Inbox, triaged and drafted" big>
            Mavis reads what arrived, sorts what needs you and drafts the replies. Nothing is sent until you say so.
          </Head>
          <InboxVisual />
        </div>
      </article>

      <article className={`${SHELL} sm:col-span-2`} data-reveal>
        <div className={CORE}>
          <div className="lg:max-w-[44%]">
            <Head Icon={CalendarDots} title="Calendar and reminders">
              Finds free time, books it, and nudges you before a deadline slips.
            </Head>
            <div className="relative z-10 mt-5 inline-flex items-center gap-2 whitespace-nowrap rounded-full border border-white/10 bg-white/[0.04] px-3 py-1.5 text-[13px] text-muted" aria-hidden="true">
              <Bell size={16} weight="light" className="text-accent-hi" /> Call accountant, Wed 17:00
            </div>
          </div>
          <CalendarVisual />
        </div>
      </article>

      <article className={SHELL} data-reveal>
        <div className={CORE}>
          <Head Icon={UsersThree} title="Knows your people">
            Who they are, what you promised, which project they belong to.
          </Head>
          <PeopleVisual />
        </div>
      </article>

      <article className={SHELL} data-reveal>
        <div className={CORE}>
          <Head Icon={SlackLogo} title="Slack catch-up">
            Ask what you missed and get the part that needs you.
          </Head>
          <SlackVisual />
        </div>
      </article>
    </div>
  )
}

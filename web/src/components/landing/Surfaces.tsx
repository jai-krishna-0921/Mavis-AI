import { useState } from 'react'
import { CalendarDots, EnvelopeSimple, FileText, GoogleDriveLogo, SlackLogo, Table, TelegramLogo, type Icon } from '@phosphor-icons/react'

type Surface = { name: string; line: string; Icon: Icon; things: string[] }

const SURFACES: Surface[] = [
  { name: 'Telegram', Icon: TelegramLogo, line: 'Text it like a friend. Mavis answers in the same thread, and can message you first when something is due.', things: ['Plain words', 'Reminders', 'Nudges'] },
  { name: 'Slack', Icon: SlackLogo, line: 'Add it to your workspace and ask in a direct message. It catches you up on the channels you invite it to.', things: ['Catch-up', 'Direct messages', 'Channels you pick'] },
  { name: 'Gmail', Icon: EnvelopeSimple, line: 'Reads the mail you allow, sorts what needs you and drafts replies that wait for your yes.', things: ['Triage', 'Drafts', 'Asks first'] },
  { name: 'Calendar', Icon: CalendarDots, line: 'Finds time that works, books it, and reminds you before it starts.', things: ['Free slots', 'Invites', 'Reminders'] },
  { name: 'Drive', Icon: GoogleDriveLogo, line: 'Finds the file you half remember, and tells you where it lives.', things: ['Search', 'Summaries'] },
  { name: 'Docs', Icon: FileText, line: 'Summarises a document and answers questions about it, quoting the lines that matter.', things: ['Summaries', 'Quotes', 'Read only'] },
  { name: 'Sheets', Icon: Table, line: 'Pulls the figures you ask for straight into your reply. It never edits unless you ask.', things: ['Figures', 'Totals', 'Read only'] },
]

// A horizontal accordion on desktop (one panel open, the rest slim), a plain stack on phones.
export function Surfaces() {
  const [active, setActive] = useState(0)
  return (
    <ul className="m-0 flex list-none flex-col gap-3 p-0 lg:h-[460px] lg:flex-row" data-reveal>
      {SURFACES.map((s, i) => {
        const on = active === i
        return (
          <li
            key={s.name}
            data-active={on}
            className="group/panel relative overflow-hidden rounded-[1.75rem] border border-line bg-surface shadow-card transition-[flex-grow,border-color,box-shadow] duration-700 ease-fluid data-[active=true]:border-accent-hi/40 data-[active=true]:shadow-lift lg:min-h-0 lg:flex-[1_1_0%] lg:data-[active=true]:flex-[5_1_0%]"
          >
            <button
              type="button" aria-expanded={on} aria-label={s.name}
              className="absolute inset-0 z-10 cursor-pointer rounded-[1.75rem] focus-visible:outline-offset-[-4px]"
              onMouseEnter={() => setActive(i)} onFocus={() => setActive(i)} onClick={() => setActive(i)}
            />
            <div
              className={`pointer-events-none absolute -bottom-12 -right-12 hidden text-accent transition-[opacity,transform] duration-1000 ease-fluid lg:block ${on ? 'translate-y-0 opacity-[0.1]' : 'translate-y-10 opacity-0'}`}
              aria-hidden="true"
            >
              <s.Icon size={340} weight="thin" />
            </div>
            <div className="pointer-events-none relative flex flex-col gap-5 p-5 lg:absolute lg:inset-0 lg:gap-0 lg:p-6">
              <div className="flex items-center gap-4">
                <span className={`grid size-12 shrink-0 place-items-center rounded-2xl border transition-colors duration-700 ease-fluid ${on ? 'border-accent-hi/30 bg-accent-soft text-accent-hi' : 'border-line bg-bg text-ink'}`}>
                  <s.Icon size={26} weight="light" aria-hidden="true" />
                </span>
                <span className={`text-xl font-bold lg:hidden ${on ? 'hidden' : ''}`}>{s.name}</span>
              </div>
              <span className={`mt-auto hidden pb-1 text-lg font-semibold [transform:rotate(180deg)] [writing-mode:vertical-rl] ${on ? '' : 'lg:block'}`}>
                {s.name}
              </span>
              <div className={`${on ? 'block' : 'hidden'} transition-[opacity,transform] duration-700 ease-fluid lg:block lg:absolute lg:inset-x-6 lg:bottom-6 lg:w-[min(30rem,calc(100%-3rem))] ${on ? 'lg:translate-y-0 lg:opacity-100' : 'lg:translate-y-4 lg:opacity-0'}`}>
                <h3 className="text-3xl font-bold tracking-[-0.02em]">{s.name}</h3>
                <p className="mt-2 text-[15px] text-muted">{s.line}</p>
                <ul className="m-0 mt-4 flex list-none flex-wrap gap-2 p-0">
                  {s.things.map((t) => <li key={t} className="rounded-full bg-sunken px-3 py-1 text-xs font-medium text-muted">{t}</li>)}
                </ul>
              </div>
            </div>
          </li>
        )
      })}
    </ul>
  )
}

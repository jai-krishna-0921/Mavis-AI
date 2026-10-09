import { useState } from 'react'
import { CalendarDots, EnvelopeSimple, FileText, GoogleDriveLogo, SlackLogo, Table, TelegramLogo, type Icon } from '@phosphor-icons/react'

type Surface = { name: string; line: string; Icons: Icon[]; things: string[] }

const SURFACES: Surface[] = [
  { name: 'Telegram', line: 'Text it like a friend. Mavis answers in the same thread, and can message you first when something is due.', Icons: [TelegramLogo], things: ['Plain words', 'Reminders', 'Nudges when something is due'] },
  { name: 'Slack', line: 'Add it to your workspace and ask in a direct message. It catches you up on the channels you invite it to.', Icons: [SlackLogo], things: ['Catch-up', 'Direct messages', 'Channels you pick'] },
  { name: 'Gmail', line: 'Reads the mail you allow, sorts what needs you and drafts replies that wait for your yes.', Icons: [EnvelopeSimple], things: ['Triage', 'Drafts', 'Asks before sending'] },
  { name: 'Calendar', line: 'Finds time that works, books it, and reminds you before it starts.', Icons: [CalendarDots], things: ['Free slots', 'Invites', 'Reminders'] },
  { name: 'Drive', line: 'Finds the file you half remember, and tells you where it lives.', Icons: [GoogleDriveLogo], things: ['Search', 'Summaries'] },
  { name: 'Docs and Sheets', line: 'Summarises a document, or pulls the figures from a sheet straight into your reply.', Icons: [FileText, Table], things: ['Summaries', 'Figures', 'Read only unless you ask'] },
]

export function Surfaces() {
  const [active, setActive] = useState(0)
  return (
    <ul className="m-0 flex list-none flex-col gap-3 p-0 lg:h-[480px] lg:flex-row" data-reveal>
      {SURFACES.map((s, i) => {
        const on = active === i
        return (
          <li
            key={s.name}
            data-active={on}
            className="group/panel relative min-h-44 overflow-hidden rounded-[2rem] border border-white/[0.07] bg-surface transition-[flex-grow,border-color] duration-700 ease-fluid data-[active=true]:border-accent/30 lg:min-h-0 lg:flex-[1_1_0%] lg:data-[active=true]:flex-[5_1_0%]"
          >
            <button
              type="button" aria-expanded={on} aria-label={s.name}
              className="absolute inset-0 z-10 cursor-pointer rounded-[2rem] focus-visible:outline-offset-[-4px]"
              onMouseEnter={() => setActive(i)} onFocus={() => setActive(i)} onClick={() => setActive(i)}
            />
            <div
              className={`pointer-events-none absolute -right-10 -top-10 size-72 rounded-full bg-accent/[0.14] blur-3xl transition-opacity duration-700 ease-fluid ${on ? 'opacity-100' : 'opacity-0 lg:opacity-0'}`}
              aria-hidden="true"
            />
            <div
              className={`pointer-events-none absolute -bottom-10 -right-10 hidden text-white transition-[opacity,transform] duration-1000 ease-fluid lg:block ${on ? 'translate-y-0 opacity-[0.07]' : 'translate-y-10 opacity-0'}`}
              aria-hidden="true"
            >
              {(() => { const W = s.Icons[0]; return <W size={340} weight="thin" /> })()}
            </div>
            <div className="pointer-events-none relative flex flex-col gap-6 p-6 lg:absolute lg:inset-0 lg:gap-0 lg:p-7">
              <div className="flex items-center gap-2">
                {s.Icons.map((I, k) => (
                  <span key={k} className={`grid size-12 place-items-center rounded-2xl border transition-colors duration-700 ease-fluid ${on ? 'border-accent/40 bg-accent/10 text-accent-hi' : 'border-white/10 bg-white/[0.04] text-ink'}`}>
                    <I size={26} weight="light" aria-hidden="true" />
                  </span>
                ))}
              </div>
              <span className={`mt-auto hidden pb-1 text-lg font-semibold [transform:rotate(180deg)] [writing-mode:vertical-rl] ${on ? '' : 'lg:block'}`}>
                {s.name}
              </span>
              <div className={`transition-[opacity,transform] duration-700 ease-fluid lg:absolute lg:inset-x-7 lg:bottom-7 lg:w-[min(30rem,calc(100%-3.5rem))] ${on ? 'lg:translate-y-0 lg:opacity-100' : 'lg:translate-y-4 lg:opacity-0'}`}>
                <h3 className="text-3xl font-bold tracking-[-0.02em]">{s.name}</h3>
                <p className="mt-2 text-[15px] text-muted">{s.line}</p>
                <ul className="m-0 mt-4 flex list-none flex-wrap gap-2 p-0">
                  {s.things.map((t) => <li key={t} className="rounded-full border border-white/10 px-3 py-1 text-xs text-muted">{t}</li>)}
                </ul>
              </div>
            </div>
          </li>
        )
      })}
    </ul>
  )
}

import { useRef } from 'react'
import { CaretLeft, CaretRight } from '@phosphor-icons/react'

type Line = { from: 'you' | 'mavis'; text: string }
const EXAMPLES: { title: string; chat: Line[] }[] = [
  {
    title: 'Clear the morning inbox',
    chat: [
      { from: 'you', text: 'Anything urgent from this morning?' },
      { from: 'mavis', text: 'Two things. Daniel sent invoice 2291, due on the 5th. The dentist moved you to Tuesday at 4:30. Want a reply drafted to Daniel?' },
      { from: 'you', text: 'Yes. Say I will pay on the 3rd.' },
      { from: 'mavis', text: 'Drafted. Say send and it goes.' },
    ],
  },
  {
    title: 'Find time for a meeting',
    chat: [
      { from: 'you', text: 'Find me 45 minutes with Priya this week.' },
      { from: 'mavis', text: 'Thursday at 11:00 is free for both of you. Shall I book it and send the invite?' },
      { from: 'you', text: 'Book it.' },
      { from: 'mavis', text: 'Booked. I will remind you at 10:45.' },
    ],
  },
  {
    title: 'Catch up on Slack',
    chat: [
      { from: 'you', text: 'Catch me up on #launch.' },
      { from: 'mavis', text: '19 messages since yesterday. Pricing page copy is still the blocker. Sam is waiting on you for the logo files. Nothing else needs you.' },
    ],
  },
  {
    title: 'Remember, and forget',
    chat: [
      { from: 'you', text: 'Remember that I prefer aisle seats.' },
      { from: 'mavis', text: 'Noted. I will check seat choice before any booking.' },
      { from: 'you', text: 'Forget the old hotel preference.' },
      { from: 'mavis', text: 'Done. It is gone from your Vault.' },
    ],
  },
  {
    title: 'Never miss a deadline',
    chat: [
      { from: 'you', text: 'Remind me to call the accountant before Thursday’s filing deadline.' },
      { from: 'mavis', text: 'I will nudge you Wednesday at 5pm and again Thursday at 9. Does that work?' },
    ],
  },
]

export function Examples() {
  const track = useRef<HTMLDivElement>(null)
  const go = (dir: 1 | -1) => {
    const el = track.current
    if (!el) return
    const card = el.querySelector<HTMLElement>('[data-card]')
    el.scrollBy({ left: dir * ((card?.offsetWidth ?? 360) + 16), behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' })
  }
  return (
    <div>
      <div className="mb-12 flex flex-wrap items-end justify-between gap-6" data-reveal>
        <div>
          <h2 id="examples" className="max-w-3xl text-[length:clamp(2.1rem,4.4vw,4rem)] font-bold leading-[1.05] tracking-[-0.035em]">What you can ask Mavis.</h2>
          <p className="mt-5 max-w-xl text-lg text-muted">Example conversations, written to show how Mavis answers.</p>
        </div>
        <div className="flex gap-2">
          <button type="button" className="btn !size-12 !p-0" aria-label="Previous example" onClick={() => go(-1)}><CaretLeft size={20} weight="bold" /></button>
          <button type="button" className="btn !size-12 !p-0" aria-label="Next example" onClick={() => go(1)}><CaretRight size={20} weight="bold" /></button>
        </div>
      </div>
      <div
        ref={track} role="region" aria-label="Example conversations" tabIndex={0}
        className="no-scrollbar flex snap-x snap-mandatory gap-4 overflow-x-auto scroll-smooth pb-4"
      >
        {EXAMPLES.map((ex) => (
          <article key={ex.title} data-card className="panel w-[min(86vw,400px)] shrink-0 snap-start">
            <div className="panel-core flex h-full min-h-[400px] flex-col !px-5 !py-5">
              <h3 className="text-lg font-bold tracking-tight">{ex.title}</h3>
              <div className="mt-6 flex flex-1 flex-col gap-2.5 text-[15px] leading-snug">
                {ex.chat.map((l, i) => (
                  <p
                    key={i}
                    className={`relative max-w-[88%] px-4 py-2.5 ${l.from === 'you' ? 'ml-auto rounded-3xl rounded-br-lg bg-navy-hi/25 text-ink' : 'rounded-3xl rounded-bl-lg bg-raised text-ink'}`}
                  >
                    <span className="sr-only">{l.from === 'you' ? 'You: ' : 'Mavis: '}</span>{l.text}
                  </p>
                ))}
              </div>
            </div>
          </article>
        ))}
      </div>
    </div>
  )
}

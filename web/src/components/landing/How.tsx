import { useRef } from 'react'
import { ChatCircleDots, Check, Eye, Eraser, Lock } from '@phosphor-icons/react'
import { gsap, MOTION_DESKTOP, ScrollTrigger, useGSAP } from '../../lib/gsap'

const STEPS = [
  {
    title: 'You send a message',
    body: 'Ask in plain words, the way you would text a person. There is no app to learn and no commands to remember.',
    visual: (
      <div className="grid gap-2 text-sm" aria-hidden="true">
        <div className="ml-auto max-w-[85%] rounded-2xl rounded-br-md bg-navy text-white px-4 py-2.5">What do I owe people this week?</div>
        <div className="max-w-[85%] rounded-2xl rounded-bl-md bg-sunken px-4 py-2.5 text-ink">Looking now. Give me a moment.</div>
      </div>
    ),
    Icon: ChatCircleDots,
  },
  {
    title: 'Mavis looks, only where you allow',
    body: 'It reads the mail, calendar and files you have connected, and treats what it finds as information. Never as orders.',
    visual: (
      <div className="flex flex-wrap gap-2 text-[13px]" aria-hidden="true">
        {['Gmail', 'Calendar', 'Drive', 'Slack'].map((s, i) => (
          <span key={s} className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 ${i < 3 ? 'border-ok/30 bg-ok-soft' : 'border-line-strong text-muted'}`}>
            {i < 3 ? <Check size={14} weight="bold" className="text-ok" /> : <Lock size={14} weight="light" />} {s}
          </span>
        ))}
      </div>
    ),
    Icon: Eye,
  },
  {
    title: 'It proposes, you decide',
    body: 'Drafts, bookings and messages wait for your yes. Nothing leaves your account until you approve it.',
    visual: (
      <div className="rounded-2xl border border-accent-hi/25 bg-accent-soft p-4 text-sm" aria-hidden="true">
        <div className="text-xs font-medium text-accent-hi">Ready to send to Daniel</div>
        <p className="mt-1.5 text-ink">I will pay invoice 2291 on the 3rd. Could you resend it with the PO number?</p>
        <div className="mt-3 flex gap-2 text-xs font-semibold"><span className="rounded-full bg-accent-hi px-3 py-1 text-white">Approve</span><span className="rounded-full border border-line-strong px-3 py-1 text-muted">Change</span></div>
      </div>
    ),
    Icon: Check,
  },
  {
    title: 'It remembers, and forgets on request',
    body: 'Useful facts go into your Vault, where you can read them, correct them or delete them whenever you like.',
    visual: (
      <div className="flex items-center justify-between gap-3 rounded-2xl border border-line bg-bg p-4 text-sm" aria-hidden="true">
        <div><div className="font-semibold">Prefers aisle seats</div><div className="text-[13px] text-muted">You told Mavis</div></div>
        <span className="inline-flex items-center gap-1.5 rounded-full border border-danger/30 bg-danger-soft px-3 py-1 text-xs text-danger"><Eraser size={14} weight="light" /> Forget</span>
      </div>
    ),
    Icon: Eraser,
  },
]

export function How() {
  const root = useRef<HTMLElement>(null)

  useGSAP(() => {
    const mm = gsap.matchMedia()
    mm.add(MOTION_DESKTOP, () => {
      // The title stays put while the steps pass by on the right.
      ScrollTrigger.create({ trigger: '[data-how-rail]', start: 'top top', end: 'bottom bottom', pin: '[data-how-title]', pinSpacing: false })
      gsap.utils.toArray<HTMLElement>('[data-step]').forEach((el) => {
        gsap.set(el, { scale: 0.85, opacity: 0.25 })
        gsap.timeline({ scrollTrigger: { trigger: el, start: 'top 85%', end: 'bottom 15%', scrub: true } })
          .to(el, { scale: 1, opacity: 1, ease: 'none', duration: 0.35 })
          .to(el, { scale: 1, opacity: 1, ease: 'none', duration: 0.3 })
          .to(el, { scale: 0.94, opacity: 0.2, ease: 'none', duration: 0.35 })
      })
    })
  }, { scope: root })

  return (
    <section id="how" ref={root} className="relative px-5 py-20 md:py-32">
      <div className="mx-auto max-w-6xl lg:grid lg:grid-cols-[5fr_7fr] lg:gap-16" data-how-rail>
        <div className="lg:relative">
          <div data-how-title className="lg:flex lg:h-[100dvh] lg:items-center">
            <div>
              <h2 className="text-[length:clamp(2.5rem,5vw,4.5rem)] font-bold leading-[1.02] tracking-[-0.04em]">How Mavis works</h2>
              <p className="mt-5 max-w-[38ch] text-lg text-muted">Four quiet steps, every time. You stay in charge of each one.</p>
            </div>
          </div>
        </div>
        <ol className="m-0 mt-16 grid list-none gap-6 p-0 lg:mt-0 lg:gap-0">
          {STEPS.map((s, i) => (
            <li key={s.title} className="lg:flex lg:min-h-[78dvh] lg:items-center" data-reveal>
              <div className="panel w-full shadow-lift" data-step>
                <div className="panel-core">
                  <div className="mb-6 flex items-center gap-4">
                    <span className="tnum grid size-11 place-items-center rounded-full bg-accent-hi text-base font-bold text-white">{i + 1}</span>
                    <s.Icon size={26} weight="light" className="text-muted" aria-hidden="true" />
                  </div>
                  <h3 className="text-2xl font-bold tracking-[-0.02em] sm:text-3xl">{s.title}</h3>
                  <p className="mt-3 max-w-[48ch] text-muted">{s.body}</p>
                  <div className="mt-6">{s.visual}</div>
                </div>
              </div>
            </li>
          ))}
        </ol>
      </div>
    </section>
  )
}

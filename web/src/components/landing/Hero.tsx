import { useRef } from 'react'
import { Link } from 'react-router-dom'
import { CalendarCheck, Check, EnvelopeSimple, Bell } from '@phosphor-icons/react'
import { gsap, MOTION, useGSAP } from '../../lib/gsap'
import { TextMavis } from '../TextMavis'
import { Logo } from '../Logo'

function Msg({ from, children, step }: { from: 'you' | 'mavis'; children: React.ReactNode; step: number }) {
  return (
    <div data-msg={step} className={`max-w-[88%] px-4 py-2.5 text-[15px] leading-snug ${from === 'you'
      ? 'ml-auto rounded-3xl rounded-br-lg bg-navy text-white'
      : 'rounded-3xl rounded-bl-lg bg-sunken text-ink'}`}>
      <span className="sr-only">{from === 'you' ? 'You: ' : 'Mavis: '}</span>
      {children}
    </div>
  )
}

function Done({ Icon, title, sub, step }: { Icon: typeof Check; title: string; sub: string; step: number }) {
  return (
    <li data-done={step} className="flex items-start gap-3 rounded-2xl border border-line bg-surface p-3.5 shadow-[0_1px_2px_rgb(26_23_20/0.04)]">
      <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-ok-soft text-ok"><Icon size={18} weight="bold" aria-hidden="true" /></span>
      <div className="min-w-0 text-sm leading-snug"><div className="font-semibold">{title}</div><div className="mt-0.5 text-muted">{sub}</div></div>
    </li>
  )
}

export function Hero({ signedIn, signIn }: { signedIn: boolean; signIn: string }) {
  const root = useRef<HTMLElement>(null)

  useGSAP(() => {
    const mm = gsap.matchMedia()
    mm.add(MOTION, () => {
      const tl = gsap.timeline({ defaults: { ease: 'expo.out' } })
      tl.from('[data-hero]', { y: 36, opacity: 0, duration: 1.1, stagger: 0.09 })
        .from('[data-preview]', { y: 70, opacity: 0, scale: 0.97, duration: 1.3 }, 0.35)
      // The conversation plays in order, then the results list fills in.
      const chat = gsap.timeline({ delay: 1.2, defaults: { ease: 'power3.out' } })
      chat.from('[data-msg="1"]', { y: 14, opacity: 0, duration: 0.5 })
        .from('[data-typing]', { opacity: 0, duration: 0.3 }, '+=0.2')
        .to('[data-typing]', { opacity: 0, height: 0, margin: 0, duration: 0.25 }, '+=1')
        .from('[data-msg="2"]', { y: 14, opacity: 0, duration: 0.5 })
        .from('[data-draft]', { y: 14, opacity: 0, duration: 0.5 }, '+=0.25')
        .from('[data-msg="3"]', { y: 14, opacity: 0, duration: 0.5 }, '+=0.6')
        .from('[data-msg="4"]', { y: 14, opacity: 0, duration: 0.5 }, '+=0.7')
        .from('[data-done]', { x: 24, opacity: 0, duration: 0.55, stagger: 0.18 }, '-=0.3')
      // A slow parallax on the preview as the page moves.
      gsap.to('[data-preview]', { yPercent: -4, ease: 'none', scrollTrigger: { trigger: root.current, start: 'top top', end: 'bottom top', scrub: true } })
    })
  }, { scope: root })

  return (
    <section ref={root} aria-labelledby="hero-h" className="relative isolate px-5 pb-24 pt-32 sm:pt-40 md:pb-40 md:pt-48">
      <div className="mesh-hero pointer-events-none absolute inset-x-0 top-0 -z-10 h-[900px]" aria-hidden="true" />
      <div className="mx-auto max-w-6xl text-center">
        <h1 id="hero-h" data-hero className="mx-auto max-w-6xl text-[length:clamp(2.5rem,6.1vw,5.5rem)] font-bold leading-[1.02] tracking-[-0.045em]">
          Hand off the busywork.<br className="hidden md:block" /> <span className="text-accent-hi">Mavis handles it in chat.</span>
        </h1>
        <p data-hero className="mx-auto mt-7 max-w-xl text-lg text-muted md:mt-9 md:text-xl">
          A calm assistant that lives in Telegram and Slack. It triages your inbox, keeps your calendar and remembers the people who matter.
        </p>
        <div data-hero className="mt-10 flex flex-wrap items-center justify-center gap-3">
          <TextMavis size="md" />
          {signedIn
            ? <Link to="/workspace" className="btn !min-h-12 !px-6 !text-[15px]">Open workspace</Link>
            : <Link to={signIn} className="btn !min-h-12 !px-6 !text-[15px]">Sign in</Link>}
        </div>
      </div>

      <figure data-preview className="relative mx-auto mt-16 max-w-5xl md:mt-24" aria-label="An example conversation with Mavis">
        <div className="overflow-hidden rounded-[2rem] border border-line bg-surface shadow-lift lg:grid lg:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
          <div className="flex flex-col gap-3 p-5 sm:p-8">
            <div className="mb-2 flex items-center gap-3 border-b border-line pb-4">
              <Logo size={36} title="Mavis AI" />
              <div className="text-left leading-tight">
                <div className="font-bold">Mavis</div>
                <div className="flex items-center gap-1.5 text-[13px] text-muted"><span className="size-1.5 rounded-full bg-ok" aria-hidden="true" />Replies in seconds</div>
              </div>
            </div>
            <div className="flex flex-col gap-3 text-left">
              <Msg from="you" step={1}>Morning. What did I miss, and can you move my 3pm with Priya?</Msg>
              <div data-typing className="flex h-9 w-16 items-center justify-center gap-1 rounded-3xl rounded-bl-lg bg-sunken" aria-hidden="true">
                {[0, 1, 2].map((i) => <span key={i} className="size-1.5 rounded-full bg-muted" style={{ animation: `blink 1.1s ${i * 0.18}s infinite` }} />)}
              </div>
              <Msg from="mavis" step={2}>Four emails need you. I drafted replies to two, and they wait for your yes. Priya is free at 4:30 today. Shall I move the meeting and tell her?</Msg>
              <div data-draft className="max-w-[88%] rounded-3xl border border-line bg-surface p-4 shadow-[0_1px_2px_rgb(26_23_20/0.04)]">
                <div className="text-xs font-semibold text-accent-hi">Draft to Daniel Okafor</div>
                <p className="mt-1.5 text-sm leading-snug">Invoice 2291: I will pay it on the 3rd. Could you resend it with the PO number?</p>
                <div className="mt-3 flex gap-2 text-xs font-semibold" aria-hidden="true">
                  <span className="rounded-full bg-accent-hi px-3.5 py-1.5 text-white">Approve</span>
                  <span className="rounded-full border border-line-strong px-3.5 py-1.5 text-muted">Edit</span>
                </div>
              </div>
              <Msg from="you" step={3}>Move it. Send Daniel&apos;s too.</Msg>
              <Msg from="mavis" step={4}>Done. You and Priya are at 4:30 and Daniel has his reply. I will check for his answer tomorrow morning.</Msg>
            </div>
          </div>
          <aside aria-label="What Mavis did" className="hidden border-l border-line bg-bg p-8 text-left lg:block">
            <div className="mb-5 text-sm font-semibold text-muted">Done for you</div>
            <ul className="m-0 grid list-none gap-3 p-0">
              <Done Icon={EnvelopeSimple} step={1} title="2 replies drafted" sub="Waiting for your yes" />
              <Done Icon={CalendarCheck} step={2} title="Meeting moved to 4:30" sub="Priya has the new invite" />
              <Done Icon={Bell} step={3} title="Follow up set" sub="Tomorrow at 9:00 about Daniel" />
            </ul>
          </aside>
        </div>
        <figcaption className="sr-only">Mavis summarises the inbox, drafts replies and moves a meeting after one message.</figcaption>
      </figure>
    </section>
  )
}

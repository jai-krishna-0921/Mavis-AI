import { useRef } from 'react'
import { Link } from 'react-router-dom'
import { Weave } from '../Weave'
import { TextMavis } from '../TextMavis'
import { gsap, MOTION, useGSAP } from '../../lib/gsap'

export function Hero({ signedIn, signIn }: { signedIn: boolean; signIn: string }) {
  const root = useRef<HTMLElement>(null)

  useGSAP(() => {
    const mm = gsap.matchMedia()
    mm.add(MOTION, () => {
      gsap.from('[data-hero]', { y: 32, opacity: 0, filter: 'blur(10px)', duration: 1.2, ease: 'expo.out', stagger: 0.12, delay: 0.1 })
      gsap.from('[data-orbit]', { scale: 0.7, opacity: 0, duration: 2.2, ease: 'expo.out', stagger: 0.18 })
    })
  }, { scope: root })

  return (
    <section ref={root} className="relative isolate flex min-h-[100dvh] items-center justify-center overflow-hidden px-5 pb-24 pt-32 text-center">
      <div className="mesh-hero absolute inset-0 -z-20" aria-hidden="true" />
      <svg className="pointer-events-none absolute left-1/2 top-[44%] -z-10 w-[min(150vw,1180px)] -translate-x-1/2 -translate-y-1/2" viewBox="-500 -500 1000 1000" fill="none" aria-hidden="true">
        <g data-orbit className="orbit origin-center"><circle r="230" stroke="rgb(255 255 255 / 0.07)" strokeDasharray="2 10" /></g>
        <g data-orbit className="orbit-rev origin-center"><circle r="340" stroke="rgb(255 255 255 / 0.06)" /><circle cx="340" r="5" fill="#f26b3a" /></g>
        <g data-orbit className="orbit origin-center"><circle r="460" stroke="rgb(255 255 255 / 0.045)" strokeDasharray="1 14" /><circle cx="-460" r="4" fill="#6f88ab" /></g>
      </svg>
      <div className="absolute inset-x-0 bottom-0 -z-10 h-48 bg-gradient-to-t from-bg to-transparent" aria-hidden="true" />

      <div className="relative mx-auto flex w-full max-w-6xl flex-col items-center">
        <div data-hero className="float"><Weave size={96} animated title="Mavis AI" /></div>
        <h1 data-hero className="mt-9 max-w-6xl text-balance text-[length:clamp(2.75rem,5vw,5.25rem)] font-bold leading-[1.02] tracking-[-0.04em]">
          The assistant that lives in your chat.
        </h1>
        <p data-hero className="mt-6 max-w-2xl text-lg text-muted sm:text-xl">
          Inbox, calendar and the people behind them, handled in a chat.
        </p>
        <div data-hero className="mt-10 flex flex-wrap items-center justify-center gap-3">
          <TextMavis size="md" />
          {signedIn
            ? <Link to="/workspace" className="btn !min-h-12 !px-6 !text-[15px]">Open workspace</Link>
            : <Link to={signIn} className="btn !min-h-12 !px-6 !text-[15px]">Sign in</Link>}
        </div>
      </div>
    </section>
  )
}

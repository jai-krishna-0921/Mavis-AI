import { useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { CalendarDots, EnvelopeSimple, UsersThree } from '@phosphor-icons/react'
import { useMe } from '../api/hooks'
import { gsap, MOTION, ScrollTrigger, useGSAP } from '../lib/gsap'
import { Nav } from '../components/landing/Nav'
import { Hero } from '../components/landing/Hero'
import { Bento } from '../components/landing/Bento'
import { Surfaces } from '../components/landing/Surfaces'
import { How } from '../components/landing/How'
import { Privacy } from '../components/landing/Privacy'
import { Examples } from '../components/landing/Examples'
import { FinalCta } from '../components/landing/FinalCta'
import { Footer } from '../components/landing/Footer'

// A small pill-shaped visual that sits inside a heading, in line with the words.
function InlinePill({ tone, children }: { tone: 'warm' | 'cool' | 'plain'; children: React.ReactNode }) {
  const bg = tone === 'warm'
    ? 'bg-[linear-gradient(135deg,#f26b3a,#c4471c)] text-bg'
    : tone === 'cool'
      ? 'bg-[linear-gradient(135deg,#3b5273,#1e2a3a)] text-ink'
      : 'bg-raised text-ink'
  return (
    <span
      className={`mx-[0.04em] inline-flex h-[0.78em] w-[1.9em] -translate-y-[0.06em] items-center justify-center overflow-hidden rounded-full border border-white/15 align-middle shadow-[inset_0_1px_0_rgba(255,255,255,0.2)] ${bg}`}
      aria-hidden="true"
    >
      {children}
    </span>
  )
}

export function Landing() {
  const me = useMe()
  const [params] = useSearchParams()
  const invite = params.get('invite')
  const signIn = invite ? `/login?invite=${encodeURIComponent(invite)}` : '/login'
  const signedIn = Boolean(me.data)
  const root = useRef<HTMLDivElement>(null)

  useGSAP(() => {
    const mm = gsap.matchMedia()
    mm.add(MOTION, () => {
      gsap.utils.toArray<HTMLElement>('[data-reveal]').forEach((el) => {
        gsap.from(el, { y: 56, opacity: 0, filter: 'blur(8px)', duration: 1.1, ease: 'expo.out', scrollTrigger: { trigger: el, start: 'top 90%', once: true } })
      })
    })
    void document.fonts?.ready.then(() => ScrollTrigger.refresh())
  }, { scope: root })

  return (
    <div ref={root}>
      <a className="skip-link" href="#main">Skip to content</a>
      <Nav signedIn={signedIn} signIn={signIn} />
      <main id="main" tabIndex={-1} className="relative w-full max-w-full overflow-x-clip">
        <Hero signedIn={signedIn} signIn={signIn} />

        <section aria-labelledby="what" className="px-5 py-32 md:py-48">
          <div className="mx-auto max-w-6xl">
            <h2 id="what" data-reveal className="mb-16 max-w-6xl text-[length:clamp(2.1rem,4.6vw,4.25rem)] font-bold leading-[1.1] tracking-[-0.035em] md:mb-24">
              Mavis keeps <InlinePill tone="warm"><EnvelopeSimple size="0.42em" weight="fill" /></InlinePill> your inbox clear,
              {' '}<InlinePill tone="cool"><CalendarDots size="0.42em" weight="fill" /></InlinePill> your week in order and
              {' '}<InlinePill tone="plain"><UsersThree size="0.42em" weight="fill" /></InlinePill> your people close.
            </h2>
            <Bento />
          </div>
        </section>

        <section aria-labelledby="where" className="px-5 py-32 md:py-48">
          <div className="mx-auto max-w-6xl">
            <h2 id="where" data-reveal className="mb-6 max-w-3xl text-[length:clamp(2.1rem,4.4vw,4rem)] font-bold leading-[1.05] tracking-[-0.035em]">Wherever you already work.</h2>
            <p data-reveal className="mb-14 max-w-xl text-lg text-muted">Mavis meets you in the apps you open every day, and only reaches into the ones you connect.</p>
            <Surfaces />
          </div>
        </section>

        <How />
        <Privacy />

        <section aria-labelledby="examples" className="px-5 py-32 md:py-48">
          <div className="mx-auto max-w-6xl"><Examples /></div>
        </section>

        <FinalCta />
      </main>
      <Footer />
    </div>
  )
}

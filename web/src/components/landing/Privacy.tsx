import { useRef } from 'react'
import { Eraser, HandPalm, ShieldCheck } from '@phosphor-icons/react'
import { gsap, MOTION, useGSAP } from '../../lib/gsap'

const PROMISE =
  'Mavis reads your mail as data, never as instructions. It asks before it sends anything in your name. And it forgets whatever you tell it to, whenever you say so.'

const POINTS = [
  { Icon: ShieldCheck, title: 'Data, not instructions', text: 'A message in your inbox cannot give Mavis orders. It is read, summarised and left alone.' },
  { Icon: HandPalm, title: 'You approve first', text: 'Emails, invites and posts wait for your yes. You can read exactly what would go out.' },
  { Icon: Eraser, title: 'Forget on request', text: 'Open your Vault, pick anything Mavis knows, and delete it. Or delete the lot.' },
]

export function Privacy() {
  const root = useRef<HTMLElement>(null)

  useGSAP(() => {
    const mm = gsap.matchMedia()
    mm.add(MOTION, () => {
      const words = gsap.utils.toArray<HTMLElement>('[data-word]')
      gsap.fromTo(words, { opacity: 0.12 }, {
        opacity: 1, ease: 'none', stagger: 0.12,
        scrollTrigger: { trigger: '[data-promise]', start: 'top 82%', end: 'bottom 48%', scrub: true },
      })
    })
  }, { scope: root })

  return (
    <section id="privacy" ref={root} className="relative px-5 py-32 md:py-48">
      <div className="mesh-cta pointer-events-none absolute inset-0 -z-10 opacity-40" aria-hidden="true" />
      <div className="mx-auto max-w-6xl">
        <h2 className="sr-only">Privacy</h2>
        <p data-promise className="max-w-6xl text-[length:clamp(1.75rem,3.7vw,3.4rem)] font-semibold leading-[1.14] tracking-[-0.03em]">
          {PROMISE.split(' ').map((w, i) => <span key={i} data-word>{w}{' '}</span>)}
        </p>
        <ul className="m-0 mt-20 grid list-none gap-10 p-0 md:mt-28 md:grid-cols-3 md:gap-8">
          {POINTS.map(({ Icon, title, text }) => (
            <li key={title} className="border-t border-white/10 pt-6" data-reveal>
              <Icon size={28} weight="light" className="text-accent-hi" aria-hidden="true" />
              <h3 className="mt-5 text-xl font-bold tracking-tight">{title}</h3>
              <p className="mt-2 max-w-[34ch] text-[15px] text-muted">{text}</p>
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}

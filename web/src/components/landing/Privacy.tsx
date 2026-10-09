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

// The one deep ink-blue moment on the page. The promise is revealed word by word as you scroll.
export function Privacy() {
  const root = useRef<HTMLElement>(null)

  useGSAP(() => {
    const mm = gsap.matchMedia()
    mm.add(MOTION, () => {
      const words = gsap.utils.toArray<HTMLElement>('[data-word]')
      gsap.fromTo(words, { opacity: 0.2 }, {
        opacity: 1, ease: 'none', stagger: 0.12,
        scrollTrigger: { trigger: '[data-promise]', start: 'top 80%', end: 'bottom 50%', scrub: true },
      })
    })
  }, { scope: root })

  return (
    <section id="privacy" ref={root} className="px-3 py-16 sm:px-5 md:py-32">
      <div className="mx-auto max-w-[1280px] overflow-hidden rounded-[2.5rem] bg-navy px-6 py-20 text-white sm:px-12 md:rounded-[3.5rem] md:px-20 md:py-32">
        <h2 className="sr-only">Privacy</h2>
        <p data-promise className="max-w-6xl text-[length:clamp(1.75rem,3.7vw,3.4rem)] font-semibold leading-[1.14] tracking-[-0.03em]">
          {PROMISE.split(' ').map((w, i) => <span key={i} data-word>{w}{' '}</span>)}
        </p>
        <ul className="m-0 mt-16 grid list-none gap-10 p-0 md:mt-24 md:grid-cols-3 md:gap-8">
          {POINTS.map(({ Icon, title, text }) => (
            <li key={title} className="border-t border-white/20 pt-6" data-reveal>
              <Icon size={28} weight="light" className="text-[#ff9a73]" aria-hidden="true" />
              <h3 className="mt-5 text-xl font-bold tracking-tight">{title}</h3>
              <p className="mt-2 max-w-[34ch] text-[15px] text-[#c9d3e3]">{text}</p>
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}

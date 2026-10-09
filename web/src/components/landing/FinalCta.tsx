import { Link } from 'react-router-dom'
import { TextMavis } from '../TextMavis'

export function FinalCta({ signIn }: { signIn: string }) {
  return (
    <section className="px-3 py-16 sm:px-5 md:py-32">
      <div className="relative mx-auto max-w-[1280px] overflow-hidden rounded-[2.5rem] bg-accent-hi px-6 py-24 text-center text-white md:rounded-[3.5rem] md:py-40">
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(60%_80%_at_50%_0%,rgb(255_255_255/0.22),transparent_70%)]" aria-hidden="true" />
        <div className="relative">
          <h2 data-reveal className="text-[length:clamp(3.5rem,13vw,11rem)] font-bold leading-[0.9] tracking-[-0.055em]">Text Mavis.</h2>
          <p data-reveal className="mx-auto mt-8 max-w-md text-lg text-white">Say hello. It will take it from there.</p>
          <div data-reveal className="mt-12 flex flex-wrap items-center justify-center gap-3">
            <TextMavis size="xl" variant="light" />
            <Link to={signIn} className="inline-flex min-h-16 items-center rounded-full border border-white/50 px-9 text-lg font-medium text-white no-underline transition-colors duration-500 ease-fluid hover:bg-white/15">Sign in</Link>
          </div>
        </div>
      </div>
    </section>
  )
}

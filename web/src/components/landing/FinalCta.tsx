import { TextMavis } from '../TextMavis'

export function FinalCta() {
  return (
    <section className="relative isolate overflow-hidden px-5 py-32 text-center md:py-56">
      <div className="mesh-cta absolute inset-0 -z-10" aria-hidden="true" />
      <div className="mx-auto max-w-6xl">
        <h2 data-reveal className="text-[length:clamp(4rem,15vw,13rem)] font-bold leading-[0.9] tracking-[-0.055em]">Text Mavis.</h2>
        <p data-reveal className="mx-auto mt-8 max-w-md text-lg text-muted">Say hello. It will take it from there.</p>
        <div data-reveal className="mt-12 flex justify-center"><TextMavis size="xl" /></div>
      </div>
    </section>
  )
}

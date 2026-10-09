import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ArrowLeft } from '@phosphor-icons/react'
import { Logo } from './Logo'

// Shared frame for /login and /link-telegram: brand on the left, one lifted card on the right.
export function AuthShell({ title, lede, children }: { title: string; lede: ReactNode; children: ReactNode }) {
  return (
    <div className="relative isolate min-h-[100dvh] overflow-x-hidden">
      <div className="mesh-hero absolute inset-0 -z-10" aria-hidden="true" />
      <a className="skip-link" href="#main">Skip to content</a>
      <main id="main" tabIndex={-1} className="mx-auto grid min-h-[100dvh] w-full max-w-6xl items-center gap-10 px-5 py-12 lg:grid-cols-[minmax(0,4fr)_minmax(0,6fr)] lg:gap-20 lg:px-10">
        <div className="grid content-center gap-6">
          <Link to="/" className="inline-flex w-max items-center gap-2 rounded-full text-sm text-muted no-underline transition-colors hover:text-ink">
            <ArrowLeft size={16} weight="light" aria-hidden="true" /> Back to home
          </Link>
          <Logo size={56} live title="Mavis AI" className="logo-intro" />
          <h1 className="text-[clamp(2.25rem,4.4vw,3.75rem)] font-bold leading-[1.02] tracking-[-0.035em]">{title}</h1>
          <p className="max-w-[44ch] text-base text-muted">{lede}</p>
        </div>
        <div className="panel shadow-lift">
          <div className="panel-core !p-6 sm:!p-9">{children}</div>
        </div>
      </main>
    </div>
  )
}

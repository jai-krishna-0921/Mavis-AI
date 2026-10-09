import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Wordmark } from '../Logo'
import { TextMavis } from '../TextMavis'

const LINKS = [
  { href: '#where', label: 'Connected apps' },
  { href: '#how', label: 'How it works' },
  { href: '#privacy', label: 'Privacy' },
]

const pill = 'rounded-full px-4 py-2 text-sm font-medium text-muted no-underline transition-colors duration-500 ease-fluid hover:bg-sunken hover:text-ink'

export function Nav({ signedIn, signIn }: { signedIn: boolean; signIn: string }) {
  const [open, setOpen] = useState(false)

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('keydown', onKey)
    document.body.style.overflow = 'hidden'
    return () => { document.removeEventListener('keydown', onKey); document.body.style.overflow = '' }
  }, [open])

  return (
    <>
      <header className="pointer-events-none fixed inset-x-0 top-3 z-40 flex justify-center px-3 sm:top-5 sm:px-4">
        <nav
          aria-label="Main"
          className="pointer-events-auto flex max-w-full items-center gap-1 rounded-full border border-line bg-surface/80 p-1.5 pl-3 shadow-card backdrop-blur-xl sm:pl-4"
        >
          <Link to="/" aria-label="Mavis AI home" className="mr-1 rounded-full no-underline sm:mr-2"><Wordmark size={30} live /></Link>
          <div className="hidden items-center md:flex">
            {LINKS.map((l) => <a key={l.href} href={l.href} className={pill}>{l.label}</a>)}
            {signedIn
              ? <Link to="/workspace" className={pill}>Open workspace</Link>
              : <Link to={signIn} className={pill}>Sign in</Link>}
          </div>
          <TextMavis size="sm" className="ml-1" />
          <button
            type="button" aria-expanded={open} aria-controls="mobile-menu" aria-label={open ? 'Close menu' : 'Open menu'}
            onClick={() => setOpen((o) => !o)}
            className="relative ml-0.5 grid size-9 shrink-0 place-items-center rounded-full border border-line-strong md:hidden"
          >
            <span className={`absolute h-0.5 w-4 rounded bg-ink transition-transform duration-500 ease-fluid ${open ? 'rotate-45' : '-translate-y-[3px]'}`} />
            <span className={`absolute h-0.5 w-4 rounded bg-ink transition-transform duration-500 ease-fluid ${open ? '-rotate-45' : 'translate-y-[3px]'}`} />
          </button>
        </nav>
      </header>

      <div
        id="mobile-menu"
        className={`fixed inset-0 z-39 flex flex-col justify-center gap-2 bg-bg/95 px-8 backdrop-blur-2xl transition-opacity duration-500 ease-fluid md:hidden ${open ? 'opacity-100' : 'pointer-events-none opacity-0'}`}
        aria-hidden={!open}
        onClick={() => setOpen(false)}
      >
        {[...LINKS.map((l) => ({ ...l, to: null as string | null })),
          { href: '', label: signedIn ? 'Open workspace' : 'Sign in', to: signedIn ? '/workspace' : signIn }].map((l, i) => {
          const cls = `block text-5xl font-bold tracking-[-0.03em] no-underline transition-[opacity,transform] duration-700 ease-fluid ${open ? 'translate-y-0 opacity-100' : 'translate-y-12 opacity-0'}`
          const delay = open ? `${100 + i * 70}ms` : '0ms'
          return l.to
            ? <Link key={l.label} to={l.to} className={cls} ref={(el) => { el?.style.setProperty('transition-delay', delay) }} tabIndex={open ? 0 : -1}>{l.label}</Link>
            : <a key={l.label} href={l.href} className={cls} ref={(el) => { el?.style.setProperty('transition-delay', delay) }} tabIndex={open ? 0 : -1}>{l.label}</a>
        })}
      </div>
    </>
  )
}

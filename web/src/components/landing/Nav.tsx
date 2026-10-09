import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Weave } from '../Weave'
import { TextMavis } from '../TextMavis'

const LINKS = [
  { href: '#how', label: 'How it works' },
  { href: '#privacy', label: 'Privacy' },
]

const pill = 'rounded-full px-4 py-2 text-sm font-medium text-muted no-underline transition-colors duration-500 ease-fluid hover:bg-white/[0.07] hover:text-ink'

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
      <header className="pointer-events-none fixed inset-x-0 top-4 z-40 flex justify-center px-4 sm:top-5">
        <nav
          aria-label="Main"
          className="pointer-events-auto flex items-center gap-1 rounded-full border border-white/10 bg-bg/55 p-1.5 pl-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_20px_50px_-24px_rgba(0,0,0,0.9)] backdrop-blur-2xl"
        >
          <Link to="/" className="mr-2 flex items-center gap-2.5 rounded-full text-[17px] font-bold tracking-tight no-underline">
            <Weave size={28} /> <span>Mavis AI</span>
          </Link>
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
            className="relative ml-1 grid size-9 shrink-0 place-items-center rounded-full border border-white/10 md:hidden"
          >
            <span className={`absolute h-px w-4 bg-ink transition-transform duration-500 ease-fluid ${open ? 'rotate-45' : '-translate-y-[3px]'}`} />
            <span className={`absolute h-px w-4 bg-ink transition-transform duration-500 ease-fluid ${open ? '-rotate-45' : 'translate-y-[3px]'}`} />
          </button>
        </nav>
      </header>

      <div
        id="mobile-menu"
        className={`fixed inset-0 z-39 flex flex-col justify-center gap-2 bg-bg/90 px-8 backdrop-blur-3xl transition-opacity duration-500 ease-fluid md:hidden ${open ? 'opacity-100' : 'pointer-events-none opacity-0'}`}
        aria-hidden={!open}
        onClick={() => setOpen(false)}
      >
        {[...LINKS.map((l) => ({ ...l, to: null as string | null })),
          { href: '', label: signedIn ? 'Open workspace' : 'Sign in', to: signedIn ? '/workspace' : signIn }].map((l, i) => {
          const cls = `block text-5xl font-bold tracking-[-0.03em] no-underline transition-[opacity,transform] duration-700 ease-fluid ${open ? 'translate-y-0 opacity-100' : 'translate-y-12 opacity-0'}`
          const style = { transitionDelay: open ? `${100 + i * 70}ms` : '0ms' }
          return l.to
            ? <Link key={l.label} to={l.to} className={cls} style={style} tabIndex={open ? 0 : -1}>{l.label}</Link>
            : <a key={l.label} href={l.href} className={cls} style={style} tabIndex={open ? 0 : -1}>{l.label}</a>
        })}
      </div>
    </>
  )
}

import { Link } from 'react-router-dom'
import { Wordmark } from '../Logo'

export function Footer({ signIn }: { signIn: string }) {
  return (
    <footer className="border-t border-line px-5 py-12">
      <div className="mx-auto flex max-w-6xl flex-col items-start justify-between gap-6 sm:flex-row sm:items-center">
        <Link to="/" aria-label="Mavis AI home" className="no-underline"><Wordmark size={32} /></Link>
        <nav aria-label="Footer" className="flex flex-wrap items-center gap-x-8 gap-y-2 text-sm text-muted">
          <a href="#where" className="no-underline hover:text-ink">Connected apps</a>
          <a href="#how" className="no-underline hover:text-ink">How it works</a>
          <a href="#privacy" className="no-underline hover:text-ink">Privacy</a>
          <Link to="/privacy" className="no-underline hover:text-ink">Privacy Policy</Link>
          <Link to="/terms" className="no-underline hover:text-ink">Terms</Link>
          <Link to={signIn} className="no-underline hover:text-ink">Sign in</Link>
          <span>© 2026 Mavis AI</span>
        </nav>
      </div>
    </footer>
  )
}

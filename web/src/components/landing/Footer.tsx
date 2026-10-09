import { Weave } from '../Weave'

export function Footer() {
  return (
    <footer className="border-t border-white/10 px-5 py-12">
      <div className="mx-auto flex max-w-6xl flex-col items-start justify-between gap-6 sm:flex-row sm:items-center">
        <span className="flex items-center gap-3 text-lg font-bold tracking-tight"><Weave size={30} /> Mavis AI</span>
        <div className="flex flex-wrap items-center gap-x-8 gap-y-2 text-sm text-muted">
          <span>Privacy</span>
          <span>Terms</span>
          <span>© 2026 Mavis AI</span>
        </div>
      </div>
    </footer>
  )
}

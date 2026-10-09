import type { ReactNode } from 'react'

export function PageHead({ title, lede }: { title: string; lede: string }) {
  return (
    <div className="mb-10 lg:mb-14">
      <h1 className="text-[clamp(2rem,4vw,2.75rem)] font-bold leading-[1.05] tracking-[-0.03em]">{title}</h1>
      <p className="mt-3 max-w-[56ch] text-base text-muted">{lede}</p>
    </div>
  )
}

export function Panel({ id, title, lede, children, action }: { id?: string; title?: string; lede?: string; children: ReactNode; action?: ReactNode }) {
  return (
    <section className="panel mb-8" aria-labelledby={title ? `${id}-h` : undefined}>
      <div className="panel-core">
        {title && (
          <header className="mb-4 flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 id={`${id}-h`} className="text-xl font-bold tracking-tight">{title}</h2>
              {lede && <p className="mt-1 text-sm text-muted">{lede}</p>}
            </div>
            {action}
          </header>
        )}
        {children}
      </div>
    </section>
  )
}

export function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`skeleton ${className}`} aria-hidden="true" />
}

export function SkeletonRows({ rows = 3, label = 'Loading' }: { rows?: number; label?: string }) {
  return (
    <div role="status" aria-label={label} className="grid gap-4">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex items-center gap-4">
          <Skeleton className="size-11 shrink-0 !rounded-2xl" />
          <div className="grid flex-1 gap-2"><Skeleton className="h-4 w-1/3" /><Skeleton className="h-3 w-3/4" /></div>
        </div>
      ))}
      <span className="sr-only">{label}</span>
    </div>
  )
}

export function EmptyState({ icon, title, children }: { icon: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="grid place-items-center gap-3 rounded-3xl border border-dashed border-line-strong bg-surface/60 px-6 py-12 text-center">
      <div className="grid size-12 place-items-center rounded-2xl bg-sunken text-muted">{icon}</div>
      <p className="font-semibold">{title}</p>
      {children && <p className="max-w-[40ch] text-sm text-muted">{children}</p>}
    </div>
  )
}

export function IconTile({ children, tone = 'plain' }: { children: ReactNode; tone?: 'plain' | 'accent' }) {
  return (
    <div className={`grid size-11 shrink-0 place-items-center rounded-2xl border ${tone === 'accent' ? 'border-accent/30 bg-accent-soft text-accent-hi' : 'border-line-strong bg-sunken text-ink'}`}>
      {children}
    </div>
  )
}

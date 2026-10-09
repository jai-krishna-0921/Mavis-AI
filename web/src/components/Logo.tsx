// The Mavis mark: a soft speech bubble with an M drawn inside it. Geometry lives in a 512 box and never
// leaves it, so nothing clips on small screens. `live` adds the idle breathing and the draw-on-hover.
const BUBBLE =
  'M166 52H346A110 110 0 0 1 456 162V290A110 110 0 0 1 346 400H236C208 400 186 410 166 428C142 450 112 466 78 468C62 469 52 458 58 448C62 440 56 420 56 366V162A110 110 0 0 1 166 52Z'
const M = 'M158 292V158L256 238L354 158V292'

export function Logo({ size = 32, live = false, title, className = '' }: { size?: number; live?: boolean; title?: string; className?: string }) {
  return (
    <svg
      className={`logo block shrink-0 overflow-visible ${live ? 'logo-live' : ''} ${className}`}
      width={size} height={size} viewBox="0 0 512 512"
      role={title ? 'img' : undefined} aria-label={title} aria-hidden={title ? undefined : true}
      data-testid="logo"
    >
      <path className="logo-bubble" d={BUBBLE} fill="var(--color-accent)" />
      <path className="logo-m" d={M} pathLength={100} fill="none" stroke="#fff" strokeWidth="42" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

// Mark plus the lowercase wordmark. The wordmark is live text so it follows the font and stays selectable.
export function Wordmark({ size = 30, live = false, className = '' }: { size?: number; live?: boolean; className?: string }) {
  return (
    <span className={`logo-link inline-flex items-center gap-2 ${className}`}>
      <Logo size={size} live={live} />
      <span className="font-bold leading-none tracking-[-0.045em] text-ink" style={{ fontSize: size * 0.82 }}>mavis</span>
    </span>
  )
}

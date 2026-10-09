import { useId } from 'react'

const D = 'M325 108 A90 90 0 1 0 256 256 A90 90 0 1 1 187 404'

// The Mavis Weave mark. The ring is cut where the orange arc crosses it with a mask, so it sits on any
// background (the old mark painted a background coloured gap). `animated` runs the loop.
export function Weave({ size = 32, animated = false, title }: { size?: number; animated?: boolean; title?: string }) {
  const mask = `weave-${useId().replace(/:/g, '')}`
  return (
    <svg
      className={`block shrink-0 ${animated ? 'weave-animated' : ''}`}
      width={size} height={size} viewBox="0 0 512 512"
      role={title ? 'img' : undefined} aria-label={title} aria-hidden={title ? undefined : true}
      data-testid="weave"
    >
      <defs>
        <mask id={mask} maskUnits="userSpaceOnUse" x="-100" y="-100" width="720" height="720">
          <rect x="-100" y="-100" width="720" height="720" fill="#fff" />
          <g transform="rotate(90 256 256)"><path d={D} fill="none" stroke="#000" strokeWidth="86" /></g>
        </mask>
      </defs>
      <g transform="translate(256 256) scale(1.12) translate(-256 -256)">
        <path d={D} fill="none" stroke="var(--color-navy-hi)" strokeWidth="54" strokeLinecap="round" mask={`url(#${mask})`} />
        <g transform="rotate(90 256 256)">
          <path className="weave-orange" d={D} fill="none" stroke="var(--color-accent)" strokeWidth="54" strokeLinecap="round" pathLength={100} />
        </g>
      </g>
    </svg>
  )
}

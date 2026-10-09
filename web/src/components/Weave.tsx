import styles from './Weave.module.css'

const D = 'M325 108 A90 90 0 1 0 256 256 A90 90 0 1 1 187 404'

// The Mavis Weave mark. `animated` runs the loop animation (landing page).
export function Weave({ size = 32, animated = false, title }: { size?: number; animated?: boolean; title?: string }) {
  return (
    <svg
      className={`${styles.weave} ${animated ? styles.animated : ''}`}
      width={size} height={size} viewBox="0 0 512 512"
      role={title ? 'img' : undefined} aria-label={title} aria-hidden={title ? undefined : true}
      data-testid="weave"
    >
      <g transform="translate(256 256) scale(1.12) translate(-256 -256)">
        <path className={styles.ring} d={D} />
        <g transform="rotate(90 256 256)">
          <path className={styles.gap} d={D} />
          <path className={styles.orange} d={D} pathLength={100} />
        </g>
      </g>
    </svg>
  )
}

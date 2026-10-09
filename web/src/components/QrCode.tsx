import { useMemo } from 'react'
import QRCode from 'qrcode'

// Renders modules as SVG rects (no innerHTML).
export function QrCode({ value, size = 176, label }: { value: string; size?: number; label: string }) {
  const { n, path } = useMemo(() => {
    const qr = QRCode.create(value, { errorCorrectionLevel: 'M' })
    const n = qr.modules.size
    let path = ''
    for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) if (qr.modules.get(r, c)) path += `M${c} ${r}h1v1h-1z`
    return { n, path }
  }, [value])
  const quiet = 2
  return (
    <svg width={size} height={size} viewBox={`${-quiet} ${-quiet} ${n + quiet * 2} ${n + quiet * 2}`}
      role="img" aria-label={label} shapeRendering="crispEdges" className="rounded-2xl bg-raised">
      <path d={path} fill="#1a1714" />
    </svg>
  )
}

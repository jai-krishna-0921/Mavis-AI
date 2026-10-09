import { useEffect, useId, useRef, type ReactNode } from 'react'
import { X } from '@phosphor-icons/react'

const FOCUSABLE = 'a[href],button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex]:not([tabindex="-1"])'

export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)
  const titleId = useId()
  const closeRef = useRef(onClose)
  useEffect(() => { closeRef.current = onClose })

  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null
    const node = ref.current
    node?.querySelector<HTMLElement>(FOCUSABLE)?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.stopPropagation(); closeRef.current(); return }
      if (e.key !== 'Tab' || !node) return
      const els = [...node.querySelectorAll<HTMLElement>(FOCUSABLE)]
      if (els.length === 0) return
      const first = els[0], last = els[els.length - 1]
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus() }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
    }
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('keydown', onKey); prev?.focus() }
  }, [])

  return (
    <div
      className="overlay-in fixed inset-0 z-50 grid place-items-center bg-ink/40 p-4 backdrop-blur-sm"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div className="dialog-in panel shadow-pop max-h-[90dvh] w-full max-w-[540px] overflow-auto" role="dialog" aria-modal="true" aria-labelledby={titleId} ref={ref}>
        <div className="panel-core relative">
          <h2 id={titleId} className="pr-10 text-2xl font-bold tracking-tight">{title}</h2>
          <button type="button" className="btn btn-sm absolute right-4 top-4 !min-h-0 !size-9 !p-0" aria-label="Close dialog" onClick={onClose}>
            <X size={16} weight="light" />
          </button>
          <div className="mt-2">{children}</div>
        </div>
      </div>
    </div>
  )
}

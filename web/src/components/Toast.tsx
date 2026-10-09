import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react'
import { CheckCircle, WarningCircle } from '@phosphor-icons/react'

type Tone = 'ok' | 'error'
type ToastItem = { id: number; text: string; tone: Tone }
type Push = (text: string, tone?: Tone) => void

const ToastContext = createContext<Push>(() => {})
export const useToast = () => useContext(ToastContext)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])
  const next = useRef(1)
  const push = useCallback<Push>((text, tone = 'ok') => {
    const id = next.current++
    setItems((l) => [...l.slice(-2), { id, text, tone }])
    setTimeout(() => setItems((l) => l.filter((t) => t.id !== id)), 4200)
  }, [])
  const value = useMemo(() => push, [push])
  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        className="pointer-events-none fixed inset-x-0 bottom-5 z-60 flex flex-col items-center gap-2 px-4"
        role="status" aria-live="polite"
      >
        {items.map((t) => (
          <div key={t.id} className="toast-in pointer-events-auto flex max-w-md items-center gap-3 rounded-full border border-line-strong bg-surface py-2.5 pl-3.5 pr-5 text-sm shadow-pop">
            {t.tone === 'ok'
              ? <CheckCircle size={20} weight="light" className="shrink-0 text-ok" />
              : <WarningCircle size={20} weight="light" className="shrink-0 text-danger" />}
            <span>{t.text}</span>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

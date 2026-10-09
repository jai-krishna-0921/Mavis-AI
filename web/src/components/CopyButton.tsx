import { useState } from 'react'
import ui from '../styles/ui.module.css'

export function CopyButton({ text, label = 'Copy', what }: { text: string; label?: string; what?: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const onClick = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setState('copied')
    } catch {
      setState('failed')
    }
    setTimeout(() => setState('idle'), 2000)
  }
  return (
    <>
      <button type="button" className={ui.btn} onClick={onClick} aria-label={what ? `${label} ${what}` : undefined}>
        {state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed' : label}
      </button>
      <span className="sr-only" role="status">{state === 'copied' ? 'Copied to clipboard' : ''}</span>
    </>
  )
}

import { useState } from 'react'
import { Check, Copy } from '@phosphor-icons/react'
import { useToast } from './Toast'

export function CopyButton({ text, label = 'Copy', what }: { text: string; label?: string; what?: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const toast = useToast()
  const onClick = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setState('copied')
      toast('Copied to your clipboard')
    } catch {
      setState('failed')
      toast('Could not copy. Select the text and copy it by hand.', 'error')
    }
    setTimeout(() => setState('idle'), 2000)
  }
  return (
    <button type="button" className="btn btn-sm" onClick={onClick} aria-label={what ? `${label} ${what}` : undefined}>
      {state === 'copied' ? <Check size={16} weight="light" /> : <Copy size={16} weight="light" />}
      {state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed' : label}
    </button>
  )
}

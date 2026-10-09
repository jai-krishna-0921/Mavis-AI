import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { ArrowClockwise, ArrowUpRight, ShieldCheck, TelegramLogo, Timer } from '@phosphor-icons/react'
import { api, ApiError } from '../api/client'
import type { TelegramStart } from '../api/types'
import { QrCode } from './QrCode'
import { Logo } from './Logo'
import { Skeleton } from './Ui'

type Phase = 'starting' | 'waiting' | 'expired' | 'error'

export function TelegramSignIn({ invite, linkId, pollIntervalMs = 2000, to = '/workspace' }: { invite?: string; linkId?: string; pollIntervalMs?: number; to?: string }) {
  const [start, setStart] = useState<TelegramStart | null>(null)
  const [phase, setPhase] = useState<Phase>('starting')
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const navigate = useNavigate()
  const qc = useQueryClient()

  const begin = useCallback(() => { setPhase('starting'); setStart(null); setAttempt((a) => a + 1) }, [])

  useEffect(() => {
    let alive = true
    api.telegramStart(invite, linkId).then(
      (s) => { if (alive) { setStart(s); setPhase('waiting') } },
      (e: unknown) => { if (alive) { setError(e instanceof ApiError ? e.message : 'Could not start sign in.'); setPhase('error') } },
    )
    return () => { alive = false }
  }, [invite, linkId, attempt])

  useEffect(() => {
    if (!start || phase !== 'waiting') return
    let alive = true
    let timer: ReturnType<typeof setTimeout>
    const tick = async () => {
      try {
        const r = await api.telegramPoll(start.nonce)
        if (!alive) return
        if (r.status === 'ok') {
          await qc.invalidateQueries({ queryKey: ['me'] })
          if (alive) navigate(to, { replace: true })
          return
        }
        if (r.status === 'expired') { setPhase('expired'); return }
      } catch (e) {
        if (!alive) return
        if (e instanceof ApiError && e.status === 429) { timer = setTimeout(tick, pollIntervalMs * 3); return }
        setError(e instanceof ApiError ? e.message : 'Lost connection while waiting.')
        setPhase('error')
        return
      }
      timer = setTimeout(tick, pollIntervalMs)
    }
    timer = setTimeout(tick, pollIntervalMs)
    return () => { alive = false; clearTimeout(timer) }
  }, [start, phase, pollIntervalMs, navigate, qc, to])

  if (phase === 'error') {
    return (
      <div>
        <p className="error !mt-0" role="alert">{error}</p>
        <button type="button" className="btn mt-4" onClick={begin}><ArrowClockwise size={16} weight="bold" aria-hidden="true" /> Try again</button>
      </div>
    )
  }
  if (phase === 'expired') {
    return (
      <div className="grid gap-4">
        <p className="flex items-center gap-2 text-muted" role="alert"><Timer size={20} weight="light" aria-hidden="true" /> This link expired. Links last ten minutes.</p>
        <div><button type="button" className="btn btn-primary" onClick={begin}>Get a new link</button></div>
      </div>
    )
  }
  if (!start) {
    return (
      <div role="status" aria-label="Preparing your sign in link" className="grid gap-4">
        <Skeleton className="h-14 w-full !rounded-full" />
        <Skeleton className="h-24 w-full !rounded-3xl" />
        <p className="status">Preparing your sign in link</p>
      </div>
    )
  }

  const spaced = start.code.split('').join(' ')
  return (
    <div className="grid gap-5">
      <a className="btn btn-primary group !min-h-14 !justify-between whitespace-nowrap !px-6 !text-base" href={start.deep_link} target="_blank" rel="noopener noreferrer">
        <span className="flex items-center gap-3"><TelegramLogo size={24} weight="fill" aria-hidden="true" />Continue with Telegram</span>
        <ArrowUpRight size={20} weight="bold" aria-hidden="true" className="transition-transform duration-500 ease-fluid group-hover:translate-x-0.5 group-hover:-translate-y-0.5" />
      </a>

      <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_auto]">
        <div className="flex flex-col justify-between gap-4 rounded-3xl border border-line-strong bg-sunken p-5" aria-label={`Sign in code ${spaced}`}>
          <p className="flex items-start gap-2 text-[13px] text-muted"><ShieldCheck size={18} weight="light" className="mt-px shrink-0" aria-hidden="true" /> Check that Telegram shows the same code</p>
          <strong className="tnum block whitespace-nowrap text-5xl font-bold tracking-[0.12em] text-ink">{spaced}</strong>
        </div>
        <div className="hidden justify-items-center gap-3 rounded-3xl border border-line-strong bg-sunken p-4 sm:grid">
          <QrCode value={start.deep_link} label="QR code that opens Mavis in Telegram" size={148} />
          <p className="status max-w-[148px] text-center">On a computer? Scan with your phone.</p>
        </div>
      </div>

      <div className="flex items-start gap-3 text-sm text-muted" role="status">
        <span className="mt-0.5"><Logo size={22} live /></span>
        <p>Waiting for you to approve in Telegram. Press Start, then tap Approve. This page signs you in on its own.</p>
      </div>
    </div>
  )
}

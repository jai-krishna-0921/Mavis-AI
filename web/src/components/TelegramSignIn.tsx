import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../api/client'
import type { TelegramStart } from '../api/types'
import { QrCode } from './QrCode'
import ui from '../styles/ui.module.css'
import styles from './TelegramSignIn.module.css'

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
        <p className={ui.error} role="alert">{error}</p>
        <button type="button" className={ui.btn} onClick={begin}>Try again</button>
      </div>
    )
  }
  if (phase === 'expired') {
    return (
      <div>
        <p role="alert">This link expired. Links last ten minutes.</p>
        <button type="button" className={`${ui.btn} ${ui.btnPrimary}`} onClick={begin}>Get a new link</button>
      </div>
    )
  }
  if (!start) return <p className={ui.status} role="status">Preparing your sign in link</p>

  return (
    <div className={styles.wrap}>
      <div>
        <a className={`${ui.btn} ${ui.btnPrimary} ${styles.big}`} href={start.deep_link} target="_blank" rel="noopener noreferrer">
          Continue with Telegram
        </a>
        <p className={ui.status} role="status" style={{ marginTop: 12 }}>
          Waiting for you to approve in Telegram. Press Start, then tap Approve. This page signs you in on its own.
        </p>
        <p className={styles.code} aria-label={`Sign in code ${start.code.split('').join(' ')}`}>
          <span className={ui.status}>Check that the code in Telegram matches</span>
          <strong>{start.code.split('').join(' ')}</strong>
        </p>
      </div>
      <div className={styles.qr}>
        <QrCode value={start.deep_link} label="QR code that opens Mavis in Telegram" />
        <p className={ui.status}>On a computer? Scan with your phone.</p>
      </div>
    </div>
  )
}

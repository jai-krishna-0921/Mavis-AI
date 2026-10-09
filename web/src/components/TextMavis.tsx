import { ArrowUpRight, ArrowClockwise } from '@phosphor-icons/react'
import { useConfig } from '../api/hooks'
import { Skeleton } from './Ui'

type Props = { size?: 'sm' | 'md' | 'xl'; variant?: 'primary' | 'ghost'; label?: string; className?: string }

const SIZE = { sm: 'btn-sm', md: '!min-h-12 !px-6 !text-[15px]', xl: '!min-h-16 !px-9 !text-lg' }

// Every Telegram link on the site is built from GET /config. If it cannot be had, the link is hidden and a
// retry is offered; there is no built-in bot name to fall back on.
export function TextMavis({ size = 'md', variant = 'primary', label = 'Text Mavis', className = '' }: Props) {
  const cfg = useConfig()
  const url = cfg.data?.bot_url
  const tone = variant === 'primary' ? 'btn-primary' : ''
  if (url) {
    return (
      <a
        className={`btn ${tone} ${SIZE[size]} group !pr-2 ${className}`}
        href={url} target="_blank" rel="noopener noreferrer"
      >
        {label}
        <span className={`grid place-items-center rounded-full transition-transform duration-500 ease-fluid group-hover:translate-x-0.5 group-hover:-translate-y-px group-hover:scale-105 ${variant === 'primary' ? 'bg-bg/12' : 'bg-white/10'} ${size === 'xl' ? 'size-11' : size === 'md' ? 'size-8' : 'size-6'}`}>
          <ArrowUpRight size={size === 'xl' ? 22 : size === 'md' ? 17 : 14} weight="bold" aria-hidden="true" />
        </span>
      </a>
    )
  }
  if (cfg.isError || (cfg.isSuccess && !url)) {
    if (size === 'sm') {
      return (
        <button type="button" className={`btn btn-sm ${className}`} onClick={() => void cfg.refetch()} disabled={cfg.isFetching} aria-label="Retry loading the Telegram link">
          <ArrowClockwise size={14} weight="bold" aria-hidden="true" /> Retry
        </button>
      )
    }
    return (
      <span className={`inline-flex items-center gap-3 rounded-full border border-white/10 bg-white/[0.04] py-1.5 pl-4 pr-1.5 text-sm text-muted ${className}`} role="alert">
        The Telegram link did not load.
        <button type="button" className="btn btn-sm" onClick={() => void cfg.refetch()} disabled={cfg.isFetching}>
          <ArrowClockwise size={14} weight="bold" aria-hidden="true" /> Retry
        </button>
      </span>
    )
  }
  return <Skeleton className={`h-12 w-44 !rounded-full ${className}`} />
}

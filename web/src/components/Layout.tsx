import { useState } from 'react'
import { Link, NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { List, SignOut, SlidersHorizontal, SquaresFour, UserPlus, Vault as VaultIcon, X } from '@phosphor-icons/react'
import { useMe } from '../api/hooks'
import { api, setCsrf } from '../api/client'
import { Wordmark } from './Logo'
import { InviteModal } from './InviteModal'

const LINKS = [
  { to: '/workspace', label: 'Workspace', Icon: SquaresFour },
  { to: '/vault', label: 'Vault', Icon: VaultIcon },
  { to: '/preferences', label: 'Preferences', Icon: SlidersHorizontal },
]

const item = 'group flex w-full items-center gap-3 rounded-2xl px-3.5 py-2.5 text-left text-[15px] font-medium text-muted no-underline transition-[background-color,color] duration-500 ease-fluid hover:bg-sunken hover:text-ink'

export function Layout() {
  const me = useMe()
  const [open, setOpen] = useState(false)
  const [invite, setInvite] = useState(false)
  const navigate = useNavigate()
  const qc = useQueryClient()

  const logout = async (all: boolean) => {
    try { await api.logout(all) } finally {
      setCsrf(null)
      qc.clear()
      navigate('/login', { replace: true })
    }
  }
  const initial = (me.data?.name || me.data?.email || 'M').trim().charAt(0).toUpperCase()

  return (
    <div className="mesh-app min-h-[100dvh] lg:grid lg:grid-cols-[272px_minmax(0,1fr)]">
      <a className="skip-link" href="#main">Skip to content</a>
      <header className="sticky top-0 z-40 flex items-center justify-between border-b border-line bg-bg/85 px-5 py-3 backdrop-blur-xl lg:hidden">
        <Link to="/" aria-label="Mavis AI home" className="no-underline"><Wordmark size={30} live /></Link>
        <button type="button" className="btn btn-sm" aria-expanded={open} aria-controls="site-nav" onClick={() => setOpen((o) => !o)}>
          {open ? <X size={16} weight="bold" /> : <List size={16} weight="bold" />}
          {open ? 'Close' : 'Menu'}
        </button>
      </header>

      <nav
        id="site-nav" aria-label="Main"
        className={`${open ? 'flex' : 'hidden'} fixed inset-x-0 top-[57px] bottom-0 z-40 flex-col gap-1 overflow-auto border-b border-line bg-bg p-4 lg:sticky lg:top-0 lg:flex lg:h-[100dvh] lg:border-r lg:bg-surface lg:p-5`}
        onClick={(e) => { if ((e.target as HTMLElement).closest('a,button')) setOpen(false) }}
      >
        <Link to="/" aria-label="Mavis AI home" className="mb-6 hidden px-2 pt-2 no-underline lg:inline-flex"><Wordmark size={36} live /></Link>
        <ul className="grid gap-1">
          {LINKS.map(({ to, label, Icon }) => (
            <li key={to}>
              <NavLink to={to} className={({ isActive }) => `${item} ${isActive ? '!bg-accent-soft !text-ink' : ''}`}>
                {({ isActive }) => (
                  <>
                    <Icon size={22} weight={isActive ? 'duotone' : 'light'} className={isActive ? 'text-accent-hi' : ''} aria-hidden="true" />
                    {label}
                  </>
                )}
              </NavLink>
            </li>
          ))}
        </ul>
        <div className="my-4 h-px bg-line" />
        <ul className="grid gap-1">
          <li><button type="button" className={item} onClick={() => setInvite(true)}><UserPlus size={22} weight="light" aria-hidden="true" />Invite a friend</button></li>
        </ul>

        <div className="mt-auto grid gap-3 pt-6">
          {me.data ? (
            <div className="flex items-center gap-3 rounded-2xl border border-line bg-bg p-3">
              <div className="grid size-10 shrink-0 place-items-center rounded-xl bg-accent-soft font-bold text-accent-hi" aria-hidden="true">{initial}</div>
              <div className="min-w-0 text-sm leading-tight">
                <div className="truncate font-semibold">{me.data.name}</div>
                {me.data.email && <div className="mt-0.5 truncate text-muted">{me.data.email}</div>}
              </div>
            </div>
          ) : (
            <div className="skeleton h-16 !rounded-2xl" aria-hidden="true" />
          )}
          <button type="button" className={item} onClick={() => void logout(false)}><SignOut size={22} weight="light" aria-hidden="true" />Log out</button>
          <button type="button" className="px-3.5 text-left text-[13px] text-muted underline decoration-ink/25 underline-offset-4 transition-colors hover:text-ink hover:decoration-current" onClick={() => void logout(true)}>
            Log out everywhere
          </button>
        </div>
      </nav>

      <main id="main" className="min-w-0 px-5 py-10 sm:px-8 lg:px-14 lg:py-16" tabIndex={-1}>
        <div className="mx-auto max-w-[880px]">
          <Outlet />
        </div>
      </main>
      {invite && <InviteModal onClose={() => setInvite(false)} />}
    </div>
  )
}

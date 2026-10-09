import { useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useMe } from '../api/hooks'
import { api, setCsrf } from '../api/client'
import { Weave } from './Weave'
import { InviteModal } from './InviteModal'
import ui from '../styles/ui.module.css'
import styles from './Layout.module.css'

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

  return (
    <div className={styles.shell}>
      <a className="skip-link" href="#main">Skip to content</a>
      <header className={styles.topbar}>
        <span className={styles.brand}><Weave size={26} /> Mavis AI</span>
        <button type="button" className={ui.btn} aria-expanded={open} aria-controls="site-nav" onClick={() => setOpen((o) => !o)}>
          {open ? 'Close' : 'Menu'}
        </button>
      </header>
      <nav id="site-nav" aria-label="Main" className={`${styles.nav} ${open ? styles.navOpen : ''}`}
        onClick={(e) => { if ((e.target as HTMLElement).closest('a,button')) setOpen(false) }}>
        <span className={styles.logo}><Weave size={30} /> Mavis AI</span>
        <ul>
          <li><NavLink to="/workspace">Workspace</NavLink></li>
          <li><NavLink to="/vault">Vault</NavLink></li>
          <li><NavLink to="/preferences">Preferences</NavLink></li>
          <li><button type="button" onClick={() => setInvite(true)}>Invite a friend</button></li>
          <li><button type="button" onClick={() => void logout(false)}>Log out</button></li>
          <li><button type="button" onClick={() => void logout(true)}>Log out everywhere</button></li>
        </ul>
        {me.data && (
          <div className={styles.who}>
            <div>{me.data.name}</div>
            {me.data.email && <div>{me.data.email}</div>}
          </div>
        )}
      </nav>
      <main id="main" className={styles.main} tabIndex={-1}>
        <Outlet />
      </main>
      {invite && <InviteModal onClose={() => setInvite(false)} />}
    </div>
  )
}

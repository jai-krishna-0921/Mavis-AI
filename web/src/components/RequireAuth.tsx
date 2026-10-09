import { Navigate, useLocation } from 'react-router-dom'
import type { ReactNode } from 'react'
import { useMe } from '../api/hooks'
import { ApiError } from '../api/client'
import { Logo } from './Logo'

export function RequireAuth({ children }: { children: ReactNode }) {
  const me = useMe()
  const loc = useLocation()
  if (me.isPending) {
    return (
      <div className="grid min-h-[100dvh] place-items-center" role="status">
        <Logo size={56} live />
        <span className="sr-only">Loading</span>
      </div>
    )
  }
  if (me.error instanceof ApiError && me.error.status === 401) return <Navigate to="/login" replace state={{ from: loc.pathname }} />
  if (me.error) return <p className="error p-10" role="alert">{me.error.message}</p>
  return <>{children}</>
}

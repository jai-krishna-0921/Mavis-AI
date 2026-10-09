import { Navigate, useLocation } from 'react-router-dom'
import type { ReactNode } from 'react'
import { useMe } from '../api/hooks'
import { ApiError } from '../api/client'

export function RequireAuth({ children }: { children: ReactNode }) {
  const me = useMe()
  const loc = useLocation()
  if (me.isPending) return <p style={{ padding: 40 }} role="status">Loading</p>
  if (me.error instanceof ApiError && me.error.status === 401) return <Navigate to="/login" replace state={{ from: loc.pathname }} />
  if (me.error) return <p style={{ padding: 40 }} role="alert">{me.error.message}</p>
  return <>{children}</>
}

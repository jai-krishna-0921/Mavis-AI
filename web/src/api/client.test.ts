import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { server } from '../mocks/server'
import { api, ApiError, getCsrf, setUnauthorizedHandler } from './client'

describe('api client', () => {
  it('stores the csrf token from /me and sends it on mutating requests only', async () => {
    const seen: Record<string, string | null> = {}
    server.use(
      http.get('/api/v1/connectors', ({ request }) => { seen.get = request.headers.get('X-Mavis-CSRF'); return HttpResponse.json([]) }),
      http.post('/api/v1/auth/logout', ({ request }) => { seen.post = request.headers.get('X-Mavis-CSRF'); return new HttpResponse(null, { status: 204 }) }),
    )
    await api.me()
    expect(getCsrf()).toBe('csrf-mock-token')
    await api.connectors()
    await api.logout(true)
    expect(seen.get).toBeNull()
    expect(seen.post).toBe('csrf-mock-token')
  })

  it('rejects mutations that lack the csrf header', async () => {
    await expect(api.revokeInvite('k3m9x2')).rejects.toMatchObject({ status: 403, code: 'csrf' })
  })

  it('calls the unauthorized handler on 401 but not for silent /me', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    server.use(http.get('/api/v1/me', () => HttpResponse.json({ error: 'unauthorized', message: 'Please sign in.' }, { status: 401 })))
    await expect(api.me()).rejects.toBeInstanceOf(ApiError)
    expect(handler).not.toHaveBeenCalled()
    server.use(http.get('/api/v1/connectors', () => HttpResponse.json({ error: 'unauthorized', message: 'x' }, { status: 401 })))
    await expect(api.connectors()).rejects.toMatchObject({ status: 401 })
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('never touches document.cookie', async () => {
    const get = vi.spyOn(document, 'cookie', 'get')
    await api.me()
    expect(get).not.toHaveBeenCalled()
  })

  it('surfaces the server message', async () => {
    await api.me()
    server.use(http.post('/api/v1/invites', () => HttpResponse.json({ error: 'invite_cap', message: 'Cap reached.' }, { status: 409 })))
    await expect(api.createInvite()).rejects.toMatchObject({ code: 'invite_cap', message: 'Cap reached.' })
  })
})

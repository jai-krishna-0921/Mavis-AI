import '@testing-library/jest-dom/vitest'
import { afterAll, afterEach, beforeAll, beforeEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'
import { server } from '../mocks/server'
import { resetDb } from '../mocks/db'
import { setCsrf, setUnauthorizedHandler } from '../api/client'

beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
beforeEach(() => {
  resetDb()
  setCsrf(null)
  setUnauthorizedHandler(() => { /* tests assert on this via spies */ })
  Object.defineProperty(navigator, 'clipboard', { value: { writeText: vi.fn().mockResolvedValue(undefined) }, configurable: true })
})
afterEach(() => { cleanup(); server.resetHandlers(); vi.restoreAllMocks() })
afterAll(() => server.close())

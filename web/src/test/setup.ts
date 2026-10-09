import '@testing-library/jest-dom/vitest'
import { afterAll, afterEach, beforeAll, beforeEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'
import { server } from '../mocks/server'
import { resetDb } from '../mocks/db'
import { setCsrf, setUnauthorizedHandler } from '../api/client'

// jsdom has no matchMedia; report nothing matching so scroll animations stay off in tests.
Object.defineProperty(window, 'matchMedia', {
  configurable: true,
  value: (query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
  }),
})

beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
beforeEach(() => {
  resetDb()
  setCsrf(null)
  setUnauthorizedHandler(() => { /* tests assert on this via spies */ })
  Object.defineProperty(navigator, 'clipboard', { value: { writeText: vi.fn().mockResolvedValue(undefined) }, configurable: true })
})
afterEach(() => { cleanup(); server.resetHandlers(); vi.restoreAllMocks() })
afterAll(() => server.close())

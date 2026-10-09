import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { server } from '../mocks/server'
import { db } from '../mocks/db'
import { renderApp } from '../test/render'
import * as nav from '../lib/nav'

const loggedOut = () => server.use(
  http.get('/api/v1/me', () => HttpResponse.json({ error: 'unauthorized', message: 'Please sign in.' }, { status: 401 })),
)

describe('landing', () => {
  it('shows Text Mavis and Sign in, and the animated Weave', async () => {
    loggedOut()
    renderApp('/')
    expect(await screen.findByRole('link', { name: /Text Mavis/ })).toHaveAttribute('href', expect.stringContaining('t.me'))
    expect(screen.getByRole('link', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.getByTestId('weave')).toBeInTheDocument()
  })
})

describe('login polling', () => {
  it('shows the deep link and QR, polls, then lands on the workspace', async () => {
    let signedIn = false
    server.use(http.get('/api/v1/me', () => signedIn
      ? HttpResponse.json(db().me)
      : HttpResponse.json({ error: 'unauthorized', message: 'x' }, { status: 401 })))
    server.use(http.get('/api/v1/auth/telegram/poll', () => { signedIn = true; return HttpResponse.json({ status: 'ok' }) }))
    renderApp('/login?invite=abc')
    const btn = await screen.findByRole('link', { name: 'Continue with Telegram' })
    expect(btn.getAttribute('href')).toMatch(/^https:\/\/t\.me\/MavisAIBot\?start=login_[0-9a-f]+_abc$/)
    expect(screen.getByRole('img', { name: /QR code/ })).toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: 'Workspace' })).toBeInTheDocument()
  })

  it('offers a new link when the nonce expires', async () => {
    loggedOut()
    server.use(http.get('/api/v1/auth/telegram/poll', () => HttpResponse.json({ status: 'expired' })))
    renderApp('/login')
    expect(await screen.findByText(/link expired/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Get a new link' })).toBeInTheDocument()
  })

  it('redirects unauthenticated visitors from /workspace to /login', async () => {
    loggedOut()
    renderApp('/workspace')
    expect(await screen.findByRole('heading', { name: 'Sign in to Mavis AI' })).toBeInTheDocument()
  })
})

describe('workspace connectors', () => {
  it('shows contact rows with copy, and connects Slack via redirect', async () => {
    const redirect = vi.spyOn(nav, 'redirectTo').mockImplementation(() => {})
    const user = userEvent.setup()
    renderApp('/workspace')
    expect(await screen.findByRole('heading', { name: 'Contact' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Copy link to Telegram' }))
    expect(await screen.findByText('Copied')).toBeInTheDocument()
    const slack = (await screen.findByText('Read channels you invite Mavis to and message you there.')).closest('li')!
    await user.click(within(slack).getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(redirect).toHaveBeenCalledWith('/oauth/mock/slack'))
  })

  it('disconnects Google with the forget choice', async () => {
    const user = userEvent.setup()
    renderApp('/workspace')
    expect(await screen.findByLabelText('Google services')).toBeInTheDocument()
    const before = db().items.filter((i) => i.source === 'gmail').length
    expect(before).toBeGreaterThan(0)
    await user.click(await screen.findByRole('button', { name: 'Disconnect' }))
    const dlg = screen.getByRole('dialog', { name: 'Disconnect Google Workspace' })
    await user.click(within(dlg).getByLabelText(/keep what was learned/))
    await user.click(within(dlg).getByRole('button', { name: 'Disconnect' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(db().connectors[0].status).toBe('none')
    expect(db().items.filter((i) => i.source === 'gmail')).toHaveLength(before)
    expect(await screen.findAllByText('Not connected')).toHaveLength(2)
  })
})

describe('vault', () => {
  it('renders third party text as plain text', async () => {
    renderApp('/vault')
    expect(await screen.findByText(/<img src=x onerror=alert\(1\)> Mallory/)).toBeInTheDocument()
    expect(document.querySelector('img[src="x"]')).toBeNull()
    expect(document.querySelector('script')).toBeNull()
  })

  it('edits an item inline', async () => {
    const user = userEvent.setup()
    renderApp('/vault')
    await user.click(await screen.findByRole('button', { name: 'Edit Priya Raman' }))
    const title = screen.getByLabelText('Title')
    await user.clear(title)
    await user.type(title, 'Priya R.')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Priya R.')).toBeInTheDocument()
    expect(db().items.find((i) => i.id === 'p1')).toMatchObject({ title: 'Priya R.', source: 'dashboard', trust: 'user' })
  })

  it('forgets an item and can suppress re-learning', async () => {
    const user = userEvent.setup()
    renderApp('/vault')
    await user.click(await screen.findByRole('button', { name: 'Forget Daniel Okoye' }))
    await user.click(screen.getByLabelText('Also stop Mavis learning this again'))
    await user.click(screen.getByRole('button', { name: 'Forget it' }))
    await waitFor(() => expect(screen.queryByText('Daniel Okoye')).not.toBeInTheDocument())
    expect(db().suppressed).toEqual(['p2'])
  })

  it('searches, switches tabs and filters by source chip', async () => {
    const user = userEvent.setup()
    renderApp('/vault')
    await screen.findByText('Priya Raman')
    await user.click(screen.getByRole('button', { name: 'Slack' }))
    expect(screen.queryByText('Priya Raman')).not.toBeInTheDocument()
    expect(screen.getByText('Sam Whitlock')).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: /Organisations/ }))
    expect(await screen.findByText('Northwind Studio')).toBeInTheDocument()
    await user.type(screen.getByRole('searchbox', { name: 'Search the vault' }), 'zento')
    await waitFor(() => expect(screen.queryByText('Northwind Studio')).not.toBeInTheDocument())
    expect(await screen.findByText('Zento Labs')).toBeInTheDocument()
  })

  it('forgets everything from a source', async () => {
    const user = userEvent.setup()
    renderApp('/vault')
    await screen.findByText('Priya Raman')
    await user.click(await screen.findByText('Sources'))
    await user.click(await screen.findByRole('button', { name: 'Forget everything from Calendar' }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Forget everything' }))
    await waitFor(() => expect(db().items.some((i) => i.source === 'calendar')).toBe(false))
  })
})

describe('invites', () => {
  it('creates, copies and revokes a link', async () => {
    const user = userEvent.setup()
    renderApp('/workspace')
    await user.click(await screen.findByRole('button', { name: 'Invite a friend' }))
    const dlg = screen.getByRole('dialog', { name: 'Invite a friend' })
    expect(await within(dlg).findByText('1 of 5 uses')).toBeInTheDocument()
    await user.type(within(dlg).getByLabelText('Name (optional)'), 'Anjali')
    await user.click(within(dlg).getByRole('button', { name: 'Create link' }))
    const row = (await within(dlg).findByText('Anjali')).closest('li')!
    expect(within(row).getByText('0 of 5 uses')).toBeInTheDocument()
    await user.click(within(row).getByRole('button', { name: /Copy link/ }))
    expect(await navigator.clipboard.readText()).toContain('?invite=')
    await user.click(within(row).getByRole('button', { name: 'Revoke link Anjali' }))
    await waitFor(() => expect(within(dlg).queryByText('Anjali')).not.toBeInTheDocument())
    expect(db().invites).toHaveLength(1)
  })

  it('shows the cap error', async () => {
    db().me.invites_left = 0
    renderApp('/workspace')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Invite a friend' }))
    expect(await screen.findByRole('button', { name: 'Create link' })).toBeDisabled()
  })
})

describe('preferences', () => {
  it('saves changes', async () => {
    const user = userEvent.setup()
    renderApp('/preferences')
    const name = await screen.findByLabelText('Name')
    await user.clear(name)
    await user.type(name, 'Jai K')
    await user.click(screen.getByLabelText('both'))
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    expect(await screen.findByText('Saved')).toBeInTheDocument()
    expect(db().preferences).toMatchObject({ name: 'Jai K', proactive_channel: 'both' })
  })

  it('requires typing DELETE before deleting the account', async () => {
    const user = userEvent.setup()
    renderApp('/preferences')
    await user.click(await screen.findByRole('button', { name: 'Delete account' }))
    const dlg = screen.getByRole('dialog', { name: 'Delete your account' })
    const go = within(dlg).getByRole('button', { name: 'Delete everything' })
    expect(go).toBeDisabled()
    await user.type(within(dlg).getByLabelText('Type DELETE to confirm'), 'delete')
    expect(go).toBeDisabled()
    await user.clear(within(dlg).getByLabelText('Type DELETE to confirm'))
    await user.type(within(dlg).getByLabelText('Type DELETE to confirm'), 'DELETE')
    expect(go).toBeEnabled()
    await user.click(go)
    await waitFor(() => expect(db().deleted).toBe(true))
    expect(await screen.findByRole('link', { name: /Text Mavis/ })).toBeInTheDocument()
  })
})

describe('logout', () => {
  it('logs out everywhere and returns to login', async () => {
    const seen: unknown[] = []
    server.use(http.post('/api/v1/auth/logout', async ({ request }) => { seen.push(await request.json()); db().loggedIn = false; return new HttpResponse(null, { status: 204 }) }))
    const user = userEvent.setup()
    renderApp('/workspace')
    await user.click(await screen.findByRole('button', { name: 'Log out everywhere' }))
    expect(await screen.findByRole('heading', { name: 'Sign in to Mavis AI' })).toBeInTheDocument()
    expect(seen).toEqual([{ all: true }])
  })
})

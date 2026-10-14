// Copyright (c) 2026 Jai Krishna. All rights reserved.
// Proprietary. Use is governed by LICENSE in the repository root.
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { server } from '../mocks/server'
import { db } from '../mocks/db'
import { mcpServer } from '../mocks/handlers'
import { renderApp } from '../test/render'
import * as nav from '../lib/nav'

const B = '/api/v1'

describe('MCP section', () => {
  it('is hidden when the server says the feature is off', async () => {
    server.use(http.get(`${B}/config`, () => HttpResponse.json({
      bot_username: 'Mavis247_bot', bot_url: 'https://telegram.me/Mavis247_bot', slack_enabled: true, google_signin_enabled: true, mcp_enabled: false,
    })))
    renderApp('/workspace')
    await screen.findByRole('heading', { name: 'Connectors' })
    await waitFor(() => expect(screen.queryByRole('heading', { name: /Tools from other apps/ })).not.toBeInTheDocument())
  })

  it('is hidden when the config does not mention it at all', async () => {
    server.use(http.get(`${B}/config`, () => HttpResponse.json({
      bot_username: 'Mavis247_bot', bot_url: 'https://telegram.me/Mavis247_bot', slack_enabled: true, google_signin_enabled: true,
    })))
    renderApp('/workspace')
    await screen.findByRole('heading', { name: 'Connectors' })
    expect(screen.queryByRole('heading', { name: /Tools from other apps/ })).not.toBeInTheDocument()
  })

  it('sits under the connectors with the catalog and the custom form', async () => {
    renderApp('/workspace')
    const heading = await screen.findByRole('heading', { name: 'Tools from other apps (MCP)' })
    const connectors = screen.getByRole('heading', { name: 'Connectors' })
    expect(connectors.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    const apps = await screen.findByRole('list', { name: 'Apps you can connect' })
    expect(within(apps).getByText('Linear')).toBeInTheDocument()
    expect(within(apps).getByText('Context7')).toBeInTheDocument()
    expect(screen.getByRole('form', { name: 'Add a custom server' })).toBeInTheDocument()
    expect(await screen.findByText('No apps connected yet')).toBeInTheDocument()
  })

  it('connects an app that needs no sign-in and shows it with its tools', async () => {
    const user = userEvent.setup()
    renderApp('/workspace')
    await user.click(await screen.findByRole('button', { name: 'Connect Context7' }))
    const list = await screen.findByRole('list', { name: 'Connected tool servers' })
    expect(within(list).getByText('Context7')).toBeInTheDocument()
    expect(within(list).getByText('Ready')).toBeInTheDocument()
    expect(within(list).getByText('mcp.example.com, 2 tools')).toBeInTheDocument()
    await user.click(within(list).getByText('Show 2 tools'))
    const tools = within(list).getByRole('list', { name: 'Context7 tools' })
    expect(within(tools).getByText('reads')).toBeInTheDocument()
    expect(within(tools).getByText('asks first')).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Apps you can connect' })?.textContent).not.toContain('Context7')
  })

  it('sends an OAuth app to its sign-in page', async () => {
    const redirect = vi.spyOn(nav, 'redirectTo').mockImplementation(() => {})
    const user = userEvent.setup()
    renderApp('/workspace')
    await user.click(await screen.findByRole('button', { name: 'Connect Linear' }))
    await waitFor(() => expect(redirect).toHaveBeenCalledWith('https://auth.example.com/oauth/mock/mcp/linear'))
  })

  it.each(['javascript:alert(1)', 'http://auth.example.com/authorize', '/oauth/relative', 'not a url', 'data:text/html,hi'])(
    'does not navigate to %s and shows the generic error', async (bad) => {
      server.use(http.post(`${B}/mcp/servers`, () => HttpResponse.json({
        server: mcpServer(9, { name: 'Linear', slug: 'linear', auth: 'oauth', status: 'NEEDS_AUTH' }), authorize_url: bad,
      }, { status: 201 })))
      const redirect = vi.spyOn(nav, 'redirectTo').mockImplementation(() => {})
      const user = userEvent.setup()
      renderApp('/workspace')
      await user.click(await screen.findByRole('button', { name: 'Connect Linear' }))
      expect(await screen.findByText('That sign-in link could not be used. Please try again.')).toBeInTheDocument()
      expect(redirect).not.toHaveBeenCalled()
    })

  it('does not keep a pasted token in the query cache or on the page', async () => {
    const user = userEvent.setup()
    const { qc } = renderApp('/workspace')
    const form = await screen.findByRole('form', { name: 'Add a custom server' })
    await user.type(within(form).getByLabelText('Name'), 'My notes')
    await user.type(within(form).getByLabelText('Address (https)'), 'https://mcp.example.com/mcp')
    await user.type(within(form).getByLabelText(/Token/), 'secret-token')
    await user.click(within(form).getByRole('button', { name: 'Add server' }))
    expect(await screen.findByText('My notes')).toBeInTheDocument()
    expect(JSON.stringify(qc.getMutationCache().getAll().map((m) => m.state.variables))).not.toContain('secret-token')
  })

  it('does not keep a reconnect token in the query cache', async () => {
    db().mcpServers.push(mcpServer(1, { name: 'Notes', auth: 'bearer', status: 'NEEDS_AUTH' }))
    const user = userEvent.setup()
    const { qc } = renderApp('/workspace')
    await user.click(await screen.findByRole('button', { name: 'Reconnect Notes' }))
    const dlg = screen.getByRole('dialog', { name: 'Reconnect Notes' })
    await user.type(within(dlg).getByLabelText('Token'), 'new-token')
    await user.click(within(dlg).getByRole('button', { name: 'Reconnect' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(JSON.stringify(qc.getMutationCache().getAll().map((m) => m.state.variables))).not.toContain('new-token')
  })

  it('adds a custom server and clears the form', async () => {
    const user = userEvent.setup()
    renderApp('/workspace')
    const form = await screen.findByRole('form', { name: 'Add a custom server' })
    const add = within(form).getByRole('button', { name: 'Add server' })
    expect(add).toBeDisabled()
    await user.type(within(form).getByLabelText('Name'), 'My notes')
    await user.type(within(form).getByLabelText('Address (https)'), 'https://mcp.example.com/mcp')
    await user.type(within(form).getByLabelText(/Token/), 'secret-token')
    await user.click(add)
    expect(await screen.findByText('My notes')).toBeInTheDocument()
    expect(db().mcpServers[0].auth).toBe('bearer')
    expect(within(form).getByLabelText('Name')).toHaveValue('')
    expect(document.body.textContent).not.toContain('secret-token')
  })

  it('shows the server error from a bad address', async () => {
    const user = userEvent.setup()
    renderApp('/workspace')
    const form = await screen.findByRole('form', { name: 'Add a custom server' })
    await user.type(within(form).getByLabelText('Name'), 'Bad')
    await user.type(within(form).getByLabelText('Address (https)'), 'http://insecure.example.com/mcp')
    await user.click(within(form).getByRole('button', { name: 'Add server' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent('That address is not allowed')
  })

  it('refreshes a server and reports it', async () => {
    db().mcpServers.push(mcpServer(1, { name: 'Notes' }))
    const user = userEvent.setup()
    renderApp('/workspace')
    await user.click(await screen.findByRole('button', { name: 'Refresh Notes' }))
    expect(await screen.findByText('Notes refreshed')).toBeInTheDocument()
  })

  it('offers Reconnect only when sign-in is needed, and sends OAuth servers to the provider', async () => {
    db().mcpServers.push(
      mcpServer(1, { name: 'Fine' }),
      mcpServer(2, { name: 'Linear', slug: 'linear', auth: 'oauth', status: 'NEEDS_AUTH', error: 'auth', error_text: 'The server did not accept the sign-in. Reconnect it.' }),
    )
    const redirect = vi.spyOn(nav, 'redirectTo').mockImplementation(() => {})
    const user = userEvent.setup()
    renderApp('/workspace')
    await screen.findByText('Fine')
    expect(screen.queryByRole('button', { name: 'Reconnect Fine' })).not.toBeInTheDocument()
    expect(screen.getByTestId('mcp-error-2')).toHaveTextContent('Reconnect it.')
    expect(screen.getByText('Needs sign-in')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Reconnect Linear' }))
    await waitFor(() => expect(redirect).toHaveBeenCalledWith('https://auth.example.com/oauth/mock/mcp/linear'))
  })

  it('reconnects a token server with a new token', async () => {
    db().mcpServers.push(mcpServer(1, { name: 'Notes', auth: 'bearer', status: 'NEEDS_AUTH' }))
    const user = userEvent.setup()
    renderApp('/workspace')
    await user.click(await screen.findByRole('button', { name: 'Reconnect Notes' }))
    const dlg = screen.getByRole('dialog', { name: 'Reconnect Notes' })
    const go = within(dlg).getByRole('button', { name: 'Reconnect' })
    expect(go).toBeDisabled()
    await user.type(within(dlg).getByLabelText('Token'), 'new-token')
    await user.click(go)
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(db().mcpServers[0].status).toBe('ACTIVE')
    expect(screen.queryByRole('button', { name: 'Reconnect Notes' })).not.toBeInTheDocument()
  })

  it('removes a server after confirming', async () => {
    db().mcpServers.push(mcpServer(1, { name: 'Notes' }))
    const user = userEvent.setup()
    renderApp('/workspace')
    await user.click(await screen.findByRole('button', { name: 'Remove Notes' }))
    const dlg = screen.getByRole('dialog', { name: 'Remove Notes' })
    await user.click(within(dlg).getByRole('button', { name: 'Cancel' }))
    expect(db().mcpServers).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: 'Remove Notes' }))
    await user.click(within(screen.getByRole('dialog', { name: 'Remove Notes' })).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(db().mcpServers).toHaveLength(0))
    expect(await screen.findByText('Notes removed')).toBeInTheDocument()
  })

  it('says what happened after the sign-in redirect', async () => {
    renderApp('/workspace?mcp=connected')
    expect(await screen.findByText('Signed in. Mavis can use that app now.')).toBeInTheDocument()
  })

  it('says when a sign-in did not finish', async () => {
    renderApp('/workspace?mcp=error')
    expect(await screen.findByText('That sign-in did not finish. You can try again.')).toBeInTheDocument()
  })

  it('keeps a failing server visible with its reason', async () => {
    db().mcpServers.push(mcpServer(1, { name: 'Gone', status: 'ERROR', error: 'not_found', error_text: 'That address did not answer as an MCP server (not found).', tools: [], tool_count: 0 }))
    renderApp('/workspace')
    expect(await screen.findByText('Not working')).toBeInTheDocument()
    expect(screen.getByTestId('mcp-error-1')).toHaveTextContent('did not answer as an MCP server')
    expect(screen.getByText('mcp.example.com, 0 tools')).toBeInTheDocument()
  })

  it('never uses an em or en dash in its own words', async () => {
    db().mcpServers.push(mcpServer(1, { name: 'Notes', status: 'NEEDS_AUTH' }))
    renderApp('/workspace')
    const heading = await screen.findByRole('heading', { name: /Tools from other apps/ })
    const panel = heading.closest('section') as HTMLElement
    await within(panel).findByText('Notes')
    expect(panel.textContent).not.toMatch(/[–—]/)
  })
})

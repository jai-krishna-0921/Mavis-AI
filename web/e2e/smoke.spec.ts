import { chromium, expect, test } from '@playwright/test'

// Skips gracefully where no headless browser can be launched.
let canLaunch = true
test.beforeAll(async () => {
  try { await (await chromium.launch()).close() } catch { canLaunch = false }
})
test.beforeEach(() => { test.skip(!canLaunch, 'no headless Chromium available') })

test('login, workspace, vault edit and forget against the mocked API', async ({ page }) => {
  await page.goto('/workspace')
  await page.getByRole('button', { name: 'Log out', exact: true }).click()
  await expect(page.getByRole('link', { name: 'Continue with Telegram' })).toBeVisible()
  // The mock API approves the login after a few polls.
  await expect(page.getByRole('heading', { name: 'Workspace' })).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('heading', { name: 'Connectors' })).toBeVisible()

  await page.getByRole('link', { name: 'Vault' }).click()
  await page.getByRole('button', { name: 'Edit Priya Raman' }).click()
  await page.getByLabel('Title').fill('Priya R.')
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByText('Priya R.')).toBeVisible()

  await page.getByRole('button', { name: 'Forget Meera Iyer' }).click()
  await page.getByRole('button', { name: 'Forget it' }).click()
  await expect(page.getByText('Meera Iyer')).toHaveCount(0)
})

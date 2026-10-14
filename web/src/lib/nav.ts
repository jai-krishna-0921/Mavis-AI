// Copyright (c) 2026 Jai Krishna. All rights reserved.
// Proprietary. Use is governed by LICENSE in the repository root.
// Full page navigation (OAuth redirects). Isolated so tests can spy on it.
export const redirectTo = (url: string) => { window.location.assign(url) }

// The address to send the browser to for a provider sign-in: only a parseable https: URL, else null.
export const httpsUrl = (raw: unknown): string | null => {
  try {
    const u = new URL(String(raw))
    return u.protocol === 'https:' ? u.href : null
  } catch { return null }
}

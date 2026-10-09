// Full page navigation (OAuth redirects). Isolated so tests can spy on it.
export const redirectTo = (url: string) => { window.location.assign(url) }

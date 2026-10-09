// A hint that this browser has signed in before. Public pages only ask /me when it is set, so an
// anonymous visitor never triggers a 401. The real session is the HttpOnly cookie; this is never trusted.
const KEY = 'mavis:signed-in'
export const hasSessionHint = () => { try { return localStorage.getItem(KEY) === '1' } catch { return false } }
export const setSessionHint = (on: boolean) => {
  try { if (on) localStorage.setItem(KEY, '1'); else localStorage.removeItem(KEY) } catch { /* storage blocked */ }
}

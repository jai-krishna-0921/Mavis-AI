# Mavis AI web app

Vite, React 19, TypeScript, React Router, TanStack Query, Tailwind CSS v4, GSAP (landing scroll motion) and Phosphor icons. Satoshi is self-hosted from `public/fonts` (Fontshare free licence, see `public/fonts/Satoshi-LICENSE.txt`). Spec: `docs/superpowers/specs/2026-10-09-mavis-dashboard-personal-layer-design.md` (sections 2 to 4).

## Run

    npm ci
    npm run dev        # http://localhost:5173, API mocked in the browser with MSW
    VITE_MOCK=0 npm run dev   # no mocks: /api and /oauth are proxied to a Mavis api on localhost:8000 (DASHBOARD_ENABLED=true)

The mock starts signed in. Use "Log out", then sign in again: the login page polls and the mock approves after three polls.

## Checks

    npm test           # Vitest + Testing Library, API mocked with MSW (node)
    npm run lint       # eslint + tsc --noEmit
    npm run build      # dist/ (mocks are not bundled)
    npm run test:e2e   # Playwright smoke; skips itself when no headless Chromium launches

## Layout

- `src/api`: typed client for `/api/v1` (CSRF header from `/me` on every mutating request, 401 handler, never reads cookies) and query hooks.
- `src/mocks`: MSW handlers and fixture data shared by `npm run dev` and the tests.
- `GET /api/v1/config` (public) gives the bot username and link and the sign-in flags from the server settings (`TELEGRAM_BOT_USERNAME`). Every Telegram link is built from it; if it cannot load, the link is hidden and a retry is shown.
- `src/pages`, `src/components`, `src/styles`: UI. Vault text from third parties is rendered as plain text only (eslint forbids `dangerouslySetInnerHTML`).

## Deploy

`deploy/caddy/Dockerfile` builds this app and copies `dist` to `/srv/web` in the Caddy image. The root `Caddyfile` serves it with SPA fallback and security headers, and proxies the API paths to `api:8000`.

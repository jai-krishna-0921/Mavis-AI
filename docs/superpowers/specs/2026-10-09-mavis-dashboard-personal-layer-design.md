# Mavis dashboard and per-user personal layer (2026-10-09)

Owner direction: a web dashboard modelled on Instinct (reference screenshots in `instinct_screenshots/`, gitignored)
from which people sign in, connect accounts, see what Mavis knows, set preferences, invite friends, and jump into
Telegram (or Slack) to work with Mavis. Every user gets their own personal layer built from their own Google and
Slack data, and Mavis acts on it per user.

Owner decisions (2026-10-09): sign in with **Telegram link and Google**; v1 pages **Landing, Workspace, Vault,
Preferences + Invite**; a **separate React app**.

## 1. Pieces and order

1. **Multi-user base** (branch `multiuser`, plan 11 Tasks 1-13 built): finish Task 14 (account deletion), merge main,
   migration renumbered to `0017_multiuser_access` after `0016_outbox_route`, flags default safe (`LLM_LIMITER=local`).
   Invite codes, onboarding, isolation and metering come from here; the dashboard reuses them, never re-implements.
2. **Dashboard API** in the existing FastAPI app under `/api/v1` (section 3).
3. **Web app** `web/` (section 4), built into static files served by Caddy at `/`.
4. **Personal layer** (section 5) feeding the agent and the Vault page.

## 2. Identity and sign-in

A Mavis user is still keyed by Telegram chat (the place Mavis lives). The dashboard adds web sessions and a
verified email as a second key.

- **Telegram link (primary).** `POST /api/v1/auth/telegram/start` returns `{nonce, deep_link, expires_at}` where
  `deep_link = https://t.me/<bot>?start=login_<nonce>`; the page shows it as a button and a QR code and polls
  `GET /api/v1/auth/telegram/poll?nonce=` . When the bot receives `/start login_<nonce>` from a chat it binds the nonce
  to that user (an active user; a new chat goes through the invite gate first, the invite code can ride in the landing
  link `?invite=` and the deep link `start=login_<nonce>_<invite>` within Telegram's 64-char start limit, else the bot
  asks for it). The poll then sets the session. Nonce: 128-bit, single use, 10 minutes, bound to the browser by an
  HttpOnly pre-session cookie so a forwarded link cannot sign someone else's browser in.
- **Google sign-in.** OpenID Connect with `openid email profile` only (separate OAuth client config entry from the
  connector grant; same Google Cloud project; basic scopes, no verification, no 7-day expiry). The verified email maps
  to the Mavis user who has that email confirmed (set when they connected Google, or confirmed in Preferences). An
  unknown email gets "Link your Telegram first" with the Telegram flow (and invite) on the same page, after which the
  email is confirmed on that user. Never create a user from Google alone (Mavis lives in chat).
- **Sessions.** Server-side session rows (`web_sessions`: id hash, user_id, created, last_seen, expires 30 days, ip,
  user agent), cookie `mavis_session` HttpOnly, Secure, SameSite=Lax, path `/`. Mutating requests require header
  `X-Mavis-CSRF` matching a per-session token returned by `GET /api/v1/me`. Logout deletes the row. "Log out
  everywhere" deletes all rows for the user. Sessions die when the user is deleted or banned.

## 3. API contract (`/api/v1`, JSON, session cookie)

| Method | Path | Body / result |
|---|---|---|
| POST | `/auth/telegram/start` | `{invite?}` -> `{nonce, deep_link, expires_at}` |
| GET | `/auth/telegram/poll?nonce=` | `{status: pending|ok|expired}`; ok sets the cookie |
| GET | `/auth/google/start` | 302 to Google |
| GET | `/auth/google/callback` | sets cookie, 302 to `/workspace` or `/link-telegram` |
| POST | `/auth/logout` | `{all?: bool}` |
| GET | `/me` | `{user_id, name, timezone, currency, channels: {telegram, slack}, email, csrf, invites_left}` |
| GET | `/connectors` | `[{id: google|slack, name, description, status: active|failed|none, account, scopes_granted: [mail, calendar, drive, docs, sheets, tasks, meet, contacts | slack...], missing_scopes, connected_at}]` |
| POST | `/connectors/{id}/connect` | `{url}` (native authorize URL; the page redirects) |
| POST | `/connectors/{id}/disconnect` | `{forget: bool=true}` |
| GET | `/vault/summary` | `{profile: {...}, counts: {people, organisations, projects, facts, sources}}` |
| GET | `/vault/items?kind=person|organisation|project|fact|preference&q=&cursor=` | paginated items with `source` (you, gmail, slack, calendar), `trust`, `updated_at`, `id` |
| PATCH | `/vault/items/{id}` | correct a fact or profile field (stored with user trust and source `dashboard`) |
| DELETE | `/vault/items/{id}` | forget it (and its vectors); `{also_suppress: bool}` stops re-learning |
| GET | `/vault/sources` | per source counts and last sync; `POST /vault/sources/{source}/forget` |
| GET/PATCH | `/preferences` | name, timezone, quiet hours, proactive channel (telegram, slack, both), morning check-in time, language register opt-out |
| GET/POST | `/invites` | list own invite links, create `{name?}` -> `{code, link, uses, max_uses}` (cap from the user's tier, owner unlimited) |
| DELETE | `/invites/{code}` | revoke |
| POST | `/account/delete` | starts the existing deletion job after `{confirm: "DELETE"}`; ends sessions |

Rules: every handler scopes by the session's user id (isolation suite extended to the API); rate limits per session
and per IP on auth endpoints; errors are `{error: code, message}` with plain messages, no em or en dashes; no
endpoint returns tokens, raw mail bodies or other users' data. Third-party content in Vault items is shown as text,
escaped by React; never rendered as HTML.

## 4. Web app (`web/`)

- Vite + React + TypeScript, React Router, TanStack Query, plain CSS modules with design tokens (light and dark),
  no UI kit dependency beyond what is needed. Look and feel: Instinct's editorial calm (serif display headings,
  generous whitespace, left nav, hairline dividers), Mavis branding (Weave mark from `assets/brand/`, the live logo
  animation on the landing page). Accessible (keyboard, labels, contrast), works at phone width.
- Routes: `/` landing (one paragraph of what Mavis is, example tasks, "Text Mavis" opens Telegram, "Sign in"),
  `/login` (Telegram button + QR, Google button), `/link-telegram`, `/workspace` (Contact: Telegram and Slack with
  copy and open buttons; Connectors: Google Workspace with per-service icons and Add account / Disconnect, Slack),
  `/vault` (profile card, tabs people / organisations / projects / facts / preferences, search, source chips, edit,
  forget, per-source forget), `/preferences` (fields from the API, data controls, delete account with typed
  confirmation), invite modal (create link, copy, uses of cap), log out.
- Build: `web/Dockerfile` (node build stage) produces `dist/`; the Caddy image copies it (`deploy/caddy/Dockerfile`)
  and Caddy serves it at `/` with SPA fallback, while `/api/*`, `/oauth/*`, `/webhooks/*`, `/telegram/webhook`,
  `/connect/callback`, `/healthz` go to the api. Same origin, so cookies need no CORS. Security headers: CSP (self
  only, no inline script), frame-ancestors none, referrer-policy strict-origin.
- Tests: Vitest + Testing Library for components and the API client; Playwright smoke for login (mocked API),
  workspace, vault edit and forget.

## 5. Personal layer per user

What exists: each user's graph (user facts with user trust; third-party facts from mail and Slack tagged `tp:`),
vectors, the profile, register mirroring and brevity from the profile.

Add:
1. **Self-authored evidence.** Mail the user sent (SENT label) and their own Slack messages are the user's own words:
   learned as user-authored records (trust medium, not USER: they are not instructions to Mavis), which ground facts
   about the user: role, organisation, team, projects, recurring meetings, frequent contacts, writing style.
2. **Profile synthesis job** per user (daily and after first sync, within the LEARN budget): builds a compact
   `personal_layer` document: who they are (role, org), key people (by interaction frequency and recency, with how
   they relate), active projects and threads, routines (meeting patterns from calendar), preferences, communication
   style (register, length, language mix from their own messages), open commitments. Every line carries its sources;
   lines from third-party evidence only are marked unconfirmed. Stored per user, versioned, shown in the Vault,
   editable (edits become user-trust facts and win).
3. **Use.** The conversation agent and the initiative reasoner read the personal layer as context (bounded size,
   user-confirmed lines first, unconfirmed lines labelled); proactive suggestions are ranked by it (people and
   projects that matter to this user). No cross-user mixing anywhere: the isolation suite covers synthesis, recall and
   the API.
4. **Controls.** Forget a source, forget an item, suppress re-learning, pause learning from a connector.

## 6. Rollout

Deploy behind `DASHBOARD_ENABLED` (Caddy serves the app only when built; API returns 404 when off). Owner first, then
invites. Google sign-in needs the redirect `https://<host>/api/v1/auth/google/callback` added to the same OAuth client.

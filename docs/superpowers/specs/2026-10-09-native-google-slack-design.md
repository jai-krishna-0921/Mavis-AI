# Native Google Workspace and Slack connectors (2026-10-09)

Owner direction (2026-10-09): for now only Google Workspace and Slack. Build them in house, not through
Composio. Read recent data with guardrails and filters, build the knowledge graph from it and use it as agent
context. Send email and Slack messages when needed. Ship today. Every other branch (ledger, programs, the
generic connectors framework, multi-user, revenue intelligence) is parked until this ships.

## What already exists on main (do not rebuild)

- Provider-neutral actions: `tools/integrations/actions.py` (`mail.search|read|thread|draft|send|reply|profile`,
  `calendar.*`, `drive.*`, `docs.read`, `sheets.*`, `contacts.*`, `slack.channels|history|send`) with risk classes,
  approval cards for OUTWARD and taint rules. The model only ever sees these names.
- The port: `tools/integrations/base.py:IntegrationProvider`, factory `tools/integrations/__init__.py:get_provider`.
- Poller (`poller.py`, Gmail and Calendar, cursors), normalizers (`normalize.py`, tolerant `pick()` keys),
  attention intake and filters (`attention/intake.py`, `initiative/filters.py`), sender authentication from
  `Authentication-Results`, `wrap_untrusted` for all provider text, connect flow over Telegram (`connect_flow.py`).
- Our own OAuth code on the parked `connectors` branch (`.worktrees/conn`): `tools/integrations/direct_oauth.py`
  (signed state, sealed PKCE verifier, single-flight refresh, revoke), `connectors/crypto.py` (envelope
  encryption, AAD bound to purpose/user/connector/column, KEK rotation), `connectors/tokens.py`. Port these,
  trimmed of the stream/spec framework. Do not merge the connectors branch.

## Decisions

1. **Transport only changes.** A `NativeRouter` implements `IntegrationProvider`. Per capability it uses the native
   executor when native is configured and the user has an ACTIVE native grant, else Composio (fallback while we
   verify). `INTEGRATION_PROVIDER=native` selects the router. Composio removal is a later one-line change.
2. **Google OAuth app is External, published "In production", unverified.** the owner's company mail is on Microsoft 365, so
   an Internal Workspace app is not available. Testing status would expire Gmail refresh tokens every 7 days;
   production does not. Unverified means a one-time "Google hasn't verified this app" screen (Advanced, then
   continue) and a lifetime cap of 100 users. Verification (restricted Gmail scopes need a security assessment) is
   a later task.
   Scopes (full Workspace read and write): `openid email profile gmail.modify gmail.send gmail.compose calendar
   drive documents spreadsheets presentations forms.body.readonly forms.responses.readonly tasks contacts
   contacts.other.readonly directory.readonly meetings.space.created meetings.space.readonly`. Broader scopes
   cover the narrower ones, so those are not asked for: `gmail.modify` reads, archives, labels and trashes mail,
   `calendar` covers events and the calendar list, `drive` covers every Drive read and edit (and Slides and
   Forms reads), `contacts` covers contacts read. `contacts.other.readonly` and `directory.readonly` let
   contact search also cover people the user has emailed and the Workspace directory; a personal account or a
   grant without them simply searches saved contacts. Router entries (`ACTION_SCOPES`) list any-of groups, so a
   grant made with the earlier narrower scopes (`gmail.readonly`, `calendar.events`, `contacts.readonly`) keeps
   every action it covered; only the new actions (mail triage, the calendar list, contact edits, Slides, Forms)
   answer permission-missing with a reconnect offer and never reach Google. `drive` (full) is needed because
   commenting on, sharing, moving and appending to the user's existing files cannot work with `drive.file`
   (it only reaches files the app made). Request `access_type=offline`, `prompt=consent`, `include_granted_scopes=true`, PKCE S256.
3. **Slack app is an internal, single-workspace app** (not distributed): internal customer-built apps keep Tier 3
   limits for `conversations.history/replies`; distributed non-Marketplace apps are capped at 1 request a minute
   and 15 messages. User token (`xoxp`) so Mavis reads what the user can read and posts as the user.
   User scopes: `channels:history groups:history im:history mpim:history channels:read groups:read im:read
   mpim:read users:read users:read.email chat:write search:read`. Events API (user events
   `message.channels message.groups message.im message.mpim`) to `POST /webhooks/slack`, verified with the
   signing secret (v0 HMAC-SHA256 over `v0:{ts}:{body}`, 5 minute window, constant-time compare), event_id dedupe.
4. **Recency.** First sync reads only recent data: Gmail `newer_than:14d`, Slack 7 days (channels the user is a
   member of plus DMs, newest first, capped per channel), Calendar 7 days back to 30 ahead. Afterwards Gmail and
   Calendar poll from cursors (existing poller), Slack is event driven with a poll safety net. Windows are settings.
5. **Read with guardrails** (rules in code, tested with varied data, no phrase regex as the primary mechanism):
   - Everything read is untrusted third-party content: wrapped, never instructions, and it taints the turn.
     Outward actions after a tainted read keep the approval card.
   - Source filters, structural: Gmail excludes `SPAM TRASH CATEGORY_PROMOTIONS CATEGORY_SOCIAL CATEGORY_FORUMS`
     at query time; `List-Unsubscribe` / `Precedence: bulk` mail goes to the bulk lane (exists). Slack skips bot
     messages, `subtype` system messages (joins, topic changes), and channels or senders the user muted.
   - Secrets never reach the graph, vectors or logs: before storage a redaction pass masks credentials (OTP and
     verification codes near their keyword, passwords, API keys and tokens by shape and entropy, card numbers by
     Luhn, bank account numbers). Measured shapes, not templates.
   - Caps: body text 20 KB per message for reading, 1000 chars per snippet into the bus; attachments are listed by
     name, never downloaded or opened by sync; links are never followed by sync.
   - Mute and forget controls: `/mute <sender|domain|#channel>`; disconnect asks, forget is the default.
6. **Knowledge graph.** Each kept message becomes a LEARN job with origin `gmail:<message_id>` /
   `slack:<team>:<channel>:<ts>` and trust from the source (authenticated sender or workspace member: medium;
   otherwise low). Entities (people with email/Slack id, organisations, projects, commitments, dates) are grounded
   against the record's own text, not the chat (run-2 defect: 0 graph nodes). People resolve by identifier first
   (email address, Slack user id plus `users:read.email`), name second. Recall labels facts with their source.
7. **Sending.** `mail.send`, `mail.reply`, `slack.send` stay OUTWARD: one tap on the card, because inbound mail can
   carry injected instructions. `mail.draft` is self only (no card). Mail to the user's own address is self only.
   Gmail send builds RFC 5322 with `email.message.EmailMessage`, base64url `raw`; replies set `threadId`,
   `In-Reply-To`, `References` and a `Re:` subject.

8. **Retries, scopes and queries** (security review). A request repeats only when that cannot do it twice:
   idempotent calls (declared per call, GET by default) on a 5xx or read error; any call on a 401 or 429 or when it
   never left the machine (connect error, pool timeout). A 5xx or read error on a send, reply, draft or event
   create is terminal and reported as "may or may not have happened". Token refresh and the single-use code
   exchange are never repeated after the request was sent. Each natively handled Google action has an entry in
   `ACTION_SCOPES` (any-of groups); a grant missing one never reaches Google and answers "permission not granted,
   reconnect" unless an ACTIVE Composio account can do it. Drive `q` is rebuilt from an allowlisted grammar with
   escaped literals (anything else is a plain `fullText` search); Gmail queries must balance quotes and brackets,
   and OR or brace queries are wrapped so the spam and trash exclusions always apply. One vendor account maps to
   one Mavis user by a database constraint on `(provider, account_key)`.
9. **Residual risk: OAuth login CSRF.** The consent URL carries a signed state bound to a Mavis user. If that URL
   is forwarded to someone else who consents, their Google or Slack account is linked to the sender's Mavis user.
   Mitigations: the state expires in 10 minutes and is single use; the callback page names the Mavis account being
   linked (Telegram display name) and the outside account, and the Telegram confirmation names the outside account
   (email or Slack workspace), so either side sees a wrong link and can disconnect. Not closed: Google's own
   consent screen cannot show the Mavis name, and the linked person need not notice before the link exists. A
   stricter fix (a browser cookie or a one-time code typed back in Telegram) is a later change if the product opens
   beyond a small trusted group.

## Modules

```
src/mavis/tools/integrations/native/
  base.py          NativeProvider, TokenSource, NativeExecutor, ReauthRequired   (on main, shared contract)
  crypto.py        ported envelope encryption (from connectors/crypto.py)
  tokens.py        NativeTokenStore(TokenSource): Postgres table native_grants, sealed tokens, single-flight refresh
  oauth.py         authorize URL (signed state, PKCE for Google), code exchange, revoke; Google and Slack endpoints
  router.py        NativeRouter(IntegrationProvider): per-capability routing, merged status, connect links
  http.py          shared httpx client: timeouts, 429 Retry-After, 5xx backoff, size cap, vendor error to FailureKind
  google.py        GoogleExecutor(NativeExecutor): mail.*, calendar.*, drive.search|list_recent|read|meta|download,
                   docs.read, sheets.find|read, contacts.search|list; Workspace writes drive.create_folder|move|
                   share|upload_file, docs.create|insert_text|comment, sheets.create|append_row|update_range,
                   tasks.list|get|add|patch|delete, meet.create|transcript
  docs_markdown.py Markdown to Docs batchUpdate requests (headings, lists, bold, italic, code, links)
  gmail_mime.py    MIME walk to text (text/plain preferred, html to text), headers, attachment names
  slack.py         SlackExecutor(NativeExecutor): slack.channels|history|send (+ internal slack.users)
  slack_events.py  signature verify, url_verification, event to Event via normalize.slack_event, dedupe
  guard.py         redaction pass and source filters (shared by Gmail and Slack ingest)
api routes:        GET /oauth/{google|slack}/callback, POST /webhooks/slack
migration:         0015_native_grants (parked branches renumber after this ships)
```

Workspace writes (decision 8 retry rules apply): creates, appends, shares, uploads, comments, task adds and Meet
creates are declared non-idempotent, so a 5xx or read error ends as "may or may not have happened" and is never
repeated; a move, a cell range write, a task patch or delete is idempotent. `docs.create` makes the document, then
writes the converted Markdown in one atomic `batchUpdate`; if that fails for a definite reason the empty document is
trashed. Uploads are one multipart request, at most 5 MB, and only read from `ARTIFACTS_DIR`. `docs.append`,
`drive.upload`, `tasks.complete` and `tasks.update` are composed by `workspace_tools` from the primitives above, so
their approval and allowlist steps still run first. A cell value that starts like a formula is written RAW.

Executor results use the keys `normalize.py` already reads (`messageId threadId sender subject messageText
labelIds messageTimestamp`, Calendar's native event JSON, Slack's native message JSON), so the poller, intake and
renderers work unchanged.

Settings: `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`,
`SLACK_SIGNING_SECRET`, `NATIVE_TOKEN_KEK` (32 random bytes, base64), `SYNC_GMAIL_DAYS=14`, `SYNC_SLACK_DAYS=7`.
Redirect URIs: `{PUBLIC_BASE_URL}/oauth/google/callback`, `{PUBLIC_BASE_URL}/oauth/slack/callback`.

## Tests

Unit tests with `httpx.MockTransport` fixtures shaped like the real APIs (pagination, 401 then refresh, 429,
invalid_grant, MIME multipart and html-only mail, Slack `ok:false` errors, bad signatures, replayed events, bot
and subtype messages). Redaction tested with varied generated secrets and with look-alikes that must survive
(order numbers, dates, phone numbers in signatures). A live check script (`scripts/live_native.py`) runs against
the owner's real accounts once credentials exist: connect, first sync counts, graph nodes created, a draft, a
self-addressed send, a Slack DM to self.

## Owner setup (needed for the live check)

Google Cloud console: new project, enable Gmail, Calendar, Drive, Docs, Sheets, Tasks, Meet, People APIs; OAuth consent screen External,
publish to production; OAuth client "Web application" with the redirect URI above; give client id and secret.
Slack: api.slack.com/apps, create from manifest (we provide it), install to the workspace; give client id,
client secret and signing secret.

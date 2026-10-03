# Mavis AI: Google Workspace Integration Design

Date: 2026-10-03
Status: draft for review

## 1. Intent

Mavis connects to the whole Google Workspace (Gmail, Calendar, Drive, Docs, Sheets, Slides files, Tasks,
Contacts, Meet) through one Google consent, uses it as real tools in chat and tasks, and notices Workspace
changes proactively (files shared with the user, comments, tasks due) the way it already notices email.

Success means:
- One `/connect google` link grants every Google capability; existing Gmail/Calendar users keep working
  until they upgrade.
- "Find the deck Priya shared", "add this to my budget sheet", "make a doc from these notes", "what's on
  my to-do list" work end to end.
- Outward or destructive writes (sharing, commenting, trashing, editing shared files) always reach the
  approval flow; prompt injection from a Doc or file cannot share, trash or send.
- Shared files, comments and due tasks show up as briefs and notifications without the user asking.

Out of scope: Ads, Analytics, Photos, Maps, Classroom, admin and bulk destructive operations (empty
trash, delete drive, ACL rules, labels, Sheets SQL/charts/formatting), and native Slides editing (decks
are created by the Phase 6 sandbox as PPTX and uploaded via `drive.upload`).

## 2. Verified facts (live Composio catalog, 2026-10-03)

- `googlesuper` toolkit: Composio-managed OAuth2, 221 live tools, 48 triggers, one consent covering
  Gmail, Calendar, Drive, Docs, Sheets, Tasks, Contacts (readonly), Meet. Default scopes also include
  Ads, Analytics and Photos; accepted for now (no own OAuth app).
- The 11 Gmail/Calendar actions Mavis maps today have identical argument keys under `GOOGLESUPER_*`
  (`CREATE_EVENT`/`UPDATE_EVENT` add an optional `workingLocationProperties`). Migration is a slug prefix swap.
- No Slides actions exist in `googlesuper`; only `GOOGLESUPER_SLIDE_ADDED_TRIGGER`.
- Relevant triggers: `GOOGLESUPER_NEW_MESSAGE`, `GOOGLESUPER_GOOGLE_CALENDAR_EVENT_CHANGE_TRIGGER`,
  `GOOGLESUPER_FILE_SHARED_PERMISSIONS_ADDED`, `GOOGLESUPER_COMMENT_ADDED_TRIGGER`,
  `GOOGLESUPER_NEW_TASK_CREATED_TRIGGER`, `GOOGLESUPER_TASK_UPDATED_TRIGGER`,
  `GOOGLESUPER_EVENT_STARTING_SOON_TRIGGER`.

## 3. Connection and routing

### 3.1 Capabilities
`Capability` gains `DRIVE`, `DOCS`, `SHEETS`, `TASKS`, `CONTACTS`, `MEET` alongside `GMAIL` and
`CALENDAR`. Capability values stop doubling as Composio toolkit slugs; existing values (`gmail`,
`googlecalendar`) are kept so stored state and pending connections stay valid.

`GOOGLE_CAPABILITIES` = the eight Google capabilities. Display name for connect prompts is "Google"
(the brand already shared by Gmail and Calendar); per-capability purpose strings remain.

### 3.2 Provider routing (Composio provider only; the provider port does not change)
- A capability-to-toolkit map: every Google capability resolves to `googlesuper`; `GMAIL` and `CALENDAR`
  also accept a legacy fallback (`gmail`, `googlecalendar`).
- `status()` returns one entry per capability: ACTIVE if `googlesuper` is ACTIVE; for GMAIL/CALENDAR,
  otherwise the legacy account's state; for the other six, otherwise the googlesuper state (NONE/FAILED).
- `execute()` resolves the account for the action's capability, then the slug: the action's base slug
  suffix with prefix `GOOGLESUPER_` when routed to googlesuper, `GMAIL_`/`GOOGLECALENDAR_` when routed to
  legacy. `composio_map.py` stores the suffix plus the translate function; nothing else knows prefixes.
- `connect_link()` for any Google capability (or the alias `google`) creates a googlesuper link.
- `disconnect(google)` removes the googlesuper account; every Google capability drops at once (legacy
  accounts are untouched and can be removed with `/disconnect gmail-legacy` / `calendar-legacy`).
- `subscribe()` attaches triggers to whichever account the trigger's capability routes to.

### 3.3 Connect UX
- `/connect google` plus aliases (`gmail`, `calendar`, `drive`, `docs`, `sheets`, `tasks`, `contacts`,
  `meet`) all open the same googlesuper link. The connect menu shows one "Google Workspace" row.
- The just-in-time connect interrupt (ConnectionRequired from a tool) names "Google" and the purpose.
- After activation Mavis sends one line listing new abilities.
- Legacy-only users get one upgrade nudge, once (flag in user state), when Workspace is enabled:
  "I can now work with your Drive, Docs, Sheets and Tasks too. Tap to upgrade your Google connection."
- On googlesuper activation: triggers are re-subscribed on the googlesuper account and legacy trigger
  instances are deleted.

## 4. Action catalog

All actions follow the existing `ActionSpec` contract: typed args model, risk class, optional `risk_fn`,
preview, renderer, `untrusted_output=True`, mapped in `composio_map.py`, verified by
`scripts/verify_composio.py`.

| Mavis action | googlesuper slug suffix | Risk |
|---|---|---|
| `drive.search` (Drive query syntax: name, fullText, owner, modifiedTime) | FIND_FILE | READ |
| `drive.list_recent` (recently modified or shared with me) | LIST_FILES | READ |
| `drive.read` (export Doc/Sheet/Slides/PDF to text) | DOWNLOAD_FILE | READ |
| `drive.create_folder` | CREATE_FOLDER | WRITE_SELF |
| `drive.upload` (from a Mavis artifact; Phase 6 hook) | UPLOAD_FILE | WRITE_SELF |
| `drive.move` | MOVE_FILE | WRITE_SELF |
| `drive.share` (person + role) | ADD_FILE_SHARING_PREFERENCE | OUTWARD |
| `drive.trash` | MOVE_TO_TRASH | DESTRUCTIVE |
| `docs.read` | GET_DOCUMENT_BY_ID | READ |
| `docs.create` (markdown) | CREATE_DOCUMENT_MARKDOWN | WRITE_SELF |
| `docs.append` (markdown at end) | UPDATE_DOCUMENT_MARKDOWN or INSERT_TEXT_ACTION | WRITE_SELF, dynamic |
| `docs.comment` | CREATE_COMMENT | OUTWARD |
| `sheets.find` | SEARCH_SPREADSHEETS | READ |
| `sheets.read` (range, capped rows) | BATCH_GET | READ |
| `sheets.append_row` | SPREADSHEETS_VALUES_APPEND | WRITE_SELF, dynamic |
| `sheets.update_range` | BATCH_UPDATE | WRITE_SELF, dynamic |
| `sheets.create` | CREATE_GOOGLE_SHEET1 | WRITE_SELF |
| `tasks.list` (default list; due/overdue filter) | LIST_TASKS (+ LIST_TASK_LISTS) | READ |
| `tasks.add` (title, notes, due) | INSERT_TASK | WRITE_SELF |
| `tasks.complete` / `tasks.update` | PATCH_TASK | WRITE_SELF |
| `tasks.delete` | DELETE_TASK | DESTRUCTIVE |
| `contacts.search` (name to email/phone) | SEARCH_PEOPLE | READ |
| `meet.create` (standalone link) | CREATE_MEET | WRITE_SELF |
| `meet.transcript` | GET_TRANSCRIPTS_BY_CONFERENCE_RECORD_ID | READ |

The exact slug for `docs.append` is chosen by the verify script run in the plan; whichever is chosen,
nothing else changes.

### 4.1 Dynamic risk (`risk_fn`)
`risk_fn` runs at registry time and stays pure; it cannot make network calls. Escalation therefore uses a
pre-step: before approval classification, the tool fetches file metadata (`GET_FILE_METADATA`, cached per
turn by file id) and stores `owned_by_me` and `shared_with_others` on the args context. Rules:
- `docs.append`, `sheets.append_row`, `sheets.update_range` on a file not owned by the user, or shared with
  anyone else: OUTWARD.
- `sheets.update_range` overwriting 20 or more non-empty cells: DESTRUCTIVE.
- Metadata lookup failure: OUTWARD (fail closed).
The plan's preflight decides whether this lives as a registry hook or inside the tool `fn` before an
explicit approval request; either way the classification precedes the write.

### 4.2 Renderers
- `drive.read`, `docs.read`: text export, max 5500 chars, URLs stripped, same as `mail_render`.
- `sheets.read`: max 50 rows x 20 cols, rendered as a compact table, cells truncated to 80 chars.
- Search/list results: id, title, owner, modified, mime kind; no previews of content.
- All output wrapped as untrusted.

### 4.3 Taint and the file allowlist
Existing taint rules apply unchanged: reading any Drive/Docs/Sheets/Contacts/Meet content taints the
turn. New rule: in a tainted turn or task, every Google WRITE_SELF action in section 4 requires approval
(OUTWARD and DESTRUCTIVE already do). Added rule, mirroring the
web_extract URL allowlist: in a tainted task, a file-mutating or sharing action (`drive.share`,
`drive.move`, `drive.trash`, `docs.append`, `docs.comment`, `sheets.append_row`, `sheets.update_range`,
`tasks.delete`) may target only:
- file ids or titles named in an untainted user message of the task root, or
- files the task itself created.
Anything else is refused with a clear tool error, before approval. The allowlist lives in task state;
loss on restart fails closed.

### 4.4 Agent exposure
- Chat: all READ actions plus `tasks.add`, `tasks.complete`, `docs.create`. The selector still offers at
  most 8 tools per turn.
- `spawn` and `research` specialists: all actions.
- No new specialist.

## 5. Proactive side

### 5.1 Workspace signals (`src/mavis/attention/workspace.py`)
Inputs:
- Webhook triggers: file shared with me, comment added, task created/updated, event starting soon.
- Tasks poll every 30 minutes in the existing poller: due today, overdue (Google sends no due trigger).
- Drive `LIST_CHANGES` poll every 30 minutes with a stored page token in per-user poll state: safety net
  for missed share/comment webhooks.

Each input normalizes to `Signal(kind, actor, actor_known, object_title, object_id, due, mentions_you,
preview)`; `preview` is URL-stripped and untrusted. Signals persist in `attention_observations` with a new
`source` column (`mail` default, `drive`, `docs`, `tasks`, `calendar`) and `message_id`
`<source>:<object_id>:<event_id>`, so the existing unique constraint dedupes.

`actor_known` = actor email seen in authenticated mail history (attention_senders) or in Contacts.

### 5.2 Deterministic scoring into the existing policy
| Signal | Default | Escalation |
|---|---|---|
| File shared by known actor | brief | notify if title or preview matches an open loop or preference (Qdrant kNN / fuzzy loop match) |
| File shared by unknown actor | log | security brief line if executable or credential-lure name |
| Comment on my doc, or @mention of me | notify | none |
| Comment on someone else's doc | brief | none |
| Task due today | morning brief | none |
| Task overdue | evening wrap | one ask after 2 days overdue: keep or drop |

- A shared file that matches an open loop marks the loop done and says so.
- Feedback buttons reuse the speaker and learning modules; "not useful" demotes that actor/kind.
- The LLM is used only for the one-line summary of a comment notify (FAST call, heuristic fallback).
- Security floors and daily budgets from the attention layer apply unchanged.

### 5.3 Briefs and wraps
Through the existing brief source registry, no new schedule:
- Morning brief: "Today" block with tasks due and files shared since the last brief.
- Evening wrap: overdue tasks and unresolved comments on the user's docs.

### 5.4 First sync after Google connect
Existing Gmail/Calendar first sync and attention backfill, plus:
- Tasks: open tasks due within 7 days (feeds the first brief).
- Drive: files shared with me in the last 7 days, logged silently as baseline (no notifications).
- Contacts: seeds `actor_known`.

## 6. Data and migration
- Migration `0010_attention_source`: `attention_observations.source` String(12), default `mail`,
  server default `mail`; index on `(user_id, source)`.
- Drive change page token and Tasks poll cursor live in existing per-user poll state; no new table.
- Upgrade nudge flag lives in user state.
- No data rewrite. Legacy connections keep working via fallback.

## 7. Configuration
- `GOOGLE_WORKSPACE_ENABLED` (default true in prod after verification, false in tests unless set). When
  false: no new capabilities exposed, no googlesuper routing, behavior identical to today.
- `WORKSPACE_POLL_MINUTES` default 30.
- Both pass through compose `x-app-env`.

## 8. Error handling
- googlesuper missing, legacy present: GMAIL/CALENDAR work; other Google tools raise
  ConnectionRequired("Google", upgrade wording).
- Auth errors on googlesuper mark all Google capabilities FAILED (one reconnect prompt, not eight).
- Provider errors are wrapped untrusted (existing `render_result`).
- Drive page token invalid: reset to a fresh start token, skip the gap silently (logged).
- Poll failures back off with the existing poller policy.

## 9. Testing
- Unit: per action args-to-slug golden dicts; risk and dynamic escalation; renderers (truncate, strip,
  wrap); taint file allowlist; signal normalization and scoring table.
- Routing: GMAIL to googlesuper when both ACTIVE; to legacy when only legacy; DRIVE with legacy only
  raises ConnectionRequired; disconnect drops all Google capabilities; auth failure marks all FAILED.
- Integration (fake provider): chat "what's on my to-do list" calls `tasks.list`; tainted task where a Doc
  asks to share with an outsider is refused by the allowlist; share webhook from known contact becomes a
  brief line; due task appears in the morning brief.
- Adversarial: prompt-injection fixture Doc attempting share, trash and mail.send ends blocked or
  approval-gated in every case.
- Live: `scripts/verify_composio.py` checks every mapped slug's argument keys; a read-only smoke run
  (drive.search, docs.read, sheets.read, tasks.list, contacts.search) on the owner's account before deploy.

## 10. Rollout
1. Deploy with the flag on via `deploy/aws/deploy.sh` (detached, migration rollback).
2. Owner reconnects Google once through the nudge; verify triggers re-subscribed and legacy deleted.
3. No new services; memory impact is registry size only, fits the t4g.small.

## 11. Build order
1. Capabilities, provider routing, connect UX, flag.
2. Action catalog with renderers, risk and dynamic escalation.
3. Taint file allowlist.
4. Trigger re-subscription, workspace signals intake, polls, migration 0010.
5. Brief/wrap sources and first sync additions.
6. Verify script, live smoke, deploy.

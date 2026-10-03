# Mavis AI: Commitments Ledger and Grounding Design (Phase B)

Date: 2026-10-03
Status: draft for review
Evidence: root-cause analysis of 2026-10-03 (production incident: stale and duplicated pending items,
false "not sent" beliefs, denial of items Mavis itself had listed). Phase A (trust follows
provenance, approval closure on evidence and time, time computed in code, one output pass, no
instruction to deny) ships first and is assumed here.

## 1. Problem (systemic, not instances)

1. Nothing owns "what is pending". Pending state is spread across `loops`, attention "waiting" rows,
   approvals, tasks, wakeups and a rolling LLM summary; four views (chat recall, morning brief,
   evening wrap, initiative reasoner) disagree and none includes approvals or task outcomes.
2. Identity is free text. Loops are deduped by fuzzy title within an LLM-chosen `kind`; the real
   subject identifiers (Gmail message id, calendar start+attendees, capability, approval identity)
   never reach the record. Re-mentions create paraphrased duplicates.
3. The LLM writes state. Three LLM writers (LEARN extraction, reasoner `track`, `track_loop`) can
   create rows and set any status, kind or due date. Silence becomes DONE after 24h; undated rows and
   goals never expire.
4. Nothing closes on evidence. An executed approval, a finished task or an activated connection
   never reaches the loops that describe it. The reasoner, which has no tools and no view of
   approvals/tasks/connections, guesses ("the invite was not sent") and its guesses become state.
5. Chat answers from unlabeled, possibly stale context instead of a source of truth, and nothing
   tells it which context is live and which is a claim.

## 2. Goals and non-goals

Goals:
- One ledger owns every pending item a user could be told about.
- Every item has a subject key derived in code from the thing it is about; duplicates are
  impossible by construction (unique per user and subject among live rows).
- All status transitions are deterministic code; the LLM may only propose items or claim closure.
- Items close on evidence (events and a reconciler), never on silence.
- Chat, briefs, evening wrap and reasoner read one rendered view; "what's pending" uses a tool.
- Proactive work is single-flight per subject.

Non-goals: new integrations; changing the attention scoring model; a UI beyond Telegram.

## 3. The ledger

### 3.1 Table `commitments` (evolves `loops`)
Columns: id, user_id, subject_key (str, required), type, title (display only), due_at (nullable),
status, provenance (`user` | `third_party` | `system`), source_ref (origin id: message id, event
id, task id, approval id), evidence (JSON list of {kind, ref, at}), watch (as today), importance,
created_at, updated_at, version.
Unique index on (user_id, subject_key, type) where status is live.

Migration: existing loop rows are copied with subject_key `legacy:<id>`, provenance `third_party`
unless created from an untainted user turn, status mapped (OPEN to open, AWAITING to
awaiting_user, DONE/DROPPED/EXPIRED kept). The `loops` name and its repo API stay as a thin
compatibility layer until callers move.

### 3.2 Subject keys (computed in code)
| Subject | Key |
|---|---|
| Email | `gmail:<message_id>`; thread-level asks `gmail-thread:<thread_id>` |
| Calendar event | `cal:<start_utc_minute>|<sorted lowercased attendees>` |
| Connection | `conn:<capability>` |
| Approval / action | `action:<tool>|<identity args>` (identity fields declared per tool, as in Phase A) |
| Background task | `task:<id>` |
| Google Task / Drive file (Workspace) | `gtask:<id>`, `gfile:<id>` |
| Free chat item with no external subject | `chat:<normalised title>` (last resort; fuzzy match is a suggestion only, never across types) |

Keys are produced by a single function from structured inputs (attention observation, tool args,
event payloads), never parsed out of LLM text.

### 3.3 Types and follow-up policy
`event` (attend at a time: prep before, "how did it go" after), `deadline` (by a time: reminder
before, overdue note after), `action` (do something, undated or dated: one nudge), `waiting_on`
(someone else owes something: check-in when due), `goal` (long-running; closes on its subject,
e.g. `conn:gmail`), `watch` (monitor a sender/thread). Follow-ups are scheduled from type, replacing
the current rule that schedules "how did it go" for every dated commitment.

### 3.4 State machine (pure function `transition(row, signal, now) -> row`)
States: `open`, `due_soon`, `overdue`, `missed`, `awaiting_user`, `done`, `dropped`, `expired`.
- Time-driven (computed at read and by a sweep): open to due_soon (type-specific window), to
  overdue at due, to missed after a type-specific grace (deadline 6h, event 2h, action 3d).
- `awaiting_user`: a follow-up was delivered. No reply in 24h goes to `expired`, never `done`.
- `done` requires evidence: approval executed, task finished ok, connection active, reconciler
  check passed, the user's own words via `resolve_pending`, attention feedback "handled".
- `dropped`: user said so (tool or button).
- Undated items expire by type TTL (action from email 7d, chat action 14d, waiting_on 14d) unless
  the user engages.
- Extracted items whose due time is already past are created as `missed` (or skipped if the user
  never engaged), never as new open items.

### 3.5 Writers
- LEARN extraction: may only propose `{subject?, title, type, due?}` and, given the current open
  items, `matches_existing: id` or `user_says_done: id`. Items are extracted only from the user's own
  words; the previous assistant reply is fenced context, never a source. Third-party items come only
  from attention records (keyed to the message id), never from LLM paraphrase of an email.
- Initiative reasoner: may propose items and claimed closures; a claimed closure without evidence is
  recorded as a note, not a transition. `status`, `source`, `provenance` fields are removed from its
  schema.
- `track_loop` tool: proposes a `chat:` or subject-keyed item; same rules.
- Attention, connect flow, approvals, tasks, Workspace intake: emit closure signals with evidence.

## 4. Evidence-based closure and reconciliation

### 4.1 Event closers (deterministic, keyed by subject)
- APPROVAL_EXECUTED(tool, args): close items with the matching `action:` key and any `cal:`/`gmail:`
  key derivable from the args; Phase A already supersedes duplicate approvals.
- TASK_FINISHED(ok/failed): close or annotate `task:` items and items the task was started for.
- CONNECTION_ACTIVE(capability): close `conn:` items, cancel connect nudges.
- EMAIL handled (attention feedback, user replied on the thread, sent mail on the thread): close
  `gmail:`/`gmail-thread:` items. Attention and ledger share the subject key.
- Workspace: task completed in Google Tasks closes `gtask:`; a shared file matching a `waiting_on`
  item closes it (existing loop-match logic moves here).

### 4.2 Reconciler
Hourly, and on demand before a morning brief or a `pending` call: for live items with checkable
subjects, verify against the source (calendar event exists, connection active, Google Task status,
user reply on thread) with bounded, cached provider calls; attach evidence and transition. Runs at
background priority; never blocks chat.

### 4.3 Reasoner inputs
The reasoner prompt gains a computed "Recently done (with evidence)" section, pending approvals,
running/failed tasks and connection states, so it stops guessing. Its "you have no tools" stance
stays; unverifiable agent wakeups ("check if X happened") become reconciler checks instead.

## 5. Grounding in chat

- `pending` tool (READ): the ledger plus open approvals plus running tasks, rendered by the Phase A
  time renderer, each item with id, provenance label ("from your inbox"), and source handle
  (message id). "What's pending" and similar questions must call it.
- `resolve_pending(id, how: done|dropped)` tool (WRITE_SELF; approval when tainted): records the
  user's words as evidence.
- Context blocks are labelled by freshness and trust: "Live", "Ledger (as of HH:MM)", "Summary
  (LLM-written, as of HH:MM, may be wrong)". State-sensitive claims from non-live blocks must be
  checked with a tool before being stated as current. The assistant's own earlier messages are
  claims.
- Items carry source handles, so "read that mail" calls `mail_read(message_id)` directly.

## 6. Single-flight proactive work
- Pings dedupe on `(subject_key, ping_kind, local_day)` computed in code; before composing, the
  ledger is re-read and the ping dropped if the subject closed.
- `act` requests carry a subject key; a live or recently completed (configurable, default 6h) task
  or approval on the same subject blocks a new one unless the user explicitly asks. This replaces
  the text-similarity task dedupe from hotfix3.
- Task failure notices group per originating request.

## 7. Data and migration
- Migration `0012_commitments`: new table, backfill from loops, compatibility view or repo shim.
- Attention observations gain nothing new (they already have message ids); the subject key function
  reads them.
- Hotfix3's text-based task dedupe is removed once 6 is live; approval identity stays (Phase A).

## 8. Error handling
- Reconciler provider failures leave items unchanged and are retried next run; auth failures route
  to the existing reconnect prompt.
- A key collision (two different asks on one thread) is resolved by `type` in the unique key; a
  second ask of the same type on the same subject updates the existing row (title/due) and appends a
  note.
- Migration is reversible (loops table untouched until the compatibility layer is removed).

## 9. Testing
- Table-driven tests for every transition, including "silence never yields done" and TTLs.
- Property tests: N re-extractions of the same email yield one row; any permutation of the same
  signals yields the same final state.
- Replay test from a synthetic event log modelled on the incident (several emails, an approval
  executed, a connection activated, re-mentions): expect one row per subject, closures at the right
  moments, no false "not sent" reasoner input.
- Chat evals with a strict fake LLM: "what's pending" calls `pending`; "read that email you
  mentioned" with a paraphrased title succeeds via the source handle.
- Single-flight: a reasoner wakeup cannot start a task for a subject with a live/recent task.

## 10. Rollout
Behind `COMMITMENTS_LEDGER_ENABLED` (default off in tests, on in prod after verification). Shadow
mode first: write the ledger alongside loops and log disagreements for a day, then switch readers.
Deploy with the existing script.

## 11. Build order
1. Subject key function, ledger table, state machine (pure), migration with backfill.
2. Writers moved to proposals (LEARN, reasoner, track_loop) with provenance from code.
3. Event closers (approvals, tasks, connections, attention, Workspace).
4. Reconciler.
5. Readers switched (chat recall, `pending`/`resolve_pending` tools, brief, evening wrap, reasoner).
6. Single-flight for pings and acts; remove hotfix3 text dedupe.
7. Shadow-mode verification, then flip.

# Mavis AI: Multi-user Production Design (Track 5)

Date: 2026-10-08
Status: draft for owner review (no code changed)
Repo: main @ 59715fe. Alembic head `0013_task_outcomes` (the Phase B ledger branch also needs a number
after it; this track takes the next free number when it lands, see section 18).
Evidence: read-only audit `scratchpad/research-multiuser.md` (pointers re-verified below), Ollama pricing
page and FAQ (fetched 2026-10-08), sandbox and connector research for T3/T4.

Owner decisions already made:
- Access is by **invite codes**. The owner hands them out. Strangers get a polite "invite only" reply.
- Server is now **t4g.medium** (2 vCPU arm64, 4 GB). Target 10 to 30 users, with a written path to bigger.
- LLM is a **paid Ollama Cloud** subscription. Chat `deepseek-v4.1-flash`, background `glm-5.3`. Tested
  at 6 or more concurrent requests. The secondary OpenAI-compatible provider hook stays as overflow.

## 1. Summary

The data model is already multi-tenant (every row, vector and graph node carries `user_id`). What is
single-user is the **front door, capacity sharing and operations**. This track adds:

1. An access gate in the worker with invite codes, user status (pending, active, banned, deleting) and
   private-chat-only intake.
2. A short onboarding that confirms timezone and currency in one tap and gets the user a first useful
   result inside two minutes.
3. Per-user fair scheduling in the worker (mailboxes plus a ready queue), so one user's burst cannot
   block anyone else.
4. A Redis-shared LLM limiter with priority, per-user fairness, shared backoff, and overflow to the
   secondary provider.
5. Per-user cost accounting, daily budgets, inbound rate limits and bans.
6. `/delete_me` with a cascade across Postgres, Qdrant, Neo4j, Redis, artifacts, Composio and
   (later) sandbox workspaces.
7. Owner-only admin commands, a metrics endpoint, Langfuse tracing, minimal alerting.
8. Off-box backups to S3 (needs owner OK for the AWS resources), Telegram send pacing, a load test that
   simulates N users through the live-test sink, and a staged rollout.

## 2. Verified current state (pointers checked at 59715fe)

| # | Finding | Pointer | Verified |
|---|---|---|---|
| A1 | Hard allowlist; strangers dropped silently, no reply | `channels/telegram_updates.py:38-51`, `:117-120`; `api/app.py:31-32`; `docker-compose.prod.yml:28` | yes |
| A2 | Any chat (groups, channels) becomes a `User`; no `chat.type` check | `telegram_updates.py:88-123`; `store/repo/users.py:15-29` | yes |
| A3 | Only a per-IP webhook limit (120/min); all Telegram traffic shares Telegram's IPs | `api/ratelimit.py:11-12`; applied `api/app.py:60,62` | yes |
| A4 | Every user gets `Asia/Kolkata`; no tool or flow sets timezone; currency global `INR` | `store/repo/users.py:20`; `config.py:18`, `:138-140` | yes |
| B1 | Composio identity `mavis-<id>`, no env prefix; inverse is a `fullmatch` regex | `domain/integrations.py:17`, `:41`, `:52-55` | yes (the audit cited `composio.py:8`, which is only the docstring) |
| C1 | Qdrant `user_id` payload index created without `is_tenant` | `memory/vector.py:91` | yes |
| C2 | Neo4j neighborhood path does not constrain node `user_id` | `memory/neo4j_graph.py:111-116` | yes |
| D1 | No account deletion; FKs to `users.id` have no `ondelete`; 6 tables carry `user_id` without an FK (`graph_nodes`, `graph_edges`, `profile_cards`, `conversation_summaries`, `audit_log`, `processed_events`) | `store/models.py:46-409` | yes |
| E1 | LLM limiter and 429/timeout state are per process and per event loop | `llm/models.py:178-279` (`_Limiter`, `_limiters`), `:282-324` (`_OllamaState`) | yes; note `llm_max_concurrency` default is now 3 (`config.py:30`) and prod sets it via `.env`; compose default is still 1 (`docker-compose.prod.yml:18`) |
| E1b | Only the worker process calls the LLM today (timer runs with `handlers=False`; api has no handlers) | `cli.py:127-142` | yes, so the per-process limiter is correct today but breaks with a second worker, a local dev run on the same key, or the live E2E harness |
| F1 | Head-of-line blocking: each consumer loop waits on the user lock (600 s); 4 loops can all wait on one user | `worker/runner.py:89-121`, `worker/locks.py:35-59`; `config.py:67` | yes |
| F2 | Single events stream, MAXLEN 100k; Redis `--maxmemory 64mb noeviction`, AOF everysec | `bus/redis_streams.py:19,46,55`; `docker-compose.prod.yml:143-146` | yes |
| F5 | DB pool 10+10 per process vs `max_connections=60` | `store/db.py:67`; `docker-compose.prod.yml:130` | yes |
| G1 | Outbox sender handles 429 per row; no global or per-chat pacing; runs inside the worker process | `channels/outbox_sender.py:43-66`; `cli.py:127-133` | yes (the audit said "both api-less roles"; it is only the worker) |
| H1 | `admin_user`/`admin_password` unused; no admin surface | `config.py:154-155` | yes |
| H2 | Langfuse is a no-op unless keys are set (they are empty in prod) | `llm/tracing.py:17-21` | yes |
| H3 | Nightly local `pg_dump` only, same EBS volume; Qdrant and Neo4j not backed up | `deploy/aws/bootstrap.sh:65-83` | yes |
| H7 | Live-test sink admits exactly one synthetic chat id | `channels/test_sink.py:35-50`; `config.py:51-52` | yes; load testing needs a range (section 15) |

## 3. Capacity plan for t4g.medium

### 3.1 Memory budget (4 GB host, about 3.7 GB usable)

| Service | Today `mem_limit` | Track 5 | Why |
|---|---|---|---|
| worker | 640m | **900m** | more executor loops, Redis limiter, T4 Playwright driver (~100 MB) |
| neo4j | 640m | 640m | unchanged (256m heap) |
| api | 224m | 256m | `/metrics`, admin reads |
| timer | 160m | 160m | |
| postgres | 192m | **256m** | more sessions, `shared_buffers` 64 MB |
| qdrant | 192m | **256m** | more points (30 users) |
| redis | 96m | **192m**, `--maxmemory 128mb` | mailboxes, limiter keys, buckets |
| caddy | 48m | 48m | |
| migrate | 256m | 256m (transient) | |
| **steady sum** | ~2.2 GB | **~2.7 GB** | leaves ~1 GB for the OS, page cache and spikes; swap stays as a safety net |

One worker process, not two. Each worker process loads the ONNX embedder (~400 MB), so a second replica
does not fit. Fairness comes from scheduling inside one process (section 7), and the shared limiter
(section 8) makes a second replica safe when the box grows.

### 3.2 Throughput

Per chat turn: about 3 LLM calls of 3 to 10 s on the fast model. With 8 global slots (section 8.2) the
system serves roughly 30 to 50 chat turns per minute if nothing else runs, and about 20 per minute with
background load. 30 invited users rarely have more than 5 in a turn at once. The LLM is the limit, not
CPU (load average 0.03 idle).

### 3.3 Growth path

| Stage | Trigger | Change |
|---|---|---|
| Now | 10-30 users | t4g.medium, one worker, everything in compose |
| 30-60 users | worker RSS > 800 MB, p95 chat wait > 10 s, or swap in use daily | t4g.large (8 GB), second worker replica (the shared limiter and mailboxes already support it), Neo4j heap 512m |
| 60-200 users | Postgres or backup pain | t4g.xlarge plus RDS db.t4g.small (managed PITR); embedder moved to a small shared service so replicas do not each load it |
| 200+ or HA | | see audit infra table (ECS on EC2, managed Qdrant/Neo4j) |

Every stage change is "resize and set an env var", not a redesign, because sections 7 and 8 are
process-count agnostic.

## 4. Access: invite codes and user status

### 4.1 Data

`users` gains columns:
- `status`: `pending` | `active` | `banned` | `deleting` | `deleted`. Default `pending`.
- `tier`: `owner` | `standard` | `trusted`. Drives budgets (section 9).
- `telegram_user_id` (from `from.id`; equals the chat id in a private chat, kept separately so identity
  never depends on the delivery address).
- `locale`, `currency` (ISO 4217), `country` (ISO 3166, optional).
- `composio_user_id` (section 6.2).
- `invite_id` (FK to `invite_codes`), `activated_at`, `banned_at`, `ban_reason`.
- `budget_override_usd_day` (nullable).

New table `invite_codes`:
`id, code_hash (sha256, unique), code_hint (last 4 chars), label, tier, max_uses, uses, expires_at,
revoked_at, created_by_user_id, default_timezone (nullable), default_currency (nullable), created_at`.

New table `invite_redemptions`: `id, invite_id, user_id, redeemed_at`.

Code format: 10 Crockford base32 characters (50 bits), displayed as `MAV-XXXXX-XXXXX`, accepted with or
without dashes, case-insensitive. Deep link `https://t.me/Mavis247_bot?start=MAVXXXXXXXXXX` (Telegram
start parameters allow `[A-Za-z0-9_-]`, so the deep-link form has no dashes). Only the hash is stored;
the plaintext is shown once to the owner.

### 4.2 Gate (worker, not api)

- **api** (`ingest_update`): admit only `chat.type == "private"` updates (plus the synthetic test chats).
  Groups and channels are dropped with a log line; if the bot was added to a group, the worker leaves
  it (`leaveChat`) once. The allowlist check is replaced by "private chat" plus the per-chat inbound
  bucket (section 9.1). `get_or_create_by_chat` creates the row as `pending` and stores **no message
  content** for pending users.
- **worker**: a new first handler `access.gate(event)` runs before routing:
  - `active`: continue.
  - `pending`: if the text is `/start <code>` or looks like a code, try to redeem it. Otherwise send the
    invite-only reply at most once per 24 h per chat:
    "Hi! Mavis is invite-only for now. If someone gave you an invite code, send it here."
  - `banned`: one reply ever ("This account is paused.") then silence.
  - `deleting` / `deleted`: silence (a `deleted` row whose chat comes back is treated as `pending`, so
    a person can rejoin with a new code).
- Redemption is one transaction: `SELECT ... FOR UPDATE` on the invite, check not revoked, not expired,
  `uses < max_uses`; increment; set user `active`, `tier`, `invite_id`, defaults; insert redemption;
  audit row. Then the onboarding starts (section 5).
- Brute force: 5 failed code attempts per chat per hour, then "Too many tries, try again in an hour".
  More than 50 failures per hour across all chats alerts the owner.
- Pending rows with no redemption after 14 days are deleted by the nightly maintenance job.

### 4.3 Migration from the allowlist

- `ALLOWED_TELEGRAM_CHAT_IDS` becomes `OWNER_TELEGRAM_CHAT_IDS` (the old name is read as an alias for one
  release). The prod start guard now requires at least one owner id.
- Migration sets every existing user whose chat is in the list to `active`, `tier=owner` for the owner,
  and keeps their current `mavis-<id>` Composio id (section 6.2). The live test user stays outside the
  gate exactly as today (`test_sink.active_test_chat`).

### 4.4 Owner invite commands

| Command | Effect |
|---|---|
| `/invite` or `/invite new [uses=1] [days=14] [tier=standard] [tz=Area/City] [cur=INR] [label...]` | mints a code, replies with the code and deep link |
| `/invite list` | active codes: hint, label, uses/max, expiry |
| `/invite revoke <hint or id>` | sets `revoked_at`; existing users keep access |
| `/invite users <id>` | who redeemed it |

Limits: at most 20 unexpired codes at once and `max_uses <= 25` per code (config), so a leaked code has a
bounded blast radius. A revoked or exhausted code gets the same reply as an unknown code (no oracle).

## 5. Onboarding a new invited user

Goal: a useful result within two minutes and no more than three questions. Everything is buttons where
possible. All copy follows the persona rules (no em dashes, product name Mavis AI).

1. **Welcome** (right after redemption):
   "Hi Priya, I'm Mavis, your personal assistant on Telegram. I remember things, remind you, and can
   watch your email and calendar if you want."
2. **Clock check, one tap.** Use the invite's `default_timezone`, else the owner's timezone, and show the
   local time: "Quick check: is it 3:42 PM Thursday where you are?" Buttons `[Yes]` `[No]`.
   - `No`: "Tell me your city, or share your location." with a reply keyboard containing a
     `request_location` button. A location resolves with an offline lookup (`tzfpy`, small arm64 wheel;
     verify) to an IANA zone. A city name resolves through a bundled GeoNames `cities15000` table
     (~2 MB); if ambiguous, show up to 3 buttons. As a last resort, the fast model maps text to an IANA
     name and the result must pass `zoneinfo.ZoneInfo(name)`.
   - Telegram's `language_code` only sets `locale` (for number and date formatting); it is never used to
     guess a timezone.
3. **Currency, implicit.** Derive from the zone's country (`zone1970.tab` country plus a small
   country-to-currency map) or the invite default, and state it rather than ask:
   "I'll show money in ₹ INR. Say 'use USD' any time to change it."
4. **Name.** Use Telegram `first_name`. Do not ask. ("Call me Pri" in chat updates it.)
5. **First value.** "What's one thing you want off your mind this week? A bill, a call, a deadline."
   The answer runs through the normal chat turn, which already creates a commitment or wakeup and
   confirms it. This is the first value.
6. **Connector suggestion, after first value**: "Want me to keep an eye on your Gmail and calendar too?"
   `[Connect Google]` `[Later]`. `[Connect Google]` runs the existing connect flow. `[Later]` records
   the decline; the suggestion is offered again at most once, after 3 days of activity (T3 owns the
   suggestion engine; this track only adds the onboarding slot).
7. **Routine seed**: morning check-in at the default 08:30 local, learned afterwards as today
   (`initiative/routines.py`). The existing `USER_QUIET` onboarding nudge (`initiative/quiet.py:32-68`)
   is unchanged.

State lives in `users.state["onboarding"] = {step, started_at, tz_confirmed, ...}` so a restart resumes.
A new tool `set_preferences(timezone?, currency?, name?, locale?)` is added to the chat tool set, and a
`/settings` command shows and changes the same fields with buttons.

**Timezone change side effects** (new `users.set_timezone`): re-anchor routine wakeups (morning check-in,
evening wrap) to the new local times; future one-off wakeups stay at their absolute UTC instant (a
"3 pm" reminder set before a move still fires at the instant it was promised, and the confirmation says
so); quiet hours and the per-local-day ping slot use the new zone from the next local midnight.

**Currency per user**: `attention_currency` becomes the fallback default only; `attention/pipeline.py`
reads `user.currency`. `attention_large_amounts` already keys by currency; add JPY, SGD, AED defaults.

## 6. Per-user isolation review

### 6.1 Stores

| Store | Today | Change |
|---|---|---|
| Postgres | every repo filters by `user_id` (app-level) | add a **two-user isolation test suite**: seed users A and B, run every repo read, chat tool and brief source as B, and assert no A ids appear. A meta-test asserts every table with a `user_id` column is listed in the deletion cascade (section 10). RLS is not adopted now (session variable per connection adds risk for little gain at this scale). |
| Qdrant | one collection, `user_id` filter on every call | recreate the payload index as a tenant index (`is_tenant=True`; Qdrant documents this for keyword and uuid fields, so verify integer support on our version, else add a `tenant` keyword payload). Keep the filter. |
| Neo4j | every query matches `{user_id:$u}` | add `AND all(n IN nodes(p) WHERE n.user_id = $u)` to the neighborhood query (defense in depth); index on `:Entity(user_id)` if missing |
| Redis | keys include user id where per user | standardize names as `mavis:<area>:u<uid>:...` so deletion can scan by user (section 10) |
| Artifacts | `data/artifacts` | path is `artifacts/u<uid>/...`; a path guard rejects reads outside the user's prefix |
| Workspace guard | `_created` in process memory (`tools/integrations/workspace_guard.py:40-72`) | move to Redis keyed by task id (needed before a second worker) |
| Langfuse | off | `user_id` sent as `sha256(env:uid)[:16]`, never the chat id or name |

### 6.2 Composio identity with environment prefix

- New users get `composio_user_id = f"mavis-{env}-{uid}"` (for example `mavis-prod-7`), stored in the
  `users` row at activation. Every call site uses the stored value (`UserRef.provider_id` reads it).
- Existing users keep `mavis-<id>`, so the owner's connected Gmail does not need reconnecting.
- `user_from_provider_id` becomes a lookup on `users.composio_user_id` (cached), and also accepts the old
  regex only for env `prod`, so a staging stack sharing the key can never claim prod user 1's webhooks.
- Staging and dev must use `mavis-dev-*` / `mavis-staging-*`. A startup check refuses to run a non-prod
  env against a Composio key whose project lists `mavis-<int>` accounts unless `COMPOSIO_SHARED_KEY_OK`.

### 6.3 Pollers and Composio quota

Gmail and Calendar poll every 2 minutes per user (`tools/integrations/poller.py:32`). At 30 users that is
about 21.6k polls/day per capability. Make the interval adaptive: 2 min while the user has messaged in the
last 2 h or has an event within 2 h, 10 min otherwise, 30 min in the user's quiet hours. That cuts calls
by about 70%. Prefer Composio triggers where available (T3 owns the trigger work).

## 7. Worker: per-user ordering and fairness

### 7.1 Design: mailboxes plus a ready queue

Replace "consumer loop takes an event, then blocks on the user lock" with two stages.

**Stage 1, intake (fast).** The existing consumer loops read the events stream and run one Lua script per
event:
- `RPUSH mavis:mbox:u<uid>:<lane> <event json>`
- if no lease is held for `(uid, lane)` and the user is not already ready, `ZADD mavis:ready:<lane>
  <arrival_ms> u<uid>`
- then `XACK`. The mailbox is durable (Redis AOF everysec, the same guarantee the stream has today).

Lanes: `chat` (USER_MESSAGE, BUTTON_PRESSED, connection replies) and `bg` (everything else: email,
calendar, wakeups, task events). This maps exactly to today's two locks (`worker/runner.py:89-94`).

**Stage 2, executors.** `chat_executors` (default 6) and `bg_executors` (default 3) loops per process.
Each loop:
1. Lua `claim_next(lane)`: `ZPOPMIN` the ready set, set lease `mavis:lease:u<uid>:<lane>` with
   `SET NX PX 120000` (renewed every 30 s by a heartbeat while the turn runs). If the lease is taken
   (another process), put the user back and try the next.
2. Read the batch: the first event, plus for the chat lane any immediately following `USER_MESSAGE`s that
   arrived within `coalesce_window_s` (3 s) of each other (see 7.2). Buttons are never coalesced.
3. Run handlers exactly as `handle_event` does today (inline retries, `processed_events` dedupe).
4. On success `LTRIM` the consumed entries. On a crash the lease expires and a reaper (timer role, every
   15 s) re-adds users whose mailbox is non-empty and lease is gone.
5. Release the lease; if the mailbox is non-empty, `ZADD` the user back with score = now. A busy user
   therefore goes to the back of the queue after every turn: **round-robin across users**.

Properties:
- Per-user order: one lease per user and lane, FIFO list. Holds across processes (fixes the non-FIFO
  Redis lock reorder too).
- One user's burst occupies at most one chat executor and one bg executor. Other users are never behind
  it.
- The acknowledgement reaction (`_acknowledge`) moves to stage 1, so the user sees the eyes reaction
  even when their turn is queued.
- Jobs stream (background tasks) keeps its consumer but gets the same per-user cap through
  `task_max_concurrency` (already 1 per user, `agents/orchestrator.py:89`) plus a global
  `task_global_concurrency` (default 3) so 30 users starting research tasks do not take every LLM slot.
- In-process mode (dev, tests) uses the same scheduler with an in-memory backend.

### 7.2 Burst coalescing

Telegram users often send three short messages in a row. While a user's turn runs, later messages wait in
the mailbox. When the next turn starts, consecutive text messages are joined into one turn (each message
is still stored as its own `messages` row with its own `event_id`; the turn input lists them in order).
A message that arrives while a turn is running does not interrupt it (unchanged behavior); its reply
comes next. Cap: at most 5 messages or 2,000 characters per coalesced turn.

### 7.3 Fan-out jitter

Per-user daily jobs (morning check-in at 08:30, retention at 03:30, evening wrap at 20:30) get a
deterministic per-user offset of 0 to 9 minutes (`hash(uid) % 600 s`), so 30 users do not hit the LLM in
the same minute. Routines already anchor on local time, so users across timezones spread naturally.

### 7.4 Pools and Redis sizing

- DB pool explicit per role: worker 8+4, api 4+2, timer 2+2 (total max 22 of 60).
- Events MAXLEN 20,000 (entries are acked within seconds now), jobs MAXLEN 20,000.
- Mailbox cap: 200 entries per user per lane; beyond that the oldest bg entries are dropped with a log
  (chat entries are rate limited earlier, section 9.1).

## 8. Shared LLM limiter

### 8.1 What the provider actually allows (Ollama pricing page and FAQ, 2026-10-08)

| Plan | Price | Concurrent requests | Included usage |
|---|---|---|---|
| Free | $0 | 1 | starter allowance |
| Pro | $20/mo | 3 | $60 credits/mo |
| Max | $100/mo | 10 | $300 credits/mo |
| Team | $500/mo (early access) | 10 | $1,000 credits/mo, shared |

- Usage is billed per token since 2026-08-31: `deepseek-v4.1-flash` $0.30 in / $1.20 out per M tokens,
  `glm-5.3` $1.40 in / $4.40 out per M tokens. Included credits are used first, then purchased credits.
- "Requests beyond your plan's concurrency limit are queued and processed as soon as a slot is
  available. Queued requests are held up to a fixed limit; if the queue is full, the request will be
  rejected." So the owner's "6 concurrent OK" test is consistent with Max (10 slots) **or** with Pro
  plus server-side queueing (latency, not errors). The plan must be confirmed (open question 1).
- Third-party summaries also mention 5-hour session and weekly caps; the official page now describes
  credits instead. Treat 429s as account-wide, as today.

Design consequence: **our own limiter must stay below the plan concurrency**, because the provider's
hidden queue adds latency we cannot prioritize, and its overflow returns 429s that hit chat and
background alike.

### 8.2 Capacity settings

- `LLM_GLOBAL_SLOTS`: Max plan 8, Pro plan 3 (leave 2 of 10 on Max for the owner's local dev and the live
  harness, which share the key).
- `LLM_BG_MAX_SLOTS`: background plus best_effort together at most `slots - 2` (Max: 6; Pro: 1), so chat
  always has at least 2 slots.
- `LLM_BEST_EFFORT_MAX_SLOTS`: Max 2, Pro 0 (best_effort only uses spare slots, keeping today's rule from
  59715fe).
- `LLM_USER_MAX_SLOTS`: 2 per user while anyone else is waiting (a user's chat turn plus their task),
  unlimited when the system is idle.

### 8.3 Mechanism (Redis, Lua)

Keys (prefix `mavis:llm:<provider>:`):
- `holders`: ZSET lease_id -> expiry_ms. Lease TTL = call timeout + `llm_timeout_cooldown_s` (a timed-out
  request still holds a provider slot server-side, matching today's cooldown).
- `holder_meta:<lease>`: HASH {uid, rank} (same TTL).
- `queue`: ZSET waiter_id -> score; `waiter_meta:<id>` HASH {uid, rank, since_ms}, TTL 15 min.
- `backoff_until`, `backoff_level`, `cooldown_until`: shared 429 and timeout state (replaces the
  per-process `_OllamaState`).
- `last_interactive_ms`: for the best_effort grace rule.

`acquire(rank, uid, deadline)`:
1. Lua `try_acquire`: drop expired holders and dead waiters; compute each queued waiter's effective
   priority: rank (interactive 0, background 1, best_effort 2), background aged to 0 after 30 s (as
   today), then **fewest slots held by that user**, then oldest `since`. Grant if this waiter is the
   best eligible one, the global slot count, the lane cap (8.2) and the user cap allow it, and the
   provider is not in backoff. best_effort is refused immediately (fail fast, as today) when it would take
   the last free slot while interactive work is queued or ran within 20 s.
2. If not granted, wait on `BLPOP mavis:llm:<provider>:wake:<waiter>` with a timeout of 250 ms plus jitter,
   then retry (release pushes a token to the best waiter's wake list; the poll covers lost wakeups).
3. Deadlines are unchanged (interactive 45 s chain-wide, background 120 s; `llm/models.py:162-166`).

`release(lease)`: remove holder, wake the next best waiter. A process that dies leaks a slot for at most
one TTL (about 105 s for SMART).

Fallback: if Redis is unreachable, fall back to the current in-process `_Limiter` with capacity
`max(1, slots // expected_processes)` and log `llm.limiter_local_fallback` (alerts, section 12).

Token attribution: a `contextvar` `llm_user_id` is set by the executor (and job runner) around each
handler, so the limiter and the usage meter know the user without changing call signatures.

### 8.4 Overflow to the secondary provider

The `llm_secondary_*` settings (`config.py:39-43`) stay. Routing rule, decided at acquire time:
- interactive: if the primary is in backoff, or the estimated wait (queue position x recent mean hold
  time) exceeds 8 s, and the secondary has a free slot, use the secondary's fast model.
- background: only when the primary is in backoff for more than 60 s.
- best_effort: never overflows.
The secondary has its own Redis semaphore (`mavis:llm:secondary:*`, `llm_secondary_max_concurrency`)
and its own price table. Off while unset, as today. Every overflow call is counted and shown in `/stats`.

## 9. Per-user limits, budgets and abuse controls

### 9.1 Inbound rate

- The webhook route is exempt from the per-IP limiter (it is authenticated by the secret header); the
  integrations webhook keeps it.
- Per chat token bucket in Redis at intake (api): 20 messages/min, burst 10. Over the limit the update is
  dropped, and at most once per minute the worker sends "You're sending a lot at once, give me a second
  to catch up." Pending users have a tighter bucket (5/min).
- Daily cap of 300 chat turns per user (standard tier). Files: 20 MB per file (Bot API limit), 200 MB
  artifacts per user.

### 9.2 Cost accounting

New table `llm_usage` (user_id, day (user-local date), provider, model, purpose
(chat|task|attention|memory|initiative|other), calls, prompt_tokens, completion_tokens, cost_micros),
unique on (user_id, day, provider, model, purpose), upserted after each call from the response `usage`.
A Redis counter `mavis:spend:u<uid>:<yyyymmdd>` (cost micros, 48 h TTL) gives an O(1) budget check. Price
table in config (`llm_prices`, per model, in and out per M tokens). Calls without a user (system jobs)
are booked to user 0 ("system").

Rough per-user cost at current prices: a heavy user (30 turns/day on flash, 40 emails/day understood by
glm-5.3) is about $0.45/day, about $14/month; a typical user about $3 to $5/month. Thirty typical users
fit in Max's $300 credits; thirty heavy users do not, so budgets matter.

### 9.3 Daily budgets (per user, per local day)

| Tier | Soft cap | Hard cap |
|---|---|---|
| standard | $0.30 | $0.60 |
| trusted | $1.00 | $2.00 |
| owner | none | none |

- Soft cap reached: background degrades. Attention understanding falls back to the heuristic path
  (already exists for `attention_max_attempts`), best_effort work (LEARN delay, summaries) is skipped,
  background tasks run on the fast model.
- Hard cap reached: chat still answers on the fast model with a trimmed context and no new background
  tasks; the user is told once: "I've hit today's limit for heavy work. Reminders and quick answers still
  work, and everything resets at midnight your time."
- 150% of hard cap (runaway): chat refuses until local midnight, owner alerted.
- Global guard: the owner sets `LLM_MONTHLY_CEILING_USD`. At 70% of it month-to-date the owner is
  alerted; at 90% all standard users drop to soft-cap behavior.

### 9.4 Bans and automatic cooldowns

- `/ban <user> [reason]`: status `banned`; cancel wakeups and pending outbox rows; stop pollers; clear
  mailboxes; Composio connections kept (so an unban restores them) unless the owner adds `purge`, which
  runs `/delete_me`'s cascade.
- Automatic: 5 rate-limit hits in an hour, or a hard-cap runaway, puts the user in a 1 h `cooldown`
  (chat answers with the slow-down text) and alerts the owner. No automatic permanent bans.

## 10. Account deletion: `/delete_me`

Flow:
1. `/delete_me` (alias `/deleteme`): "This deletes everything I know about you: messages, memories,
   reminders, connected accounts. It can't be undone." Buttons `[Delete everything]` `[Cancel]`, valid
   10 minutes. Optionally offer `/export` first (should, not must: a JSON file of messages, memories and
   commitments, sent as a document).
2. On confirm: status `deleting` at once (the gate now drops all events), then enqueue job `DELETE_USER`.
3. The job runs steps in order; each step is idempotent and records completion in
   `users.state["deletion"]`, so a crash resumes:
   1. Cancel wakeups; delete pending outbox rows; delete mailboxes, leases and `mavis:*:u<uid>:*` keys
      (SCAN by pattern).
   2. Composio: list the user's connected accounts by `composio_user_id` and delete each
      (`IntegrationProvider.disconnect_all(user)`, new method using the existing `disconnect`,
      `tools/integrations/composio.py:322`). Failures retry 5 times, then the owner is alerted; deletion of
      our data continues.
   3. Qdrant: delete points by `user_id` filter.
   4. Neo4j: `MATCH (n:Entity {user_id:$u}) CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 500 ROWS`.
   5. Artifacts: remove `artifacts/u<uid>/`. T4: delete the user's S3 workspace prefix and browser profile.
   6. Postgres: one transaction deleting from every user table in FK-safe order. The list lives in code
      (`store/repo/deletion.py`) and a test fails if any model with a `user_id` column is missing from it.
   7. Langfuse: delete traces for the hashed user id through the Langfuse API (best effort).
   8. Send the final message directly: "Done. Everything is deleted. If you ever want to come back, you'll
      need a new invite." Then set the users row to a tombstone: status `deleted`, `name`, `state`,
      `telegram_chat_id`, `telegram_user_id`, `composio_user_id` nulled; keep `id`, `created_at`,
      `deleted_at`. Audit row with counts per store, no content.
4. Backups keep deleted data until they age out (35 days, section 13). The privacy policy says so.

Owner equivalent: `/admin delete <user>` (same job, confirmation button). Also required for a public bot:
a privacy policy and terms, linked from `/privacy` and the bot description (Telegram requires a privacy
policy for bots that collect data).

## 11. Admin commands (owner only)

Handled before routing, never through the LLM, only for users with `tier=owner` whose chat id is in
`OWNER_TELEGRAM_CHAT_IDS`. Every mutating command writes `audit_log`.

| Command | Output |
|---|---|
| `/admin` | help |
| `/stats` | users by status; active today/7d; turns today; LLM spend today and month-to-date vs ceiling; p50/p95 chat latency and LLM slot wait (1 h); 429s; overflow calls; queue depths; outbox backlog; DLQ size; disk and memory |
| `/users [active\|pending\|banned]` | id, name, joined, last active, spend today, tier |
| `/user <id>` | profile, connections, spend 7d, limits hit, invite used |
| `/invite ...` | section 4.4 |
| `/ban <id> [reason]`, `/unban <id>` | section 9.4 |
| `/budget <id> <usd/day\|default>`, `/tier <id> <tier>` | overrides |
| `/broadcast <text>` | preview plus `[Send to N users]` `[Cancel]`; sent via the outbox at priority `broadcast`, each user's delivery deferred out of their quiet hours |
| `/dlq [list\|replay <id>\|drop <id>]` | dead-letter inspection (`bus/redis_streams.py:163-165`) |
| `/pause bg` / `/resume bg` | global kill switch for background LLM work |
| `/admin delete <id>` | section 10 |

`admin_user`/`admin_password` are removed from config (unused).

## 12. Observability

- **Metrics endpoint**: `GET /metrics` on the api (Prometheus text format), protected by a bearer token
  (`METRICS_TOKEN`) and Caddy path rule. Values come from Redis counters and histograms that every process
  writes (fixed-bucket histograms as `HINCRBY` on minute keys, 2 h TTL) plus live reads (XPENDING, mailbox
  sizes, outbox backlog, DLQ length). This keeps one scrape target and no Prometheus server on the box.
  Series: `mavis_chat_turn_seconds`, `mavis_llm_wait_seconds{rank}`, `mavis_llm_calls_total{provider,model,outcome}`,
  `mavis_llm_429_total`, `mavis_llm_cost_usd_total`, `mavis_mailbox_depth{lane}`, `mavis_ready_users{lane}`,
  `mavis_outbox_backlog`, `mavis_dlq_size`, `mavis_users{status}`, `mavis_rate_limited_total`.
  `/stats` reads the same source. Optional later: Grafana Cloud free tier scraping it.
- **Langfuse**: turn on with Langfuse Cloud (Hobby free tier, 50k observations/month), sample 20% of chat
  turns and 5% of background calls, 100% of failed turns. User id hashed (6.1). Self-hosting Langfuse is
  not an option on 4 GB (it needs ClickHouse). Prompts leave the box, so the privacy policy names it
  (open question 3).
- **Alerting, minimal**:
  - in-app watchdog (timer role, every minute) messages the owner on Telegram, deduped per hour: DLQ
    growing, chat p95 > 30 s for 10 min, LLM 429 storm, limiter local fallback, outbox backlog > 50, disk
    > 85%, monthly spend thresholds, invite brute force, Composio disconnect failures, backup failure.
  - external dead-man: Healthchecks.io (free) pinged by the watchdog and by the nightly backup, plus
    UptimeRobot (free) on `/healthz`; both email the owner when the box itself is down (when the in-app
    path cannot speak).
- Logs: keep docker json logs with rotation; CloudWatch Logs only if the owner approves (section 13).

## 13. Off-box backups

| What | How | When | Retention |
|---|---|---|---|
| Postgres | `pg_dump -Fc` streamed to S3 | nightly 03:00 IST | 35 days (lifecycle rule), bucket versioning on |
| Qdrant | snapshot API (`POST /collections/{c}/snapshots`), upload, delete local | nightly 03:15 | 14 days |
| Neo4j | existing `deploy/aws/graph_export.py` (logical export, no stop needed in Community) to S3 | nightly 03:30 | 14 days |
| Redis | AOF file copy (mailboxes, limiter state are transient; low value) | nightly | 3 days |
| EBS volume | AWS DLM snapshot policy | daily | 7 snapshots |
| Restore drill | restore all three stores into a throwaway local compose, run the isolation tests | monthly, scripted `deploy/aws/restore-drill.sh` | |

Backups are encrypted with SSE-S3 (or SSE-KMS if the owner prefers). The box uses an instance role with
write-only access to the backup prefix (`s3:PutObject` only, no delete), so a compromised box cannot
destroy backups; lifecycle rules do the expiry.

**AWS resources that need the owner's OK before creation** (account 276307603629, ap-south-1):
1. S3 bucket `mavis-backups-<suffix>` with versioning, public access block, lifecycle rules. About $0.5
   to $2/month.
2. IAM role plus instance profile for the EC2 box (write-only to that bucket; later also the T4 AgentCore
   permissions). No cost.
3. DLM lifecycle policy for daily EBS snapshots. About $1 to $2/month for a 20 to 40 GB volume
   (incremental).
4. Optional: gp3 volume grow from 19 GB to 40 GB (disk is 80% full). About $1.6/month extra.
5. Optional: CloudWatch Logs group and an SNS topic for alarms. Small cost; skip if Telegram plus
   Healthchecks.io is enough.
6. Optional: SSM Parameter Store SecureString for secrets (free tier).

Nothing is created until the owner says yes; `deploy/aws/backups.sh` is written to be idempotent and
prints the plan first (dry run by default).

## 14. Telegram send pacing

- Outbox sender gets two Redis token buckets checked before each send: global 25 msg/s, and per chat
  1 msg/s with burst 3 (a 3-bubble reply goes out immediately; longer bursts are spaced). A row that has
  no token is not claimed this pass (no attempt counted).
- Outbox rows get a `priority` column: `chat` (0), `proactive` (1), `broadcast` (2). `due()` orders by
  priority, then time, keeping the per-user bubble order rule.
- Broadcasts are additionally capped at 10 msg/s so chat replies always have headroom.
- `setWebhook` sets `max_connections=40` explicitly (`channels/telegram_webhook.py:41-46`), and
  `allowed_updates=["message","edited_message","callback_query","my_chat_member"]` (`my_chat_member` lets
  us leave groups and mark users who blocked the bot as inactive, which stops proactive sends to them).
- A 403 "bot was blocked by the user" marks the user `inactive_since` and pauses proactive work and
  pollers for them; their next message resumes.

## 15. Load testing plan

Uses the H7 live-test sink, extended:
- `TEST_TELEGRAM_CHAT_IDS` becomes a **range**: `LIVE_TEST_CHAT_BASE` (synthetic, below -10^15) and
  `LIVE_TEST_CHAT_COUNT` (max 50). Every id in the range passes `active_test_chat`, gets its own user row,
  status `active`, tier `standard`, and all sends go to the sink. The same safety checks apply (synthetic,
  not in the owner list, off unless enabled).
- Test users get a `test` flag: excluded from `/stats` user counts, booked to a separate spend bucket,
  and deleted by `scripts/loadtest.py --cleanup` (which reuses the deletion cascade, so the load test
  also exercises it).

Harness `scripts/loadtest.py` (runs on the owner's machine against the prod webhook with the secret, or
inside the api container): N simulated users, each a script of realistic turns (greeting, add reminder,
ask what's pending, a burst of 4 messages in 3 s, a button press, a web question), Poisson arrivals.
It reads the sink to measure end-to-end latency per turn.

Scenarios (each 15 minutes, run in a quiet hour for the owner, with the owner's own chat unaffected):
1. 10 users, normal pace. Pass: chat p95 < 12 s, no 429s, no DLQ.
2. 30 users, normal pace. Pass: chat p95 < 20 s, worker RSS < 850 MB, host free memory > 300 MB, swap
   not growing.
3. 30 users with 1 "abuser" sending 60 messages/min. Pass: abuser rate limited; other users' p95 within
   20% of scenario 2 (proves fairness).
4. 30 users plus a synthetic 08:30 fan-out (all morning check-ins due at once). Pass: jitter spreads them;
   chat p95 < 25 s during the spike.
5. Provider failure injection: force 429s (test flag on the LLM client) for 60 s. Pass: shared backoff,
   overflow to secondary if configured, no lost events.
6. Kill the worker mid-turn. Pass: lease expiry and reaper resume the user; no duplicate replies
   (`processed_events` dedupe).

Real provider calls cost money: scenario 2 at 30 users is roughly 900 turns, about $1 to $2 on flash.
Background-heavy scenarios use a stub LLM (`LLM_FAKE=1` for test users only) to test scheduling without
spend.

## 16. Interactions with other tracks

- **Phase B ledger**: every ledger query is per user already. Deletion cascade includes `commitments`.
  Ledger reconciler jobs run in the `bg` lane and count against the user's budget. Migration numbering:
  ledger lands first if it is ready; this track renumbers on top.
- **Programs (T2)**: daily plan delivery at a user-chosen fixed time uses the per-user timezone (section 5)
  and the fan-out jitter must not move a promised time: Programs deliveries are exempt from jitter but
  limited to the user-chosen minute, and their LLM generation is precomputed 30 to 60 minutes earlier in the
  `bg` lane (`best_effort` is not enough for a promise; use `background`). Programs count toward budgets;
  a user over their hard cap still gets the precomputed plan.
- **Connectors (T3)**: `composio_user_id` per user (6.2); `DirectOAuthProvider` tokens (Strava, Kite) are
  per user and must be in the deletion cascade and revoked on delete; connector suggestions slot into
  onboarding step 6; adaptive polling (6.3) applies to new pollers too; Google OAuth verification status
  matters once strangers connect Gmail (Composio managed app for the beta).
- **Sandbox (T4)**: AgentCore runs off-box, so per-user sandboxes do not consume box RAM, but the worker
  needs about 100 MB more for the Playwright driver (included in the 900m). Concurrency: cap global
  sandbox sessions at 3 and 1 per user; sandbox tasks count against the user's budget (AgentCore cost
  booked in `llm_usage` with provider `agentcore` or a sibling `compute_usage` table). Per-user S3
  workspace prefix and quota (500 MB) are in the deletion cascade.

## 17. Rollout

| Step | Content | Gate to next |
|---|---|---|
| R0 | Owner OKs AWS resources (section 13); backups to S3 live; restore drill once | drill passes |
| R1 | Migration: user status, invites, `llm_usage`, outbox priority; gate in shadow mode (logs decisions, allowlist still enforced) | 2 days of shadow logs match allowlist |
| R2 | Shared limiter, usage metering, `/metrics`, watchdog, Langfuse | 2 days, no regressions in the owner's chat |
| R3 | Mailbox scheduler behind `WORKER_SCHEDULER=mailbox` (old path kept one release) | load scenarios 1, 3, 6 pass |
| R4 | Onboarding, `/settings`, per-user tz and currency, `/delete_me`, admin commands, pacing | load scenarios 2, 4, 5 pass; deletion verified on test users |
| R5 | Gate switched on; owner invites 3 friends (codes `max_uses=1`) | one week, no isolation or cost surprises |
| R6 | Up to 30 users; review spend weekly | |

Rollback: each step is behind a flag; the gate can revert to the allowlist (`ACCESS_MODE=allowlist`).

Implementation plan split (for writing-plans): (1) access and invites, (2) onboarding and preferences,
(3) scheduler, (4) limiter and metering, (5) budgets and abuse, (6) deletion, (7) admin, (8)
observability, (9) backups (infra), (10) pacing, (11) load test. 1, 4, 9 can run in parallel; 3 before 11.

## 18. Risks

- Redis becomes more critical (mailboxes, limiter). Mitigation: AOF everysec (already on), maxmemory
  raised, alert on Redis memory > 80%, limiter local fallback.
- Hidden provider queueing: if the plan is Pro, 3 slots for 30 users is tight (chat p95 will exceed
  targets at peaks). The secondary provider or the Max plan is the answer.
- Ollama terms for serving third parties are not stated on the pricing page. Owner should confirm with
  Ollama before opening beyond friends.
- Composio prefix: two id schemes coexist; the lookup table removes ambiguity, the regex is prod-only.
- Coalescing could merge two unrelated messages; capped and only for messages within 3 s of each other.

## 19. Self-review

- Every topic in the brief is covered: onboarding (5), invite lifecycle (4.4), isolation and Composio
  prefix (6), worker fairness (7), shared limiter (8), per-user limits and bans (9), deletion (10), tz and
  currency (5), admin (11), observability (12), backups with owner-OK list (13), pacing (14), load test
  (15), rollout (17), track interactions (16).
- Audit pointers were re-checked; corrections noted in section 2 (Composio pointer, outbox sender
  location, LLM concurrency default now 3, only the worker calls the LLM, test sink is single-id).
- Fits the box: steady memory about 2.7 GB of 3.7 GB, one worker, no new on-box services.
- No AWS resource is created without the owner (13). No paid third-party service is required except the
  existing Ollama and Composio plans; Langfuse Cloud, Healthchecks.io and UptimeRobot are free tiers.
- Unverified items are marked: Ollama plan in use, Qdrant integer `is_tenant`, `tzfpy` arm64 wheel,
  Ollama terms for third-party use.

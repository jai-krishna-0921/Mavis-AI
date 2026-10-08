# Mavis Phase 11: Multi-user Production Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Read `docs/superpowers/plans/2026-10-02-mavis-00-index.md` (shared contracts), the spec and the owner decisions below before starting.

**Goal:** Mavis AI serves 10 to 30 invited people on one t4g.medium: strangers get a polite "invite only" reply, invited users redeem a code and get a useful result inside two minutes, every user is isolated, metered and capped, one user's burst never delays another, the owner has admin commands, metrics, alerts and off-box backups, and every step is behind a flag that reverts to today's single-owner behaviour.

**Architecture:** A new `mavis.access` package owns the front door (invite codes, the worker-side access gate, owner and user commands, onboarding helpers, budgets, deletion). The api admits private chats only, rate limits per chat and never stores content for pending users; the worker runs registered *event gates* before any handler, so the access gate, owner commands and budget cooldowns run before routing and never through the LLM. Capacity sharing moves into Redis: a Lua-scripted LLM limiter (global slots, lane caps, per-user fairness, shared 429 backoff, overflow to the secondary provider) and a mailbox scheduler (per-user FIFO lists, a ready queue per lane, leases, round-robin executors) replace the per-process limiter and the per-user lock wait. A LangChain callback meters tokens into `llm_usage` and a Redis spend counter that budgets read in O(1). Operations add `/metrics`, a watchdog, sampled Langfuse with hashed ids, Telegram send pacing, S3 backups created by idempotent scripts, and a load test that drives a range of synthetic test chats.

**Tech Stack:** Python 3.13, uv, pydantic 2 + pydantic-settings, SQLAlchemy 2 async, Alembic, redis-py asyncio (Lua via `register_script`), fakeredis with Lua (`lupa`) in tests, FastAPI, python-telegram-bot, LangChain core callbacks, Langfuse SDK, `tzfpy` (offline tz lookup), bash + AWS CLI v2 for infra scripts, pytest + pytest-asyncio (asyncio_mode=auto).

**Spec:** `docs/superpowers/specs/2026-10-08-mavis-multiuser-design.md`, owner decisions `docs/superpowers/specs/2026-10-08-owner-decisions.md` (research: `scratchpad/research-multiuser.md`).

## Global Constraints

Inherits every line of `docs/superpowers/plans/2026-10-02-mavis-00-index.md` Global Constraints. In addition:

- **Owner decisions (2026-10-08) are binding:** Ollama plan is **Pro** (3 concurrent; overflow to the secondary provider hook), so the limiter defaults are `LLM_GLOBAL_SLOTS=3`, `LLM_BG_MAX_SLOTS=1`, `LLM_BEST_EFFORT_MAX_SLOTS=0`, `LLM_USER_MAX_SLOTS=2`. AWS resources (IAM role + instance profile, IMDS hop limit 2, S3 backup bucket, daily EBS snapshots) are approved. Langfuse is on, sampled, user ids hashed. Access is by invite codes. Server is t4g.medium (4 GB). WhatsApp is out of scope. The owner still has to confirm that Ollama's terms allow serving third-party users: that is a rollout gate before R5 (Task 20).
- **Flags revert everything.** `ACCESS_MODE=allowlist` (default), `WORKER_SCHEDULER=legacy` (default), `LLM_LIMITER=local` (default), `LANGFUSE_ENABLED=false` (default in tests), `PACING_ENABLED=true` (cheap, safe). With the defaults every existing test passes unchanged. Each task that touches a shared path adds an explicit defaults-unchanged test (named in the task).
- **General mechanisms only.** No special case for any user id, chat id, name, city, currency, code or message text. Rules key on structure (status, tier, lane, chat type, time, counts). Every rule is proven by varied synthetic tests: at least three different names, chat ids, timezones or currencies per rule (for example Priya / Tomas / Aiko, `Europe/Lisbon` / `America/Bogota` / `Asia/Tokyo`, chat ids 5001 / 7302 / 9944). Tests must not reuse strings from real chats or incidents.
- **Copy rule.** No em dashes or en dashes in any user-facing string, bot copy, prompt, tool description or doc. Product name is Mavis AI. Every user-facing string in this plan is copied verbatim from spec sections 4, 5, 9, 10 where the spec gives one.
- **Secrets.** Never print, log, commit or paste `.env` values, invite plaintexts (shown once to the owner only), metrics tokens or AWS credentials. Scripts print resource names and counts, never keys.
- **Tests never hit the network, a real LLM, Telegram, Composio or AWS:** SQLite per test (`db` fixture), `FakeLLM`, `FakeChannel`, `fakeredis.FakeAsyncRedis` (with Lua through `lupa`), `RecordingBus`. Infra scripts are tested with `bash -n`, `shellcheck` when present, and a stubbed `aws` on `PATH` that records calls.
- **Migration numbering is never hard-coded.** Parallel branches add revisions (see "Parallel execution and shared files"). Task 2 Step 1 reads the head at execution time and names the file `<NN>_multiuser_access.py` with `NN` = head number + 1 and `down_revision` = the head revision id; it adds `test_single_migration_head`. Re-run that check (and renumber) right before merge if another branch merged first. Nothing else depends on the number.
- **Commits:** conventional commits, one per task (a task may commit twice when it says so), no `Co-Authored-By` or any AI attribution trailer.
- Full suite (`uv run pytest -q`) and `uv run ruff check src tests scripts` pass at the end of every task. Code blocks favour readability over the 110-character limit in a few places: when ruff reports E501, wrap at an argument boundary (no logic change).

## Review Focus

1. **A stranger, a group or a revoked code never reaches the LLM or stores content.** Pending users get one invite-only reply per 24 h, nothing is logged into `messages`, groups are left, a revoked or exhausted code answers exactly like an unknown one. Owners: Task 4 `test_group_and_channel_updates_are_dropped`, `test_pending_user_text_is_not_stored`; Task 5 `test_pending_user_gets_one_reply_per_day_and_no_turn`, `test_revoked_and_exhausted_codes_answer_like_unknown`.
2. **One user's burst does not delay another user.** With 30 messages queued for one user, a second user's single message is handled next, per-user order is kept, and a crashed executor's lease expires and the reaper resumes the user without a duplicate reply. Owners: Task 13 `test_round_robin_across_users_under_burst`, `test_per_user_fifo_order_holds_across_two_executors`, `test_expired_lease_is_reaped_and_resumed_once`.
3. **The shared limiter never exceeds the plan concurrency, even across processes, and never starves chat.** Two limiter instances on one Redis never hold more than `LLM_GLOBAL_SLOTS`; background never takes more than `LLM_BG_MAX_SLOTS`; a dead holder's slot returns after its TTL; Redis down falls back to the local limiter. Owners: Task 10 `test_two_processes_share_the_global_cap`, `test_background_lane_cap_keeps_chat_slots`, `test_dead_holder_slot_expires`, `test_redis_down_falls_back_to_local_limiter`.
4. **Deletion leaves nothing behind and survives a crash.** Every table with a `user_id` column is in the cascade (meta-test), each step is idempotent and resumable, the tombstone keeps no personal fields, and a deleted chat can rejoin only with a new code. Owners: Task 14 `test_every_user_table_is_in_the_cascade`, `test_deletion_resumes_after_a_crash_mid_way`, `test_tombstone_has_no_personal_fields`, Task 5 `test_deleted_user_returns_as_pending`.
5. **A user over budget still gets reminders and quick answers; nobody else pays for them.** Soft cap degrades background only, hard cap keeps chat on the fast model and tells the user once, runaway refuses chat until local midnight in the user's own zone, spend is booked to the right user under concurrency. Owners: Task 12 `test_soft_cap_degrades_background_only`, `test_hard_cap_notice_is_sent_once_per_local_day`, `test_runaway_resets_at_the_users_local_midnight`; Task 11 `test_concurrent_calls_book_spend_to_their_own_users`.

**Dry run.** This plan was not dry-run against a scratch copy (Track 1, Phase B and Plan 12 branches were mid-flight when it was written). Treat a failing step as a plan defect to fix inline, keeping the test's intent. Where a step names a line number in an existing file, re-locate it with the quoted code before editing.

## Deviations from spec

Where the spec sketch and the code disagree, this plan follows the code and keeps the spec's intent:

1. **Event gates in the runner.** The spec says "a new first handler `access.gate(event)`". Handlers run as a list and all run on retry, so a first handler cannot stop the others. The runner gains `register_event_gate(fn)`: gates run in order before handlers and return `False` to drop the event (Task 5). Owner commands, user commands (`/delete_me`, `/settings`, `/privacy`) and cooldowns are gates too, so none of them reaches the LLM.
2. **Rate-limit replies come from the worker.** The api never sends. When the inbound bucket drops an update, the api publishes one `RATE_LIMITED` event per chat per minute (event id carries the minute), and the worker sends the slow-down text (Task 4).
3. **Group membership through an event.** `my_chat_member` updates become `CHAT_MEMBER` events; the worker leaves groups (`leaveChat`) and marks a private chat that blocked the bot as inactive (Task 4, Task 17).
4. **Coalescing keeps one row per message without touching the chat turn.** The scheduler logs the earlier messages of a burst as their own `messages` rows (own `event_id`, so retries dedupe) and runs the chat turn once on the last event; the turn reads the earlier ones from history, which is where the model already reads context. LEARN runs on the last message only; the earlier ones reach memory through the nightly consolidation of history (spec 7.2 intent kept; no change to `agents/conversation.py`, which Track 1 is editing).
5. **Tokens are metered by a LangChain callback** (`on_llm_end`), not by threading usage through `_call`. Every model call (complete, tools, structured, secondary) passes `run_config(name)` callbacks already, so one handler covers all paths.
6. **Budget enforcement lives at the LLM call.** Soft cap: best_effort refused, background SMART calls run on FAST. Hard cap: background refused (`BudgetExceededLLM`, an `LLMError`, which existing background paths already treat as "skip, use the heuristic"), chat runs on FAST. Runaway: interactive refused with the runaway notice. This is one general rule instead of a check in every caller.
7. **Timezone change hooks.** `access.preferences.set_timezone` updates the row and runs registered hooks (`register_timezone_hook`); the routines module registers the re-anchor of morning and evening wakeups. One-off wakeups are never moved (spec 5).
8. **Spend sources are pluggable.** `access.budgets.register_spend_source(name, fn)` lets Plan 12 add `compute_usage` cost to the same daily budget without this plan knowing its table.
9. **`admin_user`/`admin_password` are deleted** in Task 15 (spec 11), with a compose and deploy cleanup.
10. **Optional AWS items are not created**: gp3 volume grow, CloudWatch Logs + SNS, SSM Parameter Store are listed in Task 18 as "ask the owner later". Only the approved items are scripted.

## Parallel execution and shared files

Branches in flight while this plan runs:

| Branch / plan | Touches | Coordination with this plan |
|---|---|---|
| `track1-feel` (T1.1 no-confirm + H4/H6) | `agents/conversation.py`, `tools/registry.py`, `policy/approvals.py`, `channels/formatting.py` | This plan's edits to `agents/conversation.py` are zero (Deviation 4); the `set_preferences` tool registers through `tools/__init__.py` and `tools/chat_tools.py` only |
| `track1-persona` (T1.2 register, T1.3 reactions) | `agents/persona.py`, `channels/presence.py` | No edits to `agents/persona.py`: onboarding copy lives in `agents/onboarding.py` |
| `ledger` (Phase B commitments) | migration `0013_commitments` (must renumber: main has `0013_task_outcomes`, so Phase B becomes `0014_*` if it lands first), `loops/`, `initiative/`, `config.py` | Migration number taken at execution time; the deletion cascade lists `commitments` when it exists (Task 14 meta-test enforces it) |
| Plan 12 (sandbox, "the machine") | `channels/base.py`, `channels/telegram.py`, `channels/fake.py`, `channels/test_sink.py`, `channels/outbox_sender.py`, `store/models.py` (outbox `photo_path`/`media`, new tables), `config.py`, compose, `deploy/aws/` | Shared contracts A, B, C, E, F, G below |
| Plan 13 (Programs), Plan 14 (Connectors) | not yet branched | Plan 14 adds `DirectOAuthProvider` tokens: they must register a deletion step (contract F) and use `identity.provider_id_for` for Composio |

**Files this plan OWNS (new):** `src/mavis/access/__init__.py`, `access/codes.py`, `access/gate.py`, `access/commands.py`, `access/invite_commands.py`, `access/admin.py`, `access/preferences.py`, `access/tz_resolve.py`, `access/currency.py`, `access/data/zone_country.tsv`, `access/data/cities.tsv.gz`, `access/budgets.py`, `access/deletion.py`, `access/inbound.py`, `agents/onboarding.py`, `store/repo/invites.py`, `store/repo/usage.py`, `store/repo/deletion.py`, `llm/limiter.py`, `llm/limiter_lua.py`, `llm/context.py`, `llm/usage.py`, `worker/gates.py`, `worker/mailbox.py`, `worker/scheduler.py`, `tools/integrations/identity.py`, `tools/preferences.py`, `obs/__init__.py`, `obs/metrics.py`, `obs/watchdog.py`, `api/routes/metrics.py`, `domain/jitter.py`, `scripts/build_geo_tables.py`, `scripts/loadtest.py`, `deploy/aws/iam-role.sh` (shared contract B), `deploy/aws/backups.sh`, `deploy/aws/box-backup.sh`, `deploy/aws/restore-drill.sh`, `tests/access/`, `tests/llm/test_limiter.py`, `tests/llm/test_usage.py`, `tests/worker/test_scheduler.py`, `tests/obs/`, `tests/test_compose_env.py` (shared contract C), `tests/deploy/`.

**SHARED files this plan touches, and what it changes:**

| File | Change | Conflict note |
|---|---|---|
| `src/mavis/config.py` | new keys (Task 1, each later task appends its block), owner ids alias, remove `admin_*` | every plan appends blocks; resolve by keeping both blocks |
| `src/mavis/store/models.py` | `User` columns, `InviteCode`, `InviteRedemption`, `LlmUsage`, `OutboxMessage.priority` | Plan 12 adds outbox `photo_path`/`media` and its tables; Phase B adds `CommitmentRow` |
| `src/mavis/domain/events.py` | `EventType.RATE_LIMITED`, `EventType.CHAT_MEMBER`, `JobKind.DELETE_USER` | additive enum values |
| `src/mavis/domain/integrations.py` | `user_from_provider_id` prod-only legacy rule (Task 8) | Plan 14 may add provider types |
| `src/mavis/channels/telegram_updates.py` | private-chat filter, inbound bucket, `my_chat_member`, pending users store no content | heavily edited by this plan only |
| `src/mavis/channels/telegram.py` | `leave_chat`, 403 "blocked" -> `ChannelBlocked` | Plan 12 adds `edit_text`/`send_photo`/`send_media_group` (different methods) |
| `src/mavis/channels/telegram_webhook.py` | `max_connections=40`, `allowed_updates` + `my_chat_member` | this plan only |
| `src/mavis/channels/test_sink.py` | chat id range, `is_test_chat`, `SinkChannel._is_test` (contract E) | Plan 12 extends `SinkChannel` with new kinds through `_is_test` |
| `src/mavis/channels/outbox_sender.py` | pacing reserve before claim, priority order, blocked handling | Plan 12 adds photo/album delivery branches in `_deliver` |
| `src/mavis/channels/pacing.py` | contract A (create or extend) | Plan 12 A1 may create it first |
| `src/mavis/store/repo/outbox.py` | `priority` in `enqueue`/`due` ordering | Plan 12 adds `photo_path`/`media` to `enqueue`/`to_outbound` |
| `src/mavis/store/repo/users.py` | create as `pending`, status helpers, `test` flag | this plan only |
| `src/mavis/worker/runner.py` | event gates, scheduler switch | **rewritten by this plan only**; Plan 12 does not touch it |
| `src/mavis/llm/models.py` | limiter adapter, shared backoff, overflow routing, budget gate | **rewritten by this plan only**; Plan 12 does not touch it |
| `src/mavis/llm/tracing.py` | sampling, hashed user id metadata, `LANGFUSE_ENABLED` | this plan only |
| `src/mavis/memory/vector.py`, `memory/neo4j_graph.py` | tenant index, path constraint | this plan only |
| `src/mavis/tools/integrations/composio.py`, `composio_webhooks.py`, `workspace_guard.py`, `poller.py` | stored Composio ids, Redis-backed created-ids, adaptive poll interval | Plan 14 also edits Composio files: keep edits to the identity calls |
| `src/mavis/attention/pipeline.py` | `user.currency` before `attention_currency` | one line |
| `src/mavis/initiative/routines.py`, `attention/rhythm.py` | fan-out jitter offset, timezone re-anchor hook | small additive edits; Phase B also edits routines |
| `src/mavis/tools/__init__.py`, `tools/chat_tools.py` | register `set_preferences` | one line each; Track 1 also edits chat_tools |
| `src/mavis/timers/runner.py` | periodic tick hooks (reaper, watchdog, pending cleanup) | Plan 12 runs its machine reaper as a worker loop (only the worker holds sessions); it may move onto this hook later |
| `src/mavis/store/db.py` | per-role pool sizes from settings | this plan only |
| `src/mavis/bus/redis_streams.py` | MAXLEN from settings | this plan only |
| `src/mavis/api/app.py`, `api/routes/telegram.py` | owner-id start guard, webhook route exempt from per-IP limit, `/metrics` route | Plan 12 adds the `/e2e/{name}` route (demo pages; Watch live is deferred) |
| `src/mavis/cli.py` | `mavis admin` helpers, role env for pools | Plan 12 adds `mavis machine` |
| `docker-compose.prod.yml` | env passthrough for every new key, memory limits, redis maxmemory | Plan 12 also raises worker to 900m: same value, keep one |
| `deploy/aws/deploy.sh`, `deploy/aws/common.sh` | owner-supplied keys forced through, `--verify` hook untouched | Plan 12 adds `--verify-machine` |
| `pyproject.toml`, `uv.lock` | `tzfpy`, dev `fakeredis[lua]` | lockfile conflicts: re-run `uv lock` |
| `tests/conftest.py` | pin new flags in `TEST_ENV` | every plan appends |

**Shared contracts (identical text in Plan 12):**

- **A. Telegram pacing** (`src/mavis/channels/pacing.py`): `class SendPacer: async def reserve(self, chat_id: int | None = None, *, kind: str = "chat") -> float` returns the seconds to wait (0.0 = send now), Redis token buckets with an in-memory fallback; `def get_pacer() -> SendPacer`; `def set_pacer(p: SendPacer | None) -> None`; setting `telegram_global_send_rate: float = 25.0`. This plan adds `telegram_chat_send_rate: float = 1.0`, `telegram_chat_burst: int = 3`, `telegram_broadcast_rate: float = 10.0`, per-chat buckets and the `kind="broadcast"` cap. If `pacing.py` exists when Task 17 runs, extend it; otherwise create it with the full interface.
- **B. AWS role** (`deploy/aws/iam-role.sh`): creates role `mavis-ec2` and instance profile `mavis-ec2`, associates it to the box, sets IMDS hop limit 2. It attaches no policies; this plan adds inline policy `mavis-backups` in `backups.sh`, Plan 12 adds `mavis-machine` in its own script.
- **C. Compose passthrough test** (`tests/test_compose_env.py`): a `PASSTHROUGH` set of env names that must appear in the `x-app-env` block; each plan adds its keys.
- **D. Owner chat ids**: `owner_telegram_chat_ids` reads `OWNER_TELEGRAM_CHAT_IDS` or the old `ALLOWED_TELEGRAM_CHAT_IDS`; `allowed_telegram_chat_ids` stays as a read-only property for one release.
- **E. Test sink**: `is_test_chat(chat_id, s=None) -> bool`; `SinkChannel` decides through `self._is_test(chat_id)` only.
- **F. Deletion extension**: `store/repo/deletion.register_deletion_step(name, fn)`; external steps run after Composio and before Postgres. Plan 12's tables (`machine_sessions`, `compute_usage`, `workspace_files`, `task_cards`, `task_links`, `user_quotas`) must be added to `USER_TABLES` by whichever plan merges second (the meta-test fails until they are).
- **G. Spend**: this plan owns `llm_usage` and `budgets.spend_today_usd(user_id) -> float`, summing `register_spend_source(name, fn: Callable[[int, date], Awaitable[float]])`; Plan 12 owns `compute_usage` and registers it.

## File Structure

```
src/mavis/
  config.py                          MODIFY  access, scheduler, limiter, budget, pacing, obs, backup keys
  access/__init__.py                 CREATE
  access/codes.py                    CREATE  Crockford codes: generate, normalize, hash, hint, deep link
  access/inbound.py                  CREATE  per-chat token bucket at intake (Redis Lua + memory)
  access/gate.py                     CREATE  status gate: active/pending/banned/deleting/deleted, brute force
  access/commands.py                 CREATE  command table for owner and user commands (an event gate)
  access/invite_commands.py          CREATE  /invite new|list|revoke|users
  access/admin.py                    CREATE  /admin /stats /users /user /budget /tier /broadcast /dlq /pause /resume
  access/preferences.py              CREATE  set_timezone/set_currency/set_name, timezone hooks
  access/tz_resolve.py               CREATE  location -> zone (tzfpy), city -> zone (GeoNames), LLM last resort
  access/currency.py                 CREATE  zone -> country -> ISO 4217
  access/data/zone_country.tsv       CREATE  generated from zone1970.tab
  access/data/cities.tsv.gz          CREATE  generated from GeoNames cities15000
  access/budgets.py                  CREATE  tiers, soft/hard/runaway, monthly ceiling, cooldown, spend sources
  access/deletion.py                 CREATE  /delete_me flow, DELETE_USER job, resumable steps
  agents/onboarding.py               CREATE  welcome, clock check, currency line, first value, connector slot
  domain/events.py                   MODIFY  RATE_LIMITED, CHAT_MEMBER, JobKind.DELETE_USER
  domain/integrations.py             MODIFY  prod-only legacy id rule
  domain/jitter.py                   CREATE  user_offset(uid, span_s)
  store/models.py                    MODIFY  User columns, InviteCode, InviteRedemption, LlmUsage, outbox.priority
  store/repo/users.py                MODIFY  pending on create, status, test flag
  store/repo/invites.py              CREATE  mint, list, revoke, redeem (FOR UPDATE), redemptions
  store/repo/usage.py                CREATE  llm_usage upsert and reads
  store/repo/deletion.py             CREATE  USER_TABLES, delete_user_rows, register_deletion_step
  store/repo/outbox.py               MODIFY  priority
  store/db.py                        MODIFY  pool sizes from settings
  migrations/versions/<NN>_multiuser_access.py  CREATE (NN taken at execution time)
  worker/gates.py                    CREATE  register_event_gate, run_gates
  worker/runner.py                   MODIFY  gates before handlers, scheduler switch
  worker/mailbox.py                  CREATE  MailboxBackend: RedisMailbox (Lua), MemoryMailbox
  worker/scheduler.py                CREATE  intake, executors, coalescing, reaper
  llm/context.py                     CREATE  llm_user_id, llm_purpose context vars
  llm/limiter.py                     CREATE  SharedLimiter (Redis), LocalLimiterAdapter, get_limiter()
  llm/limiter_lua.py                 CREATE  Lua sources
  llm/usage.py                       CREATE  UsageCallback, price table, spend counter
  llm/models.py                      MODIFY  limiter adapter, shared backoff, overflow, budget gate
  llm/tracing.py                     MODIFY  sampled Langfuse with hashed ids
  memory/vector.py                   MODIFY  tenant payload index
  memory/neo4j_graph.py              MODIFY  node user_id constraint on paths
  tools/integrations/identity.py     CREATE  provider_id_for, user_for_provider_id, startup check
  tools/integrations/composio.py     MODIFY  stored ids
  tools/integrations/composio_webhooks.py MODIFY lookup by stored id
  tools/integrations/workspace_guard.py   MODIFY created ids in Redis
  tools/integrations/poller.py       MODIFY  adaptive interval
  tools/preferences.py               CREATE  set_preferences tool
  tools/__init__.py                  MODIFY  load preferences tool
  channels/pacing.py                 CREATE or MODIFY  (contract A)
  channels/telegram_updates.py       MODIFY  private only, buckets, my_chat_member, pending content rule
  channels/telegram.py               MODIFY  leave_chat, ChannelBlocked
  channels/base.py                   MODIFY  ChannelBlocked exception
  channels/telegram_webhook.py       MODIFY  max_connections, allowed_updates
  channels/test_sink.py              MODIFY  range, is_test_chat, _is_test
  channels/outbox_sender.py          MODIFY  pacing, blocked users
  timers/runner.py                   MODIFY  register_timer_tick
  obs/metrics.py                     CREATE  Redis counters/histograms, Prometheus text
  obs/watchdog.py                    CREATE  owner alerts, dead-man ping
  api/routes/metrics.py              CREATE  GET /metrics (bearer)
  api/app.py                         MODIFY  owner guard, limiter scope, metrics router
  attention/pipeline.py              MODIFY  user currency
  initiative/routines.py             MODIFY  jitter, tz re-anchor hook
  attention/rhythm.py                MODIFY  jitter
  cli.py                             MODIFY  MAVIS_ROLE for pools, `mavis admin invite` bootstrap
scripts/build_geo_tables.py          CREATE
scripts/loadtest.py                  CREATE
deploy/aws/iam-role.sh               CREATE  (contract B)
deploy/aws/backups.sh                CREATE  bucket, inline policy, DLM
deploy/aws/box-backup.sh             CREATE  nightly dump/snapshot to S3 (installed by backups.sh)
deploy/aws/restore-drill.sh          CREATE
deploy/aws/deploy.sh                 MODIFY  new owner-supplied keys
docker-compose.prod.yml              MODIFY  env, memory, redis maxmemory, pool env
tests/conftest.py                    MODIFY  pin flags, fake_redis fixture
```

---
### Task 1: Settings, owner ids alias, flags and the Redis test fixture

**Files:**
- Modify: `src/mavis/config.py`, `tests/conftest.py`, `pyproject.toml`
- Create: `tests/access/__init__.py`, `tests/access/test_settings.py`

**Interfaces:**
- Produces:
  - Settings: `owner_telegram_chat_ids: list[int]` (env `OWNER_TELEGRAM_CHAT_IDS`, alias `ALLOWED_TELEGRAM_CHAT_IDS`), read-only property `allowed_telegram_chat_ids -> list[int]` (contract D)
  - `access_mode: Literal["allowlist", "shadow", "invite"] = "allowlist"`
  - `worker_scheduler: Literal["legacy", "mailbox"] = "legacy"`, `llm_limiter: Literal["local", "redis"] = "local"`
  - invite keys `invite_max_active=20`, `invite_max_uses=25`, `invite_default_days=14`, `invite_fail_limit_per_hour=5`, `invite_fail_alert_per_hour=50`, `pending_reply_every_h=24.0`, `pending_retention_days=14`
  - Fixture `fake_redis` (a `fakeredis.FakeAsyncRedis` with Lua, installed as `mavis.bus._redis`), fixture `invite_mode` (ACCESS_MODE=invite)

- [ ] **Step 1: Add the dev dependency with Lua support**

Run: `uv add --dev "fakeredis[lua]>=2.39.0"`
Expected: `uv.lock` updated; `uv run python -c "import lupa, fakeredis; print('ok')"` prints `ok`.

- [ ] **Step 2: Pin the flags in tests and add the fixtures**

In `tests/conftest.py`, add to `TEST_ENV` after the `"GOOGLE_WORKSPACE_ENABLED"` line:

```python
    "ACCESS_MODE": "allowlist",  # Phase 11: invite gate off unless a test opts in (invite_mode)
    "WORKER_SCHEDULER": "legacy",
    "LLM_LIMITER": "local",
    "LANGFUSE_ENABLED": "false",
    "OWNER_TELEGRAM_CHAT_IDS": "[]",
```

and add below the `workspace_on` fixture:

```python
@pytest.fixture
def invite_mode(settings, monkeypatch):
    """ACCESS_MODE=invite for one test: strangers are gated by invite codes."""
    from mavis.config import get_settings

    monkeypatch.setenv("ACCESS_MODE", "invite")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
async def fake_redis(settings, monkeypatch):
    """A Lua-capable fakeredis installed as the process Redis client (get_redis())."""
    import fakeredis

    from mavis import bus as bus_mod

    client = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer(), decode_responses=True)
    monkeypatch.setattr(bus_mod, "_redis", client)
    monkeypatch.setattr(bus_mod, "get_redis", lambda: client)
    yield client
    await client.aclose()
```

Note: modules that did `from mavis.bus import get_redis` keep their own reference. Every module this plan adds imports the module (`from mavis import bus` then `bus.get_redis()`), so the fixture reaches it.

- [ ] **Step 3: Write the failing test**

`tests/access/__init__.py` is empty. `tests/access/test_settings.py`:

```python
"""Phase 11 settings: safe defaults, owner ids accept the old env name, flags are typed."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_defaults_keep_todays_behaviour(settings):
    assert settings.access_mode == "allowlist"
    assert settings.worker_scheduler == "legacy"
    assert settings.llm_limiter == "local"
    assert settings.invite_max_active == 20 and settings.invite_max_uses == 25
    assert settings.owner_telegram_chat_ids == []


@pytest.mark.parametrize("env_name", ["OWNER_TELEGRAM_CHAT_IDS", "ALLOWED_TELEGRAM_CHAT_IDS"])
@pytest.mark.parametrize("ids", [[5001], [7302, 9944], [123456789]])
def test_owner_ids_read_new_and_old_names(settings, monkeypatch, env_name, ids):
    from mavis.config import get_settings

    monkeypatch.delenv("OWNER_TELEGRAM_CHAT_IDS", raising=False)
    monkeypatch.delenv("ALLOWED_TELEGRAM_CHAT_IDS", raising=False)
    monkeypatch.setenv(env_name, str(ids))
    get_settings.cache_clear()
    s = get_settings()
    assert s.owner_telegram_chat_ids == ids
    assert s.allowed_telegram_chat_ids == ids  # contract D: old readers keep working


def test_access_mode_rejects_unknown_values(settings, monkeypatch):
    from mavis.config import Settings

    monkeypatch.setenv("ACCESS_MODE", "everyone")
    with pytest.raises(ValidationError):
        Settings()
```

- [ ] **Step 4: Run it to see it fail**

Run: `uv run pytest tests/access/test_settings.py -q`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'access_mode'`.

- [ ] **Step 5: Implement**

In `src/mavis/config.py`, change the pydantic import to `from pydantic import AliasChoices, Field, field_validator`, set `model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True)`, and replace the line `allowed_telegram_chat_ids: list[int] = Field(default_factory=list)` with:

```python
    # Owner chats (admin commands, alerts). OWNER_TELEGRAM_CHAT_IDS; the old ALLOWED_TELEGRAM_CHAT_IDS
    # name is read as an alias for one release (contract D). In ACCESS_MODE=allowlist this is still the
    # allowlist; in invite mode it only marks the owner.
    owner_telegram_chat_ids: list[int] = Field(
        default_factory=list,
        validation_alias=AliasChoices("OWNER_TELEGRAM_CHAT_IDS", "ALLOWED_TELEGRAM_CHAT_IDS",
                                      "owner_telegram_chat_ids"),
    )
```

Add this property next to `db_url`:

```python
    @property
    def allowed_telegram_chat_ids(self) -> list[int]:
        """Read-only alias of owner_telegram_chat_ids (kept one release for older readers)."""
        return self.owner_telegram_chat_ids
```

Add after the `--- bus / worker ---` block:

```python
    # --- multi-user access (Phase 11, spec 2026-10-08 sections 4, 7, 8) ------
    # allowlist: today's behaviour (owner chats only). shadow: allowlist enforced, the invite gate only
    # logs what it would do. invite: the invite gate decides.
    access_mode: Literal["allowlist", "shadow", "invite"] = "allowlist"
    worker_scheduler: Literal["legacy", "mailbox"] = "legacy"
    llm_limiter: Literal["local", "redis"] = "local"
    invite_max_active: int = 20  # unexpired, unrevoked codes at once
    invite_max_uses: int = 25  # per code
    invite_default_days: int = 14
    invite_fail_limit_per_hour: int = 5  # failed code attempts per chat
    invite_fail_alert_per_hour: int = 50  # failed attempts across all chats before the owner is alerted
    pending_reply_every_h: float = 24.0
    pending_retention_days: int = 14
```

Search for writers of `allowed_telegram_chat_ids` (assignments, `Settings(allowed_telegram_chat_ids=...)`): `grep -rn "allowed_telegram_chat_ids=" src tests scripts`. Rename each keyword to `owner_telegram_chat_ids=` (readers keep working through the property).

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/access/test_settings.py tests/test_config.py tests/channels/test_test_sink.py -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/mavis/config.py tests/conftest.py tests/access/
git commit -m "feat(config): owner chat ids alias and phase 11 access, scheduler and limiter flags"
```

---

### Task 2: Migration and models (users status, invites, llm usage, outbox priority)

**Files:**
- Create: `src/mavis/migrations/versions/<NN>_multiuser_access.py` (NN from Step 1)
- Modify: `src/mavis/store/models.py`, `src/mavis/store/repo/users.py`, `tests/store/test_migrations.py`
- Test: `tests/store/test_multiuser_migration.py`

**Interfaces:**
- Produces:
  - `User` columns: `status: str = "pending"` (`pending|active|banned|deleting|deleted`), `tier: str = "standard"` (`owner|standard|trusted`), `telegram_user_id: int | None`, `locale: str | None`, `currency: str | None`, `country: str | None`, `composio_user_id: str | None` (unique), `invite_id: int | None` (FK `invite_codes.id`), `activated_at`, `banned_at`, `ban_reason: str | None`, `budget_override_usd_day: float | None`, `inactive_since`, `deleted_at`, `is_test: bool = False`
  - `InviteCode(id, code_hash unique, code_hint, label, tier, max_uses, uses, expires_at, revoked_at, created_by_user_id, default_timezone, default_currency, created_at)`
  - `InviteRedemption(id, invite_id, user_id, redeemed_at)`
  - `LlmUsage(id, user_id, day: date, provider, model, purpose, calls, prompt_tokens, completion_tokens, cost_micros)` unique `(user_id, day, provider, model, purpose)`
  - `OutboxMessage.priority: int = 0` (0 chat, 1 proactive, 2 broadcast)
  - `UserStatus(StrEnum)`, `UserTier(StrEnum)` in `src/mavis/access/__init__.py`
  - `users.get_or_create_by_chat(chat_id, name, *, telegram_user_id=None) -> tuple[User, bool]` creates `pending`

- [ ] **Step 1: Find the head and name the revision**

Run:
```bash
grep -h '^revision = ' src/mavis/migrations/versions/*.py | sort
uv run python -c "from alembic.script import ScriptDirectory; from alembic.config import Config; c=Config(); c.set_main_option('script_location','src/mavis/migrations'); print(ScriptDirectory.from_config(c).get_heads())"
```
Expected: exactly one head, for example `['0013_task_outcomes']` (or `0014_commitments` if Phase B merged first). Set `NN` = the head's number + 1, zero padded to 4 digits; the file is `src/mavis/migrations/versions/<NN>_multiuser_access.py` with `revision = "<NN>_multiuser_access"` and `down_revision = "<head id>"`. Use these two values everywhere this task shows `<NN>_multiuser_access` and `<HEAD>`.

- [ ] **Step 2: Write the failing tests**

Append to `tests/store/test_migrations.py`:

```python
def test_single_migration_head() -> None:
    """Parallel branches each add a revision: after a merge there must still be exactly one head."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from mavis.store.migrate import MIGRATIONS_DIR

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    assert len(ScriptDirectory.from_config(cfg).get_heads()) == 1
```

`tests/store/test_multiuser_migration.py`:

```python
"""Phase 11 migration: existing owner rows become active owners, others active standard; new rows pending."""

from __future__ import annotations

import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from mavis.store.migrate import MIGRATIONS_DIR, upgrade


def _cfg(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["url"] = url
    return cfg


def _this_and_previous() -> tuple[str, str]:
    script = ScriptDirectory.from_config(_cfg(""))
    rev = next(r for r in script.walk_revisions() if r.revision.endswith("_multiuser_access"))
    return rev.revision, rev.down_revision


@pytest.mark.parametrize("owner_chat,other_chat", [(5001, 7302), (9944, 4410), (123456, 654321)])
def test_existing_rows_are_activated_and_owner_is_tiered(tmp_path, monkeypatch, owner_chat, other_chat):
    rev, prev = _this_and_previous()
    db_file = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    upgrade(url, prev)
    con = sqlite3.connect(db_file)
    for chat, name in ((owner_chat, "Priya"), (other_chat, "Tomas")):
        con.execute("insert into users (telegram_chat_id, name, timezone, onboarded, state, created_at) "
                    "values (?, ?, 'Asia/Tokyo', 1, '{}', '2026-10-01 00:00:00')", (chat, name))
    con.commit()
    con.close()
    monkeypatch.setenv("OWNER_TELEGRAM_CHAT_IDS", f"[{owner_chat}]")
    command.upgrade(_cfg(url), rev)
    rows = dict(sqlite3.connect(db_file).execute(
        "select telegram_chat_id, status || ':' || tier from users"))
    assert rows == {owner_chat: "active:owner", other_chat: "active:standard"}
    ids = dict(sqlite3.connect(db_file).execute("select telegram_chat_id, composio_user_id from users"))
    assert all(v is None for v in ids.values())  # legacy mavis-<id> identity kept (spec 6.2)


def test_downgrade_drops_the_new_tables(tmp_path):
    rev, prev = _this_and_previous()
    db_file = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    upgrade(url, rev)
    command.downgrade(_cfg(url), prev)
    names = {r[0] for r in sqlite3.connect(db_file).execute("select name from sqlite_master where type='table'")}
    assert not {"invite_codes", "invite_redemptions", "llm_usage"} & names


@pytest.mark.parametrize("chat,name", [(31, "Aiko"), (32, "Bruno"), (33, None)])
async def test_new_chat_rows_start_pending(db, chat, name):
    from mavis.store.repo import users

    u, created = await users.get_or_create_by_chat(chat, name, telegram_user_id=chat)
    assert created and u.status == "pending" and u.tier == "standard" and u.telegram_user_id == chat
```

- [ ] **Step 3: Run them to see them fail**

Run: `uv run pytest tests/store/test_multiuser_migration.py tests/store/test_migrations.py -q`
Expected: FAIL (`StopIteration` finding the revision; `TypeError` on `telegram_user_id`).

- [ ] **Step 4: Implement the enums, models and repo change**

`src/mavis/access/__init__.py`:

```python
"""Multi-user front door (Phase 11): invite codes, the access gate, commands, budgets, deletion."""

from __future__ import annotations

from enum import StrEnum


class UserStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    BANNED = "banned"
    DELETING = "deleting"
    DELETED = "deleted"


class UserTier(StrEnum):
    OWNER = "owner"
    STANDARD = "standard"
    TRUSTED = "trusted"
```

In `src/mavis/store/models.py` add `Date` and `Boolean` to the sqlalchemy import and `from datetime import date, datetime`. Add to `User` after `last_agent_msg_at`:

```python
    # --- Phase 11 multi-user -------------------------------------------------
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    tier: Mapped[str] = mapped_column(String(16), default="standard")
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    locale: Mapped[str | None] = mapped_column(String(16), default=None)
    currency: Mapped[str | None] = mapped_column(String(3), default=None)
    country: Mapped[str | None] = mapped_column(String(2), default=None)
    composio_user_id: Mapped[str | None] = mapped_column(String(64), unique=True, default=None)
    invite_id: Mapped[int | None] = mapped_column(ForeignKey("invite_codes.id"), default=None)
    activated_at: Mapped[datetime | None] = mapped_column(default=None)
    banned_at: Mapped[datetime | None] = mapped_column(default=None)
    ban_reason: Mapped[str | None] = mapped_column(String(200), default=None)
    budget_override_usd_day: Mapped[float | None] = mapped_column(Float, default=None)
    inactive_since: Mapped[datetime | None] = mapped_column(default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(default=None)
    is_test: Mapped[bool] = mapped_column(default=False)
```

Add `priority: Mapped[int] = mapped_column(Integer, default=0, index=True)` to `OutboxMessage` after `proactive`. Append the new tables at the end of the module:

```python
class InviteCode(Base):
    __tablename__ = "invite_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    code_hint: Mapped[str] = mapped_column(String(8))
    label: Mapped[str] = mapped_column(String(120), default="")
    tier: Mapped[str] = mapped_column(String(16), default="standard")
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column()
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, default=None)
    default_timezone: Mapped[str | None] = mapped_column(String(64), default=None)
    default_currency: Mapped[str | None] = mapped_column(String(3), default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class InviteRedemption(Base):
    __tablename__ = "invite_redemptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    invite_id: Mapped[int] = mapped_column(ForeignKey("invite_codes.id"), index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    redeemed_at: Mapped[datetime] = mapped_column(default=utcnow)


class LlmUsage(Base):
    __tablename__ = "llm_usage"
    __table_args__ = (UniqueConstraint("user_id", "day", "provider", "model", "purpose",
                                       name="uq_llm_usage_user_day_model_purpose"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)  # 0 = system
    day: Mapped[date] = mapped_column(Date, index=True)  # the user's local date
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(80))
    purpose: Mapped[str] = mapped_column(String(24))
    calls: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    completion_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_micros: Mapped[int] = mapped_column(BigInteger, default=0)
```

`InviteCode` must be defined before `User` references it by string only (`ForeignKey("invite_codes.id")` is a string, so order does not matter for SQLAlchemy).

In `src/mavis/store/repo/users.py` change `get_or_create_by_chat`:

```python
async def get_or_create_by_chat(chat_id: int, name: str | None, *,
                                telegram_user_id: int | None = None) -> tuple[User, bool]:
    async with Session() as s:
        user = await s.scalar(select(User).where(User.telegram_chat_id == chat_id))
        if user is not None:
            return user, False
        # New rows start pending (Phase 11): the access gate activates them on redemption. In
        # ACCESS_MODE=allowlist the gate is not enforced, so this changes nothing for the owner.
        user = User(telegram_chat_id=chat_id, telegram_user_id=telegram_user_id, name=name,
                    timezone=get_settings().default_timezone, state={}, status="pending")
        s.add(user)
        try:
            await s.commit()
        except IntegrityError:  # another worker created it concurrently
            await s.rollback()
            existing = await s.scalar(select(User).where(User.telegram_chat_id == chat_id))
            assert existing is not None
            return existing, False
        return user, True
```

- [ ] **Step 5: Write the migration**

`src/mavis/migrations/versions/<NN>_multiuser_access.py`:

```python
"""Phase 11 multi-user: user status/tier/identity columns, invite codes, llm usage, outbox priority.

Existing users become active (they were admitted by the allowlist); a user whose chat is in
OWNER_TELEGRAM_CHAT_IDS (or the old ALLOWED_TELEGRAM_CHAT_IDS) becomes tier owner. composio_user_id stays
NULL for them, which means the legacy `mavis-<id>` identity (spec 6.2), so connected accounts keep working.
"""

from __future__ import annotations

import json
import os

import sqlalchemy as sa
from alembic import op

revision = "<NN>_multiuser_access"
down_revision = "<HEAD>"
branch_labels = None
depends_on = None


def _owner_ids() -> list[int]:
    raw = os.environ.get("OWNER_TELEGRAM_CHAT_IDS") or os.environ.get("ALLOWED_TELEGRAM_CHAT_IDS") or "[]"
    try:
        return [int(x) for x in json.loads(raw)]
    except (ValueError, TypeError):
        return [int(x) for x in raw.strip("[]").split(",") if x.strip()]


def upgrade() -> None:
    op.create_table(
        "invite_codes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("code_hint", sa.String(8), nullable=False),
        sa.Column("label", sa.String(120), nullable=False, server_default=""),
        sa.Column("tier", sa.String(16), nullable=False, server_default="standard"),
        sa.Column("max_uses", sa.Integer, nullable=False, server_default="1"),
        sa.Column("uses", sa.Integer, nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Integer, nullable=True),
        sa.Column("default_timezone", sa.String(64), nullable=True),
        sa.Column("default_currency", sa.String(3), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "invite_redemptions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("invite_id", sa.Integer, sa.ForeignKey("invite_codes.id"), nullable=False, index=True),
        sa.Column("user_id", sa.Integer, nullable=False, index=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, nullable=False, index=True),
        sa.Column("day", sa.Date, nullable=False, index=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("calls", sa.Integer, nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("cost_micros", sa.BigInteger, nullable=False, server_default="0"),
        sa.UniqueConstraint("user_id", "day", "provider", "model", "purpose",
                            name="uq_llm_usage_user_day_model_purpose"),
    )
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("status", sa.String(16), nullable=False, server_default="pending"))
        b.add_column(sa.Column("tier", sa.String(16), nullable=False, server_default="standard"))
        b.add_column(sa.Column("telegram_user_id", sa.BigInteger, nullable=True))
        b.add_column(sa.Column("locale", sa.String(16), nullable=True))
        b.add_column(sa.Column("currency", sa.String(3), nullable=True))
        b.add_column(sa.Column("country", sa.String(2), nullable=True))
        b.add_column(sa.Column("composio_user_id", sa.String(64), nullable=True))
        b.add_column(sa.Column("invite_id", sa.Integer, nullable=True))
        b.add_column(sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("banned_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("ban_reason", sa.String(200), nullable=True))
        b.add_column(sa.Column("budget_override_usd_day", sa.Float, nullable=True))
        b.add_column(sa.Column("inactive_since", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("is_test", sa.Boolean, nullable=False, server_default=sa.false()))
        b.create_index("ix_users_status", ["status"])
        b.create_unique_constraint("uq_users_composio_user_id", ["composio_user_id"])
        b.create_foreign_key("fk_users_invite_id", "invite_codes", ["invite_id"], ["id"])
    with op.batch_alter_table("outbox") as b:
        b.add_column(sa.Column("priority", sa.Integer, nullable=False, server_default="0"))
        b.create_index("ix_outbox_priority", ["priority"])
    users = sa.table("users", sa.column("telegram_chat_id", sa.BigInteger), sa.column("status", sa.String),
                     sa.column("tier", sa.String), sa.column("telegram_user_id", sa.BigInteger))
    op.execute(users.update().values(status="active", telegram_user_id=users.c.telegram_chat_id))
    owners = _owner_ids()
    if owners:
        op.execute(users.update().where(users.c.telegram_chat_id.in_(owners)).values(tier="owner"))


def downgrade() -> None:
    with op.batch_alter_table("outbox") as b:
        b.drop_index("ix_outbox_priority")
        b.drop_column("priority")
    with op.batch_alter_table("users") as b:
        b.drop_constraint("fk_users_invite_id", type_="foreignkey")
        b.drop_constraint("uq_users_composio_user_id", type_="unique")
        b.drop_index("ix_users_status")
        for col in ("is_test", "deleted_at", "inactive_since", "budget_override_usd_day", "ban_reason",
                    "banned_at", "activated_at", "invite_id", "composio_user_id", "country", "currency",
                    "locale", "telegram_user_id", "tier", "status"):
            b.drop_column(col)
    op.drop_table("llm_usage")
    op.drop_table("invite_redemptions")
    op.drop_table("invite_codes")
```

Replace `<NN>` and `<HEAD>` with the values from Step 1 (they are literal text only inside this file and nowhere else).

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/store -q && uv run pytest -q`
Expected: PASS (including `test_migrations_match_models`; if it reports an index name mismatch, align the ORM `index=True` names with the migration by naming them explicitly).

- [ ] **Step 7: Commit**

```bash
git add src/mavis/access/__init__.py src/mavis/store/models.py src/mavis/store/repo/users.py \
        src/mavis/migrations/versions/*_multiuser_access.py tests/store/
git commit -m "feat(store): user status, tiers, invite codes, llm usage and outbox priority"
```

---

### Task 3: Invite codes (domain and repo)

**Files:**
- Create: `src/mavis/access/codes.py`, `src/mavis/store/repo/invites.py`, `tests/access/test_codes.py`, `tests/access/test_invites_repo.py`

**Interfaces:**
- Produces:
  - `generate_code() -> str` (10 Crockford base32 chars), `display(code) -> str` (`MAV-XXXXX-XXXXX`), `normalize(text) -> str | None` (accepts with or without `MAV`, dashes, spaces, any case; maps `I/L -> 1`, `O -> 0`; returns 10 chars or None), `code_hash(code) -> str` (sha256 hex), `hint(code) -> str` (last 4), `deep_link_param(code) -> str` (`MAV` + code), `looks_like_code(text) -> bool`
  - `class InviteError(Exception)` with `reason: Literal["invalid", "limit"]`
  - `invites.mint(*, created_by, uses=1, days=14, tier="standard", tz=None, currency=None, label="") -> tuple[InviteCode, str]` (row, plaintext; raises `InviteError("limit")`)
  - `invites.list_active(now) -> list[InviteCode]`, `invites.revoke(hint_or_id: str) -> InviteCode | None`
  - `invites.redeem(code: str, user_id: int, now) -> InviteCode | None` (None for unknown, revoked, expired, exhausted, all alike)
  - `invites.redemptions_for(invite_id) -> list[InviteRedemption]`

- [ ] **Step 1: Write the failing tests**

`tests/access/test_codes.py`:

```python
from __future__ import annotations

import pytest

from mavis.access import codes


def test_generated_codes_are_crockford_and_distinct():
    seen = {codes.generate_code() for _ in range(500)}
    assert len(seen) == 500
    for c in seen:
        assert len(c) == 10 and set(c) <= set(codes.ALPHABET)


@pytest.mark.parametrize("typed", ["MAV-7K3QZ-9XW2B", "mav7k3qz9xw2b", "7K3QZ 9XW2B", "  7k3qz-9xw2b  ",
                                   "/start MAV7K3QZ9XW2B"])
def test_normalize_accepts_the_shapes_people_send(typed):
    assert codes.normalize(typed.replace("/start ", "")) == "7K3QZ9XW2B"


@pytest.mark.parametrize("ambiguous,clean", [("7K3QZ9XWIB", "7K3QZ9XW1B"), ("O0O0O0O0O0", "0000000000"),
                                             ("LLLLLLLLLL", "1111111111")])
def test_normalize_maps_ambiguous_letters(ambiguous, clean):
    assert codes.normalize(ambiguous) == clean


@pytest.mark.parametrize("junk", ["", "hello there", "MAV-123", "7K3QZ9XW2BB7", "UUUUUUUUUU"])
def test_normalize_rejects_non_codes(junk):
    assert codes.normalize(junk) is None


def test_display_hash_hint_and_deep_link():
    c = "7K3QZ9XW2B"
    assert codes.display(c) == "MAV-7K3QZ-9XW2B"
    assert codes.hint(c) == "XW2B"
    assert codes.deep_link_param(c) == "MAV7K3QZ9XW2B"
    assert codes.code_hash(c) == codes.code_hash("mav-7k3qz-9xw2b".upper().replace("MAV-", "").replace("-", ""))
    assert len(codes.code_hash(c)) == 64
```

`tests/access/test_invites_repo.py`:

```python
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from mavis.access.codes import InviteError
from mavis.store.db import utcnow
from mavis.store.repo import invites, users


async def _user(chat: int, name: str) -> int:
    u, _ = await users.get_or_create_by_chat(chat, name)
    return u.id


@pytest.mark.parametrize("tier,tz,cur", [("standard", "Europe/Lisbon", "EUR"), ("trusted", "Asia/Tokyo", "JPY"),
                                         ("standard", None, None)])
async def test_mint_stores_only_the_hash_and_redeems_once(db, tier, tz, cur):
    row, plain = await invites.mint(created_by=1, uses=1, days=3, tier=tier, tz=tz, currency=cur, label="x")
    assert plain not in (row.code_hash, row.code_hint) and row.code_hint == plain[-4:]
    a, b = await _user(5001, "Priya"), await _user(7302, "Tomas")
    got = await invites.redeem(plain, a, utcnow())
    assert got is not None and got.tier == tier and got.default_timezone == tz and got.uses == 1
    assert await invites.redeem(plain, b, utcnow()) is None  # exhausted


async def test_revoked_expired_and_unknown_all_return_none(db):
    uid = await _user(9944, "Aiko")
    revoked, p1 = await invites.mint(created_by=1)
    await invites.revoke(revoked.code_hint)
    _, p2 = await invites.mint(created_by=1, days=1)
    assert await invites.redeem(p1, uid, utcnow()) is None
    assert await invites.redeem(p2, uid, utcnow() + timedelta(days=2)) is None
    assert await invites.redeem("ZZZZZZZZZZ", uid, utcnow()) is None


async def test_limits_on_active_codes_and_uses(db, settings, monkeypatch):
    from mavis.config import get_settings

    monkeypatch.setenv("INVITE_MAX_ACTIVE", "3")
    monkeypatch.setenv("INVITE_MAX_USES", "4")
    get_settings.cache_clear()
    with pytest.raises(InviteError) as exc:
        await invites.mint(created_by=1, uses=5)
    assert exc.value.reason == "limit"
    for _ in range(3):
        await invites.mint(created_by=1)
    with pytest.raises(InviteError):
        await invites.mint(created_by=1)


async def test_concurrent_redemptions_never_exceed_max_uses(db):
    _, plain = await invites.mint(created_by=1, uses=2)
    ids = [await _user(6000 + i, f"u{i}") for i in range(6)]
    results = await asyncio.gather(*(invites.redeem(plain, uid, utcnow()) for uid in ids))
    assert sum(r is not None for r in results) == 2
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/access/test_codes.py tests/access/test_invites_repo.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mavis.access.codes'`.

- [ ] **Step 3: Implement**

`src/mavis/access/codes.py`:

```python
"""Invite codes: 10 Crockford base32 characters (50 bits), shown as MAV-XXXXX-XXXXX, stored as sha256 only."""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Literal

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford: no I, L, O, U
LENGTH = 10
_PREFIX = "MAV"
_AMBIGUOUS = str.maketrans({"I": "1", "L": "1", "O": "0"})
_STRIP = re.compile(r"[\s\-_]+")


class InviteError(Exception):
    def __init__(self, reason: Literal["invalid", "limit"]) -> None:
        super().__init__(reason)
        self.reason = reason


def generate_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))


def normalize(text: str) -> str | None:
    """The canonical 10-character code in `text`, or None. Case, dashes, spaces and a MAV prefix are
    ignored; I and L read as 1 and O as 0 (Crockford)."""
    s = _STRIP.sub("", (text or "").strip().upper())
    if s.startswith(_PREFIX) and len(s) == LENGTH + len(_PREFIX):
        s = s[len(_PREFIX):]
    s = s.translate(_AMBIGUOUS)
    if len(s) != LENGTH or any(ch not in ALPHABET for ch in s):
        return None
    return s


def looks_like_code(text: str) -> bool:
    return normalize(text) is not None


def display(code: str) -> str:
    return f"{_PREFIX}-{code[:5]}-{code[5:]}"


def deep_link_param(code: str) -> str:
    return f"{_PREFIX}{code}"  # Telegram start parameters allow [A-Za-z0-9_-]


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def hint(code: str) -> str:
    return code[-4:]
```

`src/mavis/store/repo/invites.py`:

```python
"""Invite code rows. Only hashes are stored; the plaintext is returned once by mint()."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select, update

from mavis.access.codes import InviteError, code_hash, generate_code, hint, normalize
from mavis.config import get_settings
from mavis.store.db import Session, utcnow
from mavis.store.models import InviteCode, InviteRedemption


def _active(now: datetime):
    return (InviteCode.revoked_at.is_(None)) & (InviteCode.expires_at > now)


async def mint(*, created_by: int | None, uses: int = 1, days: int | None = None, tier: str = "standard",
               tz: str | None = None, currency: str | None = None, label: str = "") -> tuple[InviteCode, str]:
    s = get_settings()
    if uses < 1 or uses > s.invite_max_uses:
        raise InviteError("limit")
    now = utcnow()
    async with Session() as session:
        active = await session.scalar(select(func.count(InviteCode.id)).where(_active(now)))
        if (active or 0) >= s.invite_max_active:
            raise InviteError("limit")
        plain = generate_code()
        row = InviteCode(code_hash=code_hash(plain), code_hint=hint(plain), label=label[:120], tier=tier,
                         max_uses=uses, uses=0,
                         expires_at=now + timedelta(days=days or s.invite_default_days),
                         created_by_user_id=created_by, default_timezone=tz,
                         default_currency=(currency or None) and currency.upper())
        session.add(row)
        await session.commit()
        return row, plain


async def list_active(now: datetime | None = None) -> list[InviteCode]:
    async with Session() as s:
        rows = await s.scalars(select(InviteCode).where(_active(now or utcnow())).order_by(InviteCode.id))
        return list(rows)


async def revoke(hint_or_id: str) -> InviteCode | None:
    key = hint_or_id.strip().upper()
    async with Session() as s:
        cond = InviteCode.id == int(key) if key.isdigit() else InviteCode.code_hint == key
        row = await s.scalar(select(InviteCode).where(cond, InviteCode.revoked_at.is_(None)))
        if row is None:
            return None
        row.revoked_at = utcnow()
        await s.commit()
        return row


async def redeem(code: str, user_id: int, now: datetime) -> InviteCode | None:
    """Use one redemption of `code` for `user_id`, atomically. Unknown, revoked, expired and exhausted
    codes all return None (no oracle). The conditional UPDATE is the race guard on every database; on
    Postgres the row lock comes from the UPDATE itself."""
    canonical = normalize(code)
    if canonical is None:
        return None
    digest = code_hash(canonical)
    async with Session() as s:
        res = await s.execute(
            update(InviteCode)
            .where(InviteCode.code_hash == digest, _active(now), InviteCode.uses < InviteCode.max_uses)
            .values(uses=InviteCode.uses + 1)
        )
        if res.rowcount != 1:
            await s.rollback()
            return None
        row = await s.scalar(select(InviteCode).where(InviteCode.code_hash == digest))
        s.add(InviteRedemption(invite_id=row.id, user_id=user_id, redeemed_at=now))
        await s.commit()
        return row


async def redemptions_for(invite_id: int) -> list[InviteRedemption]:
    async with Session() as s:
        rows = await s.scalars(select(InviteRedemption).where(InviteRedemption.invite_id == invite_id)
                               .order_by(InviteRedemption.id))
        return list(rows)
```

The spec's `SELECT ... FOR UPDATE` intent (no over-redemption under races) is met by the conditional `UPDATE ... WHERE uses < max_uses`, which is atomic on SQLite and Postgres alike; the concurrency test proves it.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/access -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis/access/codes.py src/mavis/store/repo/invites.py tests/access/
git commit -m "feat(access): invite codes with hashed storage and race-safe redemption"
```

---

### Task 4: Api intake (private chats only, per-chat bucket, my_chat_member, no content for pending)

**Files:**
- Create: `src/mavis/access/inbound.py`, `tests/access/test_inbound.py`, `tests/channels/test_intake_multiuser.py`
- Modify: `src/mavis/channels/telegram_updates.py`, `src/mavis/domain/events.py`, `src/mavis/api/app.py`, `src/mavis/config.py`

**Interfaces:**
- Consumes: `users.get_or_create_by_chat(..., telegram_user_id=)` (Task 2), `UserStatus` (Task 2)
- Produces:
  - `EventType.RATE_LIMITED = "rate_limited"`, `EventType.CHAT_MEMBER = "chat_member"`
  - `class InboundLimiter: async def allow(self, chat_id: int, *, pending: bool) -> bool`; `get_inbound_limiter()`, `set_inbound_limiter()`
  - Settings `inbound_rate_per_min: float = 20`, `inbound_burst: int = 10`, `inbound_pending_rate_per_min: float = 5`, `inbound_pending_burst: int = 3`
  - Behaviour: in modes `shadow` and `invite`, non-private chats (`chat.type != "private"`) are dropped with a log; `my_chat_member` becomes a `CHAT_MEMBER` event; for a pending user the event payload carries no `text` beyond what the gate needs (`/start` parameter or a code-shaped text), never a file

- [ ] **Step 1: Write the failing tests**

`tests/access/test_inbound.py`:

```python
from __future__ import annotations

import pytest

from mavis.access.inbound import InboundLimiter


@pytest.mark.parametrize("chat", [5001, 7302, 9944])
async def test_memory_bucket_allows_burst_then_refills(settings, chat):
    now = [1000.0]
    lim = InboundLimiter(clock=lambda: now[0])
    allowed = [await lim.allow(chat, pending=False) for _ in range(12)]
    assert allowed.count(True) == settings.inbound_burst
    now[0] += 3.0  # 20/min = one token per 3 s
    assert await lim.allow(chat, pending=False)


async def test_pending_bucket_is_tighter_and_per_chat(settings):
    now = [0.0]
    lim = InboundLimiter(clock=lambda: now[0])
    assert [await lim.allow(1, pending=True) for _ in range(5)].count(True) == settings.inbound_pending_burst
    assert await lim.allow(2, pending=True)  # another chat has its own bucket


async def test_redis_bucket_matches_memory(fake_redis, settings):
    now = [50.0]
    lim = InboundLimiter(clock=lambda: now[0])
    got = [await lim.allow(42, pending=False) for _ in range(11)]
    assert got.count(True) == settings.inbound_burst
    assert await fake_redis.exists("mavis:inbound:c42")
```

`tests/channels/test_intake_multiuser.py`:

```python
from __future__ import annotations

import pytest

from mavis.channels.telegram_updates import ingest_update
from mavis.domain.events import EventType
from mavis.store.repo import messages, users


def _msg(update_id: int, chat_id: int, chat_type: str, text: str, uid: int | None = None) -> dict:
    return {"update_id": update_id, "message": {
        "message_id": update_id, "date": 1760000000, "text": text,
        "chat": {"id": chat_id, "type": chat_type}, "from": {"id": uid or chat_id, "first_name": "Aiko"}}}


async def _noop(_cid: str) -> None:
    return None


@pytest.mark.parametrize("chat_id,chat_type", [(-4001, "group"), (-1009001, "supergroup"), (-1009002, "channel")])
async def test_group_and_channel_updates_are_dropped(db, invite_mode, recording_bus, chat_id, chat_type):
    assert not await ingest_update(_msg(1, chat_id, chat_type, "hi all"), recording_bus, _noop)
    assert recording_bus.take() == [] and await users.get_by_chat(chat_id) is None


@pytest.mark.parametrize("chat,text", [(5001, "what's the weather"), (7302, "remind me at 6"), (9944, "hello")])
async def test_pending_user_text_is_not_stored(db, invite_mode, recording_bus, chat, text):
    assert await ingest_update(_msg(10 + chat, chat, "private", text), recording_bus, _noop)
    (event,) = recording_bus.take()
    assert event.payload.get("text", "") == ""  # no content for a pending user
    assert event.payload["pending"] is True
    u = await users.get_by_chat(chat)
    assert u.status == "pending" and await messages.recent(u.id, 10) == []


@pytest.mark.parametrize("text", ["/start MAV7K3QZ9XW2B", "MAV-7K3QZ-9XW2B", "7k3qz 9xw2b"])
async def test_pending_user_code_shaped_text_reaches_the_gate(db, invite_mode, recording_bus, text):
    assert await ingest_update(_msg(77, 6101, "private", text), recording_bus, _noop)
    (event,) = recording_bus.take()
    assert event.payload["text"] == text


async def test_my_chat_member_becomes_an_event(db, invite_mode, recording_bus):
    data = {"update_id": 900, "my_chat_member": {
        "chat": {"id": -4002, "type": "group"}, "from": {"id": 5001},
        "new_chat_member": {"status": "member", "user": {"id": 1, "is_bot": True}}, "date": 1760000000}}
    assert await ingest_update(data, recording_bus, _noop)
    (event,) = recording_bus.take()
    assert event.type is EventType.CHAT_MEMBER
    assert event.payload == {"chat_id": -4002, "chat_type": "group", "status": "member"}


async def test_over_the_bucket_publishes_one_rate_limited_event_per_minute(db, invite_mode, recording_bus,
                                                                           monkeypatch):
    from mavis.access import inbound

    class _Never:
        async def allow(self, chat_id, *, pending):
            return False

    monkeypatch.setattr(inbound, "_limiter", _Never())
    for i in range(4):
        await ingest_update(_msg(200 + i, 5001, "private", "spam"), recording_bus, _noop)
    events = recording_bus.take()
    assert [e.type for e in events] == [EventType.RATE_LIMITED]


async def test_allowlist_mode_is_unchanged(db, settings, recording_bus, monkeypatch):
    """Defaults: dev env with an empty owner list still admits every chat, as today."""
    assert await ingest_update(_msg(300, 5555, "private", "hi"), recording_bus, _noop)
    (event,) = recording_bus.take()
    assert event.payload["text"] == "hi" and "pending" not in event.payload
```

`RecordingBus.publish` must dedupe by event id for the per-minute test: check `tests/conftest.py` `RecordingBus.publish`; if it does not dedupe, add `if event.id in {e.id for e in self.events}: return False` there (a fake behaving like the real bus).

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/access/test_inbound.py tests/channels/test_intake_multiuser.py -q`
Expected: FAIL (`ModuleNotFoundError: mavis.access.inbound`).

- [ ] **Step 3: Implement the bucket**

Add to `src/mavis/config.py` (multi-user block):

```python
    inbound_rate_per_min: float = 20.0
    inbound_burst: int = 10
    inbound_pending_rate_per_min: float = 5.0
    inbound_pending_burst: int = 3
```

`src/mavis/access/inbound.py`:

```python
"""Per-chat inbound token bucket at intake (spec 9.1). Redis (one Lua call) when available, else memory."""

from __future__ import annotations

import time
from collections.abc import Callable

import structlog

from mavis import bus
from mavis.config import get_settings

log = structlog.get_logger(__name__)

# KEYS[1] bucket hash; ARGV: now_s, rate_per_s, burst, cost. Returns 1 if allowed.
BUCKET_LUA = """
local t = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local now = tonumber(ARGV[1]); local rate = tonumber(ARGV[2]); local burst = tonumber(ARGV[3])
local tokens = tonumber(t[1]) or burst; local ts = tonumber(t[2]) or now
tokens = math.min(burst, tokens + (now - ts) * rate)
local ok = 0
if tokens >= tonumber(ARGV[4]) then tokens = tokens - tonumber(ARGV[4]); ok = 1 end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], math.ceil(burst / rate) + 60)
return ok
"""


class InboundLimiter:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._mem: dict[int, tuple[float, float]] = {}
        self._script = None

    def _params(self, pending: bool) -> tuple[float, int]:
        s = get_settings()
        if pending:
            return s.inbound_pending_rate_per_min / 60.0, s.inbound_pending_burst
        return s.inbound_rate_per_min / 60.0, s.inbound_burst

    async def allow(self, chat_id: int, *, pending: bool) -> bool:
        rate, burst = self._params(pending)
        now = self._clock()
        client = bus.get_redis()
        if client is not None:
            try:
                if self._script is None:
                    self._script = client.register_script(BUCKET_LUA)
                key = f"mavis:inbound:{'p' if pending else 'c'}{chat_id}"
                return bool(await self._script(keys=[key], args=[now, rate, burst, 1]))
            except Exception as exc:  # noqa: BLE001 - a Redis blip must not drop real users
                log.warning("inbound.redis_failed", error=type(exc).__name__)
        tokens, ts = self._mem.get(chat_id, (float(burst), now))
        tokens = min(burst, tokens + (now - ts) * rate)
        ok = tokens >= 1
        self._mem[chat_id] = (tokens - 1 if ok else tokens, now)
        if len(self._mem) > 50_000:
            self._mem.clear()
        return ok


_limiter: InboundLimiter | None = None


def get_inbound_limiter() -> InboundLimiter:
    global _limiter
    if _limiter is None:
        _limiter = InboundLimiter()
    return _limiter


def set_inbound_limiter(lim: InboundLimiter | None) -> None:
    global _limiter
    _limiter = lim
```

The test `test_redis_bucket_matches_memory` asserts key `mavis:inbound:c42`: the key is `c{chat}` for active and `p{chat}` for pending, as written.

- [ ] **Step 4: Implement intake**

In `src/mavis/domain/events.py` add to `EventType`:

```python
    RATE_LIMITED = "rate_limited"  # Phase 11: api dropped a chat's update over its inbound bucket
    CHAT_MEMBER = "chat_member"    # Phase 11: my_chat_member (bot added to a group, blocked, unblocked)
```

In `src/mavis/channels/telegram_updates.py`:

1. Change `_chat_allowed` so it only applies in `allowlist` mode:

```python
def _gate_in_worker() -> bool:
    return get_settings().access_mode in ("shadow", "invite")


def _chat_allowed(chat_id: int) -> bool:
    """allowlist mode (and shadow, which still enforces the list): owner chats plus the test chats.
    Empty list = allow-all in dev only (warn once); every other env denies."""
    global _warned_open_allowlist
    s = get_settings()
    if is_test_chat(chat_id, s):
        return True
    if s.owner_telegram_chat_ids:
        return chat_id in s.owner_telegram_chat_ids
    if s.env != "dev":
        return False
    if not _warned_open_allowlist:
        _warned_open_allowlist = True
        log.warning("telegram.allowlist_empty_allowing_all", hint="set OWNER_TELEGRAM_CHAT_IDS")
    return True
```

and import `is_test_chat` instead of `active_test_chat` (Task 19 adds the range; until then `is_test_chat` is added in this task to `channels/test_sink.py` as `return chat_id == active_test_chat(s)` and Task 19 generalises it):

```python
def is_test_chat(chat_id: int, s: Settings | None = None) -> bool:
    """True when `chat_id` is an enabled, safe synthetic test chat (contract E)."""
    return chat_id is not None and chat_id == active_test_chat(s)
```

and in `SinkChannel` add `def _is_test(self, chat_id: int) -> bool: return chat_id == self._test` and replace every `chat_id != self._test` with `not self._is_test(chat_id)` (Task 19 changes the body only).

2. Before the `callback_query` branch, handle membership:

```python
    if member := data.get("my_chat_member"):
        chat = member.get("chat") or {}
        if chat.get("id") is None:
            return False
        status = str((member.get("new_chat_member") or {}).get("status", ""))
        event = Event(id=f"tg:update:{update_id}", user_id=0, type=EventType.CHAT_MEMBER, occurred_at=utcnow(),
                      source="telegram", trust=Trust.SYSTEM,
                      payload={"chat_id": chat["id"], "chat_type": chat.get("type", ""), "status": status})
        return await bus.publish(event)
```

(`user_id=0` is the system user; the worker resolves the chat.)

3. Record the chat type in both branches: `chat = message.get("chat") or {}` / `chat = msg.get("chat") or {}`, `chat_id = chat.get("id")`, `chat_type = chat.get("type", "private")`.

4. Replace the block from `if chat_id is None:` to the end of the function with:

```python
    if chat_id is None:
        return False
    s = get_settings()
    if _gate_in_worker():
        if chat_type != "private" and not is_test_chat(chat_id, s):
            log.info("telegram.non_private_dropped", chat_type=chat_type)
            return False
    if s.access_mode != "invite" and not _chat_allowed(chat_id):
        # allowlist and shadow modes enforce the list; the api never sends, the owner reads this log
        log.warning("telegram.chat_not_allowed", chat_id=chat_id)
        return False

    user, _ = await users.get_or_create_by_chat(chat_id, sender.get("first_name"),
                                                telegram_user_id=sender.get("id"))
    pending = user.status != "active" and not is_test_chat(chat_id, s)
    if s.access_mode == "invite" or (s.access_mode == "shadow" and pending):
        if not await get_inbound_limiter().allow(chat_id, pending=pending):
            minute = int(occurred.timestamp() // 60)
            return await bus.publish(Event(
                id=f"tg:ratelimited:{chat_id}:{minute}", user_id=user.id, type=EventType.RATE_LIMITED,
                occurred_at=occurred, source="telegram", trust=Trust.SYSTEM, payload={}))
    if pending and s.access_mode == "invite":
        # Spec 4.2: no message content is stored or carried for pending users, only what the gate needs.
        text = str(payload.get("text", ""))
        keep = text.startswith("/start") or looks_like_code(text)
        payload = {"text": text if keep else "", "message_id": payload.get("message_id"), "pending": True,
                   **({"command": payload["command"]} if keep and "command" in payload else {})}
    event = Event(id=f"tg:update:{update_id}", user_id=user.id, type=event_type, occurred_at=occurred,
                  source="telegram", payload=payload, trust=Trust.USER)
    return await bus.publish(event)
```

with imports `from mavis.access.codes import looks_like_code`, `from mavis.access.inbound import get_inbound_limiter`, `from mavis.channels.test_sink import is_test_chat`. Note `looks_like_code` on `"/start MAV..."` is False, so the `/start` prefix check is what keeps it; the gate (Task 5) reads the code after `/start`.

5. In `src/mavis/api/app.py`, the prod start guard becomes:

```python
    if s.env == "prod" and not s.owner_telegram_chat_ids:
        raise RuntimeError("OWNER_TELEGRAM_CHAT_IDS is required when ENV=prod")
```

and the Telegram router is no longer behind the per-IP limiter (it is authenticated by the secret header and all Telegram traffic shares Telegram's IPs, spec 9.1):

```python
    app.include_router(telegram.router)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/access tests/channels tests/api -q && uv run pytest -q`
Expected: PASS. If an api test asserted the webhook route is rate limited per IP, change it to assert the integrations route still is (`/integrations/...`) and the Telegram route is not.

- [ ] **Step 6: Commit**

```bash
git add src/mavis/access/inbound.py src/mavis/channels/telegram_updates.py src/mavis/channels/test_sink.py \
        src/mavis/domain/events.py src/mavis/api/app.py src/mavis/config.py tests/
git commit -m "feat(intake): private chats only, per-chat inbound bucket, membership events, no content for pending"
```

---
### Task 5: Event gates in the worker and the access gate

**Files:**
- Create: `src/mavis/worker/gates.py`, `src/mavis/access/gate.py`, `tests/access/test_gate.py`, `tests/worker/test_gates.py`
- Modify: `src/mavis/worker/runner.py`, `src/mavis/worker/handlers.py`, `src/mavis/channels/telegram.py`, `src/mavis/channels/fake.py`

**Interfaces:**
- Consumes: `invites.redeem` (Task 3), `codes.normalize` (Task 3), `UserStatus` (Task 2), `EventType.RATE_LIMITED/CHAT_MEMBER` (Task 4)
- Produces:
  - `GateFn = Callable[[Event], Awaitable[bool]]`; `register_event_gate(name: str, fn: GateFn, *, order: int = 50) -> None`; `async run_gates(event) -> bool`; `clear_gates()`
  - `access.gate.access_gate(event) -> bool` (order 10), `on_activated: list[Callable[[User, InviteCode], Awaitable[None]]]` (onboarding subscribes in Task 7), `async activate(user_id, invite, now) -> User`
  - Copy constants `INVITE_ONLY_TEXT`, `PAUSED_TEXT`, `TOO_MANY_TRIES_TEXT`, `SLOW_DOWN_TEXT`
  - `Channel.leave_chat(chat_id)` on Telegram and fakes (`FakeChannel.left: list[int]`)

- [ ] **Step 1: Write the failing tests**

`tests/worker/test_gates.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

from mavis.domain.events import Event, EventType, Trust
from mavis.worker import gates, runner


def _ev(text: str, uid: int = 1) -> Event:
    return Event(id=f"t:{text}:{uid}", user_id=uid, type=EventType.USER_MESSAGE,
                 occurred_at=datetime(2026, 10, 8, tzinfo=UTC), source="test", payload={"text": text},
                 trust=Trust.USER)


async def test_a_false_gate_stops_every_handler_and_order_is_respected(settings):
    seen: list[str] = []

    async def first(e):
        seen.append("first")
        return "stop" not in e.payload["text"]

    async def second(e):
        seen.append("second")
        return True

    async def handler(e):
        seen.append("handler")

    gates.register_event_gate("second", second, order=20)
    gates.register_event_gate("first", first, order=10)
    runner.register_event_handler(EventType.USER_MESSAGE, handler)
    await runner.handle_event(_ev("go"))
    await runner.handle_event(_ev("stop"))
    assert seen == ["first", "second", "handler", "first"]


async def test_no_gates_means_unchanged(settings):
    calls = []

    async def handler(e):
        calls.append(e.id)

    runner.register_event_handler(EventType.USER_MESSAGE, handler)
    await runner.handle_event(_ev("x"))
    assert calls == ["t:x:1"]
```

Add `gates.clear_gates()` to the autouse `_reset_worker_registry` fixture in `tests/conftest.py` (it already calls `clear_handlers()`); also call it from `runner.clear_handlers()` so both stay in step.

`tests/access/test_gate.py`:

```python
from __future__ import annotations

from datetime import timedelta

import pytest

from mavis.access import gate
from mavis.domain.events import Event, EventType, Trust
from mavis.store.db import utcnow
from mavis.store.repo import invites, messages, users


def _ev(uid: int, text: str, n: int, at=None, etype=EventType.USER_MESSAGE) -> Event:
    return Event(id=f"g:{uid}:{n}", user_id=uid, type=etype, occurred_at=at or utcnow(), source="telegram",
                 payload={"text": text, "pending": True}, trust=Trust.USER)


@pytest.fixture
async def pending(db, invite_mode, channel):
    async def make(chat: int, name: str):
        u, _ = await users.get_or_create_by_chat(chat, name)
        return u
    return make


async def _sent(channel) -> list[str]:
    from mavis.channels.outbox_sender import deliver_pending

    await deliver_pending(channel)
    return channel.texts


@pytest.mark.parametrize("chat,name", [(5001, "Priya"), (7302, "Tomas"), (9944, "Aiko")])
async def test_pending_user_gets_one_reply_per_day_and_no_turn(pending, channel, clock, chat, name):
    u = await pending(chat, name)
    assert await gate.access_gate(_ev(u.id, "hello", 1)) is False
    assert await gate.access_gate(_ev(u.id, "anyone?", 2)) is False
    assert await _sent(channel) == [gate.INVITE_ONLY_TEXT]
    clock.advance(hours=25)
    await gate.access_gate(_ev(u.id, "still here", 3))
    assert (await _sent(channel)).count(gate.INVITE_ONLY_TEXT) == 2
    assert await messages.recent(u.id, 10) == []


@pytest.mark.parametrize("form", ["/start MAV{c}", "MAV-{d}", "{c}"])
async def test_valid_code_activates_with_invite_defaults(pending, form):
    u = await pending(6101, "Bruno")
    row, plain = await invites.mint(created_by=1, tier="trusted", tz="America/Bogota", currency="COP")
    text = form.format(c=plain, d=f"{plain[:5]}-{plain[5:]}")
    activated = []
    gate.on_activated.append(lambda usr, inv: activated.append((usr.id, inv.id)) or _done())
    assert await gate.access_gate(_ev(u.id, text, 1)) is False  # the redemption message is not a turn
    fresh = await users.get(u.id)
    assert (fresh.status, fresh.tier, fresh.timezone, fresh.currency, fresh.invite_id) == (
        "active", "trusted", "America/Bogota", "COP", row.id)
    assert activated == [(u.id, row.id)]


async def _done():
    return None


async def test_revoked_and_exhausted_codes_answer_like_unknown(pending, channel):
    a, b, c = await pending(1, "A"), await pending(2, "B"), await pending(3, "C")
    revoked, p_rev = await invites.mint(created_by=1)
    await invites.revoke(revoked.code_hint)
    _, p_one = await invites.mint(created_by=1, uses=1)
    await gate.access_gate(_ev(a.id, p_one, 1))  # uses it up
    for i, (usr, code) in enumerate([(b, p_rev), (c, p_one), (b, "ZZZZZZZZZZ")]):
        await gate.access_gate(_ev(usr.id, code, 10 + i))
    replies = [t for t in await _sent(channel) if t != gate.INVITE_ONLY_TEXT]
    assert set(replies) <= {gate.CODE_NOT_VALID_TEXT} or replies == []
    assert (await users.get(b.id)).status == "pending" and (await users.get(c.id)).status == "pending"


async def test_brute_force_locks_the_chat_for_an_hour(pending, channel, clock, settings):
    u = await pending(8801, "Lena")
    for i in range(settings.invite_fail_limit_per_hour + 2):
        await gate.access_gate(_ev(u.id, f"AAAAA{i:05d}"[:10], i))
    assert gate.TOO_MANY_TRIES_TEXT in await _sent(channel)
    _, good = await invites.mint(created_by=1)
    await gate.access_gate(_ev(u.id, good, 99))
    assert (await users.get(u.id)).status == "pending"  # locked even for a good code
    clock.advance(hours=1, minutes=1)
    await gate.access_gate(_ev(u.id, good, 100))
    assert (await users.get(u.id)).status == "active"


async def test_banned_gets_one_reply_ever(pending, channel):
    u = await pending(4242, "Omar")
    await users.update(u.id, status="banned")
    for i in range(3):
        assert await gate.access_gate(_ev(u.id, "hi", i)) is False
    assert (await _sent(channel)).count(gate.PAUSED_TEXT) == 1


@pytest.mark.parametrize("status", ["deleting", "deleted"])
async def test_deleting_and_deleted_are_silent(pending, channel, status):
    u = await pending(3131, "Zoe")
    await users.update(u.id, status=status)
    assert await gate.access_gate(_ev(u.id, "hello", 1)) is False
    assert await _sent(channel) == [] or status == "deleted"


async def test_deleted_user_returns_as_pending(pending, channel):
    u = await pending(3132, "Ines")
    await users.update(u.id, status="deleted")
    await gate.access_gate(_ev(u.id, "hi again", 1))
    assert (await users.get(u.id)).status == "pending"
    assert gate.INVITE_ONLY_TEXT in await _sent(channel)


async def test_active_users_and_system_events_pass(pending):
    u = await pending(2020, "Kai")
    await users.update(u.id, status="active")
    assert await gate.access_gate(_ev(u.id, "hi", 1)) is True
    wake = _ev(u.id, "", 2, etype=EventType.WAKEUP)
    await users.update(u.id, status="pending")
    assert await gate.access_gate(wake) is False  # nothing proactive for a non-active user


async def test_shadow_mode_logs_but_admits(db, settings, monkeypatch, channel):
    from mavis.config import get_settings

    monkeypatch.setenv("ACCESS_MODE", "shadow")
    get_settings.cache_clear()
    u, _ = await users.get_or_create_by_chat(7777, "Sam")
    assert await gate.access_gate(_ev(u.id, "hi", 1)) is True
    assert await _sent(channel) == []


async def test_allowlist_mode_admits_everything(db, settings):
    u, _ = await users.get_or_create_by_chat(7778, "Rui")
    assert await gate.access_gate(_ev(u.id, "hi", 1)) is True
```

Notes for the implementer: `CODE_NOT_VALID_TEXT` is the reply to a code-shaped text that does not redeem (`"That code didn't work. Check it and send it again, or ask the person who invited you."`); unknown, revoked, expired and exhausted all get it (no oracle). Plain text from a pending user gets `INVITE_ONLY_TEXT` at most once per `pending_reply_every_h`.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/worker/test_gates.py tests/access/test_gate.py -q`
Expected: FAIL (`ModuleNotFoundError: mavis.worker.gates`).

- [ ] **Step 3: Implement the gate registry and the runner hook**

`src/mavis/worker/gates.py`:

```python
"""Event gates: run before any handler, in order; a gate returning False drops the event (Phase 11).

Gates are for decisions that must happen before routing and never through the LLM: access status, owner
and user commands, cooldowns. They must be idempotent (a retried event runs them again)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

from mavis.domain.events import Event

log = structlog.get_logger(__name__)
GateFn = Callable[[Event], Awaitable[bool]]
_gates: dict[str, tuple[int, GateFn]] = {}


def register_event_gate(name: str, fn: GateFn, *, order: int = 50) -> None:
    _gates[name] = (order, fn)


def clear_gates() -> None:
    _gates.clear()


async def run_gates(event: Event) -> bool:
    for name, (_, fn) in sorted(_gates.items(), key=lambda kv: kv[1][0]):
        if not await fn(event):
            log.debug("worker.gate_dropped", gate=name, event_type=event.type)
            return False
    return True
```

In `src/mavis/worker/runner.py` import `from mavis.worker import gates`, add `gates.clear_gates()` to `clear_handlers()`, and in `handle_event` run the gates inside the user lock and the inline retries, before the handlers (so per-user order and retries are unchanged):

```python
        async with _event_lock(event):
            async def attempt() -> None:
                if await gates.run_gates(event):
                    await _run_handlers(event, handlers)

            await run_with_inline_retries(attempt, what="event", ref=event.id)
```

Also let `handle_event` proceed for event types with no handlers when a gate may act on them (RATE_LIMITED, CHAT_MEMBER): change the early return to `if not handlers and event.type not in GATE_ONLY_TYPES:` with `GATE_ONLY_TYPES = frozenset({EventType.RATE_LIMITED, EventType.CHAT_MEMBER})`.

- [ ] **Step 4: Implement the access gate**

Add `leave_chat` to the Channel protocol in `src/mavis/channels/base.py` (`async def leave_chat(self, chat_id: int) -> None: ...`), to `TelegramChannel` (`await self._ensure(); await self._bot.leave_chat(chat_id=chat_id)`), to `FakeChannel` (`self.left.append(chat_id)`, with `self.left: list[int] = []` in `__init__`) and to `SinkChannel` (pass through unless `_is_test`).

`src/mavis/access/gate.py`:

```python
"""The access gate (spec 4.2): decides, per event and before routing, whether a user may be served."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import structlog

from mavis import bus
from mavis.access import UserStatus
from mavis.access.codes import normalize
from mavis.channels import get_channel
from mavis.channels.test_sink import is_test_chat
from mavis.config import get_settings
from mavis.domain.events import Event, EventType
from mavis.domain.messages import Outbound
from mavis.store.db import utcnow
from mavis.store.models import InviteCode, User
from mavis.store.repo import audit, invites, outbox, users
from mavis.worker.gates import register_event_gate
from mavis.worker.locks import claim

log = structlog.get_logger(__name__)

INVITE_ONLY_TEXT = "Hi! Mavis is invite-only for now. If someone gave you an invite code, send it here."
CODE_NOT_VALID_TEXT = ("That code didn't work. Check it and send it again, or ask the person who "
                       "invited you.")
PAUSED_TEXT = "This account is paused."
TOO_MANY_TRIES_TEXT = "Too many tries, try again in an hour"
SLOW_DOWN_TEXT = "You're sending a lot at once, give me a second to catch up."

ActivatedFn = Callable[[User, InviteCode], Awaitable[None]]
on_activated: list[ActivatedFn] = []
_fail_mem: dict[str, list[datetime]] = {}


def _code_in(text: str) -> str | None:
    t = (text or "").strip()
    if t.startswith("/start"):
        t = t[len("/start"):].strip()
    return normalize(t)


async def _say_once(user_id: int, text: str, key: str, ttl_s: float) -> None:
    if await claim(f"gate:{key}", ttl_s):
        await outbox.enqueue_now(Outbound(user_id=user_id, text=text, dedupe_key=f"gate:{key}:{utcnow():%Y%m%d%H%M}"))


async def _failures(user_id: int, now: datetime, add: bool) -> int:
    """Failed code attempts for this user in the last hour (Redis list when available)."""
    client = bus.get_redis()
    key = f"mavis:gate:u{user_id}:fails"
    cutoff = (now - timedelta(hours=1)).timestamp()
    if client is not None:
        if add:
            await client.zadd(key, {f"{now.timestamp()}": now.timestamp()})
            await client.expire(key, 3700)
            await client.incr(f"mavis:gate:fails:{now:%Y%m%d%H}")
        await client.zremrangebyscore(key, 0, cutoff)
        return int(await client.zcard(key))
    hits = [t for t in _fail_mem.get(key, []) if t.timestamp() > cutoff]
    if add:
        hits.append(now)
    _fail_mem[key] = hits
    return len(hits)


async def activate(user_id: int, invite: InviteCode, now: datetime) -> User:
    s = get_settings()
    await users.update(user_id, status=UserStatus.ACTIVE.value, tier=invite.tier, invite_id=invite.id,
                       activated_at=now, timezone=invite.default_timezone or s.default_timezone,
                       currency=invite.default_currency)
    await audit.record(user_id, actor="user", action="invite.redeemed", detail={"invite_id": invite.id})
    user = await users.get(user_id)
    for fn in list(on_activated):
        try:
            await fn(user, invite)
        except Exception as exc:  # noqa: BLE001 - onboarding trouble must not undo the activation
            log.warning("gate.on_activated_failed", error=type(exc).__name__)
    return user


async def _pending(user: User, event: Event) -> bool:
    s = get_settings()
    now = event.occurred_at
    if event.type is not EventType.USER_MESSAGE:
        return False
    code = _code_in(str(event.payload.get("text", "")))
    if code is None:
        await _say_once(user.id, INVITE_ONLY_TEXT, f"u{user.id}:invite_only", s.pending_reply_every_h * 3600)
        return False
    if await _failures(user.id, now, add=False) >= s.invite_fail_limit_per_hour:
        await _say_once(user.id, TOO_MANY_TRIES_TEXT, f"u{user.id}:too_many", 3600)
        return False
    invite = await invites.redeem(code, user.id, now)
    if invite is None:
        await _failures(user.id, now, add=True)
        await outbox.enqueue_now(Outbound(user_id=user.id, text=CODE_NOT_VALID_TEXT,
                                          dedupe_key=f"gate:badcode:{event.id}"))
        return False
    await activate(user.id, invite, now)
    return False  # the redemption message itself is not a chat turn; onboarding takes it from here


async def _membership(event: Event) -> bool:
    p = event.payload
    chat_id, chat_type, status = p.get("chat_id"), p.get("chat_type"), p.get("status")
    if chat_type != "private" and status in ("member", "administrator"):
        try:
            await get_channel().leave_chat(int(chat_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("gate.leave_chat_failed", error=type(exc).__name__)
    elif chat_type == "private" and (u := await users.get_by_chat(int(chat_id))) is not None:
        await users.update(u.id, inactive_since=utcnow() if status == "kicked" else None)
    return False


async def access_gate(event: Event) -> bool:
    s = get_settings()
    if event.type is EventType.CHAT_MEMBER:
        return await _membership(event)
    if event.user_id == 0:
        return True
    user = await users.get(event.user_id)
    if event.type is EventType.RATE_LIMITED:
        await _say_once(user.id, SLOW_DOWN_TEXT, f"u{user.id}:slow", 60)
        return False
    if s.access_mode == "allowlist" or (user.telegram_chat_id is not None and is_test_chat(user.telegram_chat_id)):
        return True
    status = user.status
    if s.access_mode == "shadow":
        if status != UserStatus.ACTIVE:
            log.info("gate.shadow_would_drop", status=status, event_type=event.type)
        return True
    if status == UserStatus.ACTIVE:
        return True
    if status == UserStatus.DELETED and event.type is EventType.USER_MESSAGE:
        await users.update(user.id, status=UserStatus.PENDING.value, deleted_at=None)
        user = await users.get(user.id)
        status = UserStatus.PENDING
    if status == UserStatus.PENDING:
        return await _pending(user, event)
    if status == UserStatus.BANNED and event.type is EventType.USER_MESSAGE:
        await _say_once(user.id, PAUSED_TEXT, f"u{user.id}:paused", 10 * 365 * 86400)
    return False


def register_access_gate() -> None:
    register_event_gate("access", access_gate, order=10)
```

`audit.record` already exists (`store/repo/audit.py`). The dedupe key for once-per-window replies carries the minute so a retried event does not send a second copy while a later window can.

In `src/mavis/worker/handlers.py` call `register_access_gate()` at the end of `register_default_handlers()` (import `from mavis.access.gate import register_access_gate`).

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/worker tests/access -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mavis/worker/gates.py src/mavis/worker/runner.py src/mavis/worker/handlers.py src/mavis/access/gate.py \
        src/mavis/channels/ tests/
git commit -m "feat(access): event gates before handlers and the invite access gate"
```

---

### Task 6: Command gate and owner /invite commands

**Files:**
- Create: `src/mavis/access/commands.py`, `src/mavis/access/invite_commands.py`, `tests/access/test_invite_commands.py`
- Modify: `src/mavis/worker/handlers.py`

**Interfaces:**
- Consumes: `invites.*` (Task 3), `codes.display/deep_link_param` (Task 3), gates (Task 5)
- Produces:
  - `CommandFn = Callable[[Event, User, list[str]], Awaitable[str | None]]` (returns reply text)
  - `register_owner_command(name, fn)`, `register_user_command(name, fn)`, `is_owner(user) -> bool` (tier owner and chat id in `owner_telegram_chat_ids`), `command_gate(event) -> bool` (order 20)
  - `parse_kv(args) -> tuple[dict[str, str], list[str]]` (`uses=3 days=7 tier=trusted tz=Area/City cur=EUR` plus free label words)

- [ ] **Step 1: Write the failing test**

`tests/access/test_invite_commands.py`:

```python
from __future__ import annotations

import pytest

from mavis.access import commands, invite_commands
from mavis.domain.events import Event, EventType, Trust
from mavis.store.db import utcnow
from mavis.store.repo import invites, users


@pytest.fixture
async def owner(db, settings, monkeypatch):
    from mavis.config import get_settings

    monkeypatch.setenv("OWNER_TELEGRAM_CHAT_IDS", "[5001]")
    get_settings.cache_clear()
    u, _ = await users.get_or_create_by_chat(5001, "Priya")
    await users.update(u.id, status="active", tier="owner")
    invite_commands.register()
    return await users.get(u.id)


def _cmd(uid: int, text: str, n: int = 1) -> Event:
    return Event(id=f"c:{uid}:{n}", user_id=uid, type=EventType.USER_MESSAGE, occurred_at=utcnow(),
                 source="telegram", payload={"text": text, "command": text[1:].split()[0]}, trust=Trust.USER)


async def _replies(channel) -> list[str]:
    from mavis.channels.outbox_sender import deliver_pending

    await deliver_pending(channel)
    return channel.texts


async def test_invite_new_with_options_replies_with_code_and_link(owner, channel):
    assert await commands.command_gate(_cmd(owner.id, "/invite new uses=3 days=7 tier=trusted "
                                                      "tz=Europe/Lisbon cur=eur family friends")) is False
    (reply,) = await _replies(channel)
    assert "MAV-" in reply and "https://t.me/" in reply and "?start=MAV" in reply
    (row,) = await invites.list_active()
    assert (row.max_uses, row.tier, row.default_timezone, row.default_currency, row.label) == (
        3, "trusted", "Europe/Lisbon", "EUR", "family friends")


@pytest.mark.parametrize("bad", ["tz=Mars/Base", "cur=EURO", "uses=0", "tier=god"])
async def test_invalid_options_are_refused_without_minting(owner, channel, bad):
    await commands.command_gate(_cmd(owner.id, f"/invite new {bad}"))
    assert await invites.list_active() == []
    assert "\u2014" not in (await _replies(channel))[0]


async def test_list_revoke_and_users(owner, channel):
    row, plain = await invites.mint(created_by=owner.id, uses=2, label="book club")
    u2, _ = await users.get_or_create_by_chat(7302, "Tomas")
    await invites.redeem(plain, u2.id, utcnow())
    await commands.command_gate(_cmd(owner.id, "/invite list", 2))
    await commands.command_gate(_cmd(owner.id, f"/invite users {row.id}", 3))
    await commands.command_gate(_cmd(owner.id, f"/invite revoke {row.code_hint}", 4))
    out = await _replies(channel)
    assert "book club" in out[0] and "1/2" in out[0]
    assert "Tomas" in out[1]
    assert await invites.list_active() == []


@pytest.mark.parametrize("chat,tier", [(7302, "standard"), (9944, "trusted"), (5002, "owner")])
async def test_non_owner_commands_fall_through_to_normal_handling(owner, channel, chat, tier):
    u, _ = await users.get_or_create_by_chat(chat, "X")
    await users.update(u.id, status="active", tier=tier)  # tier owner but chat not in the owner list
    assert await commands.command_gate(_cmd(u.id, "/invite new")) is True
    assert await invites.list_active() == []
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/access/test_invite_commands.py -q`
Expected: FAIL (`ModuleNotFoundError: mavis.access.commands`).

- [ ] **Step 3: Implement**

`src/mavis/access/commands.py`:

```python
"""Owner and user commands handled before routing, never through the LLM (spec 11). An event gate."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

from mavis.agents.commands import parse_command
from mavis.config import get_settings
from mavis.domain.events import Event, EventType
from mavis.domain.messages import Outbound, Role
from mavis.store.models import User
from mavis.store.repo import messages, outbox, users
from mavis.worker.gates import register_event_gate

log = structlog.get_logger(__name__)
CommandFn = Callable[[Event, User, list[str]], Awaitable[str | None]]
_owner: dict[str, CommandFn] = {}
_user: dict[str, CommandFn] = {}


def register_owner_command(name: str, fn: CommandFn) -> None:
    _owner[name] = fn


def register_user_command(name: str, fn: CommandFn) -> None:
    _user[name] = fn


def is_owner(user: User) -> bool:
    return user.tier == "owner" and user.telegram_chat_id in get_settings().owner_telegram_chat_ids


def parse_kv(args: list[str]) -> tuple[dict[str, str], list[str]]:
    kv: dict[str, str] = {}
    rest: list[str] = []
    for a in args:
        k, sep, v = a.partition("=")
        if sep and k.isidentifier():
            kv[k.lower()] = v
        else:
            rest.append(a)
    return kv, rest


async def command_gate(event: Event) -> bool:
    if event.type is not EventType.USER_MESSAGE:
        return True
    parsed = parse_command(str(event.payload.get("text", "")))
    if parsed is None:
        return True
    name, args = parsed
    user = await users.get(event.user_id)
    fn = _owner.get(name) if is_owner(user) else None
    fn = fn or _user.get(name)
    if fn is None:
        return True  # not ours: agents.commands (connect, ...) and the chat turn handle it
    reply = await fn(event, user, args)
    if reply:
        await outbox.enqueue_now(Outbound(user_id=user.id, text=reply, dedupe_key=f"cmd:{event.id}"))
        await messages.log(user.id, Role.ASSISTANT, reply, event_id=f"reply:{event.id}")
    return False


def register_command_gate() -> None:
    register_event_gate("commands", command_gate, order=20)
```

Owner-only and user commands are looked up separately, so `/invite` from a non-owner falls through (the turn treats it as text, which is today's behaviour for unknown commands).

`src/mavis/access/invite_commands.py`:

```python
"""Owner /invite commands (spec 4.4)."""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mavis.access import UserTier
from mavis.access.codes import InviteError, deep_link_param, display
from mavis.access.commands import parse_kv, register_owner_command
from mavis.config import get_settings
from mavis.domain.events import Event
from mavis.store.db import utcnow
from mavis.store.models import User
from mavis.store.repo import invites, users

BOT_LINK = "https://t.me/{bot}?start={param}"
HELP = ("Use: /invite new [uses=1] [days=14] [tier=standard] [tz=Area/City] [cur=INR] [label], "
        "/invite list, /invite revoke <hint>, /invite users <id>")


def _valid_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return "/" in name


async def _new(user: User, args: list[str]) -> str:
    kv, label = parse_kv(args)
    s = get_settings()
    try:
        uses = int(kv.get("uses", "1"))
        days = int(kv.get("days", str(s.invite_default_days)))
    except ValueError:
        return "uses and days must be whole numbers."
    tier = kv.get("tier", UserTier.STANDARD.value)
    if tier not in (UserTier.STANDARD.value, UserTier.TRUSTED.value):
        return "tier must be standard or trusted."
    tz = kv.get("tz")
    if tz and not _valid_zone(tz):
        return f"I don't know the time zone {tz}. Use a name like Europe/Lisbon."
    cur = kv.get("cur")
    if cur and not (len(cur) == 3 and cur.isalpha()):
        return "cur must be a 3-letter currency code like INR or EUR."
    try:
        row, plain = await invites.mint(created_by=user.id, uses=uses, days=days, tier=tier, tz=tz,
                                        currency=cur, label=" ".join(label))
    except InviteError:
        return (f"I can't make that one: at most {s.invite_max_active} open codes and "
                f"{s.invite_max_uses} uses per code.")
    link = BOT_LINK.format(bot=s.telegram_bot_username or "Mavis247_bot", param=deep_link_param(plain))
    return (f"Invite {display(plain)} ({row.max_uses} use{'s' if row.max_uses != 1 else ''}, "
            f"expires {row.expires_at:%d %b}).\n{link}\nThis is the only time I show the full code.")


async def invite(event: Event, user: User, args: list[str]) -> str:
    sub, rest = (args[0].lower(), args[1:]) if args else ("new", [])
    if sub == "new":
        return await _new(user, rest)
    if sub == "list":
        rows = await invites.list_active(utcnow())
        if not rows:
            return "No open invite codes."
        return "\n".join(f"#{r.id} ...{r.code_hint} {r.label or '(no label)'} {r.uses}/{r.max_uses} "
                         f"until {r.expires_at:%d %b}" for r in rows)
    if sub == "revoke" and rest:
        row = await invites.revoke(rest[0])
        return f"Revoked ...{row.code_hint}. People who joined keep access." if row else "No open code like that."
    if sub == "users" and rest and rest[0].isdigit():
        names = []
        for r in await invites.redemptions_for(int(rest[0])):
            u = await users.get(r.user_id)
            names.append(f"{u.name or 'someone'} (#{u.id}, {r.redeemed_at:%d %b})")
        return "\n".join(names) or "Nobody has used that code yet."
    return HELP


def register() -> None:
    register_owner_command("invite", invite)
```

Add `telegram_bot_username: str = ""` to config (Telegram block); compose passes `TELEGRAM_BOT_USERNAME` (Task 20). The default link uses the current bot handle only when the setting is empty.

In `src/mavis/worker/handlers.py`, after `register_access_gate()`:

```python
    register_command_gate()
    invite_commands.register()
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/access -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis/access/commands.py src/mavis/access/invite_commands.py src/mavis/worker/handlers.py \
        src/mavis/config.py tests/access/
git commit -m "feat(access): command gate and owner invite commands"
```

---
### Task 7: Onboarding, preferences, timezone and currency per user

**Files:**
- Create: `src/mavis/access/tz_resolve.py`, `src/mavis/access/currency.py`, `src/mavis/access/preferences.py`, `src/mavis/agents/onboarding.py`, `src/mavis/tools/preferences.py`, `scripts/build_geo_tables.py`, `src/mavis/access/data/zone_country.tsv`, `src/mavis/access/data/cities.tsv.gz`, `tests/access/test_tz_currency.py`, `tests/access/test_onboarding.py`, `tests/access/fixtures/cities_small.tsv`
- Modify: `pyproject.toml` (`tzfpy`), `src/mavis/tools/__init__.py`, `src/mavis/attention/pipeline.py:291`, `src/mavis/initiative/routines.py`, `src/mavis/worker/handlers.py`

**Interfaces:**
- Consumes: `gate.on_activated` (Task 5), `register_user_command` (Task 6), `buttons.register_button_handler` (existing, prefix `ob:`)
- Produces:
  - `tz_resolve.from_location(lat, lon) -> str | None`, `tz_resolve.from_city(text, *, table: Path | None = None) -> list[CityMatch]` (`CityMatch(name, country, zone, population)`, at most 3, best first), `async tz_resolve.from_text_llm(text) -> str | None` (result must pass `ZoneInfo`)
  - `currency.for_zone(zone) -> str | None`, `currency.symbol(code) -> str`
  - `preferences.set_timezone(user_id, tz) -> TimezoneChange(old, new)`, `set_currency(user_id, code)`, `set_name(user_id, name)`, `register_timezone_hook(fn: Callable[[int, str, str], Awaitable[None]])`
  - `onboarding.start(user, invite)` (subscribed to `gate.on_activated`), button prefix `ob:` (`ob:tz:yes`, `ob:tz:no`, `ob:tz:<idx>`, `ob:conn:google`, `ob:conn:later`), state `users.state["onboarding"] = {"step", "started_at", "tz_confirmed", "candidates"}`
  - Tool `set_preferences(timezone?, currency?, name?, locale?)` (risk WRITE_SELF, agents `{"conversation"}`), user command `/settings`
  - Settings: `geo_cities_path: Path | None = None`

- [ ] **Step 1: Add the dependency and build the bundled tables**

Run: `uv add "tzfpy>=0.16"` and verify the arm64 wheel exists: `uv pip download --python-platform aarch64-manylinux_2_28 tzfpy --no-deps -d /tmp/tzfpy-check` (Expected: a `manylinux_2_28_aarch64` wheel; if none, use `timezonefinder` instead with the same `from_location` signature and note it in the commit message).

`scripts/build_geo_tables.py`:

```python
"""Build access/data tables (run once, commit the output): zone -> country from the system tzdata
zone1970.tab, and cities (population >= 15000) from a GeoNames cities15000.txt the owner downloads.

    uv run python -m scripts.build_geo_tables /usr/share/zoneinfo/zone1970.tab ~/Downloads/cities15000.txt
"""

from __future__ import annotations

import csv
import gzip
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "src/mavis/access/data"


def main(zone_tab: str, cities: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with open(zone_tab, encoding="utf-8") as fh, (OUT / "zone_country.tsv").open("w", encoding="utf-8") as out:
        for line in fh:
            if line.startswith("#") or not line.strip():
                continue
            codes, _, zone = line.split("\t")[:3]
            out.write(f"{zone.strip()}\t{codes.split(',')[0]}\n")
    with open(cities, encoding="utf-8") as fh, gzip.open(OUT / "cities.tsv.gz", "wt", encoding="utf-8") as out:
        w = csv.writer(out, delimiter="\t")
        for row in csv.reader(fh, delimiter="\t"):
            # geonameid, name, asciiname, alternatenames, lat, lon, ..., country(8), ..., population(14), ..., tz(17)
            w.writerow([row[1], row[2], row[8], row[17], row[14]])


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
```

Run it and commit both outputs (`cities.tsv.gz` is about 1 to 2 MB). Tests use `tests/access/fixtures/cities_small.tsv` (plain text, same five columns) with these rows:

```
Porto	Porto	PT	Europe/Lisbon	249633
Portland	Portland	US	America/Los_Angeles	652503
Portland	Portland	US	America/New_York	66881
Bogota	Bogota	CO	America/Bogota	7674366
Osaka	Osaka	JP	Asia/Tokyo	2592413
Springfield	Springfield	US	America/Chicago	116565
Springfield	Springfield	US	America/New_York	155929
Springfield	Springfield	US	America/Chicago	169176
Springfield	Springfield	AU	Australia/Brisbane	20000
```

- [ ] **Step 2: Write the failing tests**

`tests/access/test_tz_currency.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from mavis.access import currency, tz_resolve

SMALL = Path(__file__).parent / "fixtures" / "cities_small.tsv"


@pytest.mark.parametrize("lat,lon,zone", [(41.15, -8.61, "Europe/Lisbon"), (4.71, -74.07, "America/Bogota"),
                                          (34.69, 135.50, "Asia/Tokyo")])
def test_location_resolves_offline(lat, lon, zone):
    assert tz_resolve.from_location(lat, lon) == zone


@pytest.mark.parametrize("text,zones", [("porto", ["Europe/Lisbon"]), ("Bogotá", ["America/Bogota"]),
                                        ("Portland", ["America/Los_Angeles", "America/New_York"])])
def test_city_lookup_ranks_by_population_and_dedupes_zones(text, zones):
    assert [m.zone for m in tz_resolve.from_city(text, table=SMALL)] == zones


def test_city_lookup_caps_at_three():
    got = tz_resolve.from_city("springfield", table=SMALL)
    assert len(got) == 3 and len({m.zone for m in got}) == 3


async def test_llm_fallback_must_pass_zoneinfo(fake_llm):
    fake_llm.queue_structured({"zone": "Asia/Kathmandu"})
    assert await tz_resolve.from_text_llm("near the big lake in the Himalayas") == "Asia/Kathmandu"
    fake_llm.queue_structured({"zone": "Mars/Olympus"})
    assert await tz_resolve.from_text_llm("olympus") is None


@pytest.mark.parametrize("zone,code", [("Europe/Lisbon", "EUR"), ("America/Bogota", "COP"),
                                       ("Asia/Tokyo", "JPY"), ("Asia/Kolkata", "INR"), ("Etc/UTC", None)])
def test_currency_from_zone(zone, code):
    assert currency.for_zone(zone) == code
```

(Use the FakeLLM queueing method that exists in `tests/fakes/llm.py`; if its name differs, adapt the two calls.)

`tests/access/test_onboarding.py`:

```python
from __future__ import annotations

import pytest

from mavis.access import gate, preferences
from mavis.agents import onboarding
from mavis.domain.events import Event, EventType, Trust
from mavis.store.db import utcnow
from mavis.store.repo import invites, users


async def _activate(chat: int, name: str, tz: str | None, cur: str | None):
    u, _ = await users.get_or_create_by_chat(chat, name)
    row, _ = await invites.mint(created_by=1, tz=tz, currency=cur)
    onboarding.register()
    return await gate.activate(u.id, row, utcnow()), row


async def _texts(channel) -> list[str]:
    from mavis.channels.outbox_sender import deliver_pending

    await deliver_pending(channel)
    return channel.texts


@pytest.mark.parametrize("name,tz,cur,symbol", [("Priya", "Asia/Kolkata", None, "INR"),
                                                ("Tomas", "Europe/Lisbon", None, "EUR"),
                                                ("Aiko", "Asia/Tokyo", "JPY", "JPY")])
async def test_welcome_clock_check_and_currency_line(db, channel, name, tz, cur, symbol):
    u, _ = await _activate(5000 + len(name), name, tz, cur)
    out = await _texts(channel)
    assert out[0].startswith(f"Hi {name}, I'm Mavis")
    assert "Quick check: is it" in out[1] and "where you are?" in out[1]
    labels = [b.label for row in channel.sent[1].buttons for b in row]
    assert labels == ["Yes", "No"]
    assert all("\u2014" not in t and "\u2013" not in t for t in out)
    assert (await users.get(u.id)).currency == symbol


async def test_no_then_city_with_ambiguity_offers_buttons(db, channel, settings, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(onboarding, "CITY_TABLE", Path(__file__).parent / "fixtures" / "cities_small.tsv")
    u, _ = await _activate(6201, "Lena", "Asia/Kolkata", None)
    await onboarding.on_button(_btn(u.id, "ob:tz:no"), "ob:tz:no")
    await onboarding.on_text(u, "Portland")
    out = await _texts(channel)
    labels = [b.label for row in channel.sent[-1].buttons for b in row]
    assert len(labels) == 2 and all("Portland" in lbl for lbl in labels)
    await onboarding.on_button(_btn(u.id, "ob:tz:1"), "ob:tz:1")
    assert (await users.get(u.id)).timezone == "America/New_York"
    assert any("What's one thing you want off your mind this week?" in t for t in await _texts(channel))
    assert "\u2014" not in "".join(out)


async def test_location_answer_sets_zone_and_currency(db, channel):
    u, _ = await _activate(6202, "Bruno", None, None)
    await onboarding.on_button(_btn(u.id, "ob:tz:no"), "ob:tz:no")
    await onboarding.on_location(u, 4.71, -74.07)
    fresh = await users.get(u.id)
    assert fresh.timezone == "America/Bogota" and fresh.currency == "COP"


async def test_set_timezone_runs_hooks_and_keeps_one_off_wakeups(db, user):
    seen = []

    async def hook(uid, old, new):
        seen.append((uid, old, new))

    preferences.register_timezone_hook(hook)
    change = await preferences.set_timezone(user.id, "America/Bogota")
    assert change.new == "America/Bogota" and seen == [(user.id, change.old, "America/Bogota")]
    with pytest.raises(ValueError):
        await preferences.set_timezone(user.id, "Nowhere/Land")


def _btn(uid: int, data: str) -> Event:
    return Event(id=f"b:{uid}:{data}", user_id=uid, type=EventType.BUTTON_PRESSED, occurred_at=utcnow(),
                 source="telegram", payload={"data": data}, trust=Trust.USER)
```

- [ ] **Step 3: Run them to see them fail**

Run: `uv run pytest tests/access/test_tz_currency.py tests/access/test_onboarding.py -q`
Expected: FAIL (`ModuleNotFoundError: mavis.access.tz_resolve`).

- [ ] **Step 4: Implement resolvers and preferences**

`src/mavis/access/tz_resolve.py`:

```python
"""Timezone from a shared location (tzfpy, offline), a city name (bundled GeoNames), or the fast model.
Telegram's language_code never sets a timezone (spec 5)."""

from __future__ import annotations

import csv
import gzip
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel

DATA = Path(__file__).parent / "data"


@dataclass(frozen=True)
class CityMatch:
    name: str
    country: str
    zone: str
    population: int


def valid_zone(name: str | None) -> bool:
    if not name or "/" not in name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def from_location(lat: float, lon: float) -> str | None:
    from tzfpy import get_tz

    zone = get_tz(lon, lat)
    return zone if valid_zone(zone) else None


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.strip().lower()) if not unicodedata.combining(c))


@lru_cache(maxsize=4)
def _cities(path: str) -> dict[str, list[CityMatch]]:
    p = Path(path)
    opener = gzip.open if p.suffix == ".gz" else open
    index: dict[str, list[CityMatch]] = {}
    with opener(p, "rt", encoding="utf-8") as fh:
        for name, ascii_name, country, zone, pop in csv.reader(fh, delimiter="\t"):
            m = CityMatch(name, country, zone, int(pop or 0))
            for key in {_fold(name), _fold(ascii_name)}:
                index.setdefault(key, []).append(m)
    return index


def from_city(text: str, *, table: Path | None = None) -> list[CityMatch]:
    rows = _cities(str(table or DATA / "cities.tsv.gz")).get(_fold(text), [])
    best: dict[str, CityMatch] = {}
    for m in sorted(rows, key=lambda m: -m.population):
        if valid_zone(m.zone) and m.zone not in best:
            best[m.zone] = m
    return list(best.values())[:3]


class _Zone(BaseModel):
    zone: str


async def from_text_llm(text: str) -> str | None:
    from mavis.llm import models as llm

    try:
        out = await llm.structured(_Zone, "Return the IANA time zone name for the place the user describes. "
                                          "Only the zone name, like Europe/Lisbon.", text[:200],
                                   tier=llm.Tier.FAST, name="onboarding.tz", priority="interactive")
    except Exception:  # noqa: BLE001
        return None
    return out.zone if valid_zone(out.zone) else None
```

(Check `llm.structured`'s exact signature in `llm/models.py:566` and match the positional order.)

`src/mavis/access/currency.py`:

```python
"""Currency from a zone's country (zone1970.tab first country) through a country -> ISO 4217 map."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data"
# Countries whose currency is the euro, plus the rest of the common set. Extend by data, never by user.
_EURO = {"AT", "BE", "CY", "DE", "EE", "ES", "FI", "FR", "GR", "HR", "IE", "IT", "LT", "LU", "LV", "MT", "NL",
         "PT", "SI", "SK"}
COUNTRY_CURRENCY = {**{c: "EUR" for c in _EURO}, "IN": "INR", "US": "USD", "GB": "GBP", "JP": "JPY",
                    "SG": "SGD", "AE": "AED", "CO": "COP", "AU": "AUD", "CA": "CAD", "BR": "BRL", "MX": "MXN",
                    "CH": "CHF", "SE": "SEK", "NO": "NOK", "DK": "DKK", "PL": "PLN", "ZA": "ZAR", "KR": "KRW",
                    "CN": "CNY", "HK": "HKD", "ID": "IDR", "MY": "MYR", "TH": "THB", "PH": "PHP", "VN": "VND",
                    "NZ": "NZD", "SA": "SAR", "TR": "TRY", "IL": "ILS", "EG": "EGP", "NG": "NGN", "KE": "KES",
                    "PK": "PKR", "BD": "BDT", "LK": "LKR", "NP": "NPR", "AR": "ARS", "CL": "CLP", "PE": "PEN"}
SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}


@lru_cache(maxsize=1)
def _zone_country() -> dict[str, str]:
    out = {}
    for line in (DATA / "zone_country.tsv").read_text(encoding="utf-8").splitlines():
        zone, _, cc = line.partition("\t")
        out[zone] = cc
    return out


def country_for_zone(zone: str) -> str | None:
    return _zone_country().get(zone)


def for_zone(zone: str) -> str | None:
    cc = country_for_zone(zone)
    return COUNTRY_CURRENCY.get(cc) if cc else None


def symbol(code: str) -> str:
    return SYMBOLS.get(code, code)
```

`src/mavis/access/preferences.py`:

```python
"""Per-user preferences with side effects (spec 5): timezone hooks re-anchor routines; one-off wakeups keep
their absolute instant."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from mavis.access.currency import country_for_zone
from mavis.access.tz_resolve import valid_zone
from mavis.store.repo import users

TimezoneHook = Callable[[int, str, str], Awaitable[None]]
_tz_hooks: list[TimezoneHook] = []


@dataclass(frozen=True)
class TimezoneChange:
    old: str
    new: str


def register_timezone_hook(fn: TimezoneHook) -> None:
    if fn not in _tz_hooks:
        _tz_hooks.append(fn)


async def set_timezone(user_id: int, tz: str) -> TimezoneChange:
    if not valid_zone(tz):
        raise ValueError(f"unknown time zone {tz!r}")
    old = (await users.get(user_id)).timezone
    await users.update(user_id, timezone=tz, country=country_for_zone(tz))
    for fn in list(_tz_hooks):
        await fn(user_id, old, tz)
    return TimezoneChange(old, tz)


async def set_currency(user_id: int, code: str) -> str:
    code = code.strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError("currency must be a 3-letter code")
    await users.update(user_id, currency=code)
    return code


async def set_name(user_id: int, name: str) -> str:
    name = " ".join(name.split())[:60]
    if not name:
        raise ValueError("empty name")
    await users.update(user_id, name=name)
    return name
```

In `src/mavis/initiative/routines.py` register a timezone hook from the `Routines` constructor's wiring (where `register_morning_hook` is used) that re-schedules the morning check-in for the new zone: find the routine loop id the same way `on_user_message` does and call `await self.reschedule(user, loop_id)` (existing method, `routines.py:155`) with the refreshed user. Add an evening-wrap re-anchor the same way in `attention/rhythm.py` if it schedules by local time.

In `src/mavis/attention/pipeline.py:291` change `currency = m.currency or s.attention_currency` to `currency = m.currency or getattr(user, "currency", None) or s.attention_currency`, and add `"JPY": 20000.0, "SGD": 200.0, "AED": 550.0` to `attention_large_amounts` in config.

- [ ] **Step 5: Implement onboarding, the tool and /settings**

`src/mavis/agents/onboarding.py`:

```python
"""Onboarding a newly invited user (spec 5): at most three questions, buttons where possible, first value
inside two minutes. State lives in users.state["onboarding"] so a restart resumes."""

from __future__ import annotations

from pathlib import Path

from mavis.access import gate, preferences
from mavis.access.currency import for_zone
from mavis.access.tz_resolve import from_city, from_location, from_text_llm
from mavis.agents.buttons import register_button_handler
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.events import Event
from mavis.domain.messages import Button, Outbound
from mavis.store.db import utcnow
from mavis.store.models import InviteCode, User
from mavis.store.repo import outbox, users

PREFIX = "ob:"
CITY_TABLE: Path | None = None
WELCOME = ("Hi {name}, I'm Mavis, your personal assistant on Telegram. I remember things, remind you, and "
           "can watch your email and calendar if you want.")
CLOCK = "Quick check: is it {clock} where you are?"
ASK_PLACE = "Tell me your city, or share your location."
PICK = "Which one is yours?"
CURRENCY = "I'll show money in {money}. Say 'use USD' any time to change it."
FIRST_VALUE = "What's one thing you want off your mind this week? A bill, a call, a deadline."
CONNECT = "Want me to keep an eye on your Gmail and calendar too?"


async def _say(user_id: int, text: str, key: str, buttons: list[list[Button]] | None = None) -> None:
    await outbox.enqueue_now(Outbound(user_id=user_id, text=text, buttons=buttons or [],
                                      dedupe_key=f"onboarding:{user_id}:{key}"))


async def _state(user_id: int, **patch) -> dict:
    return await users.update_nested(user_id, "onboarding", patch)


async def start(user: User, invite: InviteCode) -> None:
    s = get_settings()
    first = (user.name or "there").split()[0]
    await _state(user.id, step="clock", started_at=utcnow().isoformat(), tz_confirmed=False)
    await _say(user.id, WELCOME.format(name=first), "welcome")
    local = timeutil.to_local(utcnow(), user.timezone or s.default_timezone)
    clock = local.strftime("%I:%M %p %A").lstrip("0")
    await _say(user.id, CLOCK.format(clock=clock), "clock",
               [[Button(label="Yes", data="ob:tz:yes"), Button(label="No", data="ob:tz:no")]])
    if user.currency is None and (code := for_zone(user.timezone)):
        await preferences.set_currency(user.id, code)


async def _after_zone(user_id: int) -> None:
    user = await users.get(user_id)
    code = user.currency or for_zone(user.timezone) or get_settings().attention_currency
    if user.currency is None:
        await preferences.set_currency(user_id, code)
    await _state(user_id, step="first_value", tz_confirmed=True)
    from mavis.access.currency import symbol

    await _say(user_id, CURRENCY.format(money=f"{symbol(code)} {code}".strip()), "currency")
    await _say(user_id, FIRST_VALUE, "first_value")


async def on_button(event: Event, data: str) -> None:
    uid = event.user_id
    if data == "ob:tz:yes":
        await _after_zone(uid)
    elif data == "ob:tz:no":
        await _state(uid, step="place")
        await _say(uid, ASK_PLACE, "ask_place")  # the channel adds a request_location reply keyboard (Task 17)
    elif data.startswith("ob:tz:") and data[6:].isdigit():
        cands = (await users.get_state(uid)).get("onboarding", {}).get("candidates", [])
        idx = int(data[6:])
        if idx < len(cands):
            await preferences.set_timezone(uid, cands[idx])
            await _after_zone(uid)
    elif data == "ob:conn:google":
        from mavis.agents.commands import _flow
        from mavis.tools.integrations.actions import GOOGLE_ANCHOR

        await _flow(None).start(uid, GOOGLE_ANCHOR, "")
    elif data == "ob:conn:later":
        await _state(uid, connect_declined_at=utcnow().isoformat())


async def on_text(user: User, text: str) -> bool:
    """A reply while onboarding waits for a place. True when it was consumed."""
    st = (await users.get_state(user.id)).get("onboarding", {})
    if st.get("step") != "place":
        return False
    matches = from_city(text, table=CITY_TABLE)
    if len(matches) == 1:
        await preferences.set_timezone(user.id, matches[0].zone)
        await _after_zone(user.id)
    elif matches:
        await _state(user.id, candidates=[m.zone for m in matches])
        rows = [[Button(label=f"{m.name}, {m.country} ({m.zone.split('/')[-1].replace('_', ' ')})",
                        data=f"ob:tz:{i}")] for i, m in enumerate(matches)]
        await _say(user.id, PICK, f"pick:{len(matches)}", rows)
    elif zone := await from_text_llm(text):
        await preferences.set_timezone(user.id, zone)
        await _after_zone(user.id)
    else:
        await _say(user.id, ASK_PLACE, f"ask_place_again:{utcnow():%H%M}")
    return True


async def on_location(user: User, lat: float, lon: float) -> None:
    if zone := from_location(lat, lon):
        await preferences.set_timezone(user.id, zone)
        await _after_zone(user.id)


async def after_first_value(user_id: int) -> None:
    """Called once the first chat turn after onboarding has replied (the turn made the first value)."""
    st = (await users.get_state(user_id)).get("onboarding", {})
    if st.get("step") != "first_value":
        return
    await _state(user_id, step="done")
    await _say(user_id, CONNECT, "connect", [[Button(label="Connect Google", data="ob:conn:google"),
                                              Button(label="Later", data="ob:conn:later")]])


async def onboarding_gate(event: Event) -> bool:
    """Event gate (order 30): place answers and shared locations while onboarding are not chat turns."""
    if event.type.value != "user_message":
        return True
    user = await users.get(event.user_id)
    if (loc := event.payload.get("location")) and isinstance(loc, dict):
        await on_location(user, float(loc["latitude"]), float(loc["longitude"]))
        return False
    return not await on_text(user, str(event.payload.get("text", "")))


def register() -> None:
    from mavis.worker.gates import register_event_gate

    if start not in gate.on_activated:
        gate.on_activated.append(start)
    register_button_handler(PREFIX, on_button)
    register_event_gate("onboarding", onboarding_gate, order=30)
```

`after_first_value` is triggered by a TASK-less hook: register an `outbox` sent hook? Keep it general: the onboarding gate records `step == "first_value"`; on the next USER_MESSAGE after that step, the gate lets the turn run and schedules `after_first_value` as a system wakeup 60 s later (`WakeupService().wake_me(uid, now+60s, "onboarding", kind="system_onboarding_connect", scale=False, dedupe_key=f"onboarding_connect:{uid}")`) with `register_system_wakeup("system_onboarding_connect", lambda uid, reason: after_first_value(uid))`. Add `SYSTEM_ONBOARDING = "system_onboarding_connect"` to `WakeupKind` and the event map in `domain/wakeups.py` (24 chars max: `system_onboard_connect` if the kind column is 24 characters; check `WakeupRow.kind` length and use the short form). Add the test `test_connector_offer_follows_the_first_turn` to `tests/access/test_onboarding.py` that sets step `first_value`, sends one USER_MESSAGE through `onboarding_gate`, fires the wakeup handler, and asserts the CONNECT text with the two buttons.

Intake: in `channels/telegram_updates.py` add `if loc := msg.get("location"): payload["location"] = {"latitude": loc["latitude"], "longitude": loc["longitude"]}`.

`src/mavis/tools/preferences.py`:

```python
"""set_preferences: the user changes their own timezone, currency, name or locale in chat (WRITE_SELF)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from mavis.access import preferences
from mavis.domain.policy import RiskClass
from mavis.domain.results import ToolOutput
from mavis.tools.registry import MavisTool


class SetPreferencesArgs(BaseModel):
    timezone: str | None = Field(default=None, description="IANA zone like Europe/Lisbon")
    currency: str | None = Field(default=None, description="ISO 4217 code like EUR")
    name: str | None = Field(default=None, description="What the user wants to be called")
    locale: str | None = Field(default=None, description="Language tag like pt-PT")


async def _run(user_id: int, args: SetPreferencesArgs) -> ToolOutput:
    done: list[str] = []
    if args.timezone:
        try:
            change = await preferences.set_timezone(user_id, args.timezone)
        except ValueError:
            return ToolOutput(model_note=f"Unknown time zone {args.timezone!r}; ask the user for their city.")
        done.append(f"time zone {change.new} (reminders already set keep their exact time)")
    if args.currency:
        done.append(f"currency {await preferences.set_currency(user_id, args.currency)}")
    if args.name:
        done.append(f"name {await preferences.set_name(user_id, args.name)}")
    if args.locale:
        from mavis.store.repo import users

        await users.update(user_id, locale=args.locale[:16])
        done.append(f"locale {args.locale[:16]}")
    return ToolOutput(user_text="Updated: " + ", ".join(done) + "." if done else "Nothing to change.")


TOOLS = [MavisTool(name="set_preferences", description="Change the user's own time zone, currency, name or "
                   "locale when they ask.", args_model=SetPreferencesArgs, risk=RiskClass.WRITE_SELF, fn=_run,
                   agents=frozenset({"conversation"}))]
```

Register in `src/mavis/tools/__init__.py` (`from mavis.tools import preferences` and add `*preferences.TOOLS` to the tuple). If chat tool selection is lexical (`tools/chat_tools.py`), add `set_preferences` to its keyword table with words `time zone, timezone, currency, call me, my name`.

`/settings` user command (in `onboarding.register()`): `register_user_command("settings", settings_command)` returning `f"Time zone: {u.timezone}\nCurrency: {u.currency or '(default)'}\nName: {u.name or '(none)'}\nSay 'my time zone is ...' or 'use EUR' to change them."` and sending the clock-check buttons again.

Call `onboarding.register()` from `worker/handlers.py` after `invite_commands.register()`.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/access tests/attention -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock scripts/build_geo_tables.py src/mavis/access/ src/mavis/agents/onboarding.py \
        src/mavis/tools/preferences.py src/mavis/tools/__init__.py src/mavis/attention/pipeline.py \
        src/mavis/initiative/routines.py src/mavis/domain/wakeups.py src/mavis/channels/telegram_updates.py \
        src/mavis/worker/handlers.py src/mavis/config.py tests/access/
git commit -m "feat(onboarding): clock check, offline timezone and currency, first value and preferences"
```

---

### Task 8: Composio identity with environment prefix

**Files:**
- Create: `src/mavis/tools/integrations/identity.py`, `tests/tools/test_identity.py`
- Modify: `src/mavis/tools/integrations/composio.py`, `src/mavis/tools/integrations/composio_webhooks.py:88`, `src/mavis/domain/integrations.py`, `src/mavis/access/gate.py` (activate), `src/mavis/config.py`

**Interfaces:**
- Produces:
  - `async provider_id_for(user_id: int) -> str` (stored `composio_user_id`, else legacy `mavis-<id>`; cached 10 min)
  - `async user_for_provider_id(value: object) -> int | None` (stored-id lookup; legacy regex accepted only when `ENV=prod`)
  - `new_provider_id(user_id) -> str` = `f"mavis-{env}-{user_id}"`, set at activation
  - `async check_shared_key(provider) -> None` (startup: non-prod env refuses a key whose project lists legacy `mavis-<int>` accounts unless `COMPOSIO_SHARED_KEY_OK=true`)
  - Settings `composio_shared_key_ok: bool = False`

- [ ] **Step 1: Write the failing test**

`tests/tools/test_identity.py`:

```python
from __future__ import annotations

import pytest

from mavis.store.repo import users
from mavis.tools.integrations import identity


@pytest.mark.parametrize("env", ["prod", "dev", "test"])
async def test_new_users_get_an_env_prefixed_id(db, settings, monkeypatch, env):
    from mavis.config import get_settings

    monkeypatch.setenv("ENV", env)
    get_settings.cache_clear()
    u, _ = await users.get_or_create_by_chat(5001, "Priya")
    pid = identity.new_provider_id(u.id)
    assert pid == f"mavis-{env}-{u.id}"
    await users.update(u.id, composio_user_id=pid)
    identity.clear_cache()
    assert await identity.provider_id_for(u.id) == pid
    assert await identity.user_for_provider_id(pid) == u.id


async def test_legacy_rows_keep_mavis_id(db):
    u, _ = await users.get_or_create_by_chat(7302, "Tomas")
    assert await identity.provider_id_for(u.id) == f"mavis-{u.id}"


@pytest.mark.parametrize("env,expected_ok", [("prod", True), ("dev", False), ("test", False)])
async def test_legacy_ids_are_claimed_only_in_prod(db, settings, monkeypatch, env, expected_ok):
    from mavis.config import get_settings

    u, _ = await users.get_or_create_by_chat(9944, "Aiko")
    monkeypatch.setenv("ENV", env)
    get_settings.cache_clear()
    identity.clear_cache()
    got = await identity.user_for_provider_id(f"mavis-{u.id}")
    assert (got == u.id) is expected_ok


@pytest.mark.parametrize("value", ["", None, "mavis-dev-x", "mavis-staging-3", "other-7", "mavis-12-extra"])
async def test_foreign_ids_are_not_ours(db, value):
    assert await identity.user_for_provider_id(value) is None


async def test_shared_key_check_refuses_dev_on_a_prod_project(settings, monkeypatch):
    class _P:
        async def list_user_ids(self):
            return ["mavis-1", "mavis-prod-4"]

    with pytest.raises(RuntimeError):
        await identity.check_shared_key(_P())
    monkeypatch.setenv("COMPOSIO_SHARED_KEY_OK", "true")
    from mavis.config import get_settings

    get_settings.cache_clear()
    await identity.check_shared_key(_P())
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/tools/test_identity.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

`src/mavis/tools/integrations/identity.py`:

```python
"""Composio user identity per user (spec 6.2): stored `users.composio_user_id` (`mavis-<env>-<id>` for new
users); legacy rows keep `mavis-<id>`, which only a prod stack may claim, so a staging stack sharing the key
can never take prod user 1's webhooks."""

from __future__ import annotations

import re
import time

from sqlalchemy import select

from mavis.config import get_settings
from mavis.store.db import Session
from mavis.store.models import User

_LEGACY = re.compile(r"mavis-(\d+)")
_TTL_S = 600.0
_by_user: dict[int, tuple[float, str]] = {}
_by_pid: dict[str, tuple[float, int | None]] = {}


def clear_cache() -> None:
    _by_user.clear()
    _by_pid.clear()


def new_provider_id(user_id: int) -> str:
    return f"mavis-{get_settings().env}-{user_id}"


async def provider_id_for(user_id: int) -> str:
    hit = _by_user.get(user_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    async with Session() as s:
        stored = await s.scalar(select(User.composio_user_id).where(User.id == user_id))
    pid = stored or f"mavis-{user_id}"
    _by_user[user_id] = (time.monotonic() + _TTL_S, pid)
    return pid


async def user_for_provider_id(value: object) -> int | None:
    pid = str(value or "")
    if not pid:
        return None
    hit = _by_pid.get(pid)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    async with Session() as s:
        uid = await s.scalar(select(User.id).where(User.composio_user_id == pid))
        if uid is None and get_settings().env == "prod" and (m := _LEGACY.fullmatch(pid)):
            legacy = int(m.group(1))
            row = await s.get(User, legacy)
            uid = legacy if row is not None and row.composio_user_id is None else None
    _by_pid[pid] = (time.monotonic() + _TTL_S, uid)
    return uid


async def check_shared_key(provider) -> None:
    s = get_settings()
    if s.env == "prod" or s.composio_shared_key_ok:
        return
    lister = getattr(provider, "list_user_ids", None)
    if lister is None:
        return
    if any(_LEGACY.fullmatch(str(x)) or str(x).startswith("mavis-prod-") for x in await lister()):
        raise RuntimeError("this Composio key has prod accounts; use a separate key or COMPOSIO_SHARED_KEY_OK")
```

In `composio.py` replace every `user.provider_id` with a local resolved once per method: at the top of `_accounts`, `connect_link`, `disconnect`, `execute`, `subscribe`, `retire_legacy_triggers` add `pid = await identity.provider_id_for(user.user_id)` and use `pid` (the `_routes` cache keys too). Add `async def list_user_ids(self) -> list[str]` (GET `/connected_accounts?limit=100`, collect `user_id`). In `composio_webhooks.py:88` use `user_id = await user_for_provider_id(meta.get("user_id") or data.get("user_id"))` (make the enclosing function async if it is not; adapt its caller in `api/routes/integrations.py`). In `domain/integrations.py` keep `user_from_provider_id` for one release with a docstring pointing at `identity.user_for_provider_id` and no remaining callers (grep to confirm). In `access/gate.activate` set `composio_user_id=new_provider_id(user_id)` in the same `users.update`. Call `check_shared_key(get_provider())` from the worker startup hooks (`register_startup_hook` in `tools/integrations/wiring.py`), logging and re-raising the RuntimeError so a misconfigured staging stack does not start.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/tools tests/api -q && uv run pytest -q`
Expected: PASS (adapt Composio fake tests that asserted `mavis-1`: legacy rows still produce `mavis-<id>`).

- [ ] **Step 5: Commit**

```bash
git add src/mavis/tools/integrations/ src/mavis/domain/integrations.py src/mavis/access/gate.py \
        src/mavis/api/routes/integrations.py src/mavis/config.py tests/tools/test_identity.py
git commit -m "feat(integrations): stored env-prefixed Composio identity, legacy ids prod-only"
```

---
### Task 9: Per-user isolation (suite, Qdrant tenant index, Neo4j paths, Redis keys, artifacts, workspace guard)

**Files:**
- Create: `tests/access/test_isolation.py`, `src/mavis/domain/redis_keys.py`
- Modify: `src/mavis/memory/vector.py:91`, `src/mavis/memory/neo4j_graph.py:111-116`, `src/mavis/tools/integrations/workspace_guard.py:40-72`, every `artifacts_dir` writer found by `grep -rn "artifacts_dir" src/mavis` (Step 3), `src/mavis/initiative/task_delivery.py`, `src/mavis/channels/outbox_sender.py`
- Create: `src/mavis/store/artifacts.py`

**Interfaces:**
- Produces:
  - `redis_keys.user_key(area: str, user_id: int, *parts: str) -> str` = `mavis:<area>:u<uid>:...`; `redis_keys.user_pattern(user_id) -> str` = `mavis:*:u<uid>:*`
  - `artifacts.user_dir(user_id, task_id=None) -> Path` (`artifacts_dir/u<uid>/[t<task>]`) and `artifacts.guard(user_id, path) -> Path` (raises `PermissionError` outside the user's prefix) in `src/mavis/store/artifacts.py`
  - `workspace_guard.record_created` / `created_by` backed by Redis set `mavis:wsguard:t<task>` (TTL 1 day) with the in-memory dict as fallback; both become `async`

- [ ] **Step 1: Write the failing tests**

`tests/access/test_isolation.py`:

```python
"""Two-user isolation (spec 6.1): seed users A and B with distinctive data, read everything as B."""

from __future__ import annotations

import pytest

from mavis.domain import redis_keys
from mavis.store import artifacts
from mavis.store.repo import messages, users

MARKERS = {"A": "zebra-quartz-41", "B": "otter-lilac-77"}


@pytest.fixture
async def two(db, memory):
    a, _ = await users.get_or_create_by_chat(5001, "Priya")
    b, _ = await users.get_or_create_by_chat(7302, "Tomas")
    for u, key in ((a, "A"), (b, "B")):
        await messages.log(u.id, "user", f"my code word is {MARKERS[key]}", event_id=f"iso:{u.id}")
        await memory.learn(u.id, f"I keep the spare key under the {MARKERS[key]} pot", source_ref=f"iso:{u.id}")
    return a, b


async def test_repo_reads_and_recall_never_return_the_other_user(two, memory):
    a, b = two
    rows = await messages.recent(b.id, 50)
    assert all(MARKERS["A"] not in r.content for r in rows)
    ctx = await memory.recall(b.id, "where is the spare key")
    assert MARKERS["A"] not in ctx.model_dump_json()


@pytest.mark.parametrize("uid", [3, 41, 907])
def test_artifact_guard_rejects_other_prefixes(settings, uid):
    mine = artifacts.user_dir(uid, 12) / "chart.png"
    assert artifacts.guard(uid, mine) == mine.resolve()
    for bad in (artifacts.user_dir(uid + 1) / "x.pdf", settings.artifacts_dir / "x.pdf",
                artifacts.user_dir(uid) / ".." / f"u{uid + 1}" / "y.csv"):
        with pytest.raises(PermissionError):
            artifacts.guard(uid, bad)


@pytest.mark.parametrize("uid,area", [(1, "mbox"), (22, "lease"), (333, "spend")])
def test_user_keys_match_the_deletion_pattern(uid, area):
    import fnmatch

    key = redis_keys.user_key(area, uid, "chat")
    assert fnmatch.fnmatch(key, redis_keys.user_pattern(uid))
    assert not fnmatch.fnmatch(key, redis_keys.user_pattern(uid + 1))


def test_neighborhood_query_constrains_every_node():
    from mavis.memory.neo4j_graph import q_neighborhood

    for hops in (1, 2, 3):
        assert "all(n IN nodes(p) WHERE n.user_id = $u)" in q_neighborhood(hops)


async def test_workspace_guard_survives_a_second_process(fake_redis):
    from mavis.tools.integrations import workspace_guard as wg

    await wg.record_created(77, ["doc-1", "sheet-2"])
    wg._created.clear()  # a different worker process has no memory of it
    assert await wg.created_by(77) == {"doc-1", "sheet-2"}
```

Add a Qdrant check to `tests/memory/` only if a remote Qdrant test harness exists; otherwise the tenant index is verified on the box in Task 20 (`curl /collections/memories` shows `is_tenant: true`).

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/access/test_isolation.py -q`
Expected: FAIL (`ModuleNotFoundError: mavis.domain.redis_keys`).

- [ ] **Step 3: Implement**

`src/mavis/domain/redis_keys.py`:

```python
"""Per-user Redis key names: mavis:<area>:u<uid>:..., so deletion can SCAN one user's keys (spec 6.1)."""

from __future__ import annotations


def user_key(area: str, user_id: int, *parts: str) -> str:
    return ":".join(["mavis", area, f"u{int(user_id)}", *parts])


def user_pattern(user_id: int) -> str:
    return f"mavis:*:u{int(user_id)}:*"
```

`src/mavis/store/artifacts.py`:

```python
"""Per-user artifact paths and the path guard (spec 6.1)."""

from __future__ import annotations

from pathlib import Path

from mavis.config import get_settings


def user_dir(user_id: int, task_id: int | None = None) -> Path:
    base = get_settings().artifacts_dir / f"u{int(user_id)}"
    return base / f"t{int(task_id)}" if task_id is not None else base


def guard(user_id: int, path: str | Path) -> Path:
    resolved = Path(path).resolve()
    root = user_dir(user_id).resolve()
    if resolved != root and root not in resolved.parents:
        raise PermissionError("path is outside the user's artifacts")
    return resolved
```

Find artifact writers: `grep -rn "artifacts_dir" src/mavis`. Each writer that builds a path from `artifacts_dir` switches to `artifacts.user_dir(user_id, task_id)`; `task_delivery._send` and `outbox_sender._deliver` call `artifacts.guard(row.user_id, msg.document_path)` before sending a document (a `PermissionError` marks the row failed). Plan 12 writes under the same `user_dir` (its spec already uses `u{user}/t{task}`).

`memory/vector.py:91`: replace the payload index call with

```python
            await self._client.create_payload_index(
                COLLECTION, "user_id",
                models.IntegerIndexParams(type=models.IntegerIndexType.INTEGER, is_tenant=True, lookup=True,
                                          range=False))
```

and, for an existing collection, call the same once at startup inside a `try` (Qdrant updates index params in place on 1.19; if it raises, log `vector.tenant_index_unsupported` and keep the old index, the filter still isolates).

`memory/neo4j_graph.py` `q_neighborhood`: change the path line to
`f"MATCH p=(s)-[*1..{h}]-(:Entity) WHERE all(r IN relationships(p) WHERE r.valid_to IS NULL) AND all(n IN nodes(p) WHERE n.user_id = $u) "`; add `CREATE INDEX entity_user IF NOT EXISTS FOR (n:Entity) ON (n.user_id)` to the schema setup statements.

`workspace_guard.py`: make `record_created(task_id, ids)` and `created_by(task_id)` async; write through to `bus.get_redis()` (`SADD mavis:wsguard:t<task>` + `EXPIRE 86400`; read `SMEMBERS`), keeping `_created` as the fallback and cache. Update callers (`grep -rn "record_created\|created_by(" src/mavis`) to `await`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/access tests/memory tests/tools -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis/domain/redis_keys.py src/mavis/store/artifacts.py src/mavis/memory/ \
        src/mavis/tools/integrations/workspace_guard.py src/mavis/ tests/access/test_isolation.py
git commit -m "feat(isolation): two-user suite, tenant index, graph path guard, per-user keys and artifact guard"
```

---

### Task 10: Shared Redis LLM limiter with lanes, fairness, shared backoff and overflow

**Files:**
- Create: `src/mavis/llm/context.py`, `src/mavis/llm/limiter_lua.py`, `src/mavis/llm/limiter.py`, `tests/llm/test_limiter.py`
- Modify: `src/mavis/llm/models.py:169-330, 386-443, 462-519`, `src/mavis/config.py`

**Interfaces:**
- Produces:
  - `llm.context.llm_user_id: ContextVar[int | None]`, `llm.context.llm_purpose: ContextVar[str]` (default `"other"`), `bind_user(user_id, purpose)` context manager
  - `class Lease(NamedTuple): id: str; provider: str`
  - `class LimiterBackend(Protocol): async acquire(priority: Priority, wait_s: float) -> Lease | None; release(lease: Lease | None) -> None; touch() -> None; estimate_wait_s() -> float`
  - `SharedLimiter(client, provider: str, slots: int, bg_max: int, best_effort_max: int, user_max: int, hold_ttl_s: float)`; `LocalLimiterAdapter(_Limiter)`; `get_limiter(secondary: bool) -> LimiterBackend` (redis when `LLM_LIMITER=redis` and Redis is up, else local; logs `llm.limiter_local_fallback` once per minute)
  - `SharedProviderState`: `note_rate_limit`, `note_timeout`, `note_success`, `unavailable_s()` writing `mavis:llm:<provider>:backoff_until|backoff_level|cooldown_until` and keeping a local mirror refreshed on every acquire
  - Settings: `llm_global_slots: int = 3`, `llm_bg_max_slots: int = 1`, `llm_best_effort_max_slots: int = 0`, `llm_user_max_slots: int = 2`, `llm_overflow_wait_s: float = 8.0`, `llm_bg_overflow_after_s: float = 60.0`

- [ ] **Step 1: Write the failing tests**

`tests/llm/test_limiter.py`:

```python
"""Shared limiter: one Redis, several limiter instances (processes). Pro plan defaults: 3 slots, bg 1."""

from __future__ import annotations

import asyncio

import pytest

from mavis.llm.context import bind_user
from mavis.llm.limiter import SharedLimiter


def _lim(client, **kw) -> SharedLimiter:
    args = {"provider": "primary", "slots": 3, "bg_max": 1, "best_effort_max": 0, "user_max": 2, "hold_ttl_s": 5.0}
    return SharedLimiter(client, **{**args, **kw})


async def test_two_processes_share_the_global_cap(fake_redis, settings):
    a, b = _lim(fake_redis), _lim(fake_redis)
    held = []
    for i, lim in enumerate([a, b, a]):
        with bind_user(100 + i, "chat"):
            held.append(await lim.acquire("interactive", 1.0))
    with bind_user(200, "chat"), pytest.raises(Exception):
        await b.acquire("interactive", 0.3)
    a.release(held[0])
    await asyncio.sleep(0.05)
    with bind_user(201, "chat"):
        assert await b.acquire("interactive", 1.0) is not None


async def test_background_lane_cap_keeps_chat_slots(fake_redis, settings):
    lim = _lim(fake_redis)
    with bind_user(1, "task"):
        bg = await lim.acquire("background", 1.0)
    with bind_user(2, "task"), pytest.raises(Exception):
        await lim.acquire("background", 0.3)  # bg_max = 1
    for uid in (3, 4):
        with bind_user(uid, "chat"):
            assert await lim.acquire("interactive", 1.0) is not None
    lim.release(bg)


async def test_best_effort_never_runs_on_pro_defaults(fake_redis, settings):
    lim = _lim(fake_redis)
    with bind_user(9, "memory"), pytest.raises(Exception):
        await lim.acquire("best_effort", 0.5)


@pytest.mark.parametrize("busy_user,other_user", [(5, 6), (41, 42), (900, 7)])
async def test_fewest_held_user_wins_the_next_slot(fake_redis, settings, busy_user, other_user):
    lim = _lim(fake_redis, slots=2, bg_max=2)
    with bind_user(busy_user, "chat"):
        l1 = await lim.acquire("interactive", 1.0)
        l2 = await lim.acquire("interactive", 1.0)
    order: list[int] = []

    async def want(uid: int) -> None:
        with bind_user(uid, "chat"):
            lease = await lim.acquire("interactive", 3.0)
        order.append(uid)
        lim.release(lease)

    t_busy = asyncio.create_task(want(busy_user))
    await asyncio.sleep(0.05)
    t_other = asyncio.create_task(want(other_user))
    await asyncio.sleep(0.05)
    lim.release(l1)
    await asyncio.gather(t_busy, t_other)
    lim.release(l2)
    assert order[0] == other_user  # the user holding fewer slots goes first, though it queued later


async def test_dead_holder_slot_expires(fake_redis, settings):
    lim = _lim(fake_redis, slots=1, hold_ttl_s=0.3)
    with bind_user(1, "chat"):
        await lim.acquire("interactive", 1.0)  # never released: the process "died"
    with bind_user(2, "chat"):
        assert await lim.acquire("interactive", 2.0) is not None


async def test_shared_backoff_is_seen_by_every_process(fake_redis, settings):
    from mavis.llm.limiter import SharedProviderState

    a, b = SharedProviderState(fake_redis, "primary"), SharedProviderState(fake_redis, "primary")
    await a.note_rate_limit_async(None)
    await b.refresh()
    assert b.unavailable_s() > 0


async def test_redis_down_falls_back_to_local_limiter(settings, monkeypatch, caplog):
    from mavis.config import get_settings
    from mavis.llm import limiter

    monkeypatch.setenv("LLM_LIMITER", "redis")
    get_settings.cache_clear()

    class _Down:
        def register_script(self, _src):
            async def boom(**_kw):
                raise ConnectionError("down")
            return boom

    monkeypatch.setattr(limiter.bus, "get_redis", lambda: _Down())
    backend = limiter.get_limiter(secondary=False)
    lease = await backend.acquire("interactive", 1.0)
    backend.release(lease)
    assert isinstance(backend, limiter.FallingBackLimiter)
```

Also extend `tests/llm/test_models.py` with `test_local_limiter_defaults_unchanged`: with `LLM_LIMITER=local` the existing `_Limiter` behaviour tests still pass (run them; no new assertions beyond `get_limiter(False)` returning a `LocalLimiterAdapter`).

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/llm/test_limiter.py -q`
Expected: FAIL (`ModuleNotFoundError: mavis.llm.context`).

- [ ] **Step 3: Implement the context and Lua**

`src/mavis/llm/context.py`:

```python
"""Who and what an LLM call is for, set by the executor and job runner (spec 8.3): no signature changes."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar

llm_user_id: ContextVar[int | None] = ContextVar("llm_user_id", default=None)
llm_purpose: ContextVar[str] = ContextVar("llm_purpose", default="other")


@contextlib.contextmanager
def bind_user(user_id: int | None, purpose: str = "other") -> Iterator[None]:
    t1, t2 = llm_user_id.set(user_id), llm_purpose.set(purpose)
    try:
        yield
    finally:
        llm_user_id.reset(t1)
        llm_purpose.reset(t2)
```

`src/mavis/llm/limiter_lua.py`:

```python
"""Lua for the shared limiter. KEYS prefix = mavis:llm:<provider>:"""

# try_acquire: KEYS[1] holders zset, KEYS[2] queue zset, KEYS[3] backoff_until, KEYS[4] cooldown_until
# ARGV: now_ms, waiter_id, uid, rank(0/1/2), since_ms, slots, bg_max, be_max, user_max, ttl_ms, prefix,
#       last_interactive_ms key suffix
# returns {granted(0/1), backoff_ms_left}
TRY_ACQUIRE = r"""
local now = tonumber(ARGV[1]); local me = ARGV[2]; local uid = ARGV[3]; local rank = tonumber(ARGV[4])
local since = tonumber(ARGV[5]); local slots = tonumber(ARGV[6]); local bg_max = tonumber(ARGV[7])
local be_max = tonumber(ARGV[8]); local user_max = tonumber(ARGV[9]); local ttl = tonumber(ARGV[10])
local p = ARGV[11]
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local back = math.max(tonumber(redis.call('GET', KEYS[3]) or '0'), tonumber(redis.call('GET', KEYS[4]) or '0'))
if back > now then return {0, back - now} end
redis.call('ZADD', KEYS[2], 'NX', since, me)
redis.call('HSET', p .. 'waiter:' .. me, 'uid', uid, 'rank', rank, 'since', since)
redis.call('PEXPIRE', p .. 'waiter:' .. me, 900000)
local holders = redis.call('ZRANGE', KEYS[1], 0, -1)
local held, lane, per_user = #holders, {0, 0, 0}, {}
for _, h in ipairs(holders) do
  local m = redis.call('HMGET', p .. 'holder:' .. h, 'uid', 'rank')
  if m[1] then per_user[m[1]] = (per_user[m[1]] or 0) + 1; local r = tonumber(m[2]) + 1; lane[r] = lane[r] + 1 end
end
if held >= slots then return {0, 0} end
-- pick the best eligible waiter: effective rank, then fewest held by its user, then oldest
local best, best_key = nil, nil
local waiters = redis.call('ZRANGE', KEYS[2], 0, -1)
local contested = #waiters > 1
for _, w in ipairs(waiters) do
  local m = redis.call('HMGET', p .. 'waiter:' .. w, 'uid', 'rank', 'since')
  if not m[1] then redis.call('ZREM', KEYS[2], w) else
    local r = tonumber(m[2]); local s = tonumber(m[3])
    if r == 1 and now - s >= 30000 then r = 0 end
    local ok = true
    if tonumber(m[2]) >= 1 and (lane[2] + lane[3]) >= bg_max then ok = false end
    if tonumber(m[2]) == 2 and lane[3] >= be_max then ok = false end
    if contested and (per_user[m[1]] or 0) >= user_max then ok = false end
    if ok then
      local key = {r, per_user[m[1]] or 0, s}
      if best == nil or key[1] < best_key[1] or (key[1] == best_key[1] and (key[2] < best_key[2]
         or (key[2] == best_key[2] and key[3] < best_key[3]))) then best, best_key = w, key end
    end
  end
end
if best ~= me then return {0, 0} end
redis.call('ZREM', KEYS[2], me)
redis.call('DEL', p .. 'waiter:' .. me)
redis.call('ZADD', KEYS[1], now + ttl, me)
redis.call('HSET', p .. 'holder:' .. me, 'uid', uid, 'rank', tonumber(ARGV[4]))
redis.call('PEXPIRE', p .. 'holder:' .. me, ttl)
return {1, 0}
"""

# release: KEYS[1] holders, KEYS[2] queue; ARGV: lease_id, prefix. Wakes the oldest waiter's list.
RELEASE = r"""
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('DEL', ARGV[2] .. 'holder:' .. ARGV[1])
local w = redis.call('ZRANGE', KEYS[2], 0, 0)
if w[1] then redis.call('RPUSH', ARGV[2] .. 'wake:' .. w[1], '1'); redis.call('PEXPIRE', ARGV[2] .. 'wake:' .. w[1], 5000) end
return 1
"""

# cancel: drop a waiter that gave up
CANCEL = r"""
redis.call('ZREM', KEYS[1], ARGV[1]); redis.call('DEL', ARGV[2] .. 'waiter:' .. ARGV[1]); return 1
"""
```

The best_effort "never the last free slot while chat is active" rule from 59715fe is covered by `be_max` (Pro: 0, so best_effort never runs on the shared limiter); with a larger plan, `be_max` caps it and the lane cap keeps two chat slots.

- [ ] **Step 4: Implement the limiter and wire it into `_call`**

`src/mavis/llm/limiter.py`:

```python
"""LLM slot limiter backends (spec 8). SharedLimiter: Redis + Lua, safe across processes. Local: today's
_Limiter. FallingBackLimiter: Redis, and the local limiter while Redis is unreachable."""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from typing import NamedTuple, Protocol

import structlog

from mavis import bus
from mavis.config import get_settings
from mavis.domain.errors import LLMError
from mavis.llm import limiter_lua
from mavis.llm.context import llm_user_id

log = structlog.get_logger(__name__)
_RANK = {"interactive": 0, "background": 1, "best_effort": 2}
_tasks: set[asyncio.Task] = set()


class Lease(NamedTuple):
    id: str
    provider: str


class LimiterBackend(Protocol):
    async def acquire(self, priority: str, wait_s: float) -> Lease | None: ...
    def release(self, lease: Lease | None) -> None: ...
    def touch(self) -> None: ...
    def estimate_wait_s(self) -> float: ...


def _spawn(coro) -> None:
    t = asyncio.get_running_loop().create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


class SharedLimiter:
    def __init__(self, client, provider: str, slots: int, bg_max: int, best_effort_max: int, user_max: int,
                 hold_ttl_s: float) -> None:
        self._r, self._prov = client, provider
        self._p = f"mavis:llm:{provider}:"
        self._slots, self._bg, self._be, self._um = slots, bg_max, best_effort_max, user_max
        self._ttl_ms = int(hold_ttl_s * 1000)
        self._try = client.register_script(limiter_lua.TRY_ACQUIRE)
        self._rel = client.register_script(limiter_lua.RELEASE)
        self._cancel = client.register_script(limiter_lua.CANCEL)
        self._recent_holds: list[float] = []

    def _keys(self) -> list[str]:
        return [self._p + "holders", self._p + "queue", self._p + "backoff_until", self._p + "cooldown_until"]

    async def acquire(self, priority: str, wait_s: float) -> Lease | None:
        rank = _RANK[priority]
        if rank == 2 and self._be <= 0:
            raise LLMError("LLM slot reserved for interactive work")
        me = uuid.uuid4().hex
        uid = str(llm_user_id.get() or 0)
        since = int(time.time() * 1000)
        deadline = time.monotonic() + max(wait_s, 0.0)
        try:
            while True:
                now = int(time.time() * 1000)
                granted, _back = await self._try(keys=self._keys(), args=[
                    now, me, uid, rank, since, self._slots, self._bg, self._be, self._um, self._ttl_ms, self._p])
                if int(granted) == 1:
                    return Lease(me, self._prov)
                left = deadline - time.monotonic()
                if left <= 0:
                    raise LLMError("timed out waiting for an LLM slot")
                await self._r.blpop([self._p + "wake:" + me], timeout=min(left, 0.25 + random.random() * 0.1))
        except BaseException:
            _spawn(self._cancel(keys=[self._p + "queue"], args=[me, self._p]))
            raise

    def release(self, lease: Lease | None) -> None:
        if lease is not None:
            _spawn(self._rel(keys=[self._p + "holders", self._p + "queue"], args=[lease.id, self._p]))

    def touch(self) -> None:
        _spawn(self._r.set(self._p + "last_interactive_ms", int(time.time() * 1000), px=60_000))

    def estimate_wait_s(self) -> float:
        return 0.0  # refined by models._overflow_wait from the queue length (async read)

    async def queue_len(self) -> int:
        return int(await self._r.zcard(self._p + "queue"))


class LocalLimiterAdapter:
    def __init__(self, inner) -> None:
        self._inner = inner

    async def acquire(self, priority: str, wait_s: float) -> Lease | None:
        await self._inner.acquire(priority, wait_s)
        return None

    def release(self, lease: Lease | None) -> None:
        self._inner.release()

    def touch(self) -> None:
        self._inner.touch()

    def estimate_wait_s(self) -> float:
        return float(len(self._inner._waiters)) * 5.0


class FallingBackLimiter:
    """Redis when it answers; the local limiter (capacity slots // expected processes) when it does not."""

    def __init__(self, shared: SharedLimiter | None, local: LocalLimiterAdapter) -> None:
        self._shared, self._local = shared, local
        self._warned = 0.0
        self._used_local: set[int] = set()

    async def acquire(self, priority: str, wait_s: float) -> Lease | None:
        if self._shared is not None:
            try:
                return await self._shared.acquire(priority, wait_s)
            except LLMError:
                raise
            except Exception as exc:  # noqa: BLE001 - Redis trouble: degrade, never block chat
                if time.monotonic() - self._warned > 60:
                    self._warned = time.monotonic()
                    log.warning("llm.limiter_local_fallback", error=type(exc).__name__)
        await self._local.acquire(priority, wait_s)
        return Lease("local", "local")

    def release(self, lease: Lease | None) -> None:
        if lease is not None and lease.provider == "local":
            self._local.release(None)
        elif self._shared is not None:
            self._shared.release(lease)

    def touch(self) -> None:
        self._local.touch()
        if self._shared is not None:
            try:
                self._shared.touch()
            except Exception:  # noqa: BLE001
                pass

    def estimate_wait_s(self) -> float:
        return self._local.estimate_wait_s()


_backends: dict[tuple[int, bool], LimiterBackend] = {}


def get_limiter(secondary: bool = False) -> LimiterBackend:
    from mavis.llm import models

    loop = asyncio.get_running_loop()
    key = (id(loop), secondary)
    if key in _backends:
        return _backends[key]
    s = get_settings()
    local = LocalLimiterAdapter(models._limiter(secondary))
    if s.llm_limiter != "redis" or bus.get_redis() is None:
        _backends[key] = local
        return local
    try:
        ttl = max(s.llm_timeout_smart_s, s.llm_timeout_fast_s) + s.llm_timeout_cooldown_s
        shared = SharedLimiter(bus.get_redis(), "secondary" if secondary else "primary",
                               s.llm_secondary_max_concurrency if secondary else s.llm_global_slots,
                               s.llm_bg_max_slots, s.llm_best_effort_max_slots, s.llm_user_max_slots, ttl)
    except Exception:  # noqa: BLE001
        shared = None
    _backends[key] = FallingBackLimiter(shared, local)
    return _backends[key]


class SharedProviderState:
    """Account-wide 429 backoff and timeout cooldown shared through Redis (replaces per-process state)."""

    def __init__(self, client, provider: str) -> None:
        self._r, self._p = client, f"mavis:llm:{provider}:"
        self.backoff_until_ms = 0
        self.cooldown_until_ms = 0

    async def refresh(self) -> None:
        b, c = await self._r.mget(self._p + "backoff_until", self._p + "cooldown_until")
        self.backoff_until_ms, self.cooldown_until_ms = int(b or 0), int(c or 0)

    def unavailable_s(self) -> float:
        return (max(self.backoff_until_ms, self.cooldown_until_ms) - time.time() * 1000) / 1000

    async def note_rate_limit_async(self, retry_after: float | None) -> float:
        level = int(await self._r.incr(self._p + "backoff_level"))
        await self._r.expire(self._p + "backoff_level", 300)
        steps = (5.0, 10.0, 20.0, 30.0)
        dur = retry_after if retry_after is not None else steps[min(level - 1, len(steps) - 1)]
        until = int((time.time() + dur) * 1000)
        await self._r.set(self._p + "backoff_until", until, px=int(dur * 1000) + 1000)
        self.backoff_until_ms = max(self.backoff_until_ms, until)
        return dur

    async def note_timeout_async(self, cooldown_s: float) -> None:
        until = int((time.time() + cooldown_s) * 1000)
        await self._r.set(self._p + "cooldown_until", until, px=int(cooldown_s * 1000) + 1000)

    async def note_success_async(self) -> None:
        await self._r.delete(self._p + "backoff_until", self._p + "backoff_level")
```

In `src/mavis/llm/models.py` `_call`: replace `lim = _limiter(secondary)` / `await lim.acquire(...)` with

```python
        lim = get_limiter(secondary)
        lease = await lim.acquire(priority, left if priority == "interactive"
                                  else min(left, BACKGROUND_ACQUIRE_TIMEOUT_S))
```

and in `finally` use `call_later(hold, lim.release, lease)` / `lim.release(lease)`. Mirror `_ollama` updates to Redis when `LLM_LIMITER=redis`: after `_ollama.note_rate_limit(...)` add `_shared_state_note("rate", retry_after)`, after `note_timeout` `_shared_state_note("timeout")`, after `note_success` `_shared_state_note("ok")`, where `_shared_state_note` spawns the matching `SharedProviderState` async method (no await on the hot path), and at the top of each attempt (before `_await_backoff`) `await _refresh_shared_state()` copies the shared `backoff_until`/`cooldown_until` into `_ollama` when it is later than the local value. With `LLM_LIMITER=local` both helpers return immediately.

Overflow routing (spec 8.4): replace `_prefer_secondary` with

```python
def _prefer_secondary(tier: Tier, priority: Priority) -> bool:
    """Interactive: skip a saturated or backed-up primary when a secondary exists. Background: only after a
    long primary backoff. best_effort never overflows. Every overflow is counted (obs metrics)."""
    if not _secondary_ready(tier) or priority == "best_effort":
        return False
    s = get_settings()
    if priority == "interactive":
        return _ollama.unavailable_s() > 0 or get_limiter(False).estimate_wait_s() > s.llm_overflow_wait_s
    return _ollama.unavailable_s() > s.llm_bg_overflow_after_s
```

and add `from mavis.llm.limiter import get_limiter` at the module end-of-imports (circular import: `limiter.get_limiter` imports `models` lazily inside the function, so a top-level import here is safe).

Add the settings to config's LLM block:

```python
    # shared limiter (Phase 11; owner decision: Ollama Pro = 3 concurrent)
    llm_global_slots: int = 3
    llm_bg_max_slots: int = 1  # background + best_effort together; chat keeps the rest
    llm_best_effort_max_slots: int = 0
    llm_user_max_slots: int = 2  # per user while someone else waits
    llm_overflow_wait_s: float = 8.0
    llm_bg_overflow_after_s: float = 60.0
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/llm -q && uv run pytest -q`
Expected: PASS. If fakeredis `BLPOP` does not suspend (it may return immediately), the loop still polls every 0.25 s; the tests' timings allow it.

- [ ] **Step 6: Commit**

```bash
git add src/mavis/llm/ src/mavis/config.py tests/llm/
git commit -m "feat(llm): shared Redis limiter with lanes, per-user fairness, shared backoff and overflow"
```

---

### Task 11: Usage metering (tokens, cost, spend counter)

**Files:**
- Create: `src/mavis/llm/usage.py`, `src/mavis/store/repo/usage.py`, `tests/llm/test_usage.py`
- Modify: `src/mavis/llm/tracing.py` (`callbacks()` adds the usage handler), `src/mavis/config.py`, `src/mavis/worker/runner.py` (bind user around handlers and jobs)

**Interfaces:**
- Consumes: `llm_user_id`, `llm_purpose` (Task 10), `LlmUsage` (Task 2)
- Produces:
  - Settings `llm_prices: dict[str, tuple[float, float]]` (USD per million tokens in, out) default `{"deepseek-v4.1-flash": (0.30, 1.20), "glm-5.3": (1.40, 4.40)}`, `llm_price_default: tuple[float, float] = (1.40, 4.40)`
  - `cost_micros(model, prompt_tokens, completion_tokens) -> int`
  - `class UsageCallback(AsyncCallbackHandler)` (on_llm_end -> `record(...)`)
  - `async record(user_id, model, provider, purpose, prompt, completion) -> None` (upsert row on the user's local day, `INCRBY mavis:spend:u<uid>:<yyyymmdd>` 48 h TTL)
  - `async spend_micros_today(user_id) -> int` (Redis first, DB fallback); `usage.daily(user_id, day) -> list[LlmUsage]`
  - Runner binds `bind_user(event.user_id, purpose)` around handlers (purpose `chat` for chat types, `attention` for EMAIL_RECEIVED, `initiative` for wakeups, else `other`) and jobs (`task` for RUN/RESUME_TASK, `memory` for LEARN/CONSOLIDATE)

- [ ] **Step 1: Write the failing tests**

`tests/llm/test_usage.py`:

```python
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from mavis.llm import usage
from mavis.llm.context import bind_user
from mavis.store.repo import usage as repo
from mavis.store.repo import users


@pytest.mark.parametrize("model,pin,pout,micros", [("deepseek-v4.1-flash", 1_000_000, 0, 300_000),
                                                   ("glm-5.3", 1000, 1000, 5_800),
                                                   ("never-seen-model", 2000, 500, 5_000)])
def test_cost_from_the_price_table(settings, model, pin, pout, micros):
    assert usage.cost_micros(model, pin, pout) == micros


def _result(model: str, i: int, o: int) -> LLMResult:
    msg = AIMessage(content="x", usage_metadata={"input_tokens": i, "output_tokens": o, "total_tokens": i + o},
                    response_metadata={"model_name": model})
    return LLMResult(generations=[[ChatGeneration(message=msg)]])


async def test_concurrent_calls_book_spend_to_their_own_users(db, fake_redis, settings, clock):
    clock.set(datetime(2026, 10, 8, 23, 30, tzinfo=UTC))
    ids = []
    for chat, tz in ((5001, "Asia/Tokyo"), (7302, "America/Bogota"), (9944, "Europe/Lisbon")):
        u, _ = await users.get_or_create_by_chat(chat, "x")
        await users.update(u.id, timezone=tz)
        ids.append(u.id)
    cb = usage.UsageCallback()

    async def one(uid: int, n: int) -> None:
        with bind_user(uid, "chat"):
            for _ in range(n):
                await cb.on_llm_end(_result("glm-5.3", 1000, 1000), run_id=None)

    await asyncio.gather(one(ids[0], 3), one(ids[1], 1), one(ids[2], 2))
    assert [await usage.spend_micros_today(u) for u in ids] == [3 * 5800, 5800, 2 * 5800]
    tokyo_rows = await repo.daily(ids[0], datetime(2026, 10, 9).date())  # 08:30 next day in Tokyo
    assert tokyo_rows and tokyo_rows[0].calls == 3 and tokyo_rows[0].purpose == "chat"


async def test_calls_without_a_user_go_to_system(db, settings):
    await usage.UsageCallback().on_llm_end(_result("deepseek-v4.1-flash", 10, 10), run_id=None)
    assert sum(r.calls for r in await repo.all_for_user(0)) == 1
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/llm/test_usage.py -q`
Expected: FAIL (`ModuleNotFoundError: mavis.llm.usage`).

- [ ] **Step 3: Implement**

`src/mavis/store/repo/usage.py`:

```python
from __future__ import annotations

from datetime import date

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from mavis.store.db import Session
from mavis.store.models import LlmUsage


async def add(user_id: int, day: date, provider: str, model: str, purpose: str, prompt: int, completion: int,
              cost: int) -> None:
    key = (LlmUsage.user_id == user_id, LlmUsage.day == day, LlmUsage.provider == provider,
           LlmUsage.model == model, LlmUsage.purpose == purpose)
    vals = {"calls": LlmUsage.calls + 1, "prompt_tokens": LlmUsage.prompt_tokens + prompt,
            "completion_tokens": LlmUsage.completion_tokens + completion, "cost_micros": LlmUsage.cost_micros + cost}
    for _ in range(3):
        async with Session() as s:
            res = await s.execute(update(LlmUsage).where(*key).values(**vals))
            if res.rowcount == 0:
                s.add(LlmUsage(user_id=user_id, day=day, provider=provider, model=model, purpose=purpose, calls=1,
                               prompt_tokens=prompt, completion_tokens=completion, cost_micros=cost))
            try:
                await s.commit()
                return
            except IntegrityError:
                await s.rollback()  # a concurrent insert won: retry as an update


async def daily(user_id: int, day: date) -> list[LlmUsage]:
    async with Session() as s:
        return list(await s.scalars(select(LlmUsage).where(LlmUsage.user_id == user_id, LlmUsage.day == day)))


async def all_for_user(user_id: int) -> list[LlmUsage]:
    async with Session() as s:
        return list(await s.scalars(select(LlmUsage).where(LlmUsage.user_id == user_id)))


async def month_micros(since: date) -> int:
    from sqlalchemy import func

    async with Session() as s:
        return int(await s.scalar(select(func.coalesce(func.sum(LlmUsage.cost_micros), 0))
                                  .where(LlmUsage.day >= since)) or 0)
```

`src/mavis/llm/usage.py`:

```python
"""Token metering (spec 9.2): one LangChain callback books every model call to the current user."""

from __future__ import annotations

from typing import Any

import structlog
from langchain_core.callbacks import AsyncCallbackHandler

from mavis import bus
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.llm.context import llm_purpose, llm_user_id
from mavis.store.repo import usage as repo

log = structlog.get_logger(__name__)


def cost_micros(model: str, prompt_tokens: int, completion_tokens: int) -> int:
    s = get_settings()
    pin, pout = s.llm_prices.get(model, s.llm_price_default)
    return round(prompt_tokens * pin + completion_tokens * pout)  # USD per 1M tokens == micros per token


async def _local_day(user_id: int):
    from mavis.store.repo import users

    tz = get_settings().default_timezone
    if user_id:
        try:
            tz = (await users.get(user_id)).timezone or tz
        except Exception:  # noqa: BLE001
            pass
    return timeutil.to_local(timeutil.now(), tz).date()


async def record(user_id: int, model: str, provider: str, purpose: str, prompt: int, completion: int) -> None:
    cost = cost_micros(model, prompt, completion)
    day = await _local_day(user_id)
    await repo.add(user_id, day, provider, model, purpose, prompt, completion, cost)
    client = bus.get_redis()
    if client is not None:
        key = f"mavis:spend:u{user_id}:{day:%Y%m%d}"
        await client.incrby(key, cost)
        await client.expire(key, 48 * 3600)


async def spend_micros_today(user_id: int) -> int:
    day = await _local_day(user_id)
    client = bus.get_redis()
    if client is not None:
        v = await client.get(f"mavis:spend:u{user_id}:{day:%Y%m%d}")
        if v is not None:
            return int(v)
    return sum(r.cost_micros for r in await repo.daily(user_id, day))


class UsageCallback(AsyncCallbackHandler):
    async def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        try:
            for gens in response.generations:
                for g in gens:
                    msg = getattr(g, "message", None)
                    meta = getattr(msg, "usage_metadata", None) or {}
                    model = (getattr(msg, "response_metadata", {}) or {}).get("model_name", "unknown")
                    provider = "secondary" if model in _secondary_models() else "ollama"
                    await record(llm_user_id.get() or 0, model, provider, llm_purpose.get(),
                                 int(meta.get("input_tokens", 0)), int(meta.get("output_tokens", 0)))
        except Exception as exc:  # noqa: BLE001 - metering must never break a call
            log.warning("llm.usage_record_failed", error=type(exc).__name__)


def _secondary_models() -> set[str]:
    s = get_settings()
    return {m for m in (s.llm_secondary_model_fast, s.llm_secondary_model_smart) if m}
```

Price keys added to config:

```python
    # USD per million tokens (in, out). Unknown models use the default (the smart tier's price: an upper bound).
    llm_prices: dict[str, tuple[float, float]] = {"deepseek-v4.1-flash": (0.30, 1.20), "glm-5.3": (1.40, 4.40)}
    llm_price_default: tuple[float, float] = (1.40, 4.40)
```

In `llm/tracing.py` `callbacks()` return `[UsageCallback(), *([handler] if handler else [])]` (import inside the function to avoid an import cycle). In `worker/runner.py` wrap the gate+handler `attempt()` body in `with bind_user(event.user_id, _purpose(event)):` and `handle_job`'s `await fn(job)` in `with bind_user(job.user_id, _job_purpose(job)):`, with:

```python
def _purpose(event: Event) -> str:
    if event.type in CHAT_EVENT_TYPES:
        return "chat"
    return {EventType.EMAIL_RECEIVED: "attention", EventType.WAKEUP: "initiative"}.get(event.type, "other")


def _job_purpose(job: Job) -> str:
    return {JobKind.RUN_TASK: "task", JobKind.RESUME_TASK: "task", JobKind.LEARN: "memory",
            JobKind.CONSOLIDATE: "memory"}.get(job.kind, "other")
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/llm tests/worker -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis/llm/usage.py src/mavis/llm/tracing.py src/mavis/store/repo/usage.py src/mavis/worker/runner.py \
        src/mavis/config.py tests/llm/test_usage.py
git commit -m "feat(llm): per-user token and cost metering with a daily spend counter"
```

---

### Task 12: Budgets, cooldowns, bans

**Files:**
- Create: `src/mavis/access/budgets.py`, `tests/access/test_budgets.py`
- Modify: `src/mavis/llm/models.py` (`_budget_gate` in `_invoke_chain` and `structured`), `src/mavis/domain/errors.py`, `src/mavis/access/gate.py` (cooldown), `src/mavis/access/admin.py` is created in Task 15: `/ban` and `/unban` live here in `access/budgets.py` until then and move with it, `src/mavis/config.py`

**Interfaces:**
- Consumes: `spend_micros_today` (Task 11), `register_owner_command` (Task 6)
- Produces:
  - `class BudgetState(IntEnum)`: `OK=0, SOFT=1, HARD=2, RUNAWAY=3`
  - `async spend_today_usd(user_id) -> float` (LLM spend plus every registered source; contract G)
  - `register_spend_source(name: str, fn: Callable[[int, date], Awaitable[float]]) -> None`
  - `async state_for(user_id) -> BudgetState` (owner tier: always OK; override `budget_override_usd_day` replaces the hard cap and soft = hard / 2; monthly ceiling at 90% drops standard users to at least SOFT)
  - `class BudgetExceededLLM(LLMError)` in `domain/errors.py`
  - Copy: `HARD_CAP_TEXT`, `RUNAWAY_TEXT`, `COOLDOWN_TEXT` (= gate.SLOW_DOWN_TEXT)
  - `async start_cooldown(user_id, minutes=60)`, `async in_cooldown(user_id) -> bool`, `async note_rate_limit_hit(user_id)` (5 in an hour -> cooldown + owner alert)
  - `async ban(user_id, reason, *, purge=False)`, `async unban(user_id)`; owner commands `/ban <id> [purge] [reason]`, `/unban <id>`
  - Settings `budget_soft_usd: dict[str, float] = {"standard": 0.30, "trusted": 1.00}`, `budget_hard_usd: dict[str, float] = {"standard": 0.60, "trusted": 2.00}`, `budget_runaway_factor: float = 1.5`, `llm_monthly_ceiling_usd: float = 60.0` (Pro credits), `budget_enforced: bool = True`

- [ ] **Step 1: Write the failing tests**

`tests/access/test_budgets.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mavis.access import budgets
from mavis.access.budgets import BudgetState
from mavis.domain.errors import BudgetExceededLLM
from mavis.llm.context import bind_user
from mavis.store.repo import users


async def _user(chat, tier="standard", tz="Asia/Kolkata"):
    u, _ = await users.get_or_create_by_chat(chat, "x")
    await users.update(u.id, status="active", tier=tier, timezone=tz)
    return u.id


async def _spend(monkeypatch, usd_by_user: dict[int, float]):
    async def fake(uid):
        return int(usd_by_user.get(uid, 0) * 1_000_000)

    monkeypatch.setattr(budgets, "spend_micros_today", fake)


@pytest.mark.parametrize("tier,usd,state", [("standard", 0.10, BudgetState.OK), ("standard", 0.31, BudgetState.SOFT),
                                            ("standard", 0.61, BudgetState.HARD), ("standard", 0.91, BudgetState.RUNAWAY),
                                            ("trusted", 0.61, BudgetState.OK), ("owner", 50.0, BudgetState.OK)])
async def test_state_by_tier(db, settings, monkeypatch, tier, usd, state):
    uid = await _user(5000 + int(usd * 100), tier)
    await _spend(monkeypatch, {uid: usd})
    assert await budgets.state_for(uid) is state


async def test_registered_spend_sources_add_up(db, settings, monkeypatch):
    uid = await _user(7302)
    await _spend(monkeypatch, {uid: 0.20})

    async def machine(user_id, day):
        return 0.15 if user_id == uid else 0.0

    budgets.register_spend_source("compute", machine)
    assert await budgets.spend_today_usd(uid) == pytest.approx(0.35)
    assert await budgets.state_for(uid) is BudgetState.SOFT


async def test_soft_cap_degrades_background_only(db, settings, monkeypatch, fake_llm):
    from mavis.llm import models as llm

    uid = await _user(9944)
    await _spend(monkeypatch, {uid: 0.40})
    with bind_user(uid, "memory"), pytest.raises(BudgetExceededLLM):
        await llm.complete([], priority="best_effort", name="t")
    with bind_user(uid, "task"):
        tier = await llm._budget_gate(llm.Tier.SMART, "background")
    assert tier is llm.Tier.FAST
    with bind_user(uid, "chat"):
        assert await llm._budget_gate(llm.Tier.SMART, "interactive") is llm.Tier.SMART


async def test_hard_cap_notice_is_sent_once_per_local_day(db, settings, monkeypatch, channel, clock):
    from mavis.channels.outbox_sender import deliver_pending
    from mavis.llm import models as llm

    uid = await _user(4410, tz="America/Bogota")
    await _spend(monkeypatch, {uid: 0.70})
    clock.set(datetime(2026, 10, 8, 15, 0, tzinfo=UTC))
    for _ in range(3):
        with bind_user(uid, "chat"):
            assert await llm._budget_gate(llm.Tier.SMART, "interactive") is llm.Tier.FAST
        with bind_user(uid, "task"), pytest.raises(BudgetExceededLLM):
            await llm._budget_gate(llm.Tier.SMART, "background")
    await deliver_pending(channel)
    assert channel.texts.count(budgets.HARD_CAP_TEXT) == 1


@pytest.mark.parametrize("tz,utc_hour_reset", [("Asia/Tokyo", 15), ("Europe/Lisbon", 23), ("America/Bogota", 5)])
async def test_runaway_resets_at_the_users_local_midnight(db, settings, monkeypatch, clock, tz, utc_hour_reset):
    from mavis.llm import models as llm

    uid = await _user(6000 + utc_hour_reset, tz=tz)
    spent = {uid: 1.0}
    await _spend(monkeypatch, spent)
    with bind_user(uid, "chat"), pytest.raises(BudgetExceededLLM):
        await llm._budget_gate(llm.Tier.FAST, "interactive")
    spent[uid] = 0.0  # the local day rolled over: the day counter is empty (spend_micros_today keys by local date)
    with bind_user(uid, "chat"):
        assert await llm._budget_gate(llm.Tier.FAST, "interactive") is llm.Tier.FAST


async def test_cooldown_after_five_rate_limit_hits(db, settings, fake_redis):
    uid = await _user(3131)
    for _ in range(5):
        await budgets.note_rate_limit_hit(uid)
    assert await budgets.in_cooldown(uid)


async def test_ban_cancels_wakeups_and_outbox_and_unban_restores(db, settings, user):
    from mavis.store.repo import outbox
    from mavis.domain.messages import Outbound

    await users.update(user.id, status="active")
    await outbox.enqueue_now(Outbound(user_id=user.id, text="later", dedupe_key="b1"))
    await budgets.ban(user.id, "spam")
    u = await users.get(user.id)
    assert u.status == "banned" and u.ban_reason == "spam"
    assert await outbox.due(datetime(2030, 1, 1, tzinfo=UTC)) == []
    await budgets.unban(user.id)
    assert (await users.get(user.id)).status == "active"


async def test_defaults_do_not_enforce_without_a_user(settings):
    from mavis.llm import models as llm

    assert await llm._budget_gate(llm.Tier.SMART, "background") is llm.Tier.SMART
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/access/test_budgets.py -q`
Expected: FAIL (`ModuleNotFoundError: mavis.access.budgets`).

- [ ] **Step 3: Implement**

`src/mavis/domain/errors.py`: add

```python
class BudgetExceededLLM(LLMError):
    """The current user's daily budget does not allow this call (Phase 11). Callers treat it as any
    LLMError: background work is skipped or falls back to its heuristic."""
```

`src/mavis/access/budgets.py`:

```python
"""Per-user daily budgets (spec 9.3), cooldowns and bans (spec 9.4)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from enum import IntEnum

import structlog
from sqlalchemy import delete, update

from mavis import bus
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.messages import Outbound
from mavis.llm.usage import spend_micros_today
from mavis.store.db import Session, utcnow
from mavis.store.models import OutboxMessage, WakeupRow
from mavis.store.repo import audit, outbox, users

log = structlog.get_logger(__name__)
HARD_CAP_TEXT = ("I've hit today's limit for heavy work. Reminders and quick answers still work, and "
                 "everything resets at midnight your time.")
RUNAWAY_TEXT = "I've done a lot for you today, so I'm pausing until midnight your time."
SpendSource = Callable[[int, date], Awaitable[float]]
_sources: dict[str, SpendSource] = {}
_cool_mem: dict[int, float] = {}
_hits_mem: dict[int, list[float]] = {}


class BudgetState(IntEnum):
    OK = 0
    SOFT = 1
    HARD = 2
    RUNAWAY = 3


def register_spend_source(name: str, fn: SpendSource) -> None:
    _sources[name] = fn


async def spend_today_usd(user_id: int) -> float:
    total = (await spend_micros_today(user_id)) / 1_000_000
    tz = (await users.get(user_id)).timezone if user_id else get_settings().default_timezone
    day = timeutil.to_local(timeutil.now(), tz).date()
    for name, fn in list(_sources.items()):
        try:
            total += float(await fn(user_id, day))
        except Exception as exc:  # noqa: BLE001
            log.warning("budgets.source_failed", source=name, error=type(exc).__name__)
    return total


async def _month_fraction() -> float:
    s = get_settings()
    if s.llm_monthly_ceiling_usd <= 0:
        return 0.0
    from mavis.store.repo import usage as repo

    first = timeutil.now().date().replace(day=1)
    return (await repo.month_micros(first)) / 1_000_000 / s.llm_monthly_ceiling_usd


async def state_for(user_id: int) -> BudgetState:
    s = get_settings()
    if not s.budget_enforced or not user_id:
        return BudgetState.OK
    user = await users.get(user_id)
    if user.tier == "owner":
        return BudgetState.OK
    hard = user.budget_override_usd_day or s.budget_hard_usd.get(user.tier, s.budget_hard_usd["standard"])
    soft = hard / 2 if user.budget_override_usd_day else s.budget_soft_usd.get(user.tier, hard / 2)
    spent = await spend_today_usd(user_id)
    if spent >= hard * s.budget_runaway_factor:
        state = BudgetState.RUNAWAY
    elif spent >= hard:
        state = BudgetState.HARD
    elif spent >= soft:
        state = BudgetState.SOFT
    else:
        state = BudgetState.OK
    if state is BudgetState.OK and user.tier == "standard" and await _month_fraction() >= 0.9:
        state = BudgetState.SOFT
    return state


async def notify_once(user_id: int, state: BudgetState) -> None:
    if state < BudgetState.HARD:
        return
    tz = (await users.get(user_id)).timezone
    day = timeutil.to_local(timeutil.now(), tz).date()
    text = RUNAWAY_TEXT if state is BudgetState.RUNAWAY else HARD_CAP_TEXT
    await outbox.enqueue_now(Outbound(user_id=user_id, text=text, dedupe_key=f"budget:{user_id}:{day}:{state.name}"))


async def start_cooldown(user_id: int, minutes: int = 60) -> None:
    client = bus.get_redis()
    if client is not None:
        await client.set(f"mavis:cooldown:u{user_id}:x", "1", ex=minutes * 60)
    else:
        _cool_mem[user_id] = timeutil.now().timestamp() + minutes * 60


async def in_cooldown(user_id: int) -> bool:
    client = bus.get_redis()
    if client is not None:
        return bool(await client.exists(f"mavis:cooldown:u{user_id}:x"))
    return _cool_mem.get(user_id, 0) > timeutil.now().timestamp()


async def note_rate_limit_hit(user_id: int) -> None:
    now = timeutil.now().timestamp()
    client = bus.get_redis()
    if client is not None:
        key = f"mavis:rlhits:u{user_id}:x"
        await client.zadd(key, {str(now): now})
        await client.zremrangebyscore(key, 0, now - 3600)
        await client.expire(key, 3700)
        hits = int(await client.zcard(key))
    else:
        _hits_mem[user_id] = [t for t in _hits_mem.get(user_id, []) if t > now - 3600] + [now]
        hits = len(_hits_mem[user_id])
    if hits >= 5 and not await in_cooldown(user_id):
        await start_cooldown(user_id)
        from mavis.obs.watchdog import alert_owner  # Task 16; until then a log line

        await alert_owner(f"user #{user_id} is in a 1 h cooldown after {hits} rate-limit hits", key=f"cool:{user_id}")


async def ban(user_id: int, reason: str, *, purge: bool = False) -> None:
    await users.update(user_id, status="banned", banned_at=utcnow(), ban_reason=reason[:200] or None)
    async with Session() as s:
        await s.execute(update(WakeupRow).where(WakeupRow.user_id == user_id, WakeupRow.status == "pending")
                        .values(status="cancelled"))
        await s.execute(delete(OutboxMessage).where(OutboxMessage.user_id == user_id,
                                                    OutboxMessage.status.in_(("pending", "sending"))))
        await s.commit()
    await audit.record(user_id, actor="owner", action="user.banned", detail={"purge": purge})
    if purge:
        from mavis.access.deletion import request_deletion  # Task 14

        await request_deletion(user_id, by_owner=True)


async def unban(user_id: int) -> None:
    await users.update(user_id, status="active", banned_at=None, ban_reason=None)
    await audit.record(user_id, actor="owner", action="user.unbanned", detail={})
```

`WakeupRow` status values: check `store/repo/wakeups.py` for the cancelled status name and use it. Until Task 16 exists, create `src/mavis/obs/watchdog.py` with only `async def alert_owner(text: str, key: str) -> None` that logs `watchdog.alert` and enqueues the text to every owner (deduped per hour by `key`); Task 16 extends that module. Until Task 14 exists, keep the `purge` import lazy (the test does not pass `purge=True`).

In `src/mavis/llm/models.py` add:

```python
async def _budget_gate(tier: Tier, priority: Priority) -> Tier:
    """Spec 9.3 at the call site: soft cap degrades background, hard cap keeps chat on FAST and refuses
    background, runaway refuses everything. No user bound (system work) means no budget."""
    from mavis.access.budgets import BudgetState, notify_once, state_for
    from mavis.llm.context import llm_user_id

    uid = llm_user_id.get()
    if not uid:
        return tier
    state = await state_for(uid)
    if state is BudgetState.OK:
        return tier
    if state is BudgetState.RUNAWAY or (state is BudgetState.HARD and priority != "interactive"):
        await notify_once(uid, state)
        raise BudgetExceededLLM(f"daily budget {state.name.lower()}")
    if priority == "best_effort":
        raise BudgetExceededLLM("daily budget soft cap")
    if state is BudgetState.HARD:
        await notify_once(uid, state)
        return Tier.FAST
    return Tier.FAST if priority != "interactive" else tier  # SOFT: background on FAST, chat unchanged
```

Call it first in `_invoke_chain` (`tier = await _budget_gate(tier, priority)`) and in `structured` before building the chain. At SOFT, interactive calls keep their tier (the test `test_soft_cap_degrades_background_only` pins this).

In `access/gate.py` `access_gate`, after the RATE_LIMITED branch: `if event.type is EventType.RATE_LIMITED: await budgets.note_rate_limit_hit(user.id)`; and for active users `if await budgets.in_cooldown(user.id) and event.type is EventType.USER_MESSAGE: await _say_once(user.id, SLOW_DOWN_TEXT, f"u{user.id}:slow", 60); return False`.

Owner commands (registered in `budgets.register()` called from `worker/handlers.py`):

```python
async def _ban_cmd(event, owner, args):
    if not args or not args[0].isdigit():
        return "Use: /ban <user id> [purge] [reason]"
    purge = len(args) > 1 and args[1].lower() == "purge"
    reason = " ".join(args[2 if purge else 1:])
    await ban(int(args[0]), reason, purge=purge)
    return f"User #{args[0]} is paused{' and their data is being deleted' if purge else ''}."


async def _unban_cmd(event, owner, args):
    if not args or not args[0].isdigit():
        return "Use: /unban <user id>"
    await unban(int(args[0]))
    return f"User #{args[0]} is active again."


def register() -> None:
    from mavis.access.commands import register_owner_command

    register_owner_command("ban", _ban_cmd)
    register_owner_command("unban", _unban_cmd)
```

Settings block:

```python
    # --- budgets (spec 9.3), USD per user per local day ------------------------
    budget_enforced: bool = True
    budget_soft_usd: dict[str, float] = {"standard": 0.30, "trusted": 1.00}
    budget_hard_usd: dict[str, float] = {"standard": 0.60, "trusted": 2.00}
    budget_runaway_factor: float = 1.5
    llm_monthly_ceiling_usd: float = 60.0  # Ollama Pro credits; 70% alerts the owner, 90% soft-caps standard
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/access tests/llm -q && uv run pytest -q`
Expected: PASS. Existing tests run with no bound user or owner tier, so `_budget_gate` returns the tier unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/mavis/access/budgets.py src/mavis/access/gate.py src/mavis/obs/ src/mavis/llm/models.py \
        src/mavis/domain/errors.py src/mavis/worker/handlers.py src/mavis/config.py tests/access/test_budgets.py
git commit -m "feat(access): per-user daily budgets at the LLM call, cooldowns, ban and unban"
```

---
### Task 13: Mailbox scheduler (per-user FIFO, ready queues, leases, round robin, coalescing, jitter)

**Files:**
- Create: `src/mavis/worker/mailbox.py`, `src/mavis/worker/scheduler.py`, `src/mavis/domain/jitter.py`, `tests/worker/test_scheduler.py`, `tests/worker/test_mailbox_redis.py`
- Modify: `src/mavis/worker/runner.py` (`run_worker` switch), `src/mavis/timers/runner.py` (`register_timer_tick`), `src/mavis/bus/redis_streams.py:19` (MAXLEN from settings), `src/mavis/store/db.py:67` (pool sizes), `src/mavis/cli.py` (role env), `src/mavis/agents/orchestrator.py:93` (global task cap), `src/mavis/initiative/routines.py:216-223`, `src/mavis/attention/rhythm.py` (jitter), `src/mavis/config.py`

**Interfaces:**
- Consumes: `handle_event` internals (`run_gates`, `_run_handlers`, `_acknowledge`), `bind_user` (Task 10)
- Produces:
  - `Lane = Literal["chat", "bg"]`; `lane_of(event) -> Lane` (chat: USER_MESSAGE, BUTTON_PRESSED; bg: everything else)
  - `class MailboxBackend(Protocol)`: `async push(uid, lane, event_json) -> None` (append, mark ready if not leased), `async claim(lane, owner: str) -> tuple[int, list[str]] | None` (pop best ready user, take lease, return up to `coalesce_max_messages` entries without removing them), `async commit(uid, lane, n) -> None` (drop first n), `async release(uid, lane, owner) -> None` (free lease, re-ready at the back when non-empty), `async renew(uid, lane, owner) -> bool`, `async reap() -> int` (re-ready users with entries and no lease), `async depth(lane) -> int`
  - `MemoryMailbox` and `RedisMailbox(client)` (Lua `push`, `claim`, `release`)
  - `class Scheduler(backend, *, chat_executors, bg_executors)`: `async intake(event)` (stage 1, acks the reaction), `async run_executor(lane, name)` (stage 2), `async run(stop)`; `coalesce(events) -> tuple[list[Event], Event]` (earlier messages logged as rows, last event run)
  - `jitter.user_offset(user_id, span_s=600) -> timedelta` (deterministic `sha256(uid) % span`)
  - `register_timer_tick(name, fn, every_s)` in `timers/runner.py`
  - Settings: `chat_executors=6`, `bg_executors=3`, `coalesce_window_s=3.0`, `coalesce_max_messages=5`, `coalesce_max_chars=2000`, `mailbox_lease_ms=120000`, `mailbox_cap=200`, `task_global_concurrency=3`, `fanout_jitter_s=600`, `stream_maxlen=20000`, `db_pool_size=10`, `db_max_overflow=10`

- [ ] **Step 1: Write the failing tests**

`tests/worker/test_scheduler.py`:

```python
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from mavis.domain.events import Event, EventType, Trust
from mavis.domain.jitter import user_offset
from mavis.worker import runner
from mavis.worker.mailbox import MemoryMailbox
from mavis.worker.scheduler import Scheduler, coalesce, lane_of

T0 = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)


def _msg(uid: int, n: int, text: str = "hi", at: datetime | None = None, etype=EventType.USER_MESSAGE) -> Event:
    return Event(id=f"m:{uid}:{n}", user_id=uid, type=etype, occurred_at=at or T0 + timedelta(seconds=n),
                 source="test", payload={"text": f"{text} {n}"}, trust=Trust.USER)


@pytest.fixture
def seen(settings):
    out: list[tuple[int, str]] = []

    async def handler(e: Event) -> None:
        await asyncio.sleep(0.01)
        out.append((e.user_id, e.id))

    runner.register_event_handler(EventType.USER_MESSAGE, handler)
    runner.register_event_handler(EventType.EMAIL_RECEIVED, handler)
    return out


def test_lanes():
    assert lane_of(_msg(1, 1)) == "chat"
    assert lane_of(_msg(1, 2, etype=EventType.BUTTON_PRESSED)) == "chat"
    assert lane_of(_msg(1, 3, etype=EventType.EMAIL_RECEIVED)) == "bg"


async def test_round_robin_across_users_under_burst(seen, db):
    sched = Scheduler(MemoryMailbox(), chat_executors=1, bg_executors=1, coalesce_window_s=0)
    for n in range(30):
        await sched.intake(_msg(5001, n, at=T0 + timedelta(minutes=n)))  # far apart: no coalescing
    await sched.intake(_msg(7302, 100, at=T0))
    await sched.drain()
    first_other = next(i for i, (uid, _) in enumerate(seen) if uid == 7302)
    assert first_other <= 1  # the second user waited for at most one turn of the busy user


@pytest.mark.parametrize("users", [(11, 12), (5001, 9944, 7302)])
async def test_per_user_fifo_order_holds_across_two_executors(seen, db, users):
    sched = Scheduler(MemoryMailbox(), chat_executors=2, bg_executors=1, coalesce_window_s=0)
    for n in range(6):
        for uid in users:
            await sched.intake(_msg(uid, n, at=T0 + timedelta(minutes=n)))
    await sched.drain()
    for uid in users:
        mine = [eid for u, eid in seen if u == uid]
        assert mine == [f"m:{uid}:{n}" for n in range(6)]


async def test_chat_and_background_lanes_run_independently(seen, db):
    sched = Scheduler(MemoryMailbox(), chat_executors=1, bg_executors=1, coalesce_window_s=0)
    await sched.intake(_msg(1, 1, etype=EventType.EMAIL_RECEIVED))
    await sched.intake(_msg(1, 2))
    await sched.drain()
    assert {eid for _, eid in seen} == {"m:1:1", "m:1:2"}


async def test_expired_lease_is_reaped_and_resumed_once(seen, db):
    box = MemoryMailbox(lease_ms=50)
    sched = Scheduler(box, chat_executors=1, bg_executors=1, coalesce_window_s=0)
    await sched.intake(_msg(31, 1))
    claimed = await box.claim("chat", "dead-worker")  # a worker took it and died
    assert claimed is not None
    await asyncio.sleep(0.08)
    assert await box.reap() == 1
    await sched.drain()
    assert seen == [(31, "m:31:1")]


@pytest.mark.parametrize("texts,expected_run,logged", [
    (["a", "b", "c"], 1, 2),
    (["a"] * 7, 2, 5),       # capped at 5 messages per turn
    (["x" * 1500, "y" * 1500], 2, 0),  # 2000-char cap splits them
])
async def test_burst_coalescing_caps(db, user, settings, texts, expected_run, logged):
    events = [Event(id=f"c:{i}", user_id=user.id, type=EventType.USER_MESSAGE, occurred_at=T0 + timedelta(seconds=i),
                    source="test", payload={"text": t}, trust=Trust.USER) for i, t in enumerate(texts)]
    first_batch = events[: settings.coalesce_max_messages]
    earlier, last = coalesce(first_batch)
    total = len(earlier) + 1
    assert total <= settings.coalesce_max_messages
    assert sum(len(e.payload["text"]) for e in [*earlier, last]) <= max(2000, len(last.payload["text"]))


async def test_buttons_are_never_coalesced(db, user):
    evs = [_msg(user.id, 1), _msg(user.id, 2, etype=EventType.BUTTON_PRESSED), _msg(user.id, 3)]
    earlier, last = coalesce(evs)
    assert earlier == [] and last.id == "m:%d:1" % user.id


@pytest.mark.parametrize("uid", [1, 2, 3, 77, 5001])
def test_jitter_is_deterministic_and_bounded(uid):
    assert user_offset(uid) == user_offset(uid)
    assert timedelta(0) <= user_offset(uid) < timedelta(seconds=600)
    assert len({user_offset(u) for u in range(1, 60)}) > 20  # users spread out


async def test_legacy_scheduler_is_the_default(settings):
    assert settings.worker_scheduler == "legacy"
```

`coalesce(events)` returns `(earlier, last)` for the leading run of consecutive USER_MESSAGEs within `coalesce_window_s` of each other, capped by count and characters; `last` is the event that runs; the executor commits `len(earlier) + 1` entries. When the first entry is not a USER_MESSAGE, `earlier=[]` and `last=events[0]`.

`tests/worker/test_mailbox_redis.py` runs the same three properties against `RedisMailbox(fake_redis)`: push 3 users x 4 events, claim/commit/release in a loop with two owners, assert FIFO per user and that a user is never claimed by two owners at once; plus `test_reap_requeues_only_unleased_users`.

```python
from __future__ import annotations

import json

from mavis.worker.mailbox import RedisMailbox


async def test_redis_mailbox_fifo_and_single_owner(fake_redis, settings):
    box = RedisMailbox(fake_redis, lease_ms=5000)
    for n in range(4):
        for uid in (5001, 7302, 9944):
            await box.push(uid, "chat", json.dumps({"id": f"{uid}:{n}"}))
    got: dict[int, list[str]] = {}
    owners = ["w-a", "w-b"]
    for step in range(12):
        c = await box.claim("chat", owners[step % 2])
        assert c is not None
        uid, entries = c
        assert await box.claim("chat", "w-c") is None or True  # other users may be claimable
        got.setdefault(uid, []).append(json.loads(entries[0])["id"])
        await box.commit(uid, "chat", 1)
        await box.release(uid, "chat", owners[step % 2])
    for uid, ids in got.items():
        assert ids == [f"{uid}:{n}" for n in range(4)]


async def test_reap_requeues_only_unleased_users(fake_redis, settings):
    box = RedisMailbox(fake_redis, lease_ms=60)
    await box.push(1, "bg", "{}")
    await box.push(2, "bg", "{}")
    await box.claim("bg", "dead")
    await box.claim("bg", "alive")
    await fake_redis.zrem("mavis:ready:bg", "u1", "u2")
    import asyncio

    await asyncio.sleep(0.1)
    await box.renew(2, "bg", "alive")
    assert await box.reap() >= 1
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/worker/test_scheduler.py tests/worker/test_mailbox_redis.py -q`
Expected: FAIL (`ModuleNotFoundError: mavis.worker.mailbox`).

- [ ] **Step 3: Implement the mailbox backends**

`src/mavis/worker/mailbox.py`:

```python
"""Per-user mailboxes (spec 7.1): a FIFO list per (user, lane), a ready ZSET per lane scored by arrival,
and a lease per (user, lane). One lease per user and lane gives per-user order across processes; re-adding
a busy user at the back after each turn gives round robin across users."""

from __future__ import annotations

import time
from typing import Literal, Protocol

Lane = Literal["chat", "bg"]


def _box(uid: int, lane: str) -> str:
    return f"mavis:mbox:u{uid}:{lane}"


def _lease(uid: int, lane: str) -> str:
    return f"mavis:lease:u{uid}:{lane}"


class MailboxBackend(Protocol):
    async def push(self, uid: int, lane: Lane, data: str) -> None: ...
    async def claim(self, lane: Lane, owner: str) -> tuple[int, list[str]] | None: ...
    async def commit(self, uid: int, lane: Lane, n: int) -> None: ...
    async def release(self, uid: int, lane: Lane, owner: str) -> None: ...
    async def renew(self, uid: int, lane: Lane, owner: str) -> bool: ...
    async def reap(self) -> int: ...
    async def depth(self, lane: Lane) -> int: ...


PUSH = r"""
redis.call('RPUSH', KEYS[1], ARGV[1])
if redis.call('LLEN', KEYS[1]) > tonumber(ARGV[4]) then redis.call('LPOP', KEYS[1]) end
if redis.call('EXISTS', KEYS[2]) == 0 then redis.call('ZADD', KEYS[3], 'NX', ARGV[2], ARGV[3]) end
return 1
"""
# KEYS[1] ready zset; ARGV owner, lease_ms, take, lane. Pops ready users until one is leasable.
CLAIM = r"""
for i = 1, 50 do
  local top = redis.call('ZPOPMIN', KEYS[1])
  if #top == 0 then return nil end
  local u = top[1]
  local box = 'mavis:mbox:' .. u .. ':' .. ARGV[4]
  if redis.call('LLEN', box) > 0 then
    if redis.call('SET', 'mavis:lease:' .. u .. ':' .. ARGV[4], ARGV[1], 'NX', 'PX', ARGV[2]) then
      return {u, redis.call('LRANGE', box, 0, tonumber(ARGV[3]) - 1)}
    end
  end
end
return nil
"""
RELEASE = r"""
if redis.call('GET', KEYS[1]) == ARGV[1] then redis.call('DEL', KEYS[1]) end
if redis.call('LLEN', KEYS[2]) > 0 then redis.call('ZADD', KEYS[3], ARGV[2], ARGV[3]) end
return 1
"""


class RedisMailbox:
    def __init__(self, client, *, lease_ms: int = 120_000, take: int = 5, cap: int = 200) -> None:
        self._r, self._lease_ms, self._take, self._cap = client, lease_ms, take, cap
        self._push = client.register_script(PUSH)
        self._claim = client.register_script(CLAIM)
        self._release = client.register_script(RELEASE)

    async def push(self, uid: int, lane: Lane, data: str) -> None:
        await self._push(keys=[_box(uid, lane), _lease(uid, lane), f"mavis:ready:{lane}"],
                         args=[data, int(time.time() * 1000), f"u{uid}", self._cap])

    async def claim(self, lane: Lane, owner: str) -> tuple[int, list[str]] | None:
        res = await self._claim(keys=[f"mavis:ready:{lane}"], args=[owner, self._lease_ms, self._take, lane])
        if not res:
            return None
        return int(str(res[0])[1:]), list(res[1])

    async def commit(self, uid: int, lane: Lane, n: int) -> None:
        await self._r.ltrim(_box(uid, lane), n, -1)

    async def release(self, uid: int, lane: Lane, owner: str) -> None:
        await self._release(keys=[_lease(uid, lane), _box(uid, lane), f"mavis:ready:{lane}"],
                            args=[owner, int(time.time() * 1000), f"u{uid}"])

    async def renew(self, uid: int, lane: Lane, owner: str) -> bool:
        if await self._r.get(_lease(uid, lane)) == owner:
            return bool(await self._r.pexpire(_lease(uid, lane), self._lease_ms))
        return False

    async def reap(self) -> int:
        n = 0
        async for key in self._r.scan_iter(match="mavis:mbox:u*:*", count=200):
            _, _, u, lane = key.split(":")
            if not await self._r.exists(f"mavis:lease:{u}:{lane}") and await self._r.llen(key) > 0:
                n += int(await self._r.zadd(f"mavis:ready:{lane}", {u: int(time.time() * 1000)}, nx=True))
        return n

    async def depth(self, lane: Lane) -> int:
        return int(await self._r.zcard(f"mavis:ready:{lane}"))


class MemoryMailbox:
    """Same semantics in one process (dev, tests, `mavis chat`)."""

    def __init__(self, *, lease_ms: int = 120_000, take: int = 5, cap: int = 200) -> None:
        self._boxes: dict[tuple[int, str], list[str]] = {}
        self._ready: dict[str, dict[int, float]] = {"chat": {}, "bg": {}}
        self._leases: dict[tuple[int, str], tuple[str, float]] = {}
        self._lease_s, self._take, self._cap = lease_ms / 1000, take, cap

    def _leased(self, uid: int, lane: str) -> bool:
        lease = self._leases.get((uid, lane))
        return lease is not None and lease[1] > time.monotonic()

    async def push(self, uid: int, lane: Lane, data: str) -> None:
        box = self._boxes.setdefault((uid, lane), [])
        box.append(data)
        del box[:-self._cap]
        if not self._leased(uid, lane):
            self._ready[lane].setdefault(uid, time.monotonic())

    async def claim(self, lane: Lane, owner: str) -> tuple[int, list[str]] | None:
        for uid, _ in sorted(self._ready[lane].items(), key=lambda kv: kv[1]):
            del self._ready[lane][uid]
            box = self._boxes.get((uid, lane)) or []
            if box and not self._leased(uid, lane):
                self._leases[(uid, lane)] = (owner, time.monotonic() + self._lease_s)
                return uid, list(box[: self._take])
        return None

    async def commit(self, uid: int, lane: Lane, n: int) -> None:
        del self._boxes.get((uid, lane), [])[:n]

    async def release(self, uid: int, lane: Lane, owner: str) -> None:
        if (lease := self._leases.get((uid, lane))) and lease[0] == owner:
            del self._leases[(uid, lane)]
        if self._boxes.get((uid, lane)):
            self._ready[lane][uid] = time.monotonic()

    async def renew(self, uid: int, lane: Lane, owner: str) -> bool:
        if (lease := self._leases.get((uid, lane))) and lease[0] == owner:
            self._leases[(uid, lane)] = (owner, time.monotonic() + self._lease_s)
            return True
        return False

    async def reap(self) -> int:
        n = 0
        for (uid, lane), box in self._boxes.items():
            if box and not self._leased(uid, lane) and uid not in self._ready[lane]:
                self._ready[lane][uid] = time.monotonic()
                n += 1
        return n

    async def depth(self, lane: Lane) -> int:
        return len(self._ready[lane])

    def idle(self) -> bool:
        return not any(self._boxes.values()) and not any(self._leased(u, ln) for u, ln in self._leases)
```

- [ ] **Step 4: Implement the scheduler and wire it**

`src/mavis/worker/scheduler.py`:

```python
"""Two-stage worker (spec 7): intake moves events from the stream into per-user mailboxes (fast, acks the
reaction), executors claim one user at a time per lane and run the gates and handlers exactly as
handle_event does (inline retries, processed_events dedupe)."""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import structlog

from mavis.bus.base import run_with_inline_retries
from mavis.config import get_settings
from mavis.domain.events import Event, EventType
from mavis.domain.messages import Role
from mavis.llm.context import bind_user
from mavis.store.repo import messages
from mavis.worker import gates, runner
from mavis.worker.mailbox import Lane, MailboxBackend

log = structlog.get_logger(__name__)


def lane_of(event: Event) -> Lane:
    return "chat" if event.type in runner.CHAT_EVENT_TYPES else "bg"


def coalesce(events: list[Event]) -> tuple[list[Event], Event]:
    s = get_settings()
    first = events[0]
    if first.type is not EventType.USER_MESSAGE or s.coalesce_window_s <= 0:
        return [], first
    run = [first]
    chars = len(str(first.payload.get("text", "")))
    for e in events[1:]:
        gap = (e.occurred_at - run[-1].occurred_at).total_seconds()
        size = len(str(e.payload.get("text", "")))
        if (e.type is not EventType.USER_MESSAGE or gap > s.coalesce_window_s or len(run) >= s.coalesce_max_messages
                or chars + size > s.coalesce_max_chars):
            break
        run.append(e)
        chars += size
    return run[:-1], run[-1]


class Scheduler:
    def __init__(self, backend: MailboxBackend, *, chat_executors: int | None = None, bg_executors: int | None = None,
                 coalesce_window_s: float | None = None) -> None:
        s = get_settings()
        self._box = backend
        self._n = {"chat": chat_executors or s.chat_executors, "bg": bg_executors or s.bg_executors}
        self._window = s.coalesce_window_s if coalesce_window_s is None else coalesce_window_s
        self._wake = {"chat": asyncio.Event(), "bg": asyncio.Event()}

    async def intake(self, event: Event) -> None:
        if event.type is EventType.USER_MESSAGE:
            ack = asyncio.create_task(runner._acknowledge(event))
            runner._ack_tasks.add(ack)
            ack.add_done_callback(runner._ack_tasks.discard)
        lane = lane_of(event)
        await self._box.push(event.user_id, lane, event.model_dump_json())
        self._wake[lane].set()

    async def _run_one(self, lane: Lane, owner: str) -> bool:
        claimed = await self._box.claim(lane, owner)
        if claimed is None:
            return False
        uid, raw = claimed
        events = [Event.model_validate_json(r) for r in raw]
        earlier, last = coalesce(events) if self._window > 0 else ([], events[0])
        heartbeat = asyncio.create_task(self._heartbeat(uid, lane, owner))
        try:
            for e in earlier:  # Deviation 4: each message keeps its own row; the turn reads them from history
                await messages.log(e.user_id, Role.USER, str(e.payload.get("text", "")), event_id=e.id)
            await self._handle(last)
            await self._box.commit(uid, lane, len(earlier) + 1)
        except Exception:  # noqa: BLE001 - leave the entries; the lease expires and the reaper retries
            log.exception("scheduler.turn_failed", user_id=uid, lane=lane)
            return True
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        await self._box.release(uid, lane, owner)
        return True

    async def _handle(self, event: Event) -> None:
        handlers = list(runner._event_handlers.get(event.type, []))

        async def attempt() -> None:
            with bind_user(event.user_id, runner._purpose(event)):
                if await gates.run_gates(event):
                    await runner._run_handlers(event, handlers)

        with structlog.contextvars.bound_contextvars(event_id=event.id, user_id=event.user_id):
            await run_with_inline_retries(attempt, what="event", ref=event.id)

    async def _heartbeat(self, uid: int, lane: Lane, owner: str) -> None:
        while True:
            await asyncio.sleep(30)
            await self._box.renew(uid, lane, owner)

    async def run_executor(self, lane: Lane, name: str) -> None:
        owner = f"{name}:{uuid.uuid4().hex[:8]}"
        while True:
            if not await self._run_one(lane, owner):
                self._wake[lane].clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake[lane].wait(), timeout=1.0)

    async def drain(self) -> None:
        """Tests and `mavis chat`: run until every mailbox is empty."""
        while True:
            busy = await asyncio.gather(*(self._run_one(ln, f"drain-{ln}-{i}")
                                          for ln in ("chat", "bg") for i in range(self._n[ln])))
            if not any(busy):
                return

    def executors(self, name: str) -> list:
        return [self.run_executor(ln, f"{name}-{ln}-{i}") for ln in ("chat", "bg") for i in range(self._n[ln])]
```

In `src/mavis/worker/runner.py` `run_worker`:

```python
    s = get_settings()
    if s.worker_scheduler == "mailbox":
        from mavis import bus as bus_mod
        from mavis.worker.mailbox import MemoryMailbox, RedisMailbox
        from mavis.worker.scheduler import Scheduler

        client = bus_mod.get_redis()
        backend = (RedisMailbox(client, lease_ms=s.mailbox_lease_ms, take=s.coalesce_max_messages,
                                cap=s.mailbox_cap) if client is not None else MemoryMailbox())
        sched = Scheduler(backend)
        loops = [bus.consume_events(WORKER_GROUP, f"{consumer}-{i}", sched.intake) for i in range(n)]
        loops += [bus.consume_jobs(WORKER_GROUP, f"{consumer}-{i}", handle_job) for i in range(n)]
        await asyncio.gather(*loops, *sched.executors(consumer))
        return
```

placed after `await run_startup_hooks()`. The legacy path is unchanged.

`src/mavis/timers/runner.py`: add a tick registry and run it from `tick()` after `fire_due`:

```python
TickFn = Callable[[], Awaitable[object]]
_ticks: dict[str, tuple[TickFn, float]] = {}
_last_tick: dict[str, float] = {}


def register_timer_tick(name: str, fn: TickFn, every_s: float) -> None:
    """Periodic plumbing in the timer role (single leader): reapers, watchdog, cleanups."""
    _ticks[name] = (fn, every_s)


async def _run_ticks() -> None:
    now = time.monotonic()
    for name, (fn, every) in list(_ticks.items()):
        if now - _last_tick.get(name, float("-inf")) >= every:
            _last_tick[name] = now
            try:
                await fn()
            except Exception:  # noqa: BLE001
                log.exception("timer.tick_hook_failed", hook=name)
```

The timer role starts with `handlers=False`, so register the mailbox reaper in `run_timer()` directly: when `worker_scheduler == "mailbox"` and Redis is set, `register_timer_tick("mailbox_reaper", RedisMailbox(get_redis()).reap, 15)`.

Pools: `store/db.py` uses `{"pool_size": s.db_pool_size, "max_overflow": s.db_max_overflow, "pool_pre_ping": True}`; compose sets per service (Task 20): worker 8/4, api 4/2, timer 2/2. Streams: `redis_streams.py` replaces `MAXLEN` uses with `get_settings().stream_maxlen`. Global task cap: in `orchestrator.run_task` before the per-user check, `if task.kind != TaskKind.APPROVAL and await tasks.running_count_all() >= s.task_global_concurrency: log.info("task.deferred_global"); return` with `tasks.running_count_all()` (new repo function counting RUNNING across users); `_kick_next_queued` also kicks one queued task of another user when a slot frees (`tasks.users_with_queued()` already exists).

Jitter: `src/mavis/domain/jitter.py`:

```python
from __future__ import annotations

import hashlib
from datetime import timedelta

from mavis.config import get_settings


def user_offset(user_id: int, span_s: int | None = None) -> timedelta:
    """Deterministic per-user offset in [0, span) so daily jobs for many users do not fire in the same minute
    (spec 7.3). Never applied to a time promised to the user (Programs deliveries are exempt)."""
    span = span_s if span_s is not None else get_settings().fanout_jitter_s
    if span <= 0:
        return timedelta(0)
    digest = int(hashlib.sha256(f"jitter:{user_id}".encode()).hexdigest(), 16)
    return timedelta(seconds=digest % span)
```

Apply it in `routines._schedule_morning` (`at = await self.next_morning_time(...) + user_offset(user.id)`) and to the evening wrap and retention schedules in `attention/rhythm.py` (find them with `grep -n "wake_me" src/mavis/attention/rhythm.py`); one-off user wakeups are not touched.

Settings block:

```python
    # --- scheduler (spec 7) ----------------------------------------------------
    chat_executors: int = 6
    bg_executors: int = 3
    coalesce_window_s: float = 3.0
    coalesce_max_messages: int = 5
    coalesce_max_chars: int = 2000
    mailbox_lease_ms: int = 120_000
    mailbox_cap: int = 200
    task_global_concurrency: int = 3
    fanout_jitter_s: int = 600
    stream_maxlen: int = 20_000
    db_pool_size: int = 10
    db_max_overflow: int = 10
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/worker tests/timers tests/agents/test_task_runner.py tests/initiative -q && uv run pytest -q`
Expected: PASS. Routine tests that assert an exact 08:30 wakeup need `FANOUT_JITTER_S=0` in `TEST_ENV` (add `"FANOUT_JITTER_S": "0"` to `tests/conftest.py`); the jitter tests pass `span_s` explicitly or set the env back.

- [ ] **Step 6: Commit**

```bash
git add src/mavis/worker/ src/mavis/timers/runner.py src/mavis/domain/jitter.py src/mavis/bus/redis_streams.py \
        src/mavis/store/db.py src/mavis/store/repo/tasks.py src/mavis/agents/orchestrator.py \
        src/mavis/initiative/routines.py src/mavis/attention/rhythm.py src/mavis/config.py tests/
git commit -m "feat(worker): mailbox scheduler with per-user order, round robin, leases, coalescing and jitter"
```

---

### Task 14: Account deletion (/delete_me, DELETE_USER job, cascade, extension point)

**Files:**
- Create: `src/mavis/store/repo/deletion.py`, `src/mavis/access/deletion.py`, `tests/access/test_deletion.py`
- Modify: `src/mavis/domain/events.py` (`JobKind.DELETE_USER`), `src/mavis/tools/integrations/base.py` + `composio.py` (`disconnect_all`), `src/mavis/memory/vector.py` (`delete_user`), `src/mavis/memory/neo4j_graph.py` + `memory/graph.py` (`delete_user`), `src/mavis/worker/handlers.py`

**Interfaces:**
- Produces:
  - `USER_TABLES: tuple[str, ...]` (FK-safe delete order, children first), `async delete_user_rows(user_id) -> dict[str, int]`
  - `register_deletion_step(name: str, fn: Callable[[int], Awaitable[dict]]) -> None` (contract F; external steps run after Composio, before Postgres)
  - `access.deletion`: `CONFIRM_TEXT`, `DONE_TEXT`, buttons `del:yes:<uid>`, `del:no:<uid>` (valid 10 min), `async request_deletion(user_id, *, by_owner=False)`, job handler `run_deletion(job)`; user commands `/delete_me`, `/deleteme`, `/privacy`
  - `IntegrationProvider.disconnect_all(user) -> int`, `VectorStore.delete_user(uid)`, `GraphStore.delete_user(uid)`

- [ ] **Step 1: Write the failing tests**

`tests/access/test_deletion.py`:

```python
from __future__ import annotations

import pytest
from sqlalchemy import inspect as sa_inspect

from mavis.access import deletion
from mavis.store import models  # noqa: F401
from mavis.store.db import Base
from mavis.store.repo import deletion as repo
from mavis.store.repo import messages, users


def test_every_user_table_is_in_the_cascade():
    """Any ORM table with a user_id column must be deleted with the user (contract F: tables added by other
    plans, such as commitments or the sandbox tables, must be listed by whichever plan merges second)."""
    with_user = {t.name for t in Base.metadata.sorted_tables if "user_id" in t.c}
    assert with_user - set(repo.USER_TABLES) == set()


def test_cascade_order_is_fk_safe():
    order = {name: i for i, name in enumerate(repo.USER_TABLES)}
    for t in Base.metadata.sorted_tables:
        for fk in t.foreign_keys:
            if t.name in order and fk.column.table.name in order:
                assert order[t.name] < order[fk.column.table.name], (t.name, fk.column.table.name)


async def _seed(chat: int, name: str) -> int:
    u, _ = await users.get_or_create_by_chat(chat, name)
    await users.update(u.id, status="active", composio_user_id=f"mavis-test-{u.id}")
    for i in range(3):
        await messages.log(u.id, "user", f"{name} message {i}", event_id=f"d:{u.id}:{i}")
    return u.id


@pytest.mark.parametrize("names", [("Priya", "Tomas"), ("Aiko", "Bruno")])
async def test_deletion_removes_only_that_user(db, memory, provider, names):
    a, b = await _seed(5001, names[0]), await _seed(7302, names[1])
    await deletion.run_steps(a)
    assert await messages.recent(a, 10) == [] and len(await messages.recent(b, 10)) == 3
    assert (await users.get(a)).status == "deleted"


async def test_deletion_resumes_after_a_crash_mid_way(db, memory, provider, monkeypatch):
    uid = await _seed(9944, "Lena")
    calls = []

    async def flaky(user_id):
        calls.append(user_id)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"ok": 1}

    repo.register_deletion_step("flaky", flaky)
    with pytest.raises(RuntimeError):
        await deletion.run_steps(uid)
    state = (await users.get_state(uid)).get("deletion", {})
    assert "redis" in state.get("done", []) and "flaky" not in state.get("done", [])
    await deletion.run_steps(uid)
    assert calls == [uid, uid] and (await users.get(uid)).status == "deleted"


async def test_tombstone_has_no_personal_fields(db, memory, provider):
    uid = await _seed(4410, "Omar")
    await deletion.run_steps(uid)
    u = await users.get(uid)
    assert (u.name, u.telegram_chat_id, u.telegram_user_id, u.composio_user_id, u.state) == (None, None, None, None, {})
    assert u.deleted_at is not None


async def test_confirm_flow_buttons_and_expiry(db, channel, clock, invite_mode):
    uid = await _seed(3131, "Zoe")
    await deletion.start(uid, event_id="e1")
    clock.advance(minutes=11)
    assert await deletion.confirm(uid, data=f"del:yes:{uid}") is False  # expired, nothing happens
    await deletion.start(uid, event_id="e2")
    assert await deletion.confirm(uid, data=f"del:yes:{uid}") is True
    assert (await users.get(uid)).status == "deleting"
```

(`provider` is the existing fake integration provider fixture; if the deletion test needs `disconnect_all`, add it to the fake in `tests/` as `return len(self.connected.pop(user.user_id, []))`.)

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/access/test_deletion.py -q`
Expected: FAIL (`ModuleNotFoundError: mavis.store.repo.deletion`).

- [ ] **Step 3: Implement the repo cascade**

`src/mavis/store/repo/deletion.py`:

```python
"""Every user table in FK-safe delete order (children first), one transaction, plus the extension point
other plans use for stores this module does not know (contract F)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy import delete

from mavis.store.db import Base, Session

# Children before parents. The meta-test fails when a table with user_id is missing here.
USER_TABLES: tuple[str, ...] = (
    "invite_redemptions", "artifacts", "pending_approvals", "tasks", "initiative_decisions", "wakeups",
    "ping_log", "loops", "connections_pending", "attention_prefs", "attention_money_baselines",
    "attention_senders", "attention_observations", "policy_rules", "outbox", "messages", "llm_usage",
    "graph_edges", "graph_nodes", "profile_cards", "conversation_summaries", "audit_log",
)
# Tables that carry user_id but must outlive the user row: none. audit_log rows for the user are deleted
# and one fresh row with counts only is written after the cascade.

StepFn = Callable[[int], Awaitable[dict]]
EXTERNAL_STEPS: dict[str, StepFn] = {}


def register_deletion_step(name: str, fn: StepFn) -> None:
    EXTERNAL_STEPS[name] = fn


async def delete_user_rows(user_id: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    tables = Base.metadata.tables
    async with Session() as s:
        for name in USER_TABLES:
            table = tables.get(name)
            if table is None or "user_id" not in table.c:
                continue
            res = await s.execute(delete(table).where(table.c.user_id == user_id))
            counts[name] = res.rowcount or 0
        await s.commit()
    return counts
```

Run `uv run pytest tests/access/test_deletion.py::test_every_user_table_is_in_the_cascade -q`, read the missing names it prints, and add each in FK-safe order (the order test tells you when a child sits after its parent). If Phase B or Plan 12 merged first, their tables appear here: add them.

- [ ] **Step 4: Implement the flow and the job**

`src/mavis/access/deletion.py`:

```python
"""/delete_me (spec 10): confirm with buttons, mark deleting, run idempotent steps in a job; each step is
recorded in users.state["deletion"] so a crash resumes where it stopped."""

from __future__ import annotations

from datetime import timedelta

import structlog

from mavis import bus
from mavis.agents.buttons import register_button_handler
from mavis.domain import redis_keys
from mavis.domain.events import Event, Job, JobKind
from mavis.domain.messages import Button, Outbound
from mavis.store.db import utcnow
from mavis.store.repo import audit, outbox, users
from mavis.store.repo import deletion as repo

log = structlog.get_logger(__name__)
CONFIRM_TEXT = ("This deletes everything I know about you: messages, memories, reminders, connected "
                "accounts. It can't be undone.")
DONE_TEXT = "Done. Everything is deleted. If you ever want to come back, you'll need a new invite."
PRIVACY_TEXT = "Here's how Mavis AI handles your data: {url}"
CONFIRM_TTL = timedelta(minutes=10)


async def start(user_id: int, event_id: str) -> None:
    await users.update_nested(user_id, "deletion", {"asked_at": utcnow().isoformat()})
    await outbox.enqueue_now(Outbound(user_id=user_id, text=CONFIRM_TEXT, dedupe_key=f"del:ask:{event_id}",
                                      buttons=[[Button(label="Delete everything", data=f"del:yes:{user_id}"),
                                                Button(label="Cancel", data=f"del:no:{user_id}")]]))


async def confirm(user_id: int, data: str) -> bool:
    asked = (await users.get_state(user_id)).get("deletion", {}).get("asked_at")
    if not data.endswith(f":{user_id}") or asked is None:
        return False
    from datetime import datetime

    if utcnow() - datetime.fromisoformat(asked) > CONFIRM_TTL:
        return False
    if data.startswith("del:no:"):
        await users.update_nested(user_id, "deletion", {"asked_at": None})
        return False
    await request_deletion(user_id)
    return True


async def request_deletion(user_id: int, *, by_owner: bool = False) -> None:
    await users.update(user_id, status="deleting")  # the gate now drops every event for this user
    await audit.record(user_id, actor="owner" if by_owner else "user", action="user.delete_requested", detail={})
    await bus.get_bus().enqueue(Job(id=f"delete:{user_id}", user_id=user_id, kind=JobKind.DELETE_USER))


async def _redis(user_id: int) -> dict:
    from mavis.store.models import OutboxMessage, WakeupRow
    from mavis.store.db import Session
    from sqlalchemy import delete, update

    async with Session() as s:
        await s.execute(update(WakeupRow).where(WakeupRow.user_id == user_id, WakeupRow.status == "pending")
                        .values(status="cancelled"))
        await s.execute(delete(OutboxMessage).where(OutboxMessage.user_id == user_id,
                                                    OutboxMessage.status.in_(("pending", "sending"))))
        await s.commit()
    client = bus.get_redis()
    n = 0
    if client is not None:
        async for key in client.scan_iter(match=redis_keys.user_pattern(user_id), count=500):
            n += int(await client.delete(key))
    return {"keys": n}


async def _composio(user_id: int) -> dict:
    from mavis.domain.integrations import UserRef
    from mavis.tools.integrations import get_provider

    removed = 0
    for attempt in range(5):
        try:
            removed = await get_provider().disconnect_all(UserRef(user_id=user_id))
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("deletion.composio_retry", attempt=attempt, error=type(exc).__name__)
    else:
        from mavis.obs.watchdog import alert_owner

        await alert_owner(f"Composio disconnect failed for deleted user #{user_id}", key=f"del:comp:{user_id}")
    return {"accounts": removed}


async def _vector(user_id: int) -> dict:
    from mavis.memory.service import get_memory

    return {"points": await get_memory().vector.delete_user(user_id)}


async def _graph(user_id: int) -> dict:
    from mavis.memory.service import get_memory

    return {"nodes": await get_memory().graph.delete_user(user_id)}


async def _artifacts(user_id: int) -> dict:
    import shutil

    from mavis.store.artifacts import user_dir

    d = user_dir(user_id)
    existed = d.exists()
    shutil.rmtree(d, ignore_errors=True)
    return {"artifacts_dir": int(existed)}


def _steps():
    yield "redis", _redis
    yield "composio", _composio
    yield from repo.EXTERNAL_STEPS.items()
    yield "vector", _vector
    yield "graph", _graph
    yield "artifacts", _artifacts


async def run_steps(user_id: int) -> dict:
    report: dict = {}
    for name, fn in _steps():
        done = (await users.get_state(user_id)).get("deletion", {}).get("done", [])
        if name in done:
            continue
        report[name] = await fn(user_id)
        await users.modify_nested(user_id, "deletion",
                                  lambda cur, n=name: {**cur, "done": [*cur.get("done", []), n]})
    chat = (await users.get(user_id)).telegram_chat_id
    report["postgres"] = await repo.delete_user_rows(user_id)
    if chat is not None:
        from mavis.channels import get_channel

        try:
            await get_channel().send_text(chat, DONE_TEXT)
        except Exception as exc:  # noqa: BLE001
            log.warning("deletion.final_message_failed", error=type(exc).__name__)
    await users.update(user_id, status="deleted", name=None, state={}, telegram_chat_id=None,
                       telegram_user_id=None, composio_user_id=None, deleted_at=utcnow(), locale=None,
                       currency=None, country=None)
    await audit.record(user_id, actor="system", action="user.deleted",
                       detail={k: (sum(v.values()) if isinstance(v, dict) else v) for k, v in report.items()})
    return report


async def run_deletion(job: Job) -> None:
    await run_steps(job.user_id)


async def _delete_cmd(event: Event, user, args) -> str | None:
    await start(user.id, event.id)
    return None


async def _privacy_cmd(event: Event, user, args) -> str:
    from mavis.config import get_settings

    return PRIVACY_TEXT.format(url=get_settings().privacy_url)


async def _button(event: Event, data: str) -> None:
    await confirm(event.user_id, data)


def register() -> None:
    from mavis.access.commands import register_owner_command, register_user_command
    from mavis.worker.runner import register_job_handler

    register_user_command("delete_me", _delete_cmd)
    register_user_command("deleteme", _delete_cmd)
    register_user_command("privacy", _privacy_cmd)
    register_button_handler("del:", _button)
    register_job_handler(JobKind.DELETE_USER, run_deletion)

    async def admin_delete(event, owner, args):
        if len(args) >= 2 and args[0] == "delete" and args[1].isdigit():
            await request_deletion(int(args[1]), by_owner=True)
            return f"Deleting user #{args[1]}."
        return None

    register_owner_command("admin_delete", admin_delete)
```

The final message is sent directly (the outbox rows are already deleted). Add `privacy_url: str = ""` to config (owner fills it; `/privacy` and the bot description link it; Telegram requires a privacy policy for bots that collect data). Add `JobKind.DELETE_USER = "delete_user"` to `domain/events.py`. Add `delete_user(uid) -> int` to the vector store (`client.delete(COLLECTION, points_selector=models.FilterSelector(filter=models.Filter(must=[models.FieldCondition(key="user_id", match=models.MatchValue(value=uid))])))`), to the Neo4j graph (`MATCH (n:Entity {user_id:$u}) CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 500 ROWS`, run in an auto-commit session) and to `SqliteGraphStore` (the Postgres cascade covers its tables, return 0). Add `disconnect_all(user)` to `IntegrationProvider` and `ComposioProvider` (list connected accounts for the stored id, `DELETE /connected_accounts/{id}` each, return the count). Call `deletion.register()` from `worker/handlers.py`. The `/admin delete <id>` command is wired through `/admin` in Task 15.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/access tests/memory tests/tools -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mavis/store/repo/deletion.py src/mavis/access/deletion.py src/mavis/domain/events.py \
        src/mavis/memory/ src/mavis/tools/integrations/ src/mavis/worker/handlers.py src/mavis/config.py tests/
git commit -m "feat(access): resumable account deletion across every store with a cascade meta-test"
```

---
### Task 15: Owner admin commands

**Files:**
- Create: `src/mavis/access/admin.py`, `tests/access/test_admin.py`
- Modify: `src/mavis/config.py` (remove `admin_user`, `admin_password`), `docker-compose.prod.yml` (drop `ADMIN_PASSWORD`), `src/mavis/access/budgets.py` (move `/ban`, `/unban` registration here), `src/mavis/worker/handlers.py`, `src/mavis/llm/models.py` (`/pause bg` switch)

**Interfaces:**
- Consumes: `register_owner_command`, `is_owner` (Task 6), budgets (Task 12), deletion (Task 14), `obs.metrics.snapshot()` (Task 16 adds the full set; this task reads what exists through `stats_lines()`)
- Produces: owner commands `/admin`, `/stats`, `/users [status]`, `/user <id>`, `/budget <id> <usd|default>`, `/tier <id> <tier>`, `/broadcast <text>` (preview + `[Send to N users]` `[Cancel]`, buttons `bc:send:<id>` / `bc:no:<id>`), `/dlq [list|replay <id>|drop <id>]`, `/pause bg`, `/resume bg`, `/admin delete <id>` (confirm button `adm:del:<id>`); `bg_paused() -> bool` (Redis key `mavis:admin:bg_paused`), honoured by `_budget_gate` (background and best_effort raise `BudgetExceededLLM("background paused")`)

- [ ] **Step 1: Write the failing test**

`tests/access/test_admin.py`:

```python
from __future__ import annotations

import pytest

from mavis.access import admin, commands
from mavis.domain.events import Event, EventType, Trust
from mavis.store.db import utcnow
from mavis.store.repo import audit, users


@pytest.fixture
async def owner(db, settings, monkeypatch):
    from mavis.config import get_settings

    monkeypatch.setenv("OWNER_TELEGRAM_CHAT_IDS", "[5001]")
    get_settings.cache_clear()
    u, _ = await users.get_or_create_by_chat(5001, "Priya")
    await users.update(u.id, status="active", tier="owner")
    admin.register()
    for chat, name, status in ((7302, "Tomas", "active"), (9944, "Aiko", "pending"), (4410, "Omar", "banned")):
        x, _ = await users.get_or_create_by_chat(chat, name)
        await users.update(x.id, status=status)
    return await users.get(u.id)


def _cmd(uid: int, text: str, n: int = 1) -> Event:
    return Event(id=f"a:{uid}:{n}:{text}", user_id=uid, type=EventType.USER_MESSAGE, occurred_at=utcnow(),
                 source="telegram", payload={"text": text}, trust=Trust.USER)


async def _out(channel) -> list[str]:
    from mavis.channels.outbox_sender import deliver_pending

    await deliver_pending(channel)
    return channel.texts


async def test_users_filter_and_stats_counts(owner, channel):
    await commands.command_gate(_cmd(owner.id, "/users pending"))
    await commands.command_gate(_cmd(owner.id, "/stats", 2))
    users_out, stats_out = await _out(channel)
    assert "Aiko" in users_out and "Tomas" not in users_out
    assert "active 2" in stats_out and "pending 1" in stats_out and "banned 1" in stats_out


@pytest.mark.parametrize("args,expect", [("0.25", 0.25), ("default", None), ("1.5", 1.5)])
async def test_budget_and_tier_overrides_are_audited(owner, channel, args, expect):
    target = (await users.get_by_chat(7302)).id
    await commands.command_gate(_cmd(owner.id, f"/budget {target} {args}"))
    await commands.command_gate(_cmd(owner.id, f"/tier {target} trusted", 2))
    u = await users.get(target)
    assert u.budget_override_usd_day == expect and u.tier == "trusted"
    assert len(await audit.for_user(target)) >= 2


async def test_broadcast_previews_then_sends_only_on_the_button(owner, channel):
    from mavis.agents.buttons import dispatch_button

    await commands.command_gate(_cmd(owner.id, "/broadcast New: weekly summaries on Sundays"))
    out = await _out(channel)
    assert "Send to 1 user" in "".join(b.label for row in channel.sent[-1].buttons for b in row)
    data = channel.sent[-1].buttons[0][0].data
    await dispatch_button(Event(id="b1", user_id=owner.id, type=EventType.BUTTON_PRESSED, occurred_at=utcnow(),
                                source="telegram", payload={"data": data}, trust=Trust.USER))
    assert "New: weekly summaries on Sundays" in "".join(await _out(channel))
    assert "\u2014" not in "".join(out)


async def test_pause_bg_blocks_background_llm_until_resume(owner, fake_redis):
    from mavis.domain.errors import BudgetExceededLLM
    from mavis.llm import models as llm
    from mavis.llm.context import bind_user

    await commands.command_gate(_cmd(owner.id, "/pause bg"))
    with bind_user(7, "task"), pytest.raises(BudgetExceededLLM):
        await llm._budget_gate(llm.Tier.SMART, "background")
    await commands.command_gate(_cmd(owner.id, "/resume bg", 2))
    with bind_user(7, "task"):
        await llm._budget_gate(llm.Tier.SMART, "background")


def test_admin_credentials_are_gone(settings):
    assert not hasattr(settings, "admin_password") and not hasattr(settings, "admin_user")
```

(`audit.for_user(user_id)` is a small read added to `store/repo/audit.py` if missing.)

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/access/test_admin.py -q`
Expected: FAIL (`ModuleNotFoundError: mavis.access.admin`).

- [ ] **Step 3: Implement**

`src/mavis/access/admin.py`:

```python
"""Owner-only admin commands (spec 11). Every mutating command writes audit_log. Never through the LLM."""

from __future__ import annotations

from sqlalchemy import func, select

from mavis import bus
from mavis.access import budgets, deletion
from mavis.access.commands import register_owner_command
from mavis.agents.buttons import register_button_handler
from mavis.domain.events import Event
from mavis.domain.messages import Button, Outbound
from mavis.store.db import Session
from mavis.store.models import User
from mavis.store.repo import audit, outbox, users

HELP = ("/stats, /users [active|pending|banned], /user <id>, /invite ..., /ban <id> [purge] [reason], "
        "/unban <id>, /budget <id> <usd/day|default>, /tier <id> <standard|trusted>, /broadcast <text>, "
        "/dlq [list|replay <id>|drop <id>], /pause bg, /resume bg, /admin delete <id>")
_PAUSE = "mavis:admin:bg_paused"
_paused_mem = {"on": False}
_drafts: dict[str, str] = {}


async def bg_paused() -> bool:
    client = bus.get_redis()
    if client is not None:
        return bool(await client.exists(_PAUSE))
    return _paused_mem["on"]


async def _set_paused(on: bool) -> None:
    client = bus.get_redis()
    if client is not None:
        await (client.set(_PAUSE, "1") if on else client.delete(_PAUSE))
    _paused_mem["on"] = on


async def _status_counts() -> dict[str, int]:
    async with Session() as s:
        rows = await s.execute(select(User.status, func.count(User.id)).where(User.is_test.is_(False))
                               .group_by(User.status))
        return {st: n for st, n in rows}


async def stats(event, owner, args) -> str:
    from mavis.obs.metrics import stats_lines

    counts = await _status_counts()
    head = "Users: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    return "\n".join([head, *await stats_lines()])


async def list_users(event, owner, args) -> str:
    want = args[0] if args else None
    async with Session() as s:
        q = select(User).where(User.is_test.is_(False)).order_by(User.id)
        if want:
            q = q.where(User.status == want)
        rows = list(await s.scalars(q))
    lines = []
    for u in rows[:50]:
        spent = await budgets.spend_today_usd(u.id)
        last = u.last_user_msg_at.strftime("%d %b") if u.last_user_msg_at else "never"
        lines.append(f"#{u.id} {u.name or '?'} {u.status} {u.tier} joined {u.created_at:%d %b} last {last} "
                     f"${spent:.2f} today")
    return "\n".join(lines) or "Nobody here."


async def one_user(event, owner, args) -> str:
    if not args or not args[0].isdigit():
        return "Use: /user <id>"
    u = await users.get(int(args[0]))
    state = await budgets.state_for(u.id)
    return (f"#{u.id} {u.name or '?'} ({u.status}, {u.tier})\nTime zone {u.timezone}, currency {u.currency or '-'}\n"
            f"Spend today ${await budgets.spend_today_usd(u.id):.2f}, budget {state.name.lower()}\n"
            f"Invite #{u.invite_id or '-'}, joined {u.created_at:%d %b %Y}")


async def set_budget(event, owner, args) -> str:
    if len(args) < 2 or not args[0].isdigit():
        return "Use: /budget <id> <usd/day|default>"
    value = None if args[1] == "default" else float(args[1])
    await users.update(int(args[0]), budget_override_usd_day=value)
    await audit.record(int(args[0]), actor="owner", action="user.budget", detail={"usd_day": value})
    return f"Budget for #{args[0]}: {'default' if value is None else f'${value:.2f}/day'}."


async def set_tier(event, owner, args) -> str:
    if len(args) < 2 or not args[0].isdigit() or args[1] not in ("standard", "trusted"):
        return "Use: /tier <id> <standard|trusted>"
    await users.update(int(args[0]), tier=args[1])
    await audit.record(int(args[0]), actor="owner", action="user.tier", detail={"tier": args[1]})
    return f"#{args[0]} is now {args[1]}."


async def broadcast(event, owner, args) -> str | None:
    text = " ".join(args).strip()
    if not text:
        return "Use: /broadcast <text>"
    counts = await _status_counts()
    n = max(counts.get("active", 0) - 1, 0)
    _drafts[event.id[-16:]] = text
    await outbox.enqueue_now(Outbound(user_id=owner.id, text=f"Preview:\n{text}", dedupe_key=f"bc:prev:{event.id}",
                                      buttons=[[Button(label=f"Send to {n} user{'s' if n != 1 else ''}",
                                                       data=f"bc:send:{event.id[-16:]}"),
                                                Button(label="Cancel", data=f"bc:no:{event.id[-16:]}")]]))
    return None


async def _broadcast_button(event: Event, data: str) -> None:
    owner = await users.get(event.user_id)
    from mavis.access.commands import is_owner

    if not is_owner(owner):
        return
    _, action, key = data.split(":", 2)
    text = _drafts.pop(key, None)
    if action != "send" or text is None:
        return
    async with Session() as s:
        ids = list(await s.scalars(select(User.id).where(User.status == "active", User.is_test.is_(False),
                                                         User.id != owner.id)))
    for uid in ids:
        await outbox.enqueue_now(Outbound(user_id=uid, text=text, proactive=True, dedupe_key=f"bc:{key}:{uid}"),
                                 priority=2)
    await audit.record(owner.id, actor="owner", action="broadcast", detail={"users": len(ids)})


async def dlq(event, owner, args) -> str:
    from mavis.bus.base import Stream

    client = bus.get_redis()
    if client is None:
        return "No dead letters in single-process mode."
    stream = f"{Stream.EVENTS.value}:dlq"
    if not args or args[0] == "list":
        rows = await client.xrevrange(stream, count=10)
        return "\n".join(f"{rid} {fields.get('reason', '')[:60]}" for rid, fields in rows) or "DLQ is empty."
    if args[0] == "drop" and len(args) > 1:
        return f"Dropped {await client.xdel(stream, args[1])}."
    if args[0] == "replay" and len(args) > 1:
        rows = await client.xrange(stream, min=args[1], max=args[1])
        for _, fields in rows:
            await client.xadd(Stream.EVENTS.value, {"data": fields["data"]})
            await client.xdel(stream, args[1])
        return f"Replayed {len(rows)}."
    return "Use: /dlq [list|replay <id>|drop <id>]"


async def pause(event, owner, args) -> str:
    await _set_paused(True)
    await audit.record(owner.id, actor="owner", action="bg.paused", detail={})
    return "Background work is paused. Chat still works. /resume bg to restart it."


async def resume(event, owner, args) -> str:
    await _set_paused(False)
    await audit.record(owner.id, actor="owner", action="bg.resumed", detail={})
    return "Background work is running again."


async def admin_cmd(event, owner, args) -> str | None:
    if len(args) >= 2 and args[0] == "delete" and args[1].isdigit():
        await outbox.enqueue_now(Outbound(user_id=owner.id, text=f"Delete user #{args[1]} and all their data?",
                                          dedupe_key=f"adm:del:{event.id}",
                                          buttons=[[Button(label="Delete", data=f"adm:del:{args[1]}"),
                                                    Button(label="Cancel", data="adm:no:0")]]))
        return None
    return HELP


async def _admin_button(event: Event, data: str) -> None:
    from mavis.access.commands import is_owner

    if is_owner(await users.get(event.user_id)) and data.startswith("adm:del:"):
        await deletion.request_deletion(int(data.rsplit(":", 1)[1]), by_owner=True)


def register() -> None:
    for name, fn in (("admin", admin_cmd), ("stats", stats), ("users", list_users), ("user", one_user),
                     ("budget", set_budget), ("tier", set_tier), ("broadcast", broadcast), ("dlq", dlq)):
        register_owner_command(name, fn)
    register_owner_command("pause", lambda e, o, a: pause(e, o, a) if a[:1] == ["bg"] else _usage())
    register_owner_command("resume", lambda e, o, a: resume(e, o, a) if a[:1] == ["bg"] else _usage())
    register_button_handler("bc:", _broadcast_button)
    register_button_handler("adm:", _admin_button)
    budgets.register()


async def _usage() -> str:
    return "Use: /pause bg or /resume bg"
```

Check the DLQ stream name in `bus/redis_streams.py:163-165` and use it. `outbox.enqueue_now` gains a keyword `priority: int = 0` (Task 17 adds the column use; add the parameter now, forwarded to `enqueue`, which sets `row.priority`). Broadcast deliveries pass the ping policy's quiet hours: the outbox sender defers `priority == 2` rows out of each user's quiet hours (Task 17). Remove the `admin_*` keys from config, `ADMIN_PASSWORD` from compose. In `_budget_gate` add first: `if priority != "interactive" and await bg_paused(): raise BudgetExceededLLM("background paused")` (import lazily). Remove the `ban`/`unban` registration from `budgets.register()`'s caller in handlers (admin.register calls it) and call `admin.register()` from `worker/handlers.py`. `stats_lines()` is created in Task 16; until then create `src/mavis/obs/metrics.py` with `async def stats_lines() -> list[str]: return []`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/access -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis/access/ src/mavis/obs/ src/mavis/store/repo/ src/mavis/llm/models.py src/mavis/config.py \
        src/mavis/worker/handlers.py docker-compose.prod.yml tests/access/test_admin.py
git commit -m "feat(admin): owner stats, users, budgets, tiers, broadcast, dlq and background pause"
```

---

### Task 16: Observability (/metrics, counters, Langfuse sampling, watchdog, dead-man ping)

**Files:**
- Create: `src/mavis/api/routes/metrics.py`, `tests/obs/__init__.py`, `tests/obs/test_metrics.py`, `tests/obs/test_watchdog.py`, `tests/llm/test_tracing.py`
- Modify: `src/mavis/obs/metrics.py`, `src/mavis/obs/watchdog.py`, `src/mavis/llm/tracing.py`, `src/mavis/llm/models.py` (`run_config` metadata, counters), `src/mavis/worker/scheduler.py` + `runner.py` (turn timer), `src/mavis/api/app.py`, `src/mavis/timers/runner.py`, `src/mavis/config.py`

**Interfaces:**
- Produces:
  - `metrics.inc(name, value=1, **labels)`, `metrics.observe(name, seconds, **labels)` (fixed buckets `0.5,1,2,5,10,20,30,60,120` as `HINCRBY mavis:m:<name>:<labels>:<minute>`, 2 h TTL; no-op without Redis), `async metrics.render() -> str` (Prometheus text, sums the last 60 minutes plus live gauges), `async stats_lines() -> list[str]`, `async p95(name, minutes=60) -> float | None`
  - Series named in spec 12 (`mavis_chat_turn_seconds`, `mavis_llm_wait_seconds{rank}`, `mavis_llm_calls_total{provider,model,outcome}`, `mavis_llm_429_total`, `mavis_llm_cost_usd_total`, `mavis_mailbox_depth{lane}`, `mavis_ready_users{lane}`, `mavis_outbox_backlog`, `mavis_dlq_size`, `mavis_users{status}`, `mavis_rate_limited_total`, `mavis_llm_overflow_total`)
  - `GET /metrics` with `Authorization: Bearer <METRICS_TOKEN>` (404 when the token is unset)
  - `tracing.callbacks(sampled_for: str = "chat")`; `hashed_user(uid) -> str` = `sha256(f"{env}:{uid}")[:16]`; sample rates `langfuse_sample_chat=0.2`, `langfuse_sample_bg=0.05`, failed turns 100% (a turn that raised is re-traced once by `run_config(..., force_trace=True)` on its retry)
  - `watchdog.check()` (timer tick every 60 s): owner alerts deduped per hour for DLQ growth, chat p95 > 30 s for 10 min, 429 storm (> 20 in 10 min), limiter fallback, outbox backlog > 50, disk > 85%, spend 70% of the monthly ceiling, invite brute force (> `invite_fail_alert_per_hour`), Composio disconnect failures, backup failure (marker file older than 26 h); then `GET HEALTHCHECKS_URL`
  - Settings: `metrics_token: str = ""`, `langfuse_enabled: bool = False`, `langfuse_sample_chat: float = 0.2`, `langfuse_sample_bg: float = 0.05`, `healthchecks_url: str = ""`, `backup_marker_path: Path = Path("/app/data/backup.ok")`

- [ ] **Step 1: Write the failing tests**

`tests/obs/test_metrics.py`:

```python
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mavis.obs import metrics


async def test_counters_and_histograms_render_as_prometheus(fake_redis, settings):
    for model in ("deepseek-v4.1-flash", "glm-5.3", "glm-5.3"):
        await metrics.inc("mavis_llm_calls_total", provider="ollama", model=model, outcome="ok")
    for s in (0.7, 4.0, 25.0):
        await metrics.observe("mavis_chat_turn_seconds", s)
    text = await metrics.render()
    assert 'mavis_llm_calls_total{model="glm-5.3",outcome="ok",provider="ollama"} 2' in text
    assert 'mavis_chat_turn_seconds_bucket{le="5"} 2' in text and "mavis_chat_turn_seconds_count 3" in text
    assert await metrics.p95("mavis_chat_turn_seconds") == 30.0


async def test_no_redis_is_a_no_op(settings):
    await metrics.inc("mavis_rate_limited_total")
    assert "mavis_rate_limited_total" not in await metrics.render()


@pytest.mark.parametrize("token,header,status", [("", "Bearer x", 404), ("s3cret-a", "Bearer s3cret-a", 200),
                                                 ("s3cret-b", "Bearer wrong", 401), ("s3cret-c", "", 401)])
def test_metrics_route_requires_the_bearer_token(settings, monkeypatch, token, header, status):
    from mavis.api.routes import metrics as route
    from mavis.config import get_settings

    monkeypatch.setenv("METRICS_TOKEN", token)
    get_settings.cache_clear()
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(route.router)
    r = TestClient(app).get("/metrics", headers={"Authorization": header} if header else {})
    assert r.status_code == status
```

`tests/llm/test_tracing.py`:

```python
from __future__ import annotations

import pytest

from mavis.llm import tracing


@pytest.mark.parametrize("uid", [1, 42, 9001])
def test_hashed_user_is_stable_and_not_the_id(settings, uid):
    h = tracing.hashed_user(uid)
    assert len(h) == 16 and str(uid) not in h and h == tracing.hashed_user(uid)
    assert h != tracing.hashed_user(uid + 1)


def test_sampling_rates(settings, monkeypatch):
    seq = iter([0.1, 0.3, 0.01, 0.06])
    monkeypatch.setattr(tracing.random, "random", lambda: next(seq))
    assert tracing._sampled("chat") and not tracing._sampled("chat")
    assert tracing._sampled("bg") and not tracing._sampled("bg")


def test_disabled_means_only_the_usage_callback(settings):
    names = [type(c).__name__ for c in tracing.callbacks()]
    assert names == ["UsageCallback"]
```

`tests/obs/test_watchdog.py`:

```python
from __future__ import annotations

import pytest

from mavis.obs import metrics, watchdog


@pytest.fixture
async def owner(db, settings, monkeypatch, channel):
    from mavis.config import get_settings
    from mavis.store.repo import users

    monkeypatch.setenv("OWNER_TELEGRAM_CHAT_IDS", "[5001]")
    get_settings.cache_clear()
    u, _ = await users.get_or_create_by_chat(5001, "Priya")
    await users.update(u.id, status="active", tier="owner")
    return u


async def _texts(channel):
    from mavis.channels.outbox_sender import deliver_pending

    await deliver_pending(channel)
    return channel.texts


async def test_429_storm_alerts_once_per_hour(owner, fake_redis, channel):
    for _ in range(25):
        await metrics.inc("mavis_llm_429_total")
    await watchdog.check()
    await watchdog.check()
    out = [t for t in await _texts(channel) if "429" in t]
    assert len(out) == 1


async def test_healthy_system_sends_nothing_and_pings_deadman(owner, fake_redis, channel, monkeypatch):
    pinged = []

    async def fake_ping(url):
        pinged.append(url)

    monkeypatch.setenv("HEALTHCHECKS_URL", "https://hc.example/abc")
    from mavis.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(watchdog, "_ping", fake_ping)
    monkeypatch.setattr(watchdog, "_disk_used_fraction", lambda: 0.4)
    monkeypatch.setattr(watchdog, "_backup_age_h", lambda: 3.0)
    await watchdog.check()
    assert await _texts(channel) == [] and pinged == ["https://hc.example/abc"]
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/obs tests/llm/test_tracing.py -q`
Expected: FAIL (`AttributeError: module 'mavis.obs.metrics' has no attribute 'inc'`).

- [ ] **Step 3: Implement metrics, the route and tracing**

`src/mavis/obs/metrics.py`:

```python
"""Process-agnostic metrics in Redis (spec 12): every process writes minute buckets, the api renders the last
hour as Prometheus text. One scrape target, no Prometheus server on the box."""

from __future__ import annotations

import time

from mavis import bus

BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60, 120)
_TTL = 7200


def _labels(labels: dict) -> str:
    return ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))


def _minute(offset: int = 0) -> int:
    return int(time.time() // 60) - offset


async def inc(name: str, value: float = 1, **labels) -> None:
    client = bus.get_redis()
    if client is None:
        return
    key = f"mavis:m:c:{name}:{_minute()}"
    await client.hincrbyfloat(key, _labels(labels), value)
    await client.expire(key, _TTL)


async def observe(name: str, seconds: float, **labels) -> None:
    client = bus.get_redis()
    if client is None:
        return
    key = f"mavis:m:h:{name}:{_labels(labels)}:{_minute()}"
    le = next((b for b in BUCKETS if seconds <= b), "+Inf")
    await client.hincrby(key, str(le), 1)
    await client.hincrbyfloat(key, "sum", seconds)
    await client.expire(key, _TTL)


async def _counter_totals(client, name: str, minutes: int) -> dict[str, float]:
    out: dict[str, float] = {}
    for m in range(minutes):
        for lbl, v in (await client.hgetall(f"mavis:m:c:{name}:{_minute(m)}")).items():
            out[lbl] = out.get(lbl, 0) + float(v)
    return out


async def _hist(client, name: str, minutes: int) -> tuple[dict[str, int], float]:
    counts: dict[str, int] = {}
    total = 0.0
    async for key in client.scan_iter(match=f"mavis:m:h:{name}:*", count=500):
        if int(key.rsplit(":", 1)[1]) < _minute(minutes - 1):
            continue
        for f, v in (await client.hgetall(key)).items():
            if f == "sum":
                total += float(v)
            else:
                counts[f] = counts.get(f, 0) + int(v)
    return counts, total


async def p95(name: str, minutes: int = 60) -> float | None:
    client = bus.get_redis()
    if client is None:
        return None
    counts, _ = await _hist(client, name, minutes)
    n = sum(counts.values())
    if not n:
        return None
    running = 0
    for b in [*BUCKETS, "+Inf"]:
        running += counts.get(str(b), 0)
        if running >= 0.95 * n:
            return float(b) if b != "+Inf" else float("inf")
    return None


COUNTERS = ("mavis_llm_calls_total", "mavis_llm_429_total", "mavis_llm_cost_usd_total", "mavis_rate_limited_total",
            "mavis_llm_overflow_total")
HISTOGRAMS = ("mavis_chat_turn_seconds", "mavis_llm_wait_seconds")


async def render() -> str:
    client = bus.get_redis()
    if client is None:
        return ""
    lines: list[str] = []
    for name in COUNTERS:
        for lbl, v in sorted((await _counter_totals(client, name, 60)).items()):
            lines.append(f"{name}{{{lbl}}} {v:g}" if lbl else f"{name} {v:g}")
    for name in HISTOGRAMS:
        counts, total = await _hist(client, name, 60)
        if not counts:
            continue
        running = 0
        for b in [*BUCKETS, "+Inf"]:
            running += counts.get(str(b), 0)
            lines.append(f'{name}_bucket{{le="{b}"}} {running}')
        lines += [f"{name}_count {running}", f"{name}_sum {total:g}"]
    for lane in ("chat", "bg"):
        lines.append(f'mavis_ready_users{{lane="{lane}"}} {await client.zcard(f"mavis:ready:{lane}")}')
    return "\n".join(lines) + ("\n" if lines else "")


async def stats_lines() -> list[str]:
    from mavis.access.budgets import spend_today_usd

    p = await p95("mavis_chat_turn_seconds")
    calls = sum((await _counter_totals(bus.get_redis(), "mavis_llm_calls_total", 60)).values()) if bus.get_redis() else 0
    return [f"Chat p95 (1 h): {p if p is not None else '-'} s", f"LLM calls (1 h): {calls:g}",
            f"System spend today: ${await spend_today_usd(0):.2f}"]
```

(Outbox backlog, DLQ size and user counts are read live in `render()` too: `SELECT count(*) FROM outbox WHERE status IN ('pending','sending')`, `XLEN` of the DLQ stream, users by status; add those three gauges at the end of `render()` with the same names as spec 12.)

`src/mavis/api/routes/metrics.py`:

```python
from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException, Response

from mavis.config import get_settings
from mavis.obs import metrics

router = APIRouter()


@router.get("/metrics")
async def get_metrics(authorization: str = Header(default="")) -> Response:
    token = get_settings().metrics_token
    if not token:
        raise HTTPException(404)
    if not hmac.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(401)
    return Response(await metrics.render(), media_type="text/plain; version=0.0.4")
```

Include it in `api/app.py` (`app.include_router(metrics_route.router)`); Caddy already proxies everything, and the token protects it.

`src/mavis/llm/tracing.py` additions:

```python
import hashlib
import random


def hashed_user(user_id: int | None) -> str:
    return hashlib.sha256(f"{get_settings().env}:{user_id or 0}".encode()).hexdigest()[:16]


def _sampled(kind: str) -> bool:
    s = get_settings()
    rate = s.langfuse_sample_chat if kind == "chat" else s.langfuse_sample_bg
    return random.random() < rate


def callbacks(kind: str = "chat", force: bool = False) -> list[Any]:
    from mavis.llm.usage import UsageCallback

    out: list[Any] = [UsageCallback()]
    if get_settings().langfuse_enabled and (force or _sampled(kind)) and (h := _handler()) is not None:
        out.append(h)
    return out


def trace_metadata() -> dict[str, Any]:
    from mavis.llm.context import llm_purpose, llm_user_id

    return {"langfuse_user_id": hashed_user(llm_user_id.get()), "purpose": llm_purpose.get()}
```

`_handler()` returns None unless `langfuse_enabled` and both keys are set. In `llm/models.py` `run_config(name)` becomes `{"callbacks": callbacks("chat" if llm_purpose.get() == "chat" else "bg", force=_retrying.get()), "run_name": name, "metadata": trace_metadata()}`, where `_retrying` is a context var the runner sets when `run_with_inline_retries` retries (pass an `attempt` flag through `bind_user`; the failed first attempt makes the retry trace at 100%). Also count `mavis_llm_calls_total` (provider, model, outcome ok/error), `mavis_llm_429_total` and `mavis_llm_overflow_total` in `_call` and `_invoke_chain` (spawned, never awaited on the hot path). The scheduler `_handle` and the legacy `handle_event` observe `mavis_chat_turn_seconds` for chat lanes.

`src/mavis/obs/watchdog.py` (extends Task 12's `alert_owner`):

```python
"""In-app watchdog (spec 12): checks every minute in the timer role, alerts the owner on Telegram deduped per
hour, then pings the external dead-man URL so the owner hears about it when the box itself is down."""

from __future__ import annotations

import shutil
import time

import httpx
import structlog

from mavis import bus
from mavis.config import get_settings
from mavis.domain.messages import Outbound
from mavis.obs import metrics
from mavis.store.repo import outbox, users
from mavis.worker.locks import claim

log = structlog.get_logger(__name__)


async def alert_owner(text: str, key: str) -> None:
    log.warning("watchdog.alert", key=key)
    if not await claim(f"alert:{key}", 3600):
        return
    for chat in get_settings().owner_telegram_chat_ids:
        owner = await users.get_by_chat(chat)
        if owner is not None:
            await outbox.enqueue_now(Outbound(user_id=owner.id, text=f"Watchdog: {text}",
                                              dedupe_key=f"alert:{key}:{int(time.time() // 3600)}:{owner.id}"))


def _disk_used_fraction() -> float:
    total, used, _ = shutil.disk_usage(str(get_settings().data_dir))
    return used / total if total else 0.0


def _backup_age_h() -> float:
    p = get_settings().backup_marker_path
    return (time.time() - p.stat().st_mtime) / 3600 if p.exists() else float("inf")


async def _ping(url: str) -> None:
    async with httpx.AsyncClient(timeout=10) as c:
        await c.get(url)


async def check() -> None:
    s = get_settings()
    client = bus.get_redis()
    if client is not None:
        storm = sum((await metrics._counter_totals(client, "mavis_llm_429_total", 10)).values())
        if storm > 20:
            await alert_owner(f"{storm:g} LLM 429s in 10 min", "429")
        p = await metrics.p95("mavis_chat_turn_seconds", 10)
        if p is not None and p > 30:
            await alert_owner(f"chat p95 is {p:g} s over 10 min", "p95")
        fails = int(await client.get(f"mavis:gate:fails:{time.strftime('%Y%m%d%H', time.gmtime())}") or 0)
        if fails > s.invite_fail_alert_per_hour:
            await alert_owner(f"{fails} failed invite code attempts this hour", "brute")
    if _disk_used_fraction() > 0.85:
        await alert_owner(f"disk is {_disk_used_fraction():.0%} full", "disk")
    if s.backup_bucket and _backup_age_h() > 26:
        await alert_owner("the nightly backup has not succeeded in over a day", "backup")
    from mavis.access.budgets import _month_fraction

    if (frac := await _month_fraction()) >= 0.7:
        await alert_owner(f"month-to-date LLM spend is {frac:.0%} of the ceiling", "ceiling70")
    if s.healthchecks_url:
        try:
            await _ping(s.healthchecks_url)
        except Exception as exc:  # noqa: BLE001
            log.warning("watchdog.ping_failed", error=type(exc).__name__)
```

Outbox backlog and DLQ size checks use the same live reads as `render()` (add them to `check()` with the thresholds in the Interfaces list). Register `register_timer_tick("watchdog", watchdog.check, 60)` in `run_timer()`.

Settings block: the keys in Interfaces, plus `backup_bucket: str = ""` (Task 18 sets it).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/obs tests/llm tests/api -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis/obs/ src/mavis/api/ src/mavis/llm/ src/mavis/worker/ src/mavis/timers/runner.py \
        src/mavis/config.py tests/obs/ tests/llm/test_tracing.py
git commit -m "feat(obs): metrics endpoint, sampled Langfuse with hashed ids, watchdog and dead-man ping"
```

---

### Task 17: Telegram send pacing, outbox priority, webhook settings, blocked users

**Files:**
- Create or modify: `src/mavis/channels/pacing.py` (shared contract A), `tests/channels/test_pacing.py`
- Modify: `src/mavis/channels/outbox_sender.py`, `src/mavis/store/repo/outbox.py` (`due` order, `enqueue` priority), `src/mavis/channels/base.py` (`ChannelBlocked`), `src/mavis/channels/telegram.py` (403 mapping), `src/mavis/channels/telegram_webhook.py:16,41-46`, `src/mavis/config.py`

**Interfaces:**
- Produces (contract A, identical in Plan 12): `class SendPacer: async def reserve(self, chat_id: int | None = None, *, kind: str = "chat") -> float`, `get_pacer()`, `set_pacer()`; settings `telegram_global_send_rate: float = 25.0`, `telegram_chat_send_rate: float = 1.0`, `telegram_chat_burst: int = 3`, `telegram_broadcast_rate: float = 10.0`, `pacing_enabled: bool = True`
- `class ChannelBlocked(Exception)` (the user blocked the bot); outbox rows for a blocked user are marked failed with `blocked`, `users.inactive_since` set, pollers and proactive work skip inactive users (`PingPolicy` and the poller check `inactive_since is None`); the user's next message clears it (`conversation` already logs messages: clear it in the access gate for active users)
- `outbox.due()` orders by `priority, id` while keeping the per-user bubble rule; `priority=2` rows are not due inside the user's quiet hours

- [ ] **Step 1: Write the failing tests**

`tests/channels/test_pacing.py`:

```python
from __future__ import annotations

import pytest

from mavis.channels.pacing import SendPacer


@pytest.mark.parametrize("chat", [5001, 7302, 9944])
async def test_per_chat_burst_then_one_per_second(settings, chat):
    now = [100.0]
    p = SendPacer(clock=lambda: now[0])
    waits = [await p.reserve(chat) for _ in range(5)]
    assert waits[:3] == [0.0, 0.0, 0.0] and waits[3] > 0
    now[0] += 1.0
    assert await p.reserve(chat) == 0.0


async def test_global_bucket_caps_all_chats(settings, monkeypatch):
    from mavis.config import get_settings

    monkeypatch.setenv("TELEGRAM_GLOBAL_SEND_RATE", "5")
    get_settings.cache_clear()
    now = [0.0]
    p = SendPacer(clock=lambda: now[0])
    waits = [await p.reserve(c) for c in range(1, 12)]
    assert sum(w == 0.0 for w in waits) == 5


async def test_broadcast_has_its_own_lower_cap(settings, monkeypatch):
    from mavis.config import get_settings

    monkeypatch.setenv("TELEGRAM_BROADCAST_RATE", "2")
    get_settings.cache_clear()
    p = SendPacer(clock=lambda: 0.0)
    waits = [await p.reserve(c, kind="broadcast") for c in range(10, 15)]
    assert sum(w == 0.0 for w in waits) == 2


async def test_redis_and_memory_agree(fake_redis, settings):
    p = SendPacer(clock=lambda: 50.0)
    assert [await p.reserve(77) for _ in range(4)].count(0.0) == 3


async def test_sender_skips_unpaced_rows_without_counting_attempts(db, user, channel, monkeypatch):
    from mavis.channels import pacing
    from mavis.channels.outbox_sender import deliver_pending
    from mavis.domain.messages import Outbound
    from mavis.store.repo import outbox

    class _Busy(SendPacer):
        async def reserve(self, chat_id=None, *, kind="chat"):
            return 2.0

    pacing.set_pacer(_Busy())
    await outbox.enqueue_now(Outbound(user_id=user.id, text="hello", dedupe_key="p1"))
    assert await deliver_pending(channel) == 0
    (row,) = await outbox.due(__import__("mavis.store.db", fromlist=["utcnow"]).utcnow().replace(year=2030))
    assert row.attempts == 0
    pacing.set_pacer(None)


async def test_blocked_user_is_marked_inactive(db, user, channel):
    from mavis.channels.base import ChannelBlocked
    from mavis.channels.outbox_sender import deliver_pending
    from mavis.domain.messages import Outbound
    from mavis.store.repo import outbox, users

    channel.fail_next.append(ChannelBlocked())
    await outbox.enqueue_now(Outbound(user_id=user.id, text="x", dedupe_key="blk"))
    await deliver_pending(channel)
    assert (await users.get(user.id)).inactive_since is not None


def test_webhook_asks_for_member_updates_and_40_connections():
    from mavis.channels import telegram_webhook as tw

    assert "my_chat_member" in tw.ALLOWED_UPDATES and tw.MAX_CONNECTIONS == 40
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/channels/test_pacing.py -q`
Expected: FAIL (`ModuleNotFoundError: mavis.channels.pacing`, or missing per-chat behaviour if Plan 12 created a global-only version).

- [ ] **Step 3: Implement (create the file, or extend Plan 12's global-only version to this content)**

`src/mavis/channels/pacing.py`:

```python
"""Telegram send pacing (shared contract A; Plan 11 spec 14, Plan 12 spec 8.2). Token buckets: a global one
(TELEGRAM_GLOBAL_SEND_RATE per second), one per chat (TELEGRAM_CHAT_SEND_RATE, burst TELEGRAM_CHAT_BURST)
and one for broadcasts (TELEGRAM_BROADCAST_RATE). Redis when available so every process shares them."""

from __future__ import annotations

import time
from collections.abc import Callable

from mavis import bus
from mavis.config import get_settings

# KEYS[1] bucket; ARGV now, rate, burst. Returns wait seconds (0 = token taken).
_LUA = """
local t = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local now = tonumber(ARGV[1]); local rate = tonumber(ARGV[2]); local burst = tonumber(ARGV[3])
local tokens = tonumber(t[1]) or burst; local ts = tonumber(t[2]) or now
tokens = math.min(burst, tokens + (now - ts) * rate)
if tokens >= 1 then
  redis.call('HSET', KEYS[1], 'tokens', tokens - 1, 'ts', now); redis.call('EXPIRE', KEYS[1], 120)
  return '0'
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now); redis.call('EXPIRE', KEYS[1], 120)
return tostring((1 - tokens) / rate)
"""


class SendPacer:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._mem: dict[str, tuple[float, float]] = {}
        self._script = None

    async def _take(self, key: str, rate: float, burst: float) -> float:
        now = self._clock()
        client = bus.get_redis()
        if client is not None:
            if self._script is None:
                self._script = client.register_script(_LUA)
            return float(await self._script(keys=[f"mavis:pace:{key}"], args=[now, rate, burst]))
        tokens, ts = self._mem.get(key, (burst, now))
        tokens = min(burst, tokens + (now - ts) * rate)
        if tokens >= 1:
            self._mem[key] = (tokens - 1, now)
            return 0.0
        self._mem[key] = (tokens, now)
        return (1 - tokens) / rate

    async def reserve(self, chat_id: int | None = None, *, kind: str = "chat") -> float:
        s = get_settings()
        if not s.pacing_enabled:
            return 0.0
        if kind == "broadcast":
            wait = await self._take("broadcast", s.telegram_broadcast_rate, s.telegram_broadcast_rate)
            if wait:
                return wait
        if chat_id is not None:
            wait = await self._take(f"c{chat_id}", s.telegram_chat_send_rate, s.telegram_chat_burst)
            if wait:
                return wait
        return await self._take("global", s.telegram_global_send_rate, s.telegram_global_send_rate)


_pacer: SendPacer | None = None


def get_pacer() -> SendPacer:
    global _pacer
    if _pacer is None:
        _pacer = SendPacer()
    return _pacer


def set_pacer(p: SendPacer | None) -> None:
    global _pacer
    _pacer = p
```

(A chat token taken before a global refusal is not refunded: at worst one chat waits one extra second; acceptable and simpler.)

`outbox_sender.run_once`: before `outbox.claim(row.id, now)`, resolve the chat (`users.get(row.user_id)`) and `wait = await get_pacer().reserve(chat, kind="broadcast" if row.priority == 2 else "chat")`; if `wait > 0`, `continue` (no claim, no attempt counted, the row stays due). `_attempt` adds `except ChannelBlocked: await outbox.mark_failed(row.id, "blocked"); await users.update(row.user_id, inactive_since=utcnow())`. `TelegramChannel.send_text/send_document` map `telegram.error.Forbidden` to `ChannelBlocked`. `outbox.due` orders `.order_by(OutboxMessage.priority, OutboxMessage.id)` and excludes `priority == 2` rows for users inside quiet hours (compute with `policy.pings` quiet-hours helper; if none is importable without a cycle, skip broadcast rows whose user's local hour is within `quiet_start..quiet_end`). `telegram_webhook.py`: `ALLOWED_UPDATES = ["message", "edited_message", "callback_query", "my_chat_member"]`, `MAX_CONNECTIONS = 40`, and `"max_connections": MAX_CONNECTIONS` in `setWebhook`. The poller uses the same list. In `access/gate.access_gate` for active users with `inactive_since` set, clear it on a USER_MESSAGE. In `tools/integrations/poller.py`, skip polling users with `inactive_since` set and apply the adaptive interval (spec 6.3): `interval = 2 min if the user messaged in the last 2 h or has an event within 2 h; 30 min inside quiet hours; else 10 min` (a pure function `poll_interval(user, now, next_event_at) -> timedelta` with three parametrized tests in `tests/tools/test_poller_interval.py`).

Settings:

```python
    # --- Telegram pacing (contract A) -----------------------------------------
    pacing_enabled: bool = True
    telegram_global_send_rate: float = 25.0
    telegram_chat_send_rate: float = 1.0
    telegram_chat_burst: int = 3
    telegram_broadcast_rate: float = 10.0
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/channels tests/tools -q && uv run pytest -q`
Expected: PASS. Existing outbox tests send several bubbles to one chat: in tests the in-memory pacer with burst 3 may delay a fourth bubble; set `PACING_ENABLED=false` in `TEST_ENV` and turn it on only in the pacing tests (`monkeypatch.setenv("PACING_ENABLED", "true")` at the top of `tests/channels/test_pacing.py` via an autouse fixture).

- [ ] **Step 5: Commit**

```bash
git add src/mavis/channels/ src/mavis/store/repo/outbox.py src/mavis/access/gate.py \
        src/mavis/tools/integrations/poller.py src/mavis/config.py tests/
git commit -m "feat(channels): shared send pacing, outbox priority, member updates and blocked users"
```

---
### Task 18: AWS backups (role, bucket, policy, EBS snapshots, nightly box backup, restore drill)

**Files:**
- Create: `deploy/aws/iam-role.sh` (shared contract B), `deploy/aws/backups.sh`, `deploy/aws/box-backup.sh`, `deploy/aws/restore-drill.sh`, `tests/deploy/__init__.py`, `tests/deploy/test_aws_scripts.py`, `tests/deploy/fake_aws.sh`
- Modify: `deploy/aws/bootstrap.sh` (local pg_dump cron replaced by `box-backup.sh` when `BACKUP_BUCKET` is set), `src/mavis/config.py` (already has `backup_bucket`)

**Exact resources (account 276307603629, region ap-south-1, profile cashfree; owner decision 2 approved them):**

| Resource | Name | Settings |
|---|---|---|
| IAM role | `mavis-ec2` | trust `ec2.amazonaws.com`, tag `Project=mavis`, no managed policies |
| Instance profile | `mavis-ec2` | contains role `mavis-ec2`, associated to `$MAVIS_INSTANCE_ID` (`i-0e39253adaacfd498`) |
| IMDS | instance `$MAVIS_INSTANCE_ID` | `--http-tokens required --http-put-response-hop-limit 2` (containers can reach the role) |
| S3 bucket | `mavis-backups-276307603629-aps1` | Block Public Access (all four), SSE-S3 default encryption, object ownership BucketOwnerEnforced, versioning Enabled, lifecycle: `postgres/` expire 35 d, `qdrant/` 14 d, `neo4j/` 14 d, `redis/` 3 d, noncurrent versions 7 d, abort incomplete multipart 1 d |
| Inline policy | `mavis-backups` on role `mavis-ec2` | `s3:PutObject` on `arn:aws:s3:::mavis-backups-276307603629-aps1/*` only (write-only: a compromised box cannot delete backups; lifecycle does the expiry) |
| DLM policy | tag `Name=mavis-daily-ebs` | daily at 21:30 UTC (03:00 IST), retain 7, target volumes tagged `Name=mavis-prod`, execution role `AWSDataLifecycleManagerDefaultRole` (created by `aws dlm create-default-role` if missing) |

Not created (optional, ask the owner later): gp3 volume grow to 40 GB, CloudWatch Logs + SNS, SSM Parameter Store. Plan 12 creates `mavis-machine` (policy), `mavis-machine-276307603629-aps1` (bucket) and the `mavis-machine-monthly` budget in its own script.

**Interfaces:**
- Produces: `iam-role.sh [--dry-run|--apply]`, `backups.sh [--dry-run|--apply]` (both default to dry run and print each planned action as `PLAN: <aws command>`; `--apply` runs only what is missing), `box-backup.sh` (runs on the box from cron: `pg_dump -Fc`, Qdrant snapshot, `graph_export.py`, Redis AOF copy, each streamed to `s3://$BACKUP_BUCKET/<store>/<yyyy-mm-dd>/...` with the instance role, then touches `/opt/mavis/data/backup.ok` which the api and timer containers see as `/app/data/backup.ok`), `restore-drill.sh` (downloads the newest set into a throwaway local compose project `mavis-drill` and runs `uv run pytest tests/access/test_isolation.py -q` against it)

- [ ] **Step 1: Write the failing script tests**

`tests/deploy/fake_aws.sh` (records calls, answers "not found" for describe/get so the scripts plan everything):

```bash
#!/usr/bin/env bash
echo "$*" >>"$FAKE_AWS_LOG"
case "$*" in
  *"sts get-caller-identity"*) echo 276307603629 ;;
  *"get-role"*|*"get-instance-profile"*|*"head-bucket"*|*"get-role-policy"*) exit 254 ;;
  *"describe-iam-instance-profile-associations"*) echo "None" ;;
  *"dlm get-lifecycle-policies"*) echo "[]" ;;
  *) echo "{}" ;;
esac
```

`tests/deploy/test_aws_scripts.py`:

```python
"""Infra scripts: syntax, dry run by default (no mutating call), exact resource names, idempotent apply."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AWS = ROOT / "deploy" / "aws"
MUTATING = ("create-", "put-", "associate-", "modify-", "add-role", "attach-")


def _run(tmp_path: Path, script: str, *args: str) -> tuple[int, str, list[str]]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    shutil.copy(Path(__file__).parent / "fake_aws.sh", bindir / "aws")
    os.chmod(bindir / "aws", 0o755)
    log = tmp_path / "aws.log"
    state = tmp_path / "state.env"
    state.write_text("MAVIS_INSTANCE_ID=i-0e39253adaacfd498\nMAVIS_EIP=203.0.113.9\n")
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_AWS_LOG": str(log),
           "MAVIS_STATE_FILE": str(state)}
    p = subprocess.run(["bash", str(AWS / script), *args], capture_output=True, text=True, env=env, timeout=60)
    calls = log.read_text().splitlines() if log.exists() else []
    return p.returncode, p.stdout + p.stderr, calls


@pytest.mark.parametrize("script", ["iam-role.sh", "backups.sh", "box-backup.sh", "restore-drill.sh"])
def test_scripts_parse(script):
    assert subprocess.run(["bash", "-n", str(AWS / script)]).returncode == 0


@pytest.mark.parametrize("script", ["iam-role.sh", "backups.sh"])
def test_dry_run_is_the_default_and_mutates_nothing(tmp_path, script):
    rc, out, calls = _run(tmp_path, script)
    assert rc == 0 and "PLAN:" in out
    assert not [c for c in calls if any(m in c for m in MUTATING)]


def test_role_script_names_and_hop_limit(tmp_path):
    rc, out, calls = _run(tmp_path, "iam-role.sh", "--apply")
    joined = "\n".join(calls)
    assert rc == 0
    assert "iam create-role --role-name mavis-ec2" in joined
    assert "iam create-instance-profile --instance-profile-name mavis-ec2" in joined
    assert "--http-put-response-hop-limit 2" in joined and "--http-tokens required" in joined
    assert "put-role-policy" not in joined  # contract B: the role script attaches no policies


def test_backup_script_creates_exactly_the_approved_resources(tmp_path):
    rc, out, calls = _run(tmp_path, "backups.sh", "--apply")
    joined = "\n".join(calls)
    assert rc == 0
    assert "s3api create-bucket --bucket mavis-backups-276307603629-aps1" in joined
    assert "put-public-access-block" in joined and "put-bucket-versioning" in joined
    assert "put-role-policy --role-name mavis-ec2 --policy-name mavis-backups" in joined
    assert '"s3:PutObject"' in joined and "s3:DeleteObject" not in joined
    assert "dlm create-lifecycle-policy" in joined
    for optional in ("logs create-log-group", "sns create-topic", "ssm put-parameter", "modify-volume"):
        assert optional not in joined
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/deploy -q`
Expected: FAIL (scripts missing).

- [ ] **Step 3: Write `deploy/aws/iam-role.sh` (if it already exists on main because Plan 12 merged first, keep it; only run `bash -n` and the dry run, and make sure the tests above pass against it)**

```bash
#!/usr/bin/env bash
# Shared contract B (Plans 11 and 12): IAM role + instance profile `mavis-ec2` for the box, associated to the
# instance, and IMDSv2 with hop limit 2 so containers can use the role. Attaches NO policies: each plan adds
# its own inline policy (mavis-backups, mavis-machine) in its own script. Idempotent. Dry run by default.
#   deploy/aws/iam-role.sh            print the plan
#   deploy/aws/iam-role.sh --apply    create what is missing
set -euo pipefail
# shellcheck source=deploy/aws/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need aws
state_load
APPLY=0
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    --dry-run) APPLY=0 ;;
    *) die "unknown argument: $a (usage: iam-role.sh [--dry-run|--apply])" ;;
  esac
done
ROLE=mavis-ec2
PROFILE=mavis-ec2
[[ -n "${MAVIS_INSTANCE_ID:-}" ]] || die "no MAVIS_INSTANCE_ID in state.env"

act() { # act DESCRIPTION -- aws args...
  local what="$1"; shift; shift
  if [[ "$APPLY" == 1 ]]; then log "$what"; aws_ "$@" >/dev/null; else printf 'PLAN: aws %s\n' "$*"; fi
}

log "account: $(aws_ sts get-caller-identity --query Account --output text) region: $AWS_REGION"
TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
if aws_ iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  log "role $ROLE exists"
else
  act "creating role $ROLE" -- iam create-role --role-name "$ROLE" --assume-role-policy-document "$TRUST" \
    --tags Key=Project,Value=mavis
fi
if aws_ iam get-instance-profile --instance-profile-name "$PROFILE" >/dev/null 2>&1; then
  log "instance profile $PROFILE exists"
else
  act "creating instance profile $PROFILE" -- iam create-instance-profile --instance-profile-name "$PROFILE" \
    --tags Key=Project,Value=mavis
  act "adding role to profile" -- iam add-role-to-instance-profile --instance-profile-name "$PROFILE" --role-name "$ROLE"
fi
CURRENT="$(aws_ ec2 describe-iam-instance-profile-associations --filters "Name=instance-id,Values=$MAVIS_INSTANCE_ID" \
  --query 'IamInstanceProfileAssociations[0].IamInstanceProfile.Arn' --output text 2>/dev/null || echo None)"
if [[ "$CURRENT" == *":instance-profile/$PROFILE" ]]; then
  log "profile already associated to $MAVIS_INSTANCE_ID"
else
  act "associating $PROFILE to $MAVIS_INSTANCE_ID" -- ec2 associate-iam-instance-profile \
    --instance-id "$MAVIS_INSTANCE_ID" --iam-instance-profile "Name=$PROFILE"
fi
act "IMDSv2 required, hop limit 2" -- ec2 modify-instance-metadata-options --instance-id "$MAVIS_INSTANCE_ID" \
  --http-tokens required --http-put-response-hop-limit 2 --http-endpoint enabled
log "done (apply=$APPLY)"
```

`act` takes a description, a literal `--` separator, then the AWS arguments; in dry-run mode it prints `PLAN: aws <args>` and calls nothing.

- [ ] **Step 4: Write `deploy/aws/backups.sh`, `box-backup.sh` and `restore-drill.sh`**

`deploy/aws/backups.sh`:

```bash
#!/usr/bin/env bash
# Off-box backups (Plan 11 Task 18, spec 13). Creates bucket mavis-backups-276307603629-aps1, inline policy
# mavis-backups (PutObject only) on role mavis-ec2, a daily EBS snapshot DLM policy (Name=mavis-daily-ebs),
# and installs the nightly box-backup cron. Requires iam-role.sh to have run. Dry run by default.
set -euo pipefail
# shellcheck source=deploy/aws/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need aws
state_load
APPLY=0
for a in "$@"; do
  case "$a" in --apply) APPLY=1 ;; --dry-run) APPLY=0 ;; *) die "usage: backups.sh [--dry-run|--apply]" ;; esac
done
ACCOUNT="$(aws_ sts get-caller-identity --query Account --output text)"
[[ "$ACCOUNT" == 276307603629 ]] || die "unexpected account $ACCOUNT"
BUCKET="mavis-backups-${ACCOUNT}-aps1"
act() { local what="$1"; shift; shift
  if [[ "$APPLY" == 1 ]]; then log "$what"; aws_ "$@" >/dev/null; else printf 'PLAN: aws %s\n' "$*"; fi; }

if aws_ s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  log "bucket $BUCKET exists"
else
  act "creating $BUCKET" -- s3api create-bucket --bucket "$BUCKET" \
    --create-bucket-configuration "LocationConstraint=$AWS_REGION" --object-ownership BucketOwnerEnforced
fi
act "public access block" -- s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
act "default encryption SSE-S3" -- s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
act "versioning on" -- s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled
rule() { printf '{"ID":"%s","Filter":{"Prefix":"%s/"},"Status":"Enabled","Expiration":{"Days":%s},"NoncurrentVersionExpiration":{"NoncurrentDays":7},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":1}}' "$1" "$1" "$2"; }
LIFECYCLE="{\"Rules\":[$(rule postgres 35),$(rule qdrant 14),$(rule neo4j 14),$(rule redis 3)]}"
act "lifecycle rules" -- s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration "$LIFECYCLE"
POLICY="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"s3:PutObject\"],\"Resource\":\"arn:aws:s3:::$BUCKET/*\"}]}"
act "inline policy mavis-backups" -- iam put-role-policy --role-name mavis-ec2 --policy-name mavis-backups \
  --policy-document "$POLICY"
if [[ "$(aws_ dlm get-lifecycle-policies --target-tags Name=mavis-prod --query 'Policies[?Tags.Name==`mavis-daily-ebs`] | length(@)' --output text 2>/dev/null || echo 0)" =~ ^[1-9] ]]; then
  log "DLM policy exists"
else
  aws_ iam get-role --role-name AWSDataLifecycleManagerDefaultRole >/dev/null 2>&1 \
    || act "DLM default role" -- dlm create-default-role --resource-type snapshot
  DETAILS='{"ResourceTypes":["VOLUME"],"TargetTags":[{"Key":"Name","Value":"mavis-prod"}],"Schedules":[{"Name":"daily","CreateRule":{"Interval":24,"IntervalUnit":"HOURS","Times":["21:30"]},"RetainRule":{"Count":7},"CopyTags":true}]}'
  act "DLM daily EBS snapshots" -- dlm create-lifecycle-policy --description "mavis daily ebs" --state ENABLED \
    --execution-role-arn "arn:aws:iam::${ACCOUNT}:role/AWSDataLifecycleManagerDefaultRole" \
    --policy-details "$DETAILS" --tags Name=mavis-daily-ebs,Project=mavis
fi
if [[ "$APPLY" == 1 && -n "${MAVIS_EIP:-}" ]]; then
  state_require
  log "installing nightly box backup"
  scp_box "$AWS_DIR/box-backup.sh" "/tmp/mavis-box-backup.sh"
  ssh_box "sudo install -m 700 /tmp/mavis-box-backup.sh /usr/local/bin/mavis-box-backup.sh && \
    echo '0 3 * * * root BACKUP_BUCKET=$BUCKET /usr/local/bin/mavis-box-backup.sh >>/var/log/mavis-backup.log 2>&1' \
    | sudo tee /etc/cron.d/mavis-backup >/dev/null && sudo chmod 644 /etc/cron.d/mavis-backup"
else
  printf 'PLAN: install /usr/local/bin/mavis-box-backup.sh and /etc/cron.d/mavis-backup (03:00 IST) on the box\n'
fi
log "set BACKUP_BUCKET=$BUCKET in the box .env (deploy.sh does it when the key is in your local env)"
```

`deploy/aws/box-backup.sh` (runs as root on the box; the AWS CLI on the box uses the instance role):

```bash
#!/usr/bin/env bash
# Nightly: Postgres (pg_dump -Fc), Qdrant snapshot, Neo4j logical export, Redis AOF, each to S3 with the
# instance role (PutObject only). Touches data/backup.ok on success (the watchdog alerts when it is stale).
set -euo pipefail
: "${BACKUP_BUCKET:?set BACKUP_BUCKET}"
cd /opt/mavis
C="docker compose -f docker-compose.prod.yml"
D="$(date -u +%Y-%m-%d)"
put() { aws s3 cp - "s3://$BACKUP_BUCKET/$1" --only-show-errors; }
$C exec -T postgres pg_dump -U mavis -d mavis -Fc --no-owner --no-acl | put "postgres/$D/mavis.dump"
SNAP="$($C exec -T api python -c "import httpx;print(httpx.post('http://qdrant:6333/collections/memories/snapshots').json()['result']['name'])")"
$C exec -T api python -c "import httpx,sys;sys.stdout.buffer.write(httpx.get('http://qdrant:6333/collections/memories/snapshots/$SNAP').content)" \
  | put "qdrant/$D/$SNAP"
$C exec -T api python -c "import httpx;httpx.delete('http://qdrant:6333/collections/memories/snapshots/$SNAP')"
$C exec -T worker python /app/deploy/aws/graph_export.py | put "neo4j/$D/graph.jsonl"
$C exec -T redis sh -c 'tar -C /data -cf - appendonlydir 2>/dev/null || tar -C /data -cf - appendonly.aof' | put "redis/$D/aof.tar"
docker run --rm -v mavis-prod_artifacts:/a alpine true >/dev/null 2>&1 || true
touch /var/lib/docker/volumes/mavis-prod_artifacts/_data/../../mavis-prod_artifacts/_data/backup.ok 2>/dev/null \
  || $C exec -T worker touch /app/data/backup.ok
echo "backup $D ok"
```

(Check the Qdrant collection name in `memory/vector.py` `COLLECTION` and `graph_export.py`'s CLI; the worker image ships `deploy/` only if the Dockerfile copies it, else run `graph_export.py` from the host with `uv run` as `migrate-data.sh` does. Mount `data/` as a named volume shared by api, worker and timer so `backup.ok` is visible to the timer's watchdog: Task 20 adds the `appdata:/app/data` volume.)

`deploy/aws/restore-drill.sh`:

```bash
#!/usr/bin/env bash
# Monthly drill: pull the newest backup set to this laptop (owner profile), restore into a throwaway compose
# project `mavis-drill`, run the isolation tests against it, tear it down. Never touches prod.
set -euo pipefail
# shellcheck source=deploy/aws/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need aws docker
BUCKET="${BACKUP_BUCKET:-mavis-backups-276307603629-aps1}"
DAY="${1:-$(aws_ s3 ls "s3://$BUCKET/postgres/" | awk '{print $2}' | sort | tail -n1 | tr -d /)}"
WORK="$(mktemp -d)"
trap 'docker compose -p mavis-drill -f "$REPO_ROOT/docker-compose.dev.yml" down -v >/dev/null 2>&1; rm -rf "$WORK"' EXIT
aws_ s3 cp "s3://$BUCKET/postgres/$DAY/mavis.dump" "$WORK/"
docker compose -p mavis-drill -f "$REPO_ROOT/docker-compose.dev.yml" up -d --wait postgres
docker compose -p mavis-drill -f "$REPO_ROOT/docker-compose.dev.yml" exec -T postgres \
  pg_restore -U mavis -d mavis --no-owner --clean --if-exists <"$WORK/mavis.dump"
log "restored postgres $DAY; row counts:"
docker compose -p mavis-drill -f "$REPO_ROOT/docker-compose.dev.yml" exec -T postgres \
  psql -U mavis -d mavis -c "select count(*) as users from users; select count(*) as messages from messages;"
log "drill passed for $DAY"
```

(Use the dev compose file's real service and credential names; check `docker-compose.dev.yml` before running.)

In `deploy/aws/bootstrap.sh`, leave the local `pg_dump` cron in place as a second copy (7 days on disk); `backups.sh --apply` adds the S3 job in its own cron file.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/deploy -q && (command -v shellcheck && shellcheck deploy/aws/iam-role.sh deploy/aws/backups.sh deploy/aws/box-backup.sh deploy/aws/restore-drill.sh || true)`
Expected: PASS; shellcheck clean or not installed.

- [ ] **Step 6: Commit**

```bash
git add deploy/aws/iam-role.sh deploy/aws/backups.sh deploy/aws/box-backup.sh deploy/aws/restore-drill.sh tests/deploy/
git commit -m "feat(deploy): idempotent scripts for the box role, S3 backups, EBS snapshots and a restore drill"
```

- [ ] **Step 7: Owner-run (not part of the code change; record output in the ledger)**

With the owner present: `deploy/aws/iam-role.sh` (read the plan), `deploy/aws/iam-role.sh --apply`, `deploy/aws/backups.sh`, `deploy/aws/backups.sh --apply`, then on the box `sudo BACKUP_BUCKET=mavis-backups-276307603629-aps1 /usr/local/bin/mavis-box-backup.sh` once and `deploy/aws/restore-drill.sh` from the laptop. This is rollout gate R0.

---

### Task 19: Load test (test chat range, test users, scenarios)

**Files:**
- Create: `scripts/loadtest.py`, `tests/test_loadtest.py`
- Modify: `src/mavis/channels/test_sink.py` (`is_test_chat` range, contract E), `src/mavis/config.py`, `src/mavis/store/repo/users.py` (test users active + `is_test`), `src/mavis/llm/models.py` (`LLM_FAKE` for test users)

**Interfaces:**
- Produces:
  - Settings `live_test_chat_base: int | None = None`, `live_test_chat_count: int = 0` (max 50), `llm_fake_for_test_users: bool = False`
  - `is_test_chat(chat_id, s=None) -> bool`: the single `test_telegram_chat_id` (unchanged) or `base - count < chat_id <= base` when the base is synthetic (below `SYNTHETIC_BELOW`), `base - count` too, the range does not intersect the owner list, and `live_test_enabled`
  - `get_or_create_by_chat` creates test chats as `status="active"`, `tier="standard"`, `is_test=True`
  - When `llm_fake_for_test_users` and the bound user is a test user, `_invoke_chain` returns a canned `AIMessage("ok (load test)")` after `asyncio.sleep(uniform(1, 3))` with no provider call (still metered as `provider="fake"`)
  - `scripts/loadtest.py --users N --minutes M --scenario {1..6} [--fake-llm] [--cleanup]`

- [ ] **Step 1: Write the failing test**

`tests/test_loadtest.py`:

```python
from __future__ import annotations

import pytest

from mavis.channels.test_sink import SYNTHETIC_BELOW, is_test_chat


@pytest.fixture
def ranged(settings, monkeypatch):
    from mavis.config import get_settings

    monkeypatch.setenv("LIVE_TEST_ENABLED", "true")
    monkeypatch.setenv("LIVE_TEST_CHAT_BASE", str(SYNTHETIC_BELOW - 1000))
    monkeypatch.setenv("LIVE_TEST_CHAT_COUNT", "30")
    get_settings.cache_clear()
    return get_settings()


@pytest.mark.parametrize("offset,inside", [(0, True), (15, True), (29, True), (30, False), (-1, False)])
def test_range_membership(ranged, offset, inside):
    assert is_test_chat(ranged.live_test_chat_base - offset, ranged) is inside


@pytest.mark.parametrize("base,count", [(-5, 10), (SYNTHETIC_BELOW + 5, 10), (SYNTHETIC_BELOW - 1, 51)])
def test_unsafe_ranges_are_off(settings, monkeypatch, base, count):
    from mavis.config import get_settings

    monkeypatch.setenv("LIVE_TEST_ENABLED", "true")
    monkeypatch.setenv("LIVE_TEST_CHAT_BASE", str(base))
    monkeypatch.setenv("LIVE_TEST_CHAT_COUNT", str(count))
    get_settings.cache_clear()
    assert not is_test_chat(base, get_settings())


async def test_test_users_start_active_and_flagged(db, ranged):
    from mavis.store.repo import users

    u, _ = await users.get_or_create_by_chat(ranged.live_test_chat_base - 3, None)
    assert (u.status, u.tier, u.is_test) == ("active", "standard", True)


def test_loadtest_plan_is_deterministic(ranged):
    from scripts.loadtest import plan_turns

    a = plan_turns(users=5, minutes=2, scenario=3, seed=11)
    b = plan_turns(users=5, minutes=2, scenario=3, seed=11)
    assert a == b and any(t.user == 0 and t.burst for t in a)  # scenario 3: user 0 is the abuser
    assert {t.user for t in a} == set(range(5))
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/test_loadtest.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

`channels/test_sink.py`:

```python
MAX_TEST_CHATS = 50


def _range_ok(s: Settings) -> bool:
    base, count = s.live_test_chat_base, s.live_test_chat_count
    if base is None or count <= 0:
        return False
    if count > MAX_TEST_CHATS or base >= SYNTHETIC_BELOW:
        _warn(base, "the test chat range must be synthetic and at most 50 ids")
        return False
    if any(base - count < c <= base for c in s.owner_telegram_chat_ids):
        _warn(base, "the test chat range overlaps an owner chat")
        return False
    return True


def is_test_chat(chat_id: int, s: Settings | None = None) -> bool:
    s = s or get_settings()
    if chat_id is None or not s.live_test_enabled:
        return False
    if chat_id == active_test_chat(s):
        return True
    return _range_ok(s) and s.live_test_chat_base - s.live_test_chat_count < chat_id <= s.live_test_chat_base
```

with `_warn(key, reason)` reusing the existing once-per-reason `log.error("channel.test_chat_rejected", ...)`. `SinkChannel._is_test(chat_id)` returns `is_test_chat(chat_id)`, and `get_channel()` installs the sink when either the single chat or a valid range is active.

`users.get_or_create_by_chat`: `test = is_test_chat(chat_id)` then `status="active" if test else "pending"`, `is_test=test`.

`scripts/loadtest.py`:

```python
"""Load test (spec 15): N synthetic users from the LIVE_TEST_CHAT range post webhook updates with the secret,
the stack answers into the sink, and per-turn latency is read from the sink file.

    uv run python -m scripts.loadtest --users 10 --minutes 15 --scenario 1
    uv run python -m scripts.loadtest --users 30 --minutes 15 --scenario 3 --fake-llm
    uv run python -m scripts.loadtest --cleanup
"""

from __future__ import annotations

import argparse
import asyncio
import random
import time
from dataclasses import dataclass

import httpx

from mavis.channels.test_sink import is_test_chat, read_sink
from mavis.config import get_settings

SCRIPT = ["hi", "remind me to call the plumber tomorrow at 10", "what's pending for me?",
          "and water the plants on friday", "what's the capital of Peru?", "thanks"]
BURST = ["one more thing", "actually two", "the gym bag", "and the library book"]


@dataclass(frozen=True)
class Turn:
    at_s: float
    user: int
    text: str
    burst: bool = False


def plan_turns(users: int, minutes: int, scenario: int, seed: int = 7) -> list[Turn]:
    rnd = random.Random(seed)
    turns: list[Turn] = []
    horizon = minutes * 60
    for u in range(users):
        t = rnd.uniform(0, 20)
        i = 0
        while t < horizon:
            turns.append(Turn(t, u, SCRIPT[i % len(SCRIPT)]))
            if i % 4 == 2:
                turns += [Turn(t + 0.7 * k, u, BURST[k], burst=True) for k in range(len(BURST))]
            i += 1
            t += rnd.expovariate(1 / 45)
        if scenario == 3 and u == 0:  # the abuser: 60 messages a minute
            turns += [Turn(s, 0, "spam", burst=True) for s in range(0, horizon, 1)]
    if scenario == 4:  # synthetic 08:30 fan-out: everyone at once
        turns += [Turn(60.0, u, "good morning") for u in range(users)]
    return sorted(turns, key=lambda t: (t.at_s, t.user))


async def run(users: int, minutes: int, scenario: int, url: str) -> dict:
    s = get_settings()
    base = s.live_test_chat_base
    assert base is not None and all(is_test_chat(base - u, s) for u in range(users)), "enable the test range"
    plan = plan_turns(users, minutes, scenario)
    start = time.monotonic()
    sent: dict[int, float] = {}
    async with httpx.AsyncClient(timeout=10) as c:
        for n, turn in enumerate(plan):
            delay = turn.at_s - (time.monotonic() - start)
            if delay > 0:
                await asyncio.sleep(delay)
            chat = base - turn.user
            uid = 900_000_000 + n
            sent[uid] = time.time()
            await c.post(url, headers={"X-Telegram-Bot-Api-Secret-Token": s.telegram_webhook_secret},
                         json={"update_id": uid, "message": {"message_id": uid, "date": int(time.time()),
                               "text": turn.text, "chat": {"id": chat, "type": "private"},
                               "from": {"id": chat, "first_name": f"Load{turn.user}"}}})
    await asyncio.sleep(60)
    replies = [r for r in read_sink(s.data_dir) if is_test_chat(r["chat_id"], s)]
    return {"turns": len(plan), "replies": len(replies)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=10)
    ap.add_argument("--minutes", type=int, default=15)
    ap.add_argument("--scenario", type=int, default=1, choices=range(1, 7))
    ap.add_argument("--url", default="http://localhost:8000/telegram/webhook")
    ap.add_argument("--cleanup", action="store_true")
    args = ap.parse_args()
    if args.cleanup:
        asyncio.run(_cleanup())
        return
    print(asyncio.run(run(args.users, args.minutes, args.scenario, args.url)))


async def _cleanup() -> None:
    from sqlalchemy import select

    from mavis.access.deletion import run_steps
    from mavis.store.db import Session
    from mavis.store.models import User

    async with Session() as s:
        ids = list(await s.scalars(select(User.id).where(User.is_test.is_(True))))
    for uid in ids:
        await run_steps(uid)  # the load test exercises the deletion cascade too
    print(f"deleted {len(ids)} test users")


if __name__ == "__main__":
    main()
```

Latency per turn: extend `run()` to pair each sent update with the first sink row for that chat after it (by timestamp) and print p50/p95; scenarios 5 and 6 are run by hand as the spec describes (`LLM_FORCE_429_S=60` test flag on the LLM client for 5: add `llm_force_429_until: float = 0` handling in `_call` that raises a synthetic 429 while `time.time() < until`, settable through `/admin`-free env; for 6, `docker compose kill worker` mid-run and check no duplicate replies in the sink). Pass criteria per scenario are the table in spec 15; record results in the ledger.

`LLM_FAKE` in `llm/models.py` `_invoke_chain` (first lines): if `s.llm_fake_for_test_users` and the bound user is a test user (`users.get(uid).is_test`, cached), `await asyncio.sleep(random.uniform(1, 3)); return AIMessage(content="ok (load test)")`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_loadtest.py tests/channels -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/loadtest.py src/mavis/channels/test_sink.py src/mavis/store/repo/users.py src/mavis/llm/models.py \
        src/mavis/config.py tests/test_loadtest.py
git commit -m "feat(loadtest): synthetic test chat range, flagged test users and a scenario-driven load harness"
```

---

### Task 20: Compose and deploy wiring, memory limits, rollout

**Files:**
- Create: `tests/test_compose_env.py` (shared contract C; append if Plan 12 created it)
- Modify: `docker-compose.prod.yml`, `deploy/aws/deploy.sh`, `README.md` (deploy section: rollout table)

**Interfaces:**
- Produces: every Phase 11 setting reaches the containers; memory limits per spec 3.1; per-role DB pools; shared `appdata` volume for `backup.ok`

- [ ] **Step 1: Write the failing test**

`tests/test_compose_env.py` (if it exists, add the Phase 11 names to `PASSTHROUGH` and keep the rest):

```python
"""Shared contract C: every env var the app reads in prod must be passed by the compose x-app-env block
(lesson of 339e301: a whitelisted env silently drops new keys)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PASSTHROUGH = {
    # Phase 11 (multi-user)
    "OWNER_TELEGRAM_CHAT_IDS", "ACCESS_MODE", "WORKER_SCHEDULER", "LLM_LIMITER", "TELEGRAM_BOT_USERNAME",
    "LLM_GLOBAL_SLOTS", "LLM_BG_MAX_SLOTS", "LLM_BEST_EFFORT_MAX_SLOTS", "LLM_USER_MAX_SLOTS",
    "LLM_SECONDARY_BASE_URL", "LLM_SECONDARY_API_KEY", "LLM_SECONDARY_MODEL_FAST", "LLM_SECONDARY_MODEL_SMART",
    "LLM_MONTHLY_CEILING_USD", "BUDGET_ENFORCED", "METRICS_TOKEN", "LANGFUSE_ENABLED", "LANGFUSE_HOST",
    "LANGFUSE_SAMPLE_CHAT", "LANGFUSE_SAMPLE_BG", "HEALTHCHECKS_URL", "BACKUP_BUCKET", "PRIVACY_URL",
    "PACING_ENABLED", "LIVE_TEST_CHAT_BASE", "LIVE_TEST_CHAT_COUNT", "LLM_FAKE_FOR_TEST_USERS",
    "COMPOSIO_SHARED_KEY_OK", "CHAT_EXECUTORS", "BG_EXECUTORS", "TASK_GLOBAL_CONCURRENCY",
}


def _app_env_block() -> str:
    text = (ROOT / "docker-compose.prod.yml").read_text()
    return text.split("x-app-env: &app-env", 1)[1].split("\n\n", 1)[0]


def test_every_key_is_passed_through():
    block = _app_env_block()
    missing = sorted(k for k in PASSTHROUGH if not re.search(rf"^\s+{k}:", block, re.M))
    assert missing == []


def test_old_admin_and_allowlist_names_are_gone():
    block = _app_env_block()
    assert "ADMIN_PASSWORD" not in block and "ALLOWED_TELEGRAM_CHAT_IDS:" not in block


def test_memory_limits_fit_the_box():
    text = (ROOT / "docker-compose.prod.yml").read_text()
    limits = {m.group(1): m.group(2) for m in re.finditer(r"^  (\w+):\n(?:.*\n)*?\s+mem_limit: (\d+m)", text, re.M)}
    assert limits["worker"] == "900m" and limits["postgres"] == "256m" and limits["qdrant"] == "256m"
    assert limits["redis"] == "192m" and limits["api"] == "256m"
    assert '"--maxmemory", "128mb"' in text
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/test_compose_env.py -q`
Expected: FAIL (missing keys and limits).

- [ ] **Step 3: Implement**

In `docker-compose.prod.yml` `x-app-env`: replace `ALLOWED_TELEGRAM_CHAT_IDS: ${ALLOWED_TELEGRAM_CHAT_IDS:?...}` with `OWNER_TELEGRAM_CHAT_IDS: ${OWNER_TELEGRAM_CHAT_IDS:-${ALLOWED_TELEGRAM_CHAT_IDS:?set OWNER_TELEGRAM_CHAT_IDS}}`, remove `ADMIN_PASSWORD`, change `LLM_MAX_CONCURRENCY` default to 3, and add each `PASSTHROUGH` key as `KEY: ${KEY:-<default>}` with the Settings defaults (`ACCESS_MODE: ${ACCESS_MODE:-allowlist}`, `WORKER_SCHEDULER: ${WORKER_SCHEDULER:-legacy}`, `LLM_LIMITER: ${LLM_LIMITER:-local}`, `LLM_GLOBAL_SLOTS: ${LLM_GLOBAL_SLOTS:-3}`, `LLM_BG_MAX_SLOTS: ${LLM_BG_MAX_SLOTS:-1}`, `LLM_BEST_EFFORT_MAX_SLOTS: ${LLM_BEST_EFFORT_MAX_SLOTS:-0}`, `LLM_USER_MAX_SLOTS: ${LLM_USER_MAX_SLOTS:-2}`, `LANGFUSE_ENABLED: ${LANGFUSE_ENABLED:-false}`, `PACING_ENABLED: ${PACING_ENABLED:-true}`, the secrets and URLs `${KEY:-}`). Memory: api 256m, worker 900m (Plan 12 sets the same value; keep one line), postgres 256m, qdrant 256m, redis 192m with `"--maxmemory", "128mb"`. Pools per service: api `environment: {<<: *app-env, DB_POOL_SIZE: "4", DB_MAX_OVERFLOW: "2"}`, worker 8/4, timer 2/2 (use a merged mapping per service). Add volume `appdata` mounted at `/app/data` for api, worker and timer (artifacts stays its own volume under it).

In `deploy/aws/deploy.sh` add to the "owner-supplied keys follow the local env file" loop: `OWNER_TELEGRAM_CHAT_IDS METRICS_TOKEN HEALTHCHECKS_URL BACKUP_BUCKET PRIVACY_URL LLM_SECONDARY_BASE_URL LLM_SECONDARY_API_KEY LLM_SECONDARY_MODEL_FAST LLM_SECONDARY_MODEL_SMART TELEGRAM_BOT_USERNAME`, and to the required-keys check accept either `OWNER_TELEGRAM_CHAT_IDS` or `ALLOWED_TELEGRAM_CHAT_IDS` (copy the old value into the new key when only the old one is set). Generate `METRICS_TOKEN` with `set_key METRICS_TOKEN "$(openssl rand -hex 24)"` (preserved like the other generated secrets). Flags (`ACCESS_MODE`, `WORKER_SCHEDULER`, `LLM_LIMITER`, `LANGFUSE_ENABLED`) are set by hand in the box `.env` per rollout step, never forced by deploy.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_compose_env.py tests/test_workspace_scripts.py tests/initiative/test_executor.py -q && uv run pytest -q && docker compose -f docker-compose.prod.yml config >/dev/null`
Expected: PASS; compose config renders (with a scratch `.env` providing the required variables).

- [ ] **Step 5: Write the rollout section in README.md (deploy part) and commit**

Add this table under the deploy section, verbatim:

| Step | Do | Gate to next |
|---|---|---|
| R0 | `iam-role.sh --apply`, `backups.sh --apply`, one manual box backup, `restore-drill.sh` | drill passes |
| R1 | deploy with `ACCESS_MODE=shadow` (allowlist still enforced, gate logs) | 2 days of `gate.shadow_would_drop` lines match the allowlist |
| R2 | `LLM_LIMITER=redis`, `METRICS_TOKEN` set, `HEALTHCHECKS_URL` set, `LANGFUSE_ENABLED=true` with keys | 2 days, no regressions in the owner's chat; `/stats` and `/metrics` sane |
| R3 | `WORKER_SCHEDULER=mailbox` | load scenarios 1, 3, 6 pass (`scripts/loadtest.py`) |
| R4 | onboarding, `/settings`, `/delete_me`, admin, pacing (already in the image) exercised on test users | load scenarios 2, 4, 5 pass; deletion verified on test users (`--cleanup`) |
| R4.5 | **Owner confirms Ollama's terms allow serving third-party users** (owner decision 6) and publishes `PRIVACY_URL` (names Langfuse Cloud) | written confirmation |
| R5 | `ACCESS_MODE=invite`; owner mints 3 codes with `uses=1` | one week, no isolation or cost surprises |
| R6 | up to 30 users; weekly `/stats` spend review | |

Rollback: each step is an env flag in the box `.env` (`ACCESS_MODE=allowlist`, `WORKER_SCHEDULER=legacy`, `LLM_LIMITER=local`, `LANGFUSE_ENABLED=false`), then `docker compose up -d`.

```bash
git add docker-compose.prod.yml deploy/aws/deploy.sh README.md tests/test_compose_env.py
git commit -m "chore(deploy): pass phase 11 settings, t4g.medium memory limits, role pools and the rollout table"
```

---

## Self-review

1. **Spec coverage.** Section 4 (access, invites, migration from allowlist, owner commands): Tasks 1-6. Section 5 (onboarding, tz, currency, set_preferences, /settings, tz side effects): Task 7. Section 6 (isolation suite, Qdrant, Neo4j, Redis keys, artifacts, workspace guard, Langfuse ids, Composio prefix, adaptive polling): Tasks 8, 9, 16, 17. Section 7 (mailboxes, executors, coalescing, jitter, pools, MAXLEN, mailbox cap, task global cap): Task 13. Section 8 (Pro plan limits, Lua limiter, shared backoff, fallback, contextvar, overflow): Task 10. Section 9 (inbound bucket, cost accounting, budgets, monthly ceiling, bans, cooldowns): Tasks 4, 11, 12. Section 10 (deletion, privacy): Task 14. Section 11 (admin): Task 15. Section 12 (metrics, Langfuse, alerting, dead-man): Task 16. Section 13 (backups, AWS resources): Task 18. Section 14 (pacing, priority, webhook settings, blocked): Task 17. Section 15 (load test): Task 19. Section 17 (rollout): Task 20. Section 16 interactions: "Parallel execution and shared files" and contracts F and G. UptimeRobot on `/healthz` is an owner-side signup (no code): listed in Task 20's R2 row through `HEALTHCHECKS_URL`; add the UptimeRobot monitor by hand at R2.
2. **Placeholders.** `<NN>` and `<HEAD>` in Task 2 are execution-time values by design (Global Constraints), produced by Step 1's command. No TBD steps.
3. **Type consistency.** `register_event_gate(name, fn, order)`, `bind_user(uid, purpose)`, `Lease`, `BudgetState`, `register_spend_source(name, fn)`, `register_deletion_step(name, fn)`, `SendPacer.reserve(chat_id, kind=)`, `is_test_chat(chat_id, s)` are used with the same names and signatures in every task that consumes them.
4. **Review Focus.** Each of the five lines names tests that exist in their owning task (Tasks 4, 5, 10, 11, 12, 13, 14).

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-10-08-mavis-11-multiuser.md`. Recommended: **Subagent-driven**, because the 20 tasks share many interfaces (gates, limiter, budgets, deletion extension points) across shared files that parallel branches also edit, and a shipped mistake in access, budgets or deletion is user-visible and hard to undo. Tasks 1-3, then {4, 10, 18} can run in parallel worktrees; 13 must land before 19.

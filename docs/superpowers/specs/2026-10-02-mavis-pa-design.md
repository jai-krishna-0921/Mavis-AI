# Mavis — Proactive Personal Assistant Agent: Design Spec

> Agent persona name: **Mavis** (Telegram bot @Mavis247_bot). "Mavis" is the project, package and CLI name.

- **Date:** 2026-10-02
- **Status:** Draft for review
- **Owner:** JK
- **Scope:** MVP (production-grade, single user), hackathon demo target

---

## 1. Purpose

Mavis is a 24×7 personal assistant that lives in the user's chat app (Telegram first) and behaves like a sharp human PA: it **notices things, decides on its own when to speak up, and acts** — with the user's OK for anything outward-facing. It is *not* a request/response chatbot.

Reference behaviour (from the "Instinct" transcripts the user shared):

- Greets and onboards, then nudges if the user goes quiet.
- Asks clarifying questions when time is ambiguous ("it's past midnight — today or tomorrow?").
- Prompts for account connection exactly when a capability needs it ("To put it on your calendar I need Google access — one tap here").
- Pushes unprompted: a morning security-alert check, "how did the interview prep go?".
- Empathises, mirrors tone, keeps internals private ("that part stays behind the curtain").

### 1.1 Success criteria

1. Conversational replies feel instant: p50 < 2 s, p95 < 4 s for non-tool turns on the free Ollama models.
2. At least four proactive behaviours work end-to-end live: memory-driven follow-ups, inbox alerts, morning check-in, act-with-approval.
3. The agent prompts for connections itself when a task needs an integration that isn't connected, and resumes the task after the connection becomes active.
4. It can produce real artefacts: PPTX, PDF, DOCX, XLSX, charts, and deep-research reports, delivered as Telegram files.
5. No message, nudge or approval is lost or duplicated across a process restart.
6. Every LLM, agent and tool step is traceable in Langfuse.

### 1.2 Decisions taken (with the user)

| Topic | Decision |
|---|---|
| Build vs adopt | Own core on **LangGraph**, borrowing ideas from Hermes/OpenClaw (channel gateways, skills, cron-as-agent-tool) |
| Tenancy | **Single user** product scope; every record still carries `user_id` so multi-user is a later config/onboarding change, not a migration |
| Persona | Warm, witty friend-PA named **Mavis** (`AGENT_NAME=Mavis`; project/package stays "Mavis"); short chat bubbles, mirrors tone, empathetic, pushes on goals |
| Sandbox | **Docker + gVisor** on the EC2 host behind a minimal `sandboxd` sidecar (primary); **AWS Bedrock AgentCore Code Interpreter** adapter (optional, ap-south-1); dev-only local subprocess backend. No E2B |
| Proactivity | **Balanced**: ≤ 6 unsolicited pings/day, quiet hours 23:00–07:00 local, tunable in chat |
| Models | Ollama Cloud free tier: `gpt-oss:20b` (FAST), `gpt-oss:120b` (SMART); env-swappable |
| Memory | Own layer: **Neo4j** (graph) + **Qdrant** (episodic) + profile card + open loops |
| Integrations | **Composio is temporary**: everything goes behind an `IntegrationProvider` port. Composio REST v3 adapter now; direct Google/Slack/Notion adapters later |
| Channel | Telegram first (webhook in prod, long-polling in dev); WhatsApp later via the same `Channel` port |
| Deploy | Local dev → AWS EC2 + docker compose (cashfree profile) for 24×7 |

### 1.3 Non-goals (MVP)

- Multi-user onboarding, billing, admin UI.
- Voice notes, WhatsApp, iMessage (port exists; adapter later).
- Spending money autonomously (only via approval, and only drafted for MVP).
- Kubernetes/autoscaling (the design allows it; we don't operate it).

---

## 2. Architecture overview

```
                          ┌────────────────────────── EDGE ──────────────────────────┐
 Telegram ──webhook──────▶│ api (FastAPI, stateless, N replicas)                     │
 Composio/Google ─push───▶│  /webhooks/telegram  /webhooks/integrations              │
 Browser (OAuth return) ─▶│  /connect/callback   /health  /admin/*                   │
                          │  verify → dedupe → normalise to Event → XADD events      │
                          └───────────────────────────────┬──────────────────────────┘
                                                          ▼
                                      Redis Streams  "events"  (+ locks, cache)
                                                          │
            ┌─────────────────────────────────────────────┼───────────────────────────────┐
            ▼                                             ▼                               ▼
 ┌─ conversation worker ──────┐      ┌─ initiative worker ─────────────┐     ┌─ timer service ──────────┐
 │ user_message events        │      │ every non-chat event            │     │ fires agent-set wakeups  │
 │ per-user lock              │      │ filter → reason → decide        │     │ (wakeups table, SKIP     │
 │ LangGraph Conversation     │      │ InitiativeDecision              │     │  LOCKED) → wakeup event  │
 │ graph (fast path / orch.)  │      │ notify | act | track | wake_me  │     └──────────────────────────┘
 └────────────┬───────────────┘      └───────────────┬─────────────────┘
              │                                      │ act
              ▼                                      ▼
 ┌─ Orchestrator (LangGraph) ───────────────────────────────────────────────────────────────┐
 │ Planner → Plan DAG → Send() fan-out → specialists / spawned workers → Critic → Responder  │
 │ Specialists: Inbox · Calendar · Comms · Knowledge · Research · DeepResearch · Analyst ·   │
 │              Docs (PPTX/PDF/DOCX/XLSX) · Coder (sandbox)                                  │
 │ approval gate = LangGraph interrupt() on risk-classed tools                               │
 └────────────┬─────────────────────────────────────────────────────────────────────────────┘
              ▼
 policy layer (quiet hours, budget, dedupe, approvals) → outbox → channel sender → Telegram
              │
              ▼ (after every turn / processed signal)
 learn job → Memory: profile card · Neo4j graph · Qdrant episodes · open loops (emit loop events)

 Stores: Postgres (state, outbox, wakeups, loops, tasks, approvals, LangGraph checkpoints)
         Neo4j · Qdrant · Redis · sandboxd (Docker+gVisor) / AgentCore · object store (local dir / S3) for artefacts
```

### 2.1 Process roles

One container image, started with different entrypoints. Each can be scaled horizontally except `timer` (single active via Redis leader lock; standby replicas allowed).

| Role | Entry | Responsibility |
|---|---|---|
| `api` | `mavis api` | Webhooks, OAuth callback, health, admin. Never calls an LLM. Responds < 50 ms. |
| `worker` | `mavis worker` | Consumes the `events` stream (consumer group) and the `jobs` queue: conversation turns, initiative decisions, orchestrator/specialist runs, learn jobs, outbox delivery. |
| `timer` | `mavis timer` | Claims due `wakeups` (`FOR UPDATE SKIP LOCKED`) and emits `wakeup` events. Also emits `event_starting`/`event_ended` for tracked calendar events. Holds no business logic. |
| `dev` | `mavis dev` | All roles in one process, SQLite + embedded Qdrant + in-process bus, Telegram long-polling. |

### 2.2 Module layout

```
src/mavis/
  config.py                 Settings (pydantic-settings)
  domain/                   Pure types: events, decisions, loops, plans, risk classes. No I/O.
  llm/                      Model routing (FAST/SMART), structured output, fallbacks, Langfuse
  bus/                      EventBus port: RedisStreamsBus, InProcessBus; Event envelope
  store/                    SQLAlchemy models + repositories (Postgres/SQLite), migrations (Alembic)
  memory/                   recall(), learn(), profile card, GraphStore (Neo4j|SQLite), VectorStore (Qdrant)
  loops/                    Open-loop service (create/update/expire/link)
  initiative/               Initiative agent: filters, reasoner, decision executor
  timers/                   wake_me tool + timer service
  agents/
    conversation.py         Conversation graph (recall ∥ route → fast reply | orchestrator)
    orchestrator.py         Planner, Send fan-out, Critic, Responder
    specialists/            One module per specialist (tool subset + prompt)
    spawn.py                spawn_agent(role, goal, tools, budget)
    persona.py              Persona card + system prompt builders
  tools/
    registry.py             Tool metadata: risk class, required capability, specialist scoping
    integrations/           IntegrationProvider port; composio.py adapter; google_direct.py (later)
    web.py                  Tavily search/extract, DDG fallback
    sandbox/                Sandbox port: DockerSandbox (gVisor, via sandboxd sidecar), AgentCoreSandbox (optional), LocalSandbox (dev only)
    documents.py            Artefact builders run inside sandbox (pptx/pdf/docx/xlsx/charts)
    assistant.py            remember/forget, wake_me, track_loop, list_tasks
  policy/                   Risk classes, approval gate, quiet hours, ping budget, dedupe
  channels/                 Channel port; telegram.py; outbox sender
  api/                      FastAPI app, routes (webhooks, connect callback, admin)
  cli.py                    `mavis api|worker|timer|dev|chat|eval`
```

**Rule:** business logic (`initiative`, `agents`, `memory`, `loops`, `policy`) imports only ports (`Channel`, `IntegrationProvider`, `Sandbox`, `EventBus`, `GraphStore`, `VectorStore`), never Telegram/Composio/boto3/Docker SDKs.

---

## 3. Event model

Everything that can make Mavis act is an `Event` on the bus. Push, not poll, wherever the source supports it.

```python
class Event(BaseModel):
    id: str                 # idempotency key (e.g. "tg:update:123", "gmail:msg:abc")
    user_id: int
    type: EventType
    occurred_at: datetime
    source: str             # "telegram" | "composio" | "google" | "timer" | "agent" | "system"
    payload: dict           # typed per EventType via discriminated union in domain/events.py
    trust: Trust            # USER | SYSTEM | UNTRUSTED (third-party content)
```

| EventType | Source | Delivery |
|---|---|---|
| `user_message` | Telegram text/file/voice-caption | webhook → bus |
| `button_pressed` | Telegram callback query (approvals, quick replies) | webhook → bus |
| `email_received` | Gmail via integration provider | Composio trigger webhook (`GMAIL_NEW_GMAIL_MESSAGE`); fallback: provider poller emits same event |
| `calendar_changed` | Calendar via provider | trigger webhook; fallback poller |
| `event_starting` / `event_ended` | timer service, derived from tracked calendar events and loops | wakeups |
| `slack_message` | Slack DM/mention via provider | trigger webhook |
| `notion_changed` | Notion via provider | trigger webhook / poller |
| `connection_changed` | provider status (ACTIVE/FAILED/REVOKED) | callback hit → status read-back; poller during pending connects |
| `wakeup` | agent-set alarm fired | timer |
| `user_quiet` | no user reply N hours after the agent asked something | timer (wakeup set by initiative agent when it asks) |
| `task_completed` / `task_progress` | orchestrator/specialist | in-process → bus |
| `loop_created` / `loop_updated` | loops service | in-process → bus |

Idempotency: `events_seen(id)` in Redis (TTL 7 d) + unique constraint on `processed_events(id)` in Postgres for side-effecting handlers.

---

## 4. Proactivity: the initiative agent

### 4.1 Principle

No hardcoded job table. Time only enters through **agent-owned wakeups** (`wake_me(at, reason, loop_id?)`), which the agent creates, moves and cancels based on what it knows. The "morning brief" is a routine the agent established during onboarding and adapts (e.g. later on weekends because it learned the user replies around 10:00).

### 4.2 Open loops (the PA's mental list)

```python
class Loop(BaseModel):
    id: int
    user_id: int
    kind: LoopKind          # COMMITMENT | WAITING_ON | GOAL | CONCERN | ROUTINE | WATCH
    title: str              # "Interview prep with Jawahar"
    due_at: datetime | None
    entities: list[str]     # graph node keys (people/orgs/projects)
    status: LoopStatus      # OPEN | DONE | EXPIRED | DROPPED
    importance: int         # 1..5
    watch: WatchSpec | None # for WATCH/WAITING_ON: match rules (sender, thread, keyword) + deadline
    source: str             # message/email id it came from
```

Created by: the learn job (extraction), the initiative agent, the user ("keep an eye on X"), the first-sync job. Each new or updated loop emits `loop_created`/`loop_updated` so the initiative agent can plan its own wakeups (pep talk at `due_at − 1h`, follow-up at `due_at + 2h`).

### 4.3 Pipeline per event

1. **Cheap filter** (no LLM, < 5 ms): drop own-sent mail, known newsletter/promo senders (List-Unsubscribe header, Gmail CATEGORY_PROMOTIONS), duplicate ids; match `WATCH` loops by rule; embedding similarity of the event summary against open loops and graph entities produces a relevance score.
2. **Context assembly**: event (wrapped as UNTRUSTED if third-party) + matched loops + graph neighbourhood + recent 10 messages + profile card + state (local time, quiet hours, pings today, last user activity, mood).
3. **Reason** (FAST model; SMART if relevance ≥ threshold or importance ≥ 4) → structured output:

```python
class InitiativeDecision(BaseModel):
    reasoning: str                          # logged to Langfuse, never sent
    notify: NotifyIntent | None             # urgency 1..5, intent text, combine_with (other signals)
    act: list[TaskRequest]                  # orchestrator tasks (e.g. draft reply, prep doc)
    track: list[LoopUpsert]                 # create/update/close loops
    wakeups: list[WakeupRequest]            # wake_me(at, reason, loop_id)
    ignore_reason: str | None
```

4. **Execute**: `track` → loops service; `wakeups` → wakeups table; `act` → orchestrator job (result comes back as `task_completed`); `notify` → **composer** (persona voice, 1–3 bubbles, may decide it's stale: `send=false`) → **policy** (quiet hours defer to next allowed slot unless urgency 5; ping budget; dedupe key per loop/thread) → outbox.
5. Proactive messages are written to the conversation log (`proactive=true`) so the user's reply continues the thread naturally.

### 4.4 Tool restrictions

The initiative reasoner has **no outward tools**. It can only notify/track/wake/request tasks. Tasks it requests run through the orchestrator, where outward tools hit the approval gate.

### 4.5 Built-in routines (seeded as loops + wakeups during onboarding, then agent-managed)

| Routine | Seed | Adapts by |
|---|---|---|
| Morning check-in | wakeup 08:30 local | learned active hours, weekday/weekend, engagement (skips if user ignored 3 in a row → asks) |
| Evening wrap | off by default; offered after a week | user opt-in |
| Onboarding nudges | `user_quiet` after first exchange (4 h) | stops after 2 unanswered |
| Weekly review | Sunday 18:00, offered | opt-in |
| Nightly consolidation | 03:00 local, silent | — |

---

## 5. Conversation and orchestration

### 5.1 Conversation graph (per `user_message`)

```
load_context (parallel: recall, history, pending approvals, connection status)
   → route (FAST model, structured: SMALL_TALK | DIRECT_TOOL | TASK | APPROVAL_REPLY | CONNECT)
      SMALL_TALK  → responder (FAST)                                  ~1–1.5 s
      DIRECT_TOOL → single ReAct step with ≤ 8 tools (FAST) → responder  ~2–4 s
      TASK        → ack immediately ("on it") → orchestrator job (background) → task_completed → initiative → user
      APPROVAL_REPLY → resume interrupted run with edit/approve/cancel
      CONNECT     → connection prompt flow (§6.3)
   → enqueue learn job
```

Typing indicator is sent as soon as the worker picks up the event. Responses are split into short bubbles by the responder (structured: `messages: list[str]`).

### 5.2 Orchestrator graph

```
Planner (SMART) → Plan{goal, steps[{id, agent, instruction, depends_on[], outputs}], budget}
  → scheduler node: dispatch ready steps via LangGraph Send() (parallel)
  → specialist subgraph per step (own tools, own prompt, step/token/time caps)
  → join → Critic (SMART): checks outputs against goal → accept | revise(step ids, feedback) (max 2 rounds)
  → Responder (persona) → artefacts attached → task_completed event
```

- **Specialists** (prebuilt subgraphs):

| Specialist | Tools | Model |
|---|---|---|
| Inbox | mail search/read/thread, draft, send (approval) | FAST |
| Calendar | list/find/free-slots, create/update (self = free; with attendees = approval) | FAST |
| Comms | Slack read/search, send (approval) | FAST |
| Knowledge | Notion search/read/create page; memory recall/remember | FAST |
| Research | web search, page extract, summarise with citations | SMART |
| DeepResearch | multi-round: sub-question planner → parallel Research workers → reader → synthesis → cited report | SMART |
| Analyst | sandbox Python (pandas, matplotlib), file read; data analysis of user files/emails/sheets | SMART |
| Docs | sandbox document builders: PPTX, PDF, DOCX, XLSX, charts, from an outline/data | SMART |
| Coder | sandbox shell/python, files, package install | SMART |

- **Dynamic spawning**: `spawn_agent(role, goal, tools: list[str], budget)` instantiates a generic worker subgraph from a template with a whitelisted subset of tools. Used for fan-out ("compare 5 laptops" → 5 workers + synthesiser).
- **Watchers**: long-lived "agents" are implemented as `WATCH` loops + wakeups + initiative rules (cheap), not as running processes.
- **Task board**: Postgres `tasks` (id, parent_id, agent, status, input, output_ref, cost, timings) and `artifacts` (id, task_id, kind, path/url, mime). User can ask "what are you working on?" and get the real list; tasks can be cancelled.
- **Progress**: a task running > 30 s emits `task_progress`, which the initiative agent may relay ("still digging, found 3 good options so far").
- **Budgets**: per task max steps (default 12), tokens, wall time (default 5 min; DeepResearch 15 min), per-user concurrency (3).
- **Durability**: LangGraph `AsyncPostgresSaver` checkpointer; thread id = task id. Interrupted runs (approvals) resume on `button_pressed`.

### 5.3 Capabilities beyond chat

| Capability | How |
|---|---|
| PPT decks | Docs specialist: Planner writes `DeckOutline` (structured: slides[title, bullets, notes, visual hint]) → sandbox `python-pptx` builder with a clean template → `.pptx` artefact; optional PDF export via LibreOffice in sandbox image |
| PDF reports | Markdown → HTML → PDF (WeasyPrint) in sandbox; charts embedded |
| Word docs | `python-docx` builder from structured `DocOutline` (headings, paragraphs, tables) |
| Spreadsheets | `openpyxl` builder; formulas and charts |
| Charts | matplotlib in sandbox → PNG artefact |
| Deep analysis | Analyst specialist over user-sent files (CSV/XLSX/PDF/DOCX text extraction in sandbox), emails, Notion pages |
| Deep research | DeepResearch specialist → cited report → optionally exported as PDF/DOCX/PPTX |
| Reading files user sends | Telegram file → object store → uploaded to sandbox `/workspace/inbox/` → extract text (pypdf, python-docx, openpyxl) |
| Delivery | Artefacts sent back as Telegram documents with a one-line summary |

Sandbox backends: **Docker + gVisor** (`--runtime=runsc` when installed, else `runc` with a warning; `--network none` by default; memory/CPU/pids limits; read-only root FS; non-root user; per-user `/workspace` volume; no secrets in env). The worker never gets `docker.sock`: a minimal **`sandboxd`** sidecar owns Docker and exposes only run/write/read/list over a unix socket. **AgentCore Code Interpreter** (boto3 `bedrock-agentcore`, `aws.codeinterpreter.v1`, ap-south-1) is an optional backend for microVM isolation; document libraries are pip-installed per session if not preinstalled. `SANDBOX_BACKEND=auto|docker|agentcore|local` (auto: docker if reachable → agentcore if AWS creds → local, dev only). The Docker sandbox image is prebuilt with: python 3.12, pandas, numpy, matplotlib, python-pptx, python-docx, openpyxl, weasyprint, pypdf, libreoffice-core (headless).

---

## 6. Integrations

### 6.1 Port

```python
class IntegrationProvider(Protocol):
    async def catalog(self) -> list[Toolkit]                          # what can be connected
    async def status(self, user: UserRef) -> dict[str, ConnectionStatus]  # ACTIVE|INITIATED|FAILED|NONE
    async def connect_link(self, user: UserRef, toolkit: str, callback_url: str) -> str
    async def disconnect(self, user: UserRef, toolkit: str) -> None
    async def execute(self, user: UserRef, action: str, args: dict) -> ToolResult
    async def subscribe(self, user: UserRef, trigger: str, config: dict) -> str   # push triggers
    def parse_webhook(self, headers: dict, body: bytes) -> list[Event]            # verify + normalise
```

Actions are referenced by **Mavis action names** (`mail.search`, `mail.send`, `calendar.create_event`, `slack.send`, `notion.search` …), mapped per provider in one table. Swapping Composio for direct Google APIs is a new adapter plus a mapping, with no agent changes.

### 6.2 Composio adapter (temporary)

Modelled on the working integration in `~/Desktop/comarketer` (`revenue_intel/integrations.py`, `scripts/composio_mcp_server.py`):

- **REST v3 via httpx** (`https://backend.composio.dev/api/v3`, header `x-api-key`), no SDK coupling.
- Auth config: find an `ENABLED` config for the toolkit or create one with `use_composio_managed_auth`.
- Connect: `POST /connected_accounts/link {auth_config_id, user_id, callback_url}` → `redirect_url`. (Not `/connected_accounts`, which 400s for managed OAuth.)
- Status: `GET /connected_accounts?user_ids={user}`; newest/ACTIVE wins per toolkit.
- Execute: `POST /tools/execute/{SLUG} {user_id, arguments}` → `{successful, data, error}`; results truncated (6 000 chars) before reaching a model.
- **Explicit identity**: Composio `user_id` = `mavis-{user_id}`; never default to "first ACTIVE connection on the key".
- **Curated slug allowlist** per Mavis action (never `tools.get(toolkits=…)`, which returns hundreds of actions).
- Callback route writes nothing; it renders "Connected, you can close this tab" and enqueues a `connection_check` job that reads status back from Composio and emits `connection_changed`.
- API key never appears in responses, logs or tool results; HTTP errors surface status codes only.

Initial mapping:

| Mavis action | Composio slug | Risk |
|---|---|---|
| `mail.search` | `GMAIL_FETCH_EMAILS` | read |
| `mail.read` | `GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID` | read |
| `mail.thread` | `GMAIL_FETCH_MESSAGE_BY_THREAD_ID` | read |
| `mail.draft` | `GMAIL_CREATE_EMAIL_DRAFT` | write_self |
| `mail.send` / `mail.reply` | `GMAIL_SEND_EMAIL` / `GMAIL_REPLY_TO_THREAD` | outward |
| `calendar.list` | `GOOGLECALENDAR_EVENTS_LIST` | read |
| `calendar.find` | `GOOGLECALENDAR_FIND_EVENT` | read |
| `calendar.free_slots` | `GOOGLECALENDAR_FIND_FREE_SLOTS` | read |
| `calendar.create_event` | `GOOGLECALENDAR_CREATE_EVENT` | write_self, or outward if attendees |
| `calendar.update_event` | `GOOGLECALENDAR_UPDATE_EVENT` | same rule |
| `slack.channels` / `slack.history` | `SLACK_LIST_ALL_CHANNELS` / `SLACK_FETCH_CONVERSATION_HISTORY` | read |
| `slack.send` | `SLACK_SEND_MESSAGE` | outward |
| `notion.search` / `notion.read` | `NOTION_SEARCH_NOTION_PAGE` / `NOTION_FETCH_DATA` | read |
| `notion.create_page` | `NOTION_CREATE_NOTION_PAGE` | write_self |

Slugs are verified against the live catalog in implementation (Phase 4, task "verify slugs"); the table lives in `tools/integrations/composio_map.py`.

Triggers: `GMAIL_NEW_GMAIL_MESSAGE`, `GOOGLECALENDAR_EVENT_CREATED/UPDATED` (or equivalent), `SLACK_RECEIVE_MESSAGE`. If trigger subscription fails, a `ProviderPoller` (inside `worker`, driven by agent-independent wakeups of kind `SYSTEM_POLL`, every 2 min) emits identical events, so downstream is unchanged.

### 6.3 Connection prompting (first-class flow)

1. Every tool declares `requires: Capability` (e.g. `GMAIL`, `CALENDAR`).
2. When a plan step or direct tool call needs a capability that isn't ACTIVE, the tool layer raises `ConnectionRequired(capability, reason)`; the run is **interrupted** (LangGraph `interrupt`) rather than failing.
3. Mavis sends, in persona voice: what it's trying to do + why it needs access + a one-tap connect link (+ a "Not now" button). Mirrors the Instinct pattern.
4. Pending connection → initiative sets a short wakeup series (check status at +1, +3, +10 min) in addition to the callback signal.
5. On `connection_changed: ACTIVE` → the interrupted run **resumes automatically** ("Connected. Setting up Monday 10am with Jawahar now.") and the **first-sync job** runs (§6.4).
6. Proactive suggestion: the initiative agent may suggest connecting a service when it would clearly help (max once per service per week; respects "not now").
7. `/connect`, `/connections`, `/disconnect <service>` commands exist as explicit controls.

### 6.4 First sync (on new connection)

- Gmail: last 14 days of headers + snippets → people (frequent correspondents), threads awaiting the user's reply → `WAITING_ON`/`COMMITMENT` loops, security/bill/travel emails → facts.
- Calendar: next 14 days → `Event` nodes + `COMMITMENT` loops with `event_starting` wakeups for importance ≥ 3.
- Slack/Notion: channels/pages list + recent mentions → entities.

Runs as a background task with progress; ends with a short "here's what I noticed" message (max 3 items).

---

## 7. Memory

Five layers:

| Layer | Store | In prompt |
|---|---|---|
| Working | Postgres `messages` (last 20) + rolling summary (`conversation_summaries`) | always |
| Profile card | Postgres `profile_cards` (≤ 400 tokens, versioned) | always |
| Semantic graph | Neo4j | relevant 2-hop neighbourhood |
| Episodic | Qdrant `episodes` (conversation snippets, email/event summaries, outcomes) | top-k |
| Open loops | Postgres `loops` | active + relevant |

### 7.1 Graph schema

Node labels (fixed vocabulary): `User, Person, Organization, Project, Goal, Event, Topic, Place, Preference`.
Relationship types (fixed vocabulary, `RELATED_TO` fallback): `KNOWS, FAMILY_OF, FRIEND_OF, COLLEAGUE_OF, WORKS_AT, STUDIES_AT, PURSUING, SUPPORTS, WITH, ABOUT, PREFERS, DISLIKES, STRUGGLES_WITH, SKILLED_AT, LOCATED_IN, ATTENDED, INTERVIEWING_AT`.

Every node: `key` (normalised name + label), `name`, `aliases[]`, `user_id`, `created_at`, `last_seen_at`, `embedding_ref`.
Every edge: `statement`, `valid_from`, `valid_to` (null = current), `confidence`, `source_ref`.
Contradiction: a new edge of a single-valued type (e.g. `WORKS_AT`) closes the previous edge (`valid_to = now`).

### 7.2 learn job

1. Extraction (FAST, structured) over the turn or signal → `Extraction{entities[], relations[], events[], loops[], profile_updates[], mood?}`.
2. Entity resolution: normalise → alias match → embedding similarity ≥ 0.86 within the same label → else create.
3. Writes: Neo4j `MERGE` (idempotent by key), Qdrant upsert (deterministic UUIDv5 ids), loops upsert (emits loop events), profile card patch queue.
4. Never blocks the reply; runs as a job after the response is sent.

### 7.3 recall (target ≤ 80 ms, no LLM)

1. Entity spotting: Aho-Corasick automaton over the user's entity names + aliases (rebuilt on change, cached in-process).
2. Parallel: Neo4j 2-hop neighbourhood (current edges, ranked by recency × confidence), Qdrant top-k (k=6, score ≥ 0.35), active loops linked to spotted entities + due ≤ 48 h.
3. Budgeted assembly (≈ 1 200 tokens): profile card → loops → graph facts → episodes.

### 7.4 Consolidation (nightly, agent-scheduled)

Rewrite the profile card from the day's facts; merge duplicate entities; expire stale loops; decay unconfirmed low-confidence facts; learn routines (active hours, reply latency, ping engagement per kind) → stored in profile card + `user_stats`, consumed by initiative timing and budget.

### 7.5 User control

"What do you know about me?" (renders profile + top facts), "forget X" (graph edges + episodes + loops matching, with confirmation), "don't track my mood" (profile flag honoured by extraction).

---

## 8. Policy, approvals, safety

### 8.1 Risk classes

| Class | Examples | Behaviour |
|---|---|---|
| `read` | search mail, list events, web search | run |
| `write_self` | draft, self-only calendar block, Notion note, sandbox files | run, mention in reply |
| `outward` | send email/Slack, invite with attendees | **approval** |
| `spend` | purchases, bookings | **approval** (MVP: draft only) |
| `destructive` | delete anything | **approval** |

### 8.2 Approval flow

Tool call with risk ≥ outward → `interrupt({action, preview})` → Telegram message with exact preview (to/subject/body or channel/text) and buttons ✅ Send · ✏️ Edit · ❌ Cancel → `button_pressed` (or a text reply while an approval is pending = edit instructions) → `Command(resume=…)` → execute → confirm. Approvals expire after 48 h (the agent asks once before expiry). User-defined standing rules (`policy_rules`: "always OK invites to Jawahar") are checked before interrupting. Every decision is written to `audit_log`.

### 8.3 Prompt-injection posture

Third-party content (email bodies, web pages, Slack messages, file contents) is wrapped as `<untrusted source="…">…</untrusted>` with an instruction that it is data. The initiative reasoner has no outward tools. Outward actions always need approval regardless of who suggested them. The sandbox holds no credentials (no env secrets, no network by default, gVisor syscall isolation, worker has no Docker socket — only the `sandboxd` sidecar does); integration calls happen server-side only.

### 8.4 Pings policy

Quiet hours (default 23:00–07:00 local) defer non-urgent notifications to the next allowed slot; urgency-5 (security alerts, imminent events) bypass. Daily budget 6 unsolicited messages (user-adjustable in chat). Dedupe key per loop/thread/day.

### 8.5 Secrets and access

Secrets come from env (dev) or AWS Secrets Manager (prod); never logged (structlog redaction processor). Telegram webhook secret-token header verified; integration webhooks signature-verified; in single-user mode only `ALLOWED_TELEGRAM_CHAT_IDS` are served (others get a polite refusal). Persona keeps internals private.

---

## 9. Reliability and performance

- **Transactional outbox**: every outbound message is inserted into `outbox` in the same transaction as the state change that produced it; a sender loop delivers with retries (exp. backoff, Telegram 429 `retry_after` honoured), marks `sent`.
- **Per-user serialisation**: Redis lock `lock:user:{id}` around conversation turns; initiative decisions for the same user also serialised.
- **Redis Streams consumer groups** with `XAUTOCLAIM` for stuck messages; poison events → `events:dlq` after 5 attempts.
- **LLM resilience**: timeouts (FAST 20 s, SMART 60 s), 2 retries, SMART→FAST fallback, user-facing "give me a sec" + job retry if both fail.
- **Caching**: profile card and entity automaton cached per user in-process with version stamps; Composio status cached 60 s.
- **Latency budget (SMALL_TALK)**: webhook 30 ms → queue 10 ms → context 80 ms → LLM ~1 000 ms → send 150 ms.
- **Health**: `/health/live`, `/health/ready` (checks Postgres, Redis, Neo4j, Qdrant, Ollama reachability).

---

## 10. Observability

- Langfuse: one trace per event; nested spans for graph nodes, specialists, tools; tags `user`, `event_type`, `decision`, `route`; session id = user id + day.
- structlog JSON logs with `event_id`, `task_id`, `trace_id`.
- `/admin/metrics` (basic-auth): queue depth, DLQ size, p50/p95 turn latency, pings/day, approval rate, task success rate, LLM error rate.

---

## 11. Data model (Postgres)

`users, messages, conversation_summaries, profile_cards, loops, wakeups, tasks, artifacts, pending_approvals (+ LangGraph checkpoint tables), policy_rules, outbox, processed_events, connections_cache, user_stats, audit_log`. Migrations via Alembic. SQLite is supported for `dev` only.

---

## 12. Testing strategy

- **Unit** (pytest-asyncio): policy (quiet hours, budget, dedupe, risk), entity resolution, recall assembly, loop → wakeup planning, event normalisation, Composio adapter (httpx mocked with respx), outbox retry.
- **Contract**: recorded Telegram updates and Composio webhook/execute payloads as fixtures.
- **Graph tests**: conversation and orchestrator graphs with a fake LLM (scripted responses) to assert routing, interrupts and resume.
- **LLM evals** (`mavis eval`, on demand, real models): extraction catches events/people; triage flags security alerts, ignores promos; persona stays in voice; route classifier accuracy. Golden set in `evals/`.
- **E2E smoke**: `mavis dev` + fake Telegram channel driving a scripted conversation (interview → pep talk → follow-up).

---

## 13. Deployment

- `Dockerfile` (uv, python 3.13-slim), `docker-compose.yml`: `api, worker, timer, sandboxd, redis, postgres, neo4j, qdrant, caddy`. Only `sandboxd` mounts `/var/run/docker.sock`; it shares `/run/mavis/sandboxd.sock` with the worker.
- AWS (profile `cashfree`, region ap-south-1): EC2 `t3.large` (Ubuntu, Docker + gVisor `runsc` registered in `/etc/docker/daemon.json` via user-data), Elastic IP, security group 80/443/22 (22 restricted to the deployer's IP), Caddy auto-TLS on `<eip>.nip.io` or a domain, Secrets Manager for keys pulled at boot, CloudWatch agent for logs. Scripted in `deploy/` (bash + AWS CLI), idempotent.
- Telegram webhook set on deploy (`setWebhook` with secret token); dev uses long-polling (`deleteWebhook`).
- Scaling path (config only): api/worker → ECS Fargate, Postgres → RDS, Redis → ElastiCache, Neo4j → Aura, Qdrant → Qdrant Cloud, artefacts → S3.

---

## 14. Build stages (each ends demoable)

> Plan note: stages 4 and 5 are executed in swapped order (orchestrator before integrations) because connection prompting relies on the orchestrator's interrupt/resume machinery. See `docs/superpowers/plans/2026-10-02-mavis-00-index.md`.

1. **Foundation**: config, domain types, LLM layer, Postgres + Alembic, event bus (Redis + in-process), worker/api/timer/dev entrypoints, Telegram channel + outbox, persona chat with working memory.
2. **Memory**: extraction, entity resolution, Neo4j + SQLite graph, Qdrant episodes, profile card, recall.
3. **Initiative**: loops, wakeups/timer, initiative agent, composer, policy (quiet hours/budget/dedupe), onboarding routines. *Demo: interview → pep talk → "how'd it go?"*
4. **Integrations**: IntegrationProvider port, Composio adapter, connection prompting + resume, triggers/poller, first sync, inbox triage. *Demo: security-alert ping; morning check-in with calendar + inbox.*
5. **Orchestrator**: planner, Send fan-out, specialists, spawn_agent, critic, approvals via interrupt, task board. *Demo: draft email → Approve/Edit/Cancel; calendar invite with Jawahar.*
6. **Sandbox and artefacts**: Docker+gVisor sandbox via `sandboxd` (AgentCore optional), file intake, Docs/Analyst/DeepResearch specialists, PPTX/PDF/DOCX/XLSX. *Demo: "make me a 6-slide deck on Teamcenter basics" → .pptx in chat.*
7. **Deploy and harden**: Docker/compose, EC2 scripts, webhooks, Langfuse, evals, metrics.

## 15. Demo script (target)

1. `/start` → Mavis greets, asks name and what's on the plate; goes quiet → nudges after the onboarding delay (shortened via `DEMO_TIME_SCALE`).
2. "Interview prep with Jawahar Monday 10am" → asks ambiguity if past midnight → needs Calendar → **prompts connect** → user taps → resumes → asks to approve invite to Jawahar → ✅.
3. Show Neo4j graph: Jai → FRIEND_OF → Jawahar; Event → WITH → Jawahar.
4. Fire a Gmail security alert email → unprompted Telegram ping within seconds.
5. Wakeup fires (time-scaled): pep talk before; "how did it go?" after; user vents → empathetic, offers a Teamcenter crash-course deck → .pptx delivered.
6. Langfuse trace of the whole thing.

`DEMO_TIME_SCALE` (default 1.0) compresses wakeup offsets for stage demos without changing logic.

## 16. Risks and mitigations

| Risk | Mitigation |
|---|---|
| gpt-oss structured-output flakiness | tool-mode → JSON-mode fallback with validation + retry; small schemas; evals |
| Composio trigger availability/latency | poller fallback emitting identical events |
| Composio slug drift | single mapping table, verified at boot (`/admin/integrations/verify`) |
| Free-tier rate limits on Ollama | FAST for most calls, SMART only when needed; backoff; env switch to paid models |
| Docker daemon / gVisor unavailable | `runsc` missing → `runc` with a loud warning; no Docker → AgentCore backend if AWS creds; else documents feature degrades with a clear message (local backend is dev-only) |
| Sandbox escape via generated code | gVisor, no network, no secrets, read-only root, resource limits, worker has no Docker socket (sidecar exposes only run/write/read/list) |
| Over-pinging annoys user | budget, quiet hours, engagement-based learning, "ping me less" command |
| Prompt injection via email | untrusted wrapping, no outward tools in triage, approvals |

## 17. Existing code in this repo

A scaffold was started before this spec (config, domain models, LLM routing, SQLAlchemy store, memory graph/vector modules under `src/mavis/`). Phase 1 of the plan reconciles it with this spec: keep what matches (settings, structured-output helper, graph/vector stores), restructure the rest.

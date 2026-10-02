# Mavis AI

Mavis is a 24/7 personal assistant that lives in Telegram and behaves like a sharp human PA. She notices things, decides on her own when to speak up, and acts on your behalf, while anything that goes out to another person or costs money waits for your OK.

She is not a request and response chatbot. She greets you in the morning, checks in before the moments that matter, asks how they went afterwards, and keeps track of what you are waiting on.

> The codebase and Python package are named `zento` (the original project name). The agent persona and product are Mavis.

## What Mavis can do

| Capability | Status |
|---|---|
| Chat on Telegram in a warm, witty persona with short replies | Built |
| Remember the last 20 turns of conversation | Built |
| Long-term memory: people, plans, goals and preferences in a knowledge graph plus semantic recall, kept across restarts | Built on `phase-2-memory`, merging next |
| Proactive engine: pep talks before events, "how did it go?" afterwards, an adaptive morning check-in, quiet hours and a daily ping budget | Planned (Phase 3) |
| Gmail and Calendar: connect from chat with one tap, inbox watcher that pings you only about what matters, morning brief with today's schedule | Planned (Gmail slice, after Phase 3) |
| Multi-agent orchestration: a planner that fans work out to specialist agents in parallel, with approve, edit and cancel buttons for outward actions | Planned (Phase 4) |
| Slack and Notion, plus connect prompts that pause a task and resume it once you connect | Planned (Phase 5) |
| Sandboxed code execution and documents: PPTX, PDF, DOCX, XLSX, charts, data analysis and cited deep research | Planned (Phase 6) |
| 24/7 deployment on AWS with HTTPS webhooks, metrics and evals | Planned (Phase 7) |

## How it works

```
 Telegram ──webhook / long-poll──▶ api ──▶ event bus (Redis Streams, in-process in dev)
                                                   │
                     ┌─────────────────────────────┼──────────────────────────────┐
                     ▼                             ▼                              ▼
          conversation worker            initiative engine (P3)          timer (P3)
          per-user ordered turns         decides whether to notify,      fires wakeups
          recall ▶ LLM ▶ outbox          act, track or set an alarm      Mavis set herself
                     │
                     ▼
          learn job: extract facts, people and events ▶ Neo4j graph + Qdrant vectors + profile card
                     │
                     ▼
          outbox sender ▶ Telegram (ordered, retried, rate-limit aware)
```

Key design choices:

- **Event driven.** Every signal (your message, a new email, a calendar change, an alarm Mavis set for herself) becomes an event. Mavis decides what to do with it instead of following a fixed schedule.
- **Memory in five layers.** Working memory (recent turns plus a rolling summary), a profile card that is always in the prompt, a knowledge graph of your world, semantic episodes, and open loops (things that are still unfinished).
- **Reliable by default.** Webhooks are deduplicated, turns for the same user run in order, replies go through a transactional outbox, and failed messages are retried and then dead-lettered.
- **Safe by default.** Third-party text (email, web, Slack) is fenced as untrusted data, outward actions need approval, and secrets never reach logs.

## Tech stack

| Area | Choice |
|---|---|
| Language and tooling | Python 3.13, uv, ruff, pytest |
| Agent framework | LangGraph |
| Models | Ollama Cloud (`gpt-oss:20b` for fast replies, `gpt-oss:120b` for planning), swappable via env |
| API and channels | FastAPI, python-telegram-bot |
| State | SQLAlchemy 2 async with Postgres (SQLite in dev), Alembic migrations |
| Bus | Redis Streams (in-process in dev) |
| Memory | Neo4j (SQLite fallback), Qdrant (embedded in dev), fastembed local embeddings |
| Integrations | Composio for Gmail, Calendar, Notion and Slack, behind a swappable provider interface |
| Sandbox | Docker with gVisor, AWS Bedrock AgentCore Code Interpreter as an option |
| Observability | Langfuse, structlog |

## Quick start

Requirements: Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:jai-krishna-0921/Mavis-AI.git
cd Mavis-AI
uv sync
cp .env.example .env
```

Fill in at least these keys in `.env`:

| Key | Where to get it |
|---|---|
| `OLLAMA_API_KEY` | [ollama.com](https://ollama.com) account settings |
| `TELEGRAM_BOT_TOKEN` | Create a bot with [@BotFather](https://t.me/BotFather) |

Then run:

```bash
uv run zento chat     # local chat in the terminal, no Telegram needed
uv run zento dev      # everything in one process, polling Telegram
```

Message your bot on Telegram and Mavis will reply.

### Production roles

```bash
uv run zento migrate  # apply database migrations
uv run zento api      # webhook API (requires REDIS_URL and TELEGRAM_WEBHOOK_SECRET)
uv run zento worker   # conversation and job workers (requires REDIS_URL)
```

In production set `ENV=prod` and list your Telegram chat id in `ALLOWED_TELEGRAM_CHAT_IDS`. With an empty list, only dev mode accepts messages from anyone.

## Development

```bash
uv run pytest -q          # full test suite (no network calls, models are faked)
uv run ruff check src tests
```

Project layout:

```
src/zento/
  agents/     persona and conversation turns
  api/        FastAPI app, Telegram webhook, health checks
  bus/        event bus (in-process and Redis Streams)
  channels/   Telegram channel, outbox sender, dev poller
  domain/     shared types (events, memory, loops, decisions, plans)
  llm/        model routing, structured output, tracing
  memory/     extraction, graph, vectors, profile card, recall (phase-2-memory)
  store/      database models, repositories, migrations
  worker/     handler registry, per-user locks
docs/superpowers/
  specs/      design spec
  plans/      phase-by-phase implementation plans
```

## Documentation

- Design spec: [`docs/superpowers/specs/2026-10-02-zento-pa-design.md`](docs/superpowers/specs/2026-10-02-zento-pa-design.md)
- Implementation plans, one per phase: [`docs/superpowers/plans/`](docs/superpowers/plans/) (start with the `00-index` file)

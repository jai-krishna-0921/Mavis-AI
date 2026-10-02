# Mavis AI

Mavis is a 24/7 personal assistant that lives in Telegram and behaves like a sharp human PA. She notices things, decides on her own when to speak up, and acts on your behalf, while anything that goes out to another person or costs money waits for your OK.

She is not a request and response chatbot. She greets you in the morning, checks in before the moments that matter, asks how they went afterwards, and keeps track of what you are waiting on.

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
uv run mavis chat     # local chat in the terminal, no Telegram needed
uv run mavis dev      # everything in one process, polling Telegram
```

Message your bot on Telegram and Mavis will reply.

### Production roles

```bash
uv run mavis migrate  # apply database migrations
uv run mavis api      # webhook API (requires REDIS_URL and TELEGRAM_WEBHOOK_SECRET)
uv run mavis worker   # conversation and job workers (requires REDIS_URL)
```

In production set `ENV=prod` and list your Telegram chat id in `ALLOWED_TELEGRAM_CHAT_IDS`. With an empty list, only dev mode accepts messages from anyone.

## Development

```bash
uv run pytest -q          # full test suite (no network calls, models are faked)
uv run ruff check src tests
```

Project layout:

```
src/mavis/
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

## Deploy to AWS

One small box runs everything in a single docker compose stack: Postgres, Redis, Qdrant, Neo4j, the Mavis image as three services (`api`, `worker`, `timer`) and Caddy for HTTPS. Target: one `t4g.small` (ARM64, 2 GB RAM plus a 2 GB swapfile) in `ap-south-1`, Ubuntu 24.04, 20 GB gp3, one Elastic IP, about 17 USD a month on demand.

HTTPS needs no domain: Caddy gets a Let's Encrypt certificate for `<elastic-ip-with-dashes>.sslip.io`, for example `13-233-10-5.sslip.io`. Telegram runs in webhook mode at `https://<host>/telegram/webhook`. Caddy proxies only `/telegram/webhook`, `/webhooks/*`, `/connect/callback` and `/healthz`; everything else is a 404.

Prerequisites: the AWS CLI with a `cashfree` profile (override with `AWS_PROFILE`, `AWS_REGION`), `ssh`, `rsync`, `openssl`, `docker` and `uv` locally. The scripts live in `deploy/aws/` and share state through `deploy/aws/state.env` (resource ids only, gitignored).

```bash
deploy/aws/provision.sh              # key pair, security group, t4g.small, Elastic IP (idempotent)
deploy/aws/bootstrap.sh              # docker + compose, 2 GB swap, unattended-upgrades, ufw
deploy/aws/deploy.sh --no-webhook    # rsync, prod .env, build on the box, up, migrate (Telegram untouched)
deploy/aws/migrate-data.sh --yes     # copy demo Postgres, Qdrant and Neo4j to the box
# stop the local poller (`mavis dev`, the demo bot) now, then:
deploy/aws/webhook.sh set            # switch Telegram to the webhook
```

Later redeploys are just `deploy/aws/deploy.sh`. It reuses the `.env` already on the box: existing keys (generated secrets and any you added by hand) are kept and only missing ones are added, except `ENV`, `PUBLIC_BASE_URL`, `DOMAIN` and `TELEGRAM_MODE`, which follow the run. An ssh failure aborts the deploy rather than regenerating secrets.

The prod `.env` is built by `deploy.sh` from the demo `.env` keys (`OLLAMA_API_KEY`, `TAVILY_API_KEY`, `COMPOSIO_API_KEY`, `TELEGRAM_BOT_TOKEN`, `ALLOWED_TELEGRAM_CHAT_IDS`) plus generated secrets (`TELEGRAM_WEBHOOK_SECRET`, database passwords) and the in-stack URLs; the template is `deploy/.env.prod.example`. It is copied to `/opt/mavis/.env` with mode 600. `ENV=prod` makes the allowlist mandatory: the api refuses to start without `ALLOWED_TELEGRAM_CHAT_IDS`. `COMPOSIO_WEBHOOK_SECRET` stays empty, which keeps Composio on polling. The Composio connect redirect lands on `https://<host>/connect/callback`, served by the `api` service.

### Telegram: webhook or polling, never both

Telegram allows either a webhook or `getUpdates` polling for a bot. While a webhook is set, a local `mavis dev` poller gets `409 Conflict` and loses updates, so stop it before running `webhook.sh set`. To go back to local development:

```bash
deploy/aws/webhook.sh delete         # then run `mavis dev` locally again
uv run mavis telegram info           # current webhook state (also: set-webhook, delete-webhook)
```

### Moving the demo data

`migrate-data.sh --yes` replaces the box's data with the local demo state, so persona, memory and connection state carry over. Everything it reads from the demo is read-only.

| Store | Method |
|---|---|
| Postgres | `pg_dump` via `docker exec` into the demo container, `psql` restore on the box, then `mavis migrate` |
| Qdrant | collection snapshot API on the demo, then streamed into a one-off container on the box's compose network (Qdrant publishes no host port; the temporary snapshot on the demo is deleted again) |
| Neo4j | `deploy/aws/graph_export.py` writes all nodes and relationships to JSON with read-only Cypher; `graph_import.py` rebuilds them inside the mavis image. This avoids stopping the database for `neo4j-admin dump` |
| Redis | not copied (streams and locks are ephemeral) |

Run it before switching Telegram to the webhook, and do not run it again once the box has live data you want to keep.

### Backups

`bootstrap.sh` installs a cron job (03:00) that writes a gzipped `pg_dump` to `/var/backups/mavis` (mode 600, newest 7 kept). It covers Postgres only. Copy the dumps off the box if you hold data you cannot lose, since the volume is deleted with the instance. The instance has termination protection on; `teardown.sh --yes` lifts it.

### Operations

```bash
deploy/aws/status.sh                 # instance, containers, memory, readiness, webhook
deploy/aws/logs.sh api -f            # logs for a service (omit the name for all)
deploy/aws/teardown.sh               # lists everything tagged Project=mavis
deploy/aws/teardown.sh --yes         # deletes it all (instance, Elastic IP, security group, key pair)
```

`provision.sh` allows SSH only from your current public IP. When your IP changes, re-run it to move the rule. `/healthz` is liveness and `/readyz` checks Postgres, Redis, Qdrant and Neo4j with a 3 second timeout each; compose uses `/readyz` for the api healthcheck. `/readyz` is reachable from inside the stack only.

### Memory budget

Limits are set in `docker-compose.prod.yml`. Idle use was measured in a local run of the same stack, except where marked est.

| Service | Limit | Idle use |
|---|---|---|
| neo4j (heap 256m, pagecache 128m) | 640 MB | about 520 MB |
| worker (embedding model) | 640 MB | about 400 MB |
| api | 224 MB | est. 130 MB |
| qdrant | 192 MB | about 110 MB |
| postgres | 192 MB | about 30 MB |
| timer | 160 MB | about 105 MB |
| redis (maxmemory 64mb) | 96 MB | about 10 MB |
| caddy | 48 MB | est. 20 MB |

Limits sum to about 2.2 GB on purpose (idle use is about 1.35 GB); the 2 GB swapfile covers spikes, and roughly 400 MB stays free for a future sandbox container. On redeploy, `deploy.sh` stops the worker and timer while the image builds, then prunes old images and build cache.

## Documentation

- Design spec: [`docs/superpowers/specs/2026-10-02-mavis-pa-design.md`](docs/superpowers/specs/2026-10-02-mavis-pa-design.md)
- Implementation plans, one per phase: [`docs/superpowers/plans/`](docs/superpowers/plans/) (start with the `00-index` file)

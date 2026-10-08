# Mavis AI: Connectors and Knowledge Graph Design (Track 3)

Date: 2026-10-08
Status: draft for review
Input: connector strategy research (2026-10-08), verified against `main` at 59715fe.

## 1. Problem, goals, non-goals

### 1.1 Problem

Mavis knows a user only through chat plus five hand-wired sources (Gmail, Calendar, Slack, Notion, Google
Workspace via `googlesuper`). Three things block going wider:

1. **Adding a connector is bespoke work in about nine places.** The research counted seven; the code shows
   more (section 2). Every connector gets its own `EventType`, normalizer, webhook builder, first-sync
   routine, poll entry, args models, `ActionSpec`s and menu text.
2. **Everything enters memory as text through `memory.learn`.** Structured provider fields (attendees,
   amounts, durations) are flattened to text and re-extracted by the LLM. That is lossy, costs one LLM call
   per batch, and is why first sync is capped at `MAX_LEARN_JOBS = 3` per user.
3. **The graph cannot say where a fact came from.** So it cannot show "what I learned from Strava", cannot
   purge one source, and cannot tell trusted facts from third-party ones when it recalls them.

### 1.2 Goals

- G1. **One declarative connector spec per source.** A new Composio connector is one spec file, one mapper
  module and fixtures. No edits to enums, routers, normalizers or first sync.
- G2. **A single ingestion pipeline** for every provider: backfill, incremental (trigger or poll), cursors,
  idempotency, rate limits and budgets.
- G3. **Structured data goes into the graph deterministically.** The LLM reads only free text, on the
  `best_effort` lane, within per-user budgets.
- G4. **Every graph node, edge and vector carries multi-source provenance and trust.** Recall labels
  third-party facts. A source can be listed, forgotten and purged.
- G5. **Acting through connectors uses the existing `ActionSpec` / `RiskClass` / approval / taint
  machinery,** generated from the spec.
- G6. **Connector records produce Phase B subject keys,** feed the attention layer, and publish daily metrics
  that Programs (Track 2) can read.
- G7. **24 phase-1 connectors** across four adapters: Composio, direct OAuth, remote MCP and data archive
  import. LinkedIn and Instagram are included with their real API limits (owner input, 2026-10-08).
- G8. **A general data archive import route** (LinkedIn export, Google Takeout, Instagram export, WhatsApp
  chat export `.txt`, Apple Health) for sources with no usable live API. Archives use the same Record,
  graph, provenance and purge path as live connectors.
- G9. **WhatsApp as a second chat channel** (Cloud API business number), sharing the same brain as Telegram.
  This needs a channel-agnostic identity and delivery layer (section 11A).

### 1.3 Non-goals

- A second aggregator (Pipedream, Nango, Arcade and the rest). The research rejects them on cost and fit.
- Reading personal WhatsApp chats live. The Cloud API serves only the business number, so Mavis sees only
  messages sent to Mavis. A user can still send a chat export file (G8).
- LinkedIn feed, connections or messages over the API. The consumer API does not offer them, so the export
  archive covers them.
- Instagram personal accounts over the API. Instagram API with Instagram Login serves only Business and
  Creator accounts; personal accounts can use the export archive.
- Account Aggregator / FIU finance, Spotify, X (deferred; see research section 3). Zerodha Kite is phase 2.
- Our own branded Google OAuth app and the CASA audit. That is a launch gate and only a configuration change
  in Composio (section 4.4). This track does not do it.
- A web dashboard. The UX is chat: Telegram today, and WhatsApp per section 11A.
- Rewriting the attention scoring model or the commitments ledger. This track feeds them.

## 2. Verified current state (code pointers)

The research is accurate on its main points. The corrections and additions below change the design.

| Claim | Verified | Note |
|---|---|---|
| `IntegrationProvider` port in `tools/integrations/base.py` | yes | `catalog/status/connect_link/disconnect/execute/subscribe/parse_webhook`. No record listing. |
| `Capability` StrEnum in `domain/policy.py` | yes | 12 values. Agents, policy and first sync all depend on it. |
| Slug maps in `composio_map.py` | yes | `COMPOSIO_ACTIONS`, `MAVIS_TRIGGERS`, `COMPOSIO_TRIGGERS`, `GOOGLESUPER_TRIGGERS`, `WORKSPACE_TRIGGERS`, `TRIGGER_CONFIGS`, `_TOOLKIT_PREFIX`. |
| Webhook routing `SLUG_BUILDERS` / `_BUILDERS` | yes | `composio_webhooks.py`. googlesuper routes by full slug; the rest route by prefix. |
| Per-source `EventType`s | yes | `EMAIL_RECEIVED`, `CALENDAR_CHANGED`, `SLACK_MESSAGE`, `NOTION_CHANGED`, `WORKSPACE_SIGNAL`. |
| `MAX_LEARN_JOBS = 3`, `register_first_sync_handler` | yes | `first_sync.py`. Workspace handlers are registered in `attention/wiring.py:235-237`. |
| `POLLABLE = {GMAIL, CALENDAR}` | yes | `poller.py`. |
| **Missed by research:** agent tools | | `actions.py` already holds a declarative `ActionSpec` catalog (`name, capability, args_model, risk, risk_fn, agents, preview, taint_approve, identity, target`). That is the base for G5, so we do not need a new action format. Each new connector also needs pydantic args models there. |
| **Missed:** menu and commands | | `connect_flow._menu`, `actions.display_name`, `agents/commands.py` (`/connect`, `/connections`, `/disconnect`). |
| **Missed:** cursors live in `users.state` JSON | | Workspace cursors (`shared_after`, baseline) are kept in `users.state`, under the user row lock. That does not scale to 20 connectors. |
| `RiskClass` | yes | `READ, WRITE_SELF, OUTWARD, SPEND, DESTRUCTIVE`. `needs_approval` covers OUTWARD, SPEND and DESTRUCTIVE. WRITE_SELF needs approval after taint (`taint_approve`). |
| **Gap:** graph provenance | | Neo4j edges hold one `source_ref`, and `q_update_current_edge` **overwrites** it. Nodes hold none. A fact seen in chat and in Gmail remembers only the last source. |
| **Gap:** graph trust in recall | | `recall.py` wraps and taints only vector hits with `kind="signal"` and untrusted loops. **Graph neighborhood facts reach the prompt unlabelled, even when they were extracted from email.** This is an existing taint hole, and this track must close it first. |
| **Gap:** forget | | `MemoryService.forget(needle)` deletes by substring across graph, vector and profile. Nothing deletes by source. |
| **Gap:** vector ids | | `point_id = uuid5(user, text)`. The same sentence from two sources collapses to one point that holds one `source_ref`. |
| Composio auth | yes | `_auth_config` auto-creates `use_composio_managed_auth` per toolkit. A BYO OAuth app is a different auth config id per toolkit. |
| Commitments ledger (Phase B) | spec and plan only | Migrations stop at `0013_task_outcomes`. Subject keys come from the ledger spec section 3.2: `gmail:`, `gmail-thread:`, `cal:`, `gtask:`, `gfile:`, `conn:`, `action:`, `task:`. |
| Multi-user | partial | Everything is keyed by `user_id`, and Composio uses `mavis-<id>`. Invite codes, tiers and an environment-prefixed `composio_user_id` are specified in the sibling multi-user spec (`2026-10-08-mavis-multiuser-design.md`) but not built. |
| Channel layer | partial | `channels/base.py` defines a `Channel` protocol (`send_text`, `send_document`, `send_typing`, `react`, `download_file`). But identity is Telegram-shaped: `users.telegram_chat_id` (unique BigInteger), `chat_id: int` in the protocol, and `outbox` rows with no channel column. `outbox_sender.py`, `worker/runner.py` and `agents/conversation.py` read `user.telegram_chat_id` directly. Formatting and buttons assume Telegram (4096-char split, inline keyboards). |

## 3. Architecture

```
     Composio            DirectOAuth            RemoteMcp          ArchiveImport
   (googlesuper,      (Strava, Google Health, (Zomato, Swiggy,   (LinkedIn, Takeout,
    slack, github,     Instagram Login,         later others)     Instagram, WhatsApp
    linkedin..)        later Zerodha)                             .txt, Apple Health)
          \                   |                    |                   /
           +-------------- IntegrationProvider port (extended) -------+
                                        |
                              ProviderRouter (by spec.provider)
                                        |
   ConnectorRegistry  <---  specs/<connector>.py  (data + mapper)
        |   validates at load: unique ids, subject prefixes, slugs, risks
        v
   SyncEngine (jobs on the existing worker)
     backfill(window, budget) | incremental: trigger webhook or poll(cursor)
        |  raw payload page
        v
   mapper(raw) -> Record (canonical)            -> connector_records (Postgres, dedupe + tombstones)
        |                                       -> connector_cursors (Postgres)
        +--> bus: CONNECTOR_RECORD{record_id}   -> attention (by spec.attention.route)
        |                                       -> ledger closers (subject keys)
        |                                       -> metrics rollup (Programs)
        v
   IngestWorker
     1. GraphMapper: spec.graph rules -> nodes/edges with provenance (no LLM)
     2. resolver: identifiers first (email, phone, handle), names last
     3. Qdrant chunk per record (id = uuid5(user, record_key))
     4. free-text extraction (best_effort LLM, budgeted, batched, untrusted-wrapped)
```

### 3.1 Connector identity

`Capability` stays as the type that policy, agents and tools use, but it is no longer a closed enum we edit
by hand. It becomes a validated `str` newtype backed by the registry. The existing members stay as module
constants (`Capability.GMAIL` keeps working), and `Capability("strava")` is valid only when the registry
holds that spec. A connector id is lowercase `[a-z0-9_]+` and stable forever, because it appears in
provenance and subject keys.

Google keeps its fan-out: one `google` connection grants the Workspace capabilities. In the registry this is
a **bundle spec** (`google`) that lists member specs (`gmail`, `googlecalendar`, `drive`, `tasks`,
`contacts`, ...) sharing one provider account. Microsoft 365 uses the same pattern (`microsoft` bundles
`outlook`, `outlookcalendar`, `onedrive`, `teams`).

### 3.2 Connector spec format

A spec is a Python module under `src/mavis/connectors/specs/`. It is declarative data plus pure mapper
functions. We use Python rather than YAML because mappers and arg models are typed code, and one file keeps
the spec and its mapper together. The registry imports every module in the package. Nothing else is edited.

```python
SPEC = ConnectorSpec(
    id="strava",
    name="Strava",
    category=Category.HEALTH,                # menu grouping
    provider=Provider.DIRECT_OAUTH,          # COMPOSIO | DIRECT_OAUTH | REMOTE_MCP | ARCHIVE
    auth=DirectOAuth(authorize_url=..., token_url=..., scopes=("activity:read_all",),
                     client_id_env="STRAVA_CLIENT_ID", client_secret_env="STRAVA_CLIENT_SECRET"),
    status=Status.BETA,                      # BETA | GA | DISABLED (rollout gate, section 11)
    sensitivity=Sensitivity.HEALTH,          # NORMAL | FINANCIAL | HEALTH | MESSAGING: extra consent, shorter raw retention
    subject_prefix="strava",                 # subject key "strava:<external_id>"; unique across registry
    streams=(
        Stream(
            kind=Kind.ACTIVITY,
            list_action="activities.list",   # provider-side action used for backfill and polling
            paginate=Paginate.before_after(field="start_date"),
            backfill=Backfill(window_days=180, max_records=500),
            incremental=Webhook("activity.create", "activity.update", "activity.delete")
                        | Poll(every_minutes=60, cursor="start_date"),
            deletes=Deletes.TRACKED,
            map=map_activity,                # raw dict -> Record | None (pure, unit-tested)
        ),
    ),
    graph=(                                  # deterministic graph rules over Record.fields (section 6.2)
        Edge("User", "DID_ACTIVITY", node=Node("Activity", key="external_id", props=("sport", "distance_m", "moving_s"))),
        Edge("Activity", "AT", node=Node("Place", key="fields.start_place"), when="fields.start_place"),
    ),
    extract_text=False,                      # no LLM on this connector's bodies
    attention=AttentionRoute.NONE,           # EMAIL | SIGNAL(rules) | NONE
    metrics=(Metric("workouts", agg="count"), Metric("active_minutes", field="moving_s", agg="sum", scale=1/60)),
    actions=(),                              # ActionSpec entries (section 7)
    self_authored=lambda r: True,            # whose words/fields these are (section 6.3)
)
```

Registry validation runs at import and in CI. It fails the build on any of these:

- a duplicate id or `subject_prefix`;
- a subject prefix that collides with a ledger built-in (`cal`, `conn`, `action`, `task`, `chat`, `legacy`);
- a write action with no explicit `RiskClass`;
- a `Webhook` trigger the provider cannot route;
- a graph rule that names a label or relation outside `names.sanitize_label` / `sanitize_rel`;
- a spec with no fixture directory.

`scripts/verify_composio.py` is extended to check every Composio spec's slugs and trigger names against the
live catalog. It runs nightly, not on every PR, because it needs the API key.

### 3.3 Provider port (extended) and adapters

The port gains three methods. Existing methods are kept, and `Capability` is still the argument.

```python
class IntegrationProvider(Protocol):
    ...existing...
    async def list_records(self, user: UserRef, stream: StreamRef, cursor: Cursor | None,
                           page_size: int) -> Page: ...        # raw payloads + next cursor + has_more
    async def fetch_record(self, user: UserRef, stream: StreamRef, external_id: str) -> dict | None: ...
    async def revoke(self, user: UserRef, connector: str) -> None: ...   # disconnect AND revoke token upstream
```

`ProviderRouter` implements the port by delegating on `spec.provider`. Callers keep depending only on the
port, as today.

- **ComposioProvider** (existing). `list_records` calls `execute` with the stream's `list_action` and passes
  the cursor through the spec's paginator. The slug maps move out of `composio_map.py` into specs. The
  module keeps only the googlesuper/legacy routing helpers and the arg translators that specs import.
- **DirectOAuthProvider** (new, `tools/integrations/direct_oauth.py`) runs authorization code + PKCE
  against our own app. The callback route `/oauth/<connector>/callback` uses a signed `state` that binds
  user, connector and expiry. Tokens live in a new `connector_tokens` table, encrypted with
  envelope encryption (a data key per row, wrapped by a KMS or `MAVIS_TOKEN_KEK`). Refresh happens on 401
  or ahead of expiry, and is single-flight per (user, connector). A refresh failure marks the connection
  FAILED and goes through the existing `prompt_reconnect`. Each connector has a typed client, which the spec
  names (`client=StravaClient`). Webhooks are verified per vendor and mapped to the user through the token
  row's `external_account_id`.
- **RemoteMcpProvider** (new, `tools/integrations/remote_mcp.py`) is an MCP client over Streamable HTTP
  with OAuth 2.1, DCR and PKCE. It keeps one client registration per server and tokens per user, in the same
  `connector_tokens` table. Tools are discovered with `tools/list`, but **only tools named in the spec are
  exposed**, and the agent sees **our** description, never the vendor's. Vendor tool descriptions are
  untrusted (tool poisoning), and a newly listed tool is logged and ignored until a spec names it. MCP
  servers have no record listing, so `list_records` maps to a named read tool (for example
  `get_order_history`) where one exists, and otherwise the stream is `Poll`-only or actions-only.

- **ArchiveImportProvider** (new, `tools/integrations/archive_import.py`) handles sources with no live API,
  or whose API leaves gaps. Section 3.4 describes it.

`status()` merges all four sources into one `dict[str, ConnectionState]`, and `ConnectionCache` is
unchanged. An archive connector reports ACTIVE once at least one import has finished, and NONE otherwise.

### 3.4 Data archive import (general route)

Many sources let a user download their own data even when there is no API for it. Mavis supports this as
one generic provider, `Provider.ARCHIVE`.

**Format.** A spec of `Provider.ARCHIVE` declares an `Archive` block:

- `accepts`: file name patterns and MIME types, for example `Basic_LinkedInDataExport_*.zip`, `takeout-*.zip`,
  `instagram-*.zip`, `WhatsApp Chat with *.txt` or `.zip`, and `export.zip` for Apple Health;
- `detect`: a pure function over the archive's file listing, plus the first bytes of at most two named
  files, that returns a confidence;
- `members`: a map from an inner path glob to a parser, for example `Connections.csv` to
  `parse_connections`, `messages/inbox/*/message_1.json` to `parse_ig_thread`, or `Takeout/My Activity/*`
  to `parse_activity`;
- `max_compressed_mb` and `max_uncompressed_mb`;
- `historical=True` always, so imported records never ping and never open live ledger items.

**Flow.**

1. **Upload.** The user sends the file to Mavis in chat (Telegram or WhatsApp document), or asks "import my
   LinkedIn export". Mavis replies with the steps for downloading the export from that service, taken from
   the spec. The file arrives through `Channel.download_file` into a per-user quarantine directory.
2. **Detect.** The registry asks every archive spec's `detect`, and the highest confidence above 0.8 wins.
   Otherwise Mavis asks "Is this your LinkedIn or Instagram export?" with buttons. Detection never trusts
   the file name alone.
3. **Consent.** Mavis states what it will read and asks for a Yes button, as connect does (section 8.1).
   Health and messaging archives always ask.
4. **Parse** in a job, not in the chat turn. Archives are untrusted input:
   - zip-slip and symlink entries are rejected;
   - the size, entry count and compression ratio are capped (zip bomb defence);
   - the job parses with `defusedxml` and streaming CSV/JSON readers;
   - nothing in the archive is executed or rendered as HTML;
   - each parser emits ordinary Records into `connector_records`.
5. **Ingest** through the normal SyncEngine path (section 4). The archive id forms the cursor, so
   re-uploading the same file is a no-op (dedupe on `content_hash`), and a newer export only adds or
   updates.
6. **Delete the file** once parsing finishes, and keep only Records. Mavis reports: "From your LinkedIn
   export I learned 412 connections, 9 jobs and 3 schools."

**External ids.** An archive often has no stable ids. The parser derives `external_id` from a hash of the
identity fields declared in the spec. A LinkedIn connection uses its profile URL, or else name plus
connected date. A WhatsApp message uses chat, timestamp, author and text. Repeated imports then dedupe
correctly.

**Self-authored and trust.** The user's own profile, positions and posts are self_authored. Messages,
connections' names and other people's content are third_party. A WhatsApp export is third-party text
except the user's own lines, which the parser identifies by the author name the user confirms once.

**Budgets.** Archives can be very large, so the same extraction budget applies (section 4.5), with a
one-time archive allowance of 100 calls per archive. Deterministic parsing covers most of the value: people,
jobs, schools and dates need no LLM.

**Purge.** Per connector, as for a live one (section 8.4). `/forget linkedin` removes everything any
LinkedIn import produced.

**Phase-1 archive specs:**

| Archive | What it yields |
|---|---|
| LinkedIn data export | `Profile.csv`, `Positions.csv`, `Education.csv`, `Skills.csv`, `Connections.csv` (name, company, position, connected on; email only when the member shared it), `messages.csv`, `Invitations.csv` |
| Google Takeout | selected products only: `My Activity` (Search, YouTube watch history, which the API lacks), Maps saved places and Timeline exports where present, `Fit` legacy data. The user picks products in Takeout, and Mavis ignores products without a parser. |
| Instagram export (personal accounts) | followers and following, liked posts and saved items as interests, DMs as third-party messages |
| WhatsApp chat export `.txt` (per chat, optionally with media in a zip) | one chat's messages: people (name and phone where the export shows numbers), relationships and third-party facts, searchable as vectors. Media is skipped. Being historical, it creates no live ledger items; the user can ask "what did Ravi ask me for?" and track one explicitly. |
| Apple Health `export.zip` | as in section 10, row 22 |

## 4. Ingestion pipeline

### 4.1 Jobs

All jobs run on the existing worker (`mavis.worker`) as typed jobs. Each job is idempotent and safe to
rerun.

| Job | Trigger | Work |
|---|---|---|
| `connector.backfill(user, connector, stream)` | connection becomes ACTIVE; user asks to relearn | Pages back to `backfill.window_days` or `max_records`, newest first, so the most useful data lands first. Stops when the budget is hit and resumes on the next day's budget. |
| `connector.poll(user, connector, stream)` | scheduler, per `Poll.every_minutes` (jittered), only when no healthy webhook is registered, or as a periodic safety net (default 6h) | Lists records since the cursor. |
| `connector.webhook(event)` | provider webhook | Maps the payload to a Record. If the payload is thin, calls `fetch_record`. |
| `connector.ingest(record_id)` | new or changed record row | Graph mapping, vectors, and an optional extraction enqueue. |
| `connector.extract(user, batch)` | best_effort | LLM extraction over the bodies of several records. |
| `connector.purge(user, connector)` | disconnect, `/forget <source>` | Section 8.4. |
| `connector.reconcile(user, connector)` | daily | Re-lists the last N days to catch missed webhooks and deletes. Checks token health. |

The existing per-source first syncs (`first_sync._gmail/_calendar/_slack/_notion`, Workspace handlers) are
replaced by `connector.backfill` per stream, after the shadow period (section 11). The attention-specific
part of first sync (a quiet baseline, no pings for historical items) becomes a generic rule: **records with
`occurred_at` before the connection's `activated_at` are ingested with `historical=True`, never pinged, and
never create live ledger items.** They may close existing ones.

### 4.2 Cursors

Table `connector_cursors(user_id, connector, stream, cursor JSON, backfill_state JSON, last_ok_at,
last_error_kind, consecutive_failures, webhook_id, webhook_healthy_at, paused_until, llm_day, llm_used)`,
with primary key (user_id, connector, stream).

- The cursor is opaque per paginator: a timestamp, page token, history id or sync token.
- A cursor advances only after the page's records are committed to `connector_records`. A crash replays the
  page, and dedupe absorbs it.
- `backfill_state` holds the backward cursor, the counts and `done`. Backfill and incremental use separate
  cursors, so live data flows while backfill is still running.
- The Workspace cursors in `users.state` move here through a data migration.

### 4.3 Idempotency and dedupe

- A record key is `<connector>:<stream_kind>:<external_id>`. It is unique per user.
- `content_hash` is sha256 of the canonical JSON of `fields`, `title` and `body`. The same key with the same
  hash is a no-op. The same key with a new hash is an **update**: `version` goes up, and the ingest job
  re-derives the facts from that record (section 6.4).
- Webhook events are first recorded in the existing `processed_events` table, under the provider event id.
- A deletion upstream (`Deletes.TRACKED`) sets `deleted_at` on the record row and retracts facts sourced
  only from it. Rows marked `Deletes.IGNORED` keep their facts until purge or until `retention` ends.

### 4.4 Rate limits and auth configs

- **Provider buckets.** The Composio account bucket is 80% of the plan RPM. Each direct API has its own
  bucket: Strava's documented 15-minute and daily limits are split across users. A `RateLimited` result
  pauses the stream (`paused_until`) and leaves the cursor where it is.
- **Per-user fairness.** At most 2 connector jobs run at once per user and 1 per (user, connector).
  Backfills yield to incremental work: incremental is `background` and backfill is `best_effort` in job
  priority.
- **Auth configs.** The spec says `auth=ComposioManaged()` or `ComposioCustom(config_id_env=...)`. Moving
  Gmail to our own Google app later changes one spec field and an env var.

### 4.5 LLM cost and budgets

Deterministic mapping costs no LLM call, so most records never need one. Free-text extraction is optional
per stream (`extract_text`), and it is limited three ways:

- **Value gate (code, no LLM).** Email in categories that attention already drops (`label_dropped`,
  newsletter senders), bodies under 20 words, bot authors, and records older than
  `extract_window_days` (default 30) are mapped but never extracted.
- **Batching.** Up to 8 records or 6,000 chars per call. Each record is wrapped untrusted with its own
  `record_key`, so extracted facts are attributed per record.
- **Budgets, counted in LLM calls on `connector_cursors` per user:**
  - steady state: 40 calls per user per day across all connectors;
  - backfill: 60 calls per connector, once, spread over up to 3 days;
  - a global daily cap across users, from config.

  Over budget, records stay `pending_extract`. They are extracted the next day, newest first, or dropped
  after 7 days. The deterministic facts are already in, so dropping loses only nuance.
- All extraction runs on the `best_effort` lane (59715fe), so it never takes the last free slot while chat
  is active. `LLMError` defers the work and does not count against the budget.

Attention's own `understand` call per email is unchanged and keeps its existing background priority. It is
not double-billed. Extraction for a Gmail record reuses the attention understanding when one exists (kind,
money, people, deadline) and extracts free text only when needed.

## 5. Common record schema

`domain/records.py`:

```python
class Kind(StrEnum):
    MESSAGE, THREAD, EVENT, TASK, DOC, CONTACT, ACTIVITY, MEASUREMENT, TRANSACTION, ORDER, TRIP,
    BOOKING, ISSUE, MEDIA, NOTE

class Actor(BaseModel):
    role: str                     # "from", "to", "attendee", "assignee", "owner", "merchant", ...
    name: str | None
    email: str | None             # normalised lower
    phone: str | None             # E.164
    handle: str | None            # "<connector>:<provider user id>"
    is_self: bool = False         # matches the connection's own account identifiers

class Record(BaseModel):
    user_id: int
    connector: str                # spec id
    kind: Kind
    external_id: str
    parent_external_id: str | None    # thread, calendar, board, repo
    occurred_at: datetime | None      # when it happened (event start, activity start, txn time)
    updated_at: datetime | None       # provider's change time
    actors: list[Actor]
    title: str | None                 # third-party text unless self_authored
    body: str | None                  # free text; never trusted; capped at 20k chars
    fields: dict[str, Scalar | list[Scalar]]   # typed, validated per kind (amount, currency, due_at, ...)
    url: str | None
    labels: list[str]                 # provider labels and categories
    self_authored: bool               # computed by spec.self_authored
    historical: bool                  # before activation (section 4.1)
    content_hash: str

    @property
    def record_key(self) -> str: ...  # "<connector>:<kind>:<external_id>"
    @property
    def subject_key(self) -> str: ... # "<subject_prefix>:<external_id>" (ledger section 3.2)
```

`fields` are validated per kind against a small schema in `domain/records.py`. Examples: TRANSACTION needs
`amount` (Decimal), `currency` (ISO) and `direction` (debit or credit). EVENT needs `start` and allows
`end`, `all_day`, `location` and `status`. ACTIVITY needs `sport` and `moving_s`. A mapper that produces
invalid fields gets the record **quarantined**: it is stored with `status=invalid`, not ingested, and
counted on a metric.

**Subject keys.** These are the ledger's own keys, produced by one function:

- Gmail: `gmail:<message_id>` and `gmail-thread:<thread_id>`.
- Google Tasks and Drive: `gtask:<id>` and `gfile:<id>`.
- Calendar: `cal:<start_utc_minute>|<attendees>`. The ledger spec defines it from structured fields, so the
  calendar spec sets `subject_key_fn` instead of using the prefix.
- New connectors: `<subject_prefix>:<external_id>`, for example `gh:<owner/repo#123>`,
  `linear:<issue id>`, `todoist:<id>` or `splitwise:<expense id>`.

`Record.subject_key` is the only producer. Ledger closers and attention observations read it from the
record and never parse text.

**Raw store.** Table `connector_records`:

- columns: `id, user_id, connector, kind, external_id, record_key (unique per user), subject_key,
  occurred_at, updated_at, title, body, fields JSON, actors JSON, url, labels, self_authored, historical,
  content_hash, version, status (new | ingested | pending_extract | extracted | invalid | deleted),
  ingested_at, deleted_at, body_expires_at`;
- `body` and `title` are encrypted at rest with the same envelope key as tokens. They are needed for
  re-extraction and for "show me the source";
- `body` is nulled at `body_expires_at`: 30 days by default, 7 days for `Sensitivity.HEALTH`,
  `FINANCIAL` and `MESSAGING`. `fields`, `actors` and the hash stay until purge, so dedupe and provenance keep working;
- a purge deletes the row's content and leaves a tombstone of `(user_id, record_key, content_hash,
  purged_at)`, so a late webhook cannot bring the record back.

## 6. Graph model, provenance and trust

### 6.1 Labels and relations

The existing vocabulary in `memory/names.py` grows by a small fixed set. Labels and relations are still
interpolated only after sanitising.

- **New labels:** `Event`, `Task`, `Document`, `Activity`, `Transaction`, `Order`, `Merchant`, `Trip`,
  `Repo`, `Issue`, `Course`, `Account` (a source account of the user).
- **New relations:** `ATTENDED, ORGANIZED, SENT_TO, ASSIGNED, OWNS, COLLABORATES_ON, DID_ACTIVITY, AT, PAID,
  OWES, ORDERED_FROM, TRAVELS_ON, MEMBER_OF, ENROLLED_IN, MENTIONS`.

These are typed facts with properties (`at`, `amount`, `currency`, `distance_m`). An edge `statement` is
rendered by code from a template in the spec rule ("attended {event} on {date}"), never by the LLM.

High-volume kinds are **not** one node per record: MEASUREMENT (steps, heart rate, sleep) and most
MESSAGE records. They go to metrics (section 9.3) and vectors. The graph keeps only what recall needs to
reason with: people, organisations, places, recurring events, projects, open tasks, notable transactions and
orders, and activities rolled up into a routine (`User -[DOES {sport, per_week}]-> Routine`).

### 6.2 Deterministic mapping and resolution

`GraphMapper.apply(record, spec.graph)` turns the spec's rules into `upsert_entity` / `upsert_relation`
calls with provenance. Entity resolution runs in this order:

1. `Actor.email`, `phone` or `handle` matched against `Entity.identifiers`. This is a new indexed property
   holding a list of normalised `email:x`, `phone:+91...` and `handle:...`. Contacts records seed this list.
2. The existing name and alias path in `memory/resolver.py`, only when no identifier is present. A
   name-only match from a third-party record creates an **alias suggestion** and never merges on its own.
   Merges by name stay with the existing consolidation.
3. `is_self` actors map to the `User` node, and their identifiers are added to the user's own identifier
   list. That list is how later records detect `is_self`.

### 6.3 Provenance and trust on every fact

Each node and edge carries:

- `sources: [record_key | "chat:<event_id>" | "system:<ref>"]`, capped at 50 with the oldest dropped.
  `source_count` keeps the true count.
- `origins: [connector id | "chat" | "system"]`, a small set used for per-source listing and purge.
- `trust`: the **highest** trust among its live sources, ordered `user > self_authored > third_party`. It is
  recomputed whenever sources change.

This replaces the single overwritten `source_ref` (`q_update_current_edge`). Edges that already exist are
migrated to `sources=[source_ref]`, with origin and trust derived from the ref's prefix **once, during
migration only**. A missing prefix maps to `third_party`. After migration, trust is carried explicitly from
the record, never inferred from the ref's shape, as `Provenance` already requires.

There are three trust classes for graph facts:

- `user`: the user said it in chat (`Trust.USER`).
- `self_authored`: typed fields and titles of records the user authored through their own account, as
  decided by the spec's `self_authored` rule. Examples: the user's own contacts, events they organised,
  their own Strava activities and health metrics, mail they sent (headers only), their own Todoist tasks.
  Bodies are never self_authored, because sent mail quotes others and notes can contain pasted text.
- `third_party`: everything else, which is anything written by someone other than the user.

Recall changes (`memory/recall.py`):

- Graph neighborhood lines whose `trust` is `third_party` are wrapped with
  `wrap_untrusted(..., source="<origin>")` and added to `tainted`, exactly as vector `signal` hits are
  today. This closes the existing hole in section 2 and is **build step 1**.
- Each fact line gets a short source label ("from Gmail", "from your calendar"), so the model and the user
  can tell where a fact came from.
- `self_authored` facts are not wrapped and do not taint. That keeps "add my run to my notes" from always
  needing approval just because Strava data was recalled. The trade-off is open question 1.

Vectors (`memory/vector.py`) change too:

- Connector chunks use `point_id = uuid5(user, record_key)`, so two sources never collapse into one point.
- The payload gains `origin`, `record_key`, `kind=connector:<kind>` and `trust`.
- Any `trust=third_party` payload is treated like `signal` on recall.
- The existing conversation points (`uuid5(user, text)`) are unchanged.

Ledger and attention keep their existing rules: third-party items enter the ledger only from attention or
connector records keyed by subject, never from an LLM paraphrase (ledger spec section 3.5). Free-text
extraction over connector bodies runs through `learn` with `Trust.UNTRUSTED` and `conversation=False`. The
T3 grounding already forbids loops and events from that path, and it may produce only entities and
relations.

### 6.4 Updates and retraction

- A changed record (new hash) re-applies its rules. Edges that this record alone sourced and that the new
  version no longer produces lose the record from `sources`. When `sources` becomes empty the edge is closed
  (`valid_to`). Nodes left with no sources and no edges are deleted.
- The same code path handles retraction of deleted records (section 4.3) and purges (section 8.4). There is
  one function, `retract(user, record_keys)`.

## 7. Actions

Spec `actions` are `ActionSpec` entries, the existing dataclass, contributed by the spec instead of the
hand-written `_SPECS` tuple. The registry merges them into the catalog that `tools/registry.py` already
selects from. Rules:

- **Risk is declared.** `READ`, `WRITE_SELF`, `OUTWARD` (anything another person sees: send, comment,
  share, RSVP with a note, post), `SPEND` (orders, bookings, payments, Splitwise settle-up), `DESTRUCTIVE`
  (delete, trash, cancel someone else's booking). `risk_fn` covers dynamic cases, as calendar does today
  (attendees make it OUTWARD).
- **Defaults are safe.** Connector `WRITE_SELF` actions default to `taint_approve=True`. A REMOTE_MCP tool
  with no spec entry is not exposed. A spec entry for an MCP tool must declare its risk, and the tool's
  `readOnlyHint` annotation is ignored for policy, because it is vendor-asserted.
- **Identity and target.** `identity` fields give the ledger its `action:<tool>|<identity args>` keys and
  give approvals their dedupe (Phase A). `target` is used by the guard checks (`workspace_guard.py`
  patterns: ownership, external domains).
- **Results.** Every result still goes through `render_result` / `wrap_untrusted`. A successful write emits
  `APPROVAL_EXECUTED` with args, and the ledger closes items by subject key from it.
- **SPEND.** These need approval every time, with no standing rule allowed: `policy_rules` cannot
  auto-approve SPEND. The preview shows the amount, merchant and items, rendered from typed args.
- **Agent exposure.** Tools are grouped by `category`, mapped to the existing agent names (`inbox`,
  `calendar`, `comms`, `knowledge` and the new `life`). A user sees a connector's tools only while it is
  ACTIVE, which the existing `active_capabilities` path already enforces.

## 8. User-facing UX (Telegram, and WhatsApp per section 11A)

### 8.1 Connect

- `/connect` shows a menu grouped by category: Google, Microsoft, Work, Tasks, Health, Food and shopping,
  Money, Learning. It is built from the registry, and specs with `status=DISABLED`, or not enabled for the
  user's cohort (section 11), are hidden.
- `/connect strava` sends a link straight away. Composio and MCP use their hosted consent pages. Direct
  OAuth uses our callback.
- Before the link, one line says **what Mavis will read and do**, from the spec. For example: "Strava: I'll
  read your activities to learn your routine. I won't post." `Sensitivity.HEALTH` and `FINANCIAL` connectors
  ask for an explicit Yes button before the link is sent. That button is consent under DPDP for a sensitive
  purpose.
- After activation (the existing `connect_flow._announce`): "Connected. Reading your last 6 months of
  activities in the background." When backfill finishes, one summary follows: "From Strava I learned: you
  run about 3 times a week, mostly mornings near Cubbon Park." That summary is built from the graph facts
  and metrics, rendered by code.
- **Learning off.** A toggle on `/connections` (`learn: on | off`) lets a connector serve actions only.
  With learning off, records still feed attention, and nothing is written to graph or vectors.

### 8.2 Connections and what Mavis learned

- `/connections` lists each connector with its status, last sync, items learned (facts and records) and
  buttons for `What you learned`, `Forget`, `Learning on/off` and `Disconnect`.
- `/learned <source>` (also "what do you know from my Gmail?" through a READ tool,
  `memory.learned_from(source)`) shows:
  - counts by kind;
  - the 10 most-used facts from that origin, with their source labels;
  - the 5 most recent records, each with a "show source" link.

  It is rendered by code from graph and record rows. Third-party text is wrapped, as in recall.
- Each fact line from `/learned` has a **Forget this** button. It removes the fact (all sources), and adds a
  `(user, fact signature)` suppression, so re-ingestion does not bring it back.

### 8.3 Forget per source

`/forget <source>` asks for confirmation and then purges everything learned from that source, while the
connection stays. The cursor is reset to "now" by default, so old data is not relearned, and a
`Relearn from scratch` button runs a fresh backfill. The bot copy: "Forgot 214 facts and 1,030 notes from
Strava. I'm still connected for new activities; say /disconnect strava to stop completely."

### 8.4 Purge semantics (disconnect, forget)

`connector.purge(user, connector)` runs these steps in order. Each step is idempotent, and the job is
resumable.

1. Stop: remove triggers (`subscribe` ids on the cursor rows), pause the streams, and cancel pending
   extract jobs.
2. Graph: `retract(user, all record_keys for connector)`. Facts that also have other sources (chat,
   another connector) survive with that origin removed. Nodes left with no sources are deleted, except
   `User`.
3. Vectors: delete points by filter `user_id` + `origin`.
4. Raw store: delete content and keep tombstones (section 5).
5. Ledger and attention: live items whose subject prefix belongs to the connector are dropped with evidence
   `source_purged`. Attention observations from that connector are deleted.
6. Metrics: delete rows for the connector.
7. On disconnect only, upstream: `revoke` (Composio account delete, direct OAuth token revoke, MCP token
   revoke) and delete the `connector_tokens` row.
8. A summary audit row is written to `audit_log`, with counts and no content.

The purge runs on **every disconnect**. The research proposed this, and DPDP (purpose limitation and
erasure) supports it. The disconnect confirmation states it: "This also forgets what I learned from Strava."
A data export before purge is out of scope.

**Account deletion** (Track 5) runs purge for every connector, then the existing per-user deletes.

## 9. Attention, Programs and the ledger

### 9.1 Attention routing

`spec.attention` chooses the route:

- `EMAIL`: Gmail and Outlook records go to the existing `Intake.on_email` path, adapted to take a Record.
  `understand.py`, baselines, money anomaly and the speaker are unchanged.
- `SIGNAL(rules)`: deterministic rules over Record fields produce an attention signal with urgency and
  kind, the way `attention/workspace_signals.py` does for Workspace today. Rules are spec data. Examples:
  - GitHub `review_requested` for self, `ISSUE` assigned to self, `due_at` within 24h;
  - Splitwise expense where `fields.owed_by_self > 0`;
  - Calendly booking created;
  - Classroom assignment due in 48h.

  Signals flow through `attention/policy.py` (ping budget, quiet hours, per-subject daily slot) and dedupe
  on the record's subject key.
- `NONE`: no pings. Health, YouTube and most Drive changes take this route.

Historical records never ping (section 4.1). Third-party free text in signals is still wrapped, as today.

### 9.2 Ledger

Once Phase B ships:

- connector records with `kind in (TASK, ISSUE)`, assigned to self and open, become `action` or `deadline`
  items keyed by the subject key. They close when the record's `fields.status` turns done, which is
  evidence;
- `BOOKING` and `TRIP` become `event` items;
- `waiting_on` items close when a matching record appears (the Workspace spec's file-share rule,
  generalised to subject keys).

Before Phase B, no connector creates loops. That is the current Phase 3 rule, and it stays.

### 9.3 Signals for Programs (Track 2)

Table `connector_metrics(user_id, connector, metric, local_day, value, unit, updated_at)`. It is computed by
code from records, using the spec's `metrics` and the user's timezone (`domain/localtime.py`).

| Metric | Unit | Source |
|---|---|---|
| `calendar.busy_minutes` | min | calendar |
| `calendar.meetings` | count | calendar |
| `calendar.first_start` | local minute of day | calendar |
| `calendar.last_end` | local minute of day | calendar |
| `fitness.steps` | count | Google Health, Apple Health |
| `fitness.active_minutes` | min | Strava, Google Health, Apple Health |
| `fitness.workouts` | count | Strava, Google Health, Apple Health |
| `fitness.distance_m` | m | Strava, Google Health, Apple Health |
| `sleep.minutes` | min | Google Health, Apple Health |
| `sleep.start` | local minute of day | Google Health, Apple Health |
| `hr.resting` | bpm | Google Health, Apple Health |
| `money.spend` | per currency | email extractors, Splitwise |
| `money.food_orders` | count | Zomato, Swiggy, email |
| `work.reviews_waiting` | count | GitHub, Linear |
| `work.tasks_open` | count | Todoist, Linear |
| `work.tasks_overdue` | count | Todoist, Linear |

Programs (Track 2 spec, section 10.4) read through two read-only APIs:

- `signals.series(user_id, metric, days)` and `signals.latest(...)` for aggregates such as calendar load
  and sleep;
- `records.query(user_id, kinds, since, connector=None)` for event-level matching, for example auto-logging
  a workout from a Strava activity. It returns `record_key`, `kind`, `occurred_at`, typed `fields`,
  `connector` and `self_authored`, and **never `title` or `body`**. Any third-party string a pack needs
  for display goes through `wrap_untrusted`.

Programs never touch `connector_records` directly. When several sources report the same metric, the source priority is declared in the
metric registry (for example Google Health over Apple Health for steps, chosen per user by the most recent
data), and values are not summed. Metric values are numbers computed by code, so they are trusted as
computed facts. Metric rows count toward per-source purge.

## 10. Phase-1 connectors

Here "graph" means deterministic nodes and edges, and "text" means LLM extraction allowed.

| # | Connector | Provider | Streams (kinds) | Graph / text contribution | Attention | Metrics | Actions (risk) |
|---|---|---|---|---|---|---|---|
| 1 | Gmail (google bundle) | Composio googlesuper | messages (MESSAGE, THREAD) | people and orgs from headers (graph); bodies via understand and text; typed **email extractors** (see below) emit TRANSACTION / ORDER / TRIP / BOOKING records | EMAIL | `money.*` via extractors | search/read (READ), draft (WRITE_SELF), send/reply (OUTWARD), label/archive (WRITE_SELF) |
| 2 | Google Calendar | googlesuper | events | Event, ATTENDED, ORGANIZED, Place; routines from recurrence | SIGNAL (invites, changes by others) | `calendar.*` | list/slots (READ), create self (WRITE_SELF), with attendees or RSVP (OUTWARD), delete (DESTRUCTIVE) |
| 3 | Google Contacts | googlesuper | contacts | canonical Person with identifiers, birthdays, org (self_authored) | NONE | none | search (READ), create/update (WRITE_SELF) |
| 4 | Drive / Docs / Sheets | googlesuper | files, comments | Document, OWNS, COLLABORATES_ON; text only on comments addressed to self | SIGNAL (existing workspace rules) | none | existing Workspace catalog |
| 5 | Google Tasks | googlesuper | tasks | Task (self_authored) | SIGNAL (due) | `work.tasks_*` | add/complete (WRITE_SELF) |
| 6 | Outlook mail + calendar (microsoft bundle) | Composio `outlook` | messages, events | same as 1 and 2 | EMAIL, SIGNAL | `calendar.*` | as 1 and 2 |
| 7 | Slack | Composio | messages (DMs, mentions only) | Person (handle), MEMBER_OF channel; text on asks of the user | SIGNAL (DM or mention asking) | none | history (READ), post/reply (OUTWARD) |
| 8 | Notion | Composio | pages | Document, project nodes; text on pages edited by self (bodies untrusted) | NONE | none | search/read (READ), create/append (WRITE_SELF) |
| 9 | GitHub | Composio | PRs, issues, review requests | Repo, Issue, COLLABORATES_ON, skills from repo languages | SIGNAL (review requested, assigned) | `work.reviews_waiting` | comment (OUTWARD), create issue (OUTWARD) |
| 10 | Todoist | Composio | tasks, projects | Task, project (self_authored) | SIGNAL (due) | `work.tasks_*` | add/complete (WRITE_SELF) |
| 11 | Linear | Composio | issues assigned or created | Issue, team members | SIGNAL (assigned, due) | `work.*` | create/update (OUTWARD: visible to the team) |
| 12 | Trello | Composio (OAuth1) | cards on member boards | board and card as Task | SIGNAL (assigned, due) | `work.tasks_*` | add/move card (OUTWARD if the board is shared) |
| 13 | Zoom | Composio | meetings, transcripts | Event, attendees; text on transcripts (rich, budgeted) | NONE | `calendar.meetings` (dedupe with calendar by time) | schedule (OUTWARD) |
| 14 | Calendly | Composio | bookings | Event, Person (invitee identifiers) | SIGNAL (new or cancelled booking) | none | share link (READ), cancel (DESTRUCTIVE) |
| 15 | Teams / OneDrive (microsoft bundle) | Composio | chats (mentions), files | as 7 and 4 | SIGNAL | none | message (OUTWARD), share (OUTWARD) |
| 16 | Splitwise | Composio | expenses, friends | Person, OWES {amount}, Transaction | SIGNAL (you owe, settle requested) | `money.spend` (own share) | add expense (OUTWARD: friends see it), settle (SPEND) |
| 17 | YouTube | Composio | subscriptions, likes, playlists | interests as topic nodes, `User -[INTERESTED_IN]->` (self_authored) | NONE | none | playlist add (WRITE_SELF) |
| 18 | Google Classroom | Composio | courses, coursework | Course, ENROLLED_IN, Task with due | SIGNAL (due in 48h) | `work.tasks_*` | list (READ) |
| 19 | Strava | Direct OAuth | activities (webhook) | Activity, routine, Place | NONE | `fitness.*` | none (read only) |
| 20 | Google Health API | Direct OAuth (Google, health scopes) | daily summaries, sleep, workouts (poll) | routine nodes only; MEASUREMENT goes to metrics | NONE | `fitness.*`, `sleep.*`, `hr.resting` | none |
| 21 | Zomato (Swiggy when invited) | Remote MCP | order history (if a read tool exists) | Order, ORDERED_FROM Merchant, Place, favourites | NONE | `money.food_orders` | search (READ), reorder (SPEND), Dineout book (SPEND) |
| 22 | Apple Health | Archive (`export.zip` sent in chat) | parsed workouts and daily aggregates | same as 20 | NONE | as 20 | none |
| 23 | LinkedIn | Composio `linkedin` (live: Sign In with LinkedIn using OpenID Connect, plus Share on LinkedIn `w_member_social`) **and** Archive (one-time import of the data export) | live: the user's own profile basics (name, email, picture), self_authored. Archive: Person nodes for connections (company, position, connected-on), Org and positions (`WORKED_AT`), schools (`STUDIED_AT`), skills, message threads as third-party text | NONE | none | create post, text or link (OUTWARD, always approval, preview shows the exact text and visibility); delete own post (DESTRUCTIVE) |
| 24 | Instagram (Business/Creator) | Direct OAuth: Instagram API with Instagram Login, needs Meta app review for `instagram_business_basic`, `instagram_business_content_publish`, `instagram_business_manage_messages`, `instagram_business_manage_comments` and insights | media (MEDIA), comments, DMs to the professional account (MESSAGE, webhook), insights | own media and captions (self_authored), audience people from comments and DMs (third_party); insights go to metrics, not graph | SIGNAL (DM from a new person, comment asking a question) | `ig.reach`, `ig.followers`, `ig.engagement` | reply to DM or comment (OUTWARD), publish media (OUTWARD), hide or delete comment (DESTRUCTIVE) |

Personal Instagram accounts get the Instagram archive (section 3.4) instead. The connect flow checks the
account type first and says so plainly: "Instagram only lets apps connect to Business or Creator accounts.
For a personal account, send me your Instagram data export instead." DMs over the API follow Meta's
messaging rules: Mavis replies only inside the 24-hour window after the person's last message, and never
starts a conversation.

**LinkedIn limits.** The consumer API offers no feed, connections or messages, and the Mavis copy says so
when asked: "LinkedIn doesn't let apps read your connections or messages. Send me your LinkedIn data export
and I'll learn them from that." The live connection exists for sign-in identity and posting. The export is
offered right after connecting, and again whenever a LinkedIn question cannot be answered.

**Apple Health and the other archives** use the archive provider (section 3.4). The Apple Health parser
streams the XML, because exports can exceed 1 GB, keeps daily aggregates and workouts only, and enforces a
500 MB compressed cap.

**WhatsApp chat exports** hold other people's messages. Mavis imports only the chat the user sends,
treats every line not written by the user as third-party, keeps no media, and applies the shorter
`Sensitivity.MESSAGING` raw retention (7 days for bodies). Like every archive, it creates no live
ledger items on its own.

**Email extractors** (the India hub): a stream transform on Gmail and Outlook records, declared as
`derive=(EmailExtractor(...),)`. It is not a connector. Order, ticket, bank or UPI alert, and bill emails
produce **derived records**:

- the connector is still `gmail`;
- the kind is TRANSACTION, ORDER, TRIP or BOOKING;
- `parent_external_id` is the message id;
- the subject key is `gmail:<id>`.

Extraction order:

1. **schema.org JSON-LD and microdata** when present (Order, FlightReservation, TrainReservation,
   EventReservation). This is deterministic.
2. Otherwise **the attention `understand` output already computed** (kind, money, deadline). This costs no
   extra call.
3. Otherwise **generic field patterns**: amount, PNR, order id, date. These are generic, never per
   merchant, following the owner's rule.

Derived records are purged with Gmail.

## 11. Multi-user, rollout and configuration

- **Per-user everything.** Tokens, cursors, records, metrics and budgets are keyed by `user_id`. Composio
  uses the user's `composio_user_id`, the environment-prefixed identity from the multi-user spec (section
  6.2), and no longer a bare `mavis-<id>`. Direct OAuth `state` binds `user_id`, and webhooks map to the user through the token
  row. Neo4j and Qdrant are already filtered by `user_id`, and every new query includes it. A test
  enforces this by scanning every Cypher string for `user_id:$u`.
- **Invite cohorts (Track 5).** A spec's `status` and a config `CONNECTORS_ENABLED` (per cohort, once Track
  5 adds cohorts to invite codes) decide visibility. Until Track 5 exists, `BETA` specs are visible to users
  on an allowlist in config. Per-cohort budgets come from config. Connector LLM budgets (section 4.5) scale with the user `tier`
  (`owner`, `standard`, `trusted`) from the multi-user spec, and count toward that user's daily LLM cap.
  New pollers follow the multi-user spec's adaptive polling (section 6.3). Account deletion revokes every
  `connector_tokens` row as part of the deletion cascade.
- **Shadow migration of existing sources.** The specs for Gmail, Calendar, Slack, Notion and Workspace run
  the new pipeline in **shadow** (`MAVIS_CONNECTORS_SHADOW=gmail,...`): records and graph writes go to a
  shadow namespace, and nothing is pinged. A comparison script checks attention verdicts and fact counts
  against the old path for a week of live-test traffic. Then the old builders, `EventType`s (aliased to
  `CONNECTOR_RECORD` during transition) and first-sync routines are deleted.
- **Sensitive scopes in the beta.** Google Health ships only after Google verifies its health scopes, and
  until then it stays `DISABLED`. Strava, the Apple Health archive and the email extractors carry fitness
  and money signals for the beta. Instagram and WhatsApp stay `DISABLED` until Meta app review and business
  verification pass.
- **Kill switch per connector.** Setting a spec to `DISABLED` (env override) stops jobs, hides it from menus
  and keeps the data.
- **Config:**
  - token KEK;
  - Strava, Google Health and Meta (Instagram) client ids;
  - MCP server URLs per spec;
  - global and per-user LLM budgets;
  - raw body retention;
  - the cohort allowlist.

## 11A. WhatsApp as a second channel (same brain)

WhatsApp here is a **channel**, not a data connector. Users chat with Mavis on a WhatsApp Business number
through the WhatsApp Cloud API, as they do on Telegram today. Mavis **cannot read the user's personal
WhatsApp chats**: the Cloud API delivers only messages sent to the business number. Personal chat history
reaches Mavis only through the chat export (section 3.4). Bot copy says this plainly when asked.

### 11A.1 Make the channel layer channel-agnostic

The `Channel` protocol is already the seam, but identity and delivery are Telegram-shaped (section 2).

- **Identity.** Add a table `channel_identities(id, user_id, channel, account_id, address, verified_at,
  is_primary, created_at)`, unique on (channel, account_id). `account_id` is the stable identity: the
  multi-user spec's `telegram_user_id`, or the WhatsApp wa_id. `address` is where to deliver: the Telegram
  chat id, or the E.164 number. The multi-user spec keeps those columns on `users` for now, and this table
  generalises them. `users.telegram_chat_id` and `telegram_user_id` are migrated into it and kept as a read-only compatibility
  column until callers move. A user has one brain: memory, ledger, attention, connectors and budgets stay
  keyed by `user_id`, whatever channel a message arrives on.
- **Protocol.** `chat_id: int` becomes `address: str`. Capabilities differ per channel, so a channel
  declares `ChannelCaps(max_text, buttons_max, button_text_max, supports_reactions, supports_typing,
  supports_documents, session_window_h)`. Telegram reports 4096 chars, inline keyboards, reactions and no
  window. WhatsApp reports 4096 chars, at most 3 reply buttons or a 10-row list, reactions, and a 24h
  customer service window.
- **Rendering.** `channels/formatting.py` gains a per-channel renderer. Telegram keeps its Markdown
  handling. WhatsApp gets its own bold and italic syntax and drops unsupported markup. Buttons go through
  one `ButtonLayout` adapter: more than 3 options become a list message, and more than 10 become numbered
  text replies that map back to button data. Button `data` stays server-side, keyed by a short token, so
  the same approval and connect callbacks work on both channels.
- **Outbox.** `outbox` gains `channel` and `address` columns. The default is the channel the user last
  wrote from, or else the primary. `OutboxSender` dispatches to the channel adapter, and the callers in
  `worker/runner.py` and `agents/conversation.py` ask a `ChannelRouter` for the user's reply target
  instead of reading `telegram_chat_id`. Presence (typing, react) follows the inbound message's channel.
- **Inbound.** A WhatsApp webhook route `/webhooks/whatsapp` verifies `X-Hub-Signature-256` with the app
  secret, answers the `hub.challenge` handshake, dedupes on the WhatsApp message id in `processed_events`,
  and emits the same `USER_MESSAGE` / `BUTTON_PRESSED` events with `source="whatsapp"`. Media goes through
  `download_file` (Graph API media URL) into the per-user quarantine, which is how archives arrive on
  WhatsApp.

### 11A.2 WhatsApp rules that change behaviour

- **24-hour window.** Free-form messages are allowed only within 24h of the user's last inbound message.
  Proactive pings outside the window must use a **pre-approved template** ("You have an update from Mavis.
  Reply to see it.") and the content follows when the user replies. The attention policy
  (`attention/policy.py`) asks `ChannelRouter.can_send_freeform(user)`. When it cannot, the ping is sent on
  Telegram if the user has it, or else as a template, or else held for the next brief. Template sends cost
  money per conversation, so they count against a per-user daily template budget (default 2).
- **Opt-in.** A user links WhatsApp from Telegram ("/link whatsapp" gives a one-time code to send to the
  business number), or arrives through an invite code (Track 5) that starts on WhatsApp. An unknown number
  that writes without a valid invite gets a fixed reply and nothing else: no LLM call and no user row.
- **Account linking** never merges two existing users automatically. A code proves control of both
  addresses, and the second address is attached to the first user.
- **Approvals** work on both channels, through the shared button tokens. An approval is valid on whichever
  channel it is pressed, and only once (existing pending-approval state).
- **Security notices and OTP-like content** are never put into templates.

### 11A.3 Config

WhatsApp phone number id, WABA id, app secret, permanent system user token (in the KEK-encrypted secret
store), template names, and the per-user template budget. Meta app review covers both Instagram (section
10) and WhatsApp, so the owner's single Meta app holds both products.

## 12. Error handling

| Failure | Behaviour |
|---|---|
| Auth (401, `invalid_grant`, revoked) | Existing `failures.classify`. The stream is paused, the connection marked FAILED, and `prompt_reconnect` sends one nudge per day at most. The cursor is kept, so a reconnect resumes where it stopped. After 14 days FAILED, the user is asked once whether to disconnect and purge. |
| Rate limited (429, provider quota) | `paused_until` from Retry-After or exponential backoff (1m to 1h). The cursor is unchanged. |
| Provider outage or 5xx | Retry 3 times with jitter, then pause 15 min. Health metric per connector. |
| Webhook missing or unhealthy | `webhook_healthy_at` is older than 2x the expected interval, so polling turns on, and the daily reconcile catches up. |
| Schema drift (mapper raises, fields invalid) | The record is quarantined (`invalid`), the stream continues, and an alert fires when more than 5% of a page is invalid. A spec version bump replays the invalid rows. |
| Partial page or crash | The cursor did not advance, so the page replays and dedupe absorbs it. |
| LLM busy or over budget | The record stays `pending_extract`. Deterministic facts are already in. |
| Neo4j or Qdrant down | The ingest job retries with backoff and the record stays `new`. Recall degrades as today (`_safe`). |
| Purge partially fails | The job resumes from the failed step. `/connections` shows "forgetting…" until it is done. A disconnect still revokes upstream first, so no new data arrives. |
| MCP server lists new or changed tools | Ignored unless named in the spec. A changed input schema for a named tool disables that action, with an alert. |
| Prompt injection in record text | It is always wrapped untrusted. Extraction can produce entities and relations only. A third-party fact taints the run. Actions after taint require approval. |

The user only ever sees the existing user-facing error vocabulary (`domain/errors.py`). Provider text never
reaches the user.

## 13. Testing

- **Spec contract tests,** parametrised over the registry. Each spec loads, its prefixes are unique, every
  write has a risk, and every fixture in `tests/fixtures/connectors/<id>/*.json` maps to a Record that
  matches a golden `*.record.json`.
- **Graph golden tests.** A Record plus the spec's rules give an expected list of graph ops (labels,
  relations, provenance, trust), checked against `SqliteGraphStore`. The same ops run against Neo4j in the
  integration suite.
- **Provenance tests:**
  - one fact from chat and Gmail, after Gmail is purged, survives with origin `chat` and trust `user`;
  - a fact from Gmail only is retracted;
  - updating a record drops the edges it stopped producing.
- **Trust tests:** a third-party graph fact in recall is wrapped and marks the run tainted, while a
  self_authored fact does not. This is the regression test for the section 2 hole.
- **Purge residue test:** connect a fake connector, ingest, disconnect, then assert zero rows or points for
  that origin in Postgres, Neo4j, Qdrant, metrics, ledger and attention. Tombstones block a replayed webhook.
- **Pipeline tests,** with a fake provider and a fake clock: backfill pagination with budget stop and
  resume, cursor-after-commit, duplicate webhooks, deletes, rate-limit pauses, and the switch from webhook
  to poll.
- **Budget tests:** extraction never exceeds the per-user daily calls, and the best_effort lane yields to
  chat (reusing the 59715fe tests).
- **Injection suite:** records whose bodies tell Mavis to send, share or order produce no action without
  approval, and no loops.
- **Adapter tests:**
  - DirectOAuth: state forgery, refresh single-flight, revoke;
  - RemoteMcp: unlisted tool hidden, vendor description not used, schema change disables the action.
- **Archive tests:**
  - detection picks the right spec from fixture listings, and asks when confidence is low;
  - zip-slip, symlink, oversize and high-ratio archives are rejected before parsing;
  - re-importing the same export changes nothing, and a newer export only adds;
  - the uploaded file is gone after parsing;
  - in a WhatsApp export, only the confirmed author's lines are self_authored.
- **Channel tests:**
  - the same conversation, approval and connect flows run against a fake Telegram and a fake WhatsApp
    channel, using one parametrised suite;
  - button layouts degrade to list and numbered replies;
  - outside the 24h window a ping falls back to Telegram, then a template, then the brief;
  - an unknown WhatsApp number gets no LLM call;
  - linking requires a code, and a bad signature is rejected.
- **Live checks:** `verify_composio.py` runs nightly over all Composio specs, and a `live_e2e.py` scenario
  covers connect Strava, see an activity, then forget.

## 14. Build order

Each step ships on its own behind flags, in a worktree, through SDD as usual.

1. **Close the graph taint hole and add provenance.** Add `sources`, `origins` and `trust` on nodes and
   edges, migrate the existing `source_ref`, make recall wrap and taint third-party graph facts, and add
   `retract()`. This has value even without new connectors.
2. **Records and registry.** Add `domain/records.py`, the `connector_records`, `connector_cursors` and
   `connector_tokens` migrations, `ConnectorSpec` plus the registry with validation, and `Capability` as a
   registry-backed type.
3. **SyncEngine:**
   - backfill, poll and webhook jobs, cursors, dedupe, rate buckets and budgets;
   - the `CONNECTOR_RECORD` event;
   - `list_records` on ComposioProvider.
4. **GraphMapper and resolver identifiers.** Specs for Gmail, Calendar and Contacts **in shadow**.
5. **Purge, `/learned`, `/forget`, purge on disconnect,** and `/connections` with counts and the learning
   toggle.
6. **Cut over the existing sources** (Gmail, Calendar, Slack, Notion, Workspace) after the shadow
   comparison. Delete the old builders and first syncs.
7. **Actions from specs.** Move the `_SPECS` entries into their connector specs and add SPEND handling.
8. **Composio connectors batch:** Tasks, Outlook bundle, GitHub, Todoist, Linear, Trello, Zoom, Calendly,
   Teams/OneDrive, Splitwise, YouTube, Classroom. Each is a spec, a mapper and fixtures.
9. **`connector_metrics` and the `signals` API** for Programs.
10. **DirectOAuthProvider,** then Strava and Google Health. Google Health needs its own Google app
    verification for health scopes, so start that early.
11. **Email extractors** (JSON-LD, then understand reuse, then generic patterns).
12. **RemoteMcpProvider,** then Zomato.
13. **ArchiveImportProvider** (detect, quarantine, safe unzip, parsers), then the LinkedIn export, Apple
    Health, WhatsApp chat export, Instagram export and Google Takeout, in that order.
14. **LinkedIn live** (Composio: OpenID profile plus posting), shipped together with its export offer.
15. **Channel-agnostic layer:** `channel_identities`, address-based protocol, `ChannelCaps`, per-channel
    renderer, outbox channel column and `ChannelRouter`. Telegram is the only adapter at this step, and the
    existing tests must pass unchanged.
16. **WhatsApp channel:** webhook, linking codes, 24h window and templates, after the Meta app and business
    verification are done.
17. **Instagram (Business/Creator)** via Direct OAuth, after Meta app review grants the permissions.

Steps 15 to 17 depend on the owner's Meta app keys and reviews, so start the review requests at step 1.
Step 15 has no connector dependency and can run in parallel with steps 2 to 8.

Steps 1 to 6 are the platform. After step 8, a new Composio connector costs one spec file, one mapper and
its fixtures, which is G1.

## 15. Open questions (owner)

1. **Self-authored trust.** Should facts from the user's own records (their contacts, events they
   organised, their own Strava and health data) count as trusted, so they neither taint the run nor force
   approval on later writes? The proposed default is yes for typed fields and titles, and never for bodies.
   Choosing no is safer, but nearly every turn that recalls people would then require approval for writes.
2. **Purge on disconnect: always, or ask?** The spec purges on every disconnect, which matches DPDP and the
   research. The alternative asks "keep what I learned?" with a default of forget, which preserves context
   for users who disconnect only to reconnect a different account.
3. **WhatsApp as primary or secondary channel.** Can a new user start on WhatsApp alone (invite code sent
   to the business number), or must they always link it from Telegram first? WhatsApp-only users mean
   proactive pings outside the 24h window rely on paid templates, which affects cost and the ping budget.
   The spec allows both, with a 2-template daily cap.

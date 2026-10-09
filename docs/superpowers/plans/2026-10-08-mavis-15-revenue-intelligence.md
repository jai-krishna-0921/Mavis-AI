# Mavis Phase 15: Revenue Intelligence and Organisations (Track 6) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Read `docs/superpowers/plans/2026-10-02-mavis-00-index.md` (shared contracts), the spec, the owner decisions (items 14 to 18) and the "Start gates" table below before picking up any task: some tasks wait for sibling branches.

**Goal:** Mavis AI serves the owner's own company as one organisation: reps and managers work in an org space next to their personal one, every read and write passes one `authorize()` (roles, scopes, approval policy, hash-chained audit, Postgres RLS), HubSpot deals flow into a SQL mirror, a Fathom recording becomes a cited recap and an approval-gated CRM update, deal risk, pipeline and stage-weighted forecast numbers are computed by code (never generated), alerts ride the existing ledger and ping slots, and all of it ships behind flags with evals for number integrity, citations and permission leaks.

**Architecture:** A new `mavis.revintel` package. The seam layer (domain types, the permission matrix, `authorize`/`scope_filter`, the audit chain, approval policy resolution, principals and spaces, the tool `action` hook, org tables with RLS) depends only on main and starts first. The data layer (`rev_*` projection tables, metrics, forecast, risk rules, narrator) is pure SQL and Python over those tables. Connector-dependent work (HubSpot spec, Fathom webhook, org-bound Google sync) is written as ordinary Track 3 connector specs plus a projection job, and waits for the connectors branch. Proactive behaviour reuses the ledger (subject keys, closers) and Programs (system wakeups, ping slots).

**Tech Stack:** Python 3.13, uv, pydantic 2, SQLAlchemy 2 async, Alembic, Postgres 16 (RLS, advisory locks; SQLite in unit tests), redis-py asyncio, pytest + pytest-asyncio (asyncio_mode=auto), fakeredis.

**Spec:** `docs/superpowers/specs/2026-10-08-mavis-revenue-intelligence-design.md`; owner decisions `docs/superpowers/specs/2026-10-08-owner-decisions.md` items 14 to 18.

## Global Constraints

- **Owner decisions 14 to 18 are binding.** 14: primary CRM is HubSpot (use a HubSpot developer test account until the owner supplies a sandbox portal). 15: Mavis AI serves the owner's own company first, as ONE org; external customer orgs are later (no self-serve org creation, no DPA work in this plan). 16: Teams and Outlook stay `DISABLED` until the owner confirms tenant-admin consent (stubs only). 17: the owner is org owner; teammates join by org invite with roles admin, manager, rep, viewer; managers see team-level summaries and risk flags; full transcripts only for meetings they attended or that the rep shares. 18: market data is a later phase, tracking/alerts/scenario analysis only, no predictions or advice (a stub and a refusal test only here).
- **Flags revert everything.** `ORGS_ENABLED=false` (default) and per-org `features` JSON (`recap`, `risk`, `forecast`, `nba`, `alerts`, `writes`, all false by default). With the defaults every existing test passes unchanged; each task that touches a shared path names its defaults-unchanged test.
- **Default deny, one function.** Every tool and every org data read goes through `authorize`/`scope_filter`; org and user ids never appear in tool argument models; resource ids from a model are loaded through scoped repositories.
- **General mechanisms only.** Roles' powers, approval modes, risk weights, thresholds, stage meaning (from CRM won/lost flags and `rev_stage_config`, never stage names), buying roles, retention and budgets are rows or settings. No vendor stage names, no per-customer branches. Tests use at least three different names, orgs, currencies and stage labels per rule (for example orgs Acme / Globex / Initech, people Priya / Tomas / Aiko, currencies INR / EUR / USD, stage labels "Discovery" / "Qualified" / "Etapa 2").
- **Numbers are computed.** Money is integer minor units plus an ISO currency. Any figure shown to a user comes from a named SQL function with a figure id; narrated prose uses `{{fig:...}}` placeholders; no silent currency conversion.
- **Copy rule.** No em dashes or en dashes in any user-facing string, bot copy, prompt, tool description or doc. Product name is Mavis AI.
- **Third-party limits are settings.** HubSpot / Fathom / Gong rate and quota figures in the spec are "verify" values: they live in `Settings` and in the spec's `RateBudget`, never as constants.
- **Tests never hit the network, a real LLM, Telegram, Composio or HubSpot:** SQLite per test (`db` fixture), `FakeLLM`, `FakeChannel`, `fakeredis`. Tests that need Postgres (RLS, advisory locks, append-only grants) are marked `@pytest.mark.pg` and skip unless `TEST_PG_URL` is set (Task 11 adds the marker and fixture).
- **Secrets.** Never print or log tokens, webhook secrets or invite plaintexts. Audit `detail` holds counts and ids only, never content.
- **Migrations.** Track 6 revisions chain after the multi-user revision (`0022_multiuser_access`) at execution time. Revision ids here are unnumbered (`revintel_org_core`, ...) so the plan survives renumbering; the file name carries the number: Task 4 Step 1 reads the head and names the file `<NN>_revintel_org_core.py`; later migrations take `NN+1`, ... and `down_revision` is the previous Track 6 revision id. Every ORM table ships with its revision in the same task (`tests/store/test_migrations.py::test_migrations_match_models` guards it). Re-run `test_single_migration_head` right before merge.
- **Commits:** conventional commits, one per task, no `Co-Authored-By` or any AI trailer. Full suite (`uv run pytest -q`) and `uv run ruff check src tests scripts` pass at the end of every task.

## Review Focus

1. **A manager reads a transcript of a call they did not attend** (owner decision 17), or an admin/owner reads one at all. Expected: "I can't find that meeting in your view"; attendance or an explicit share is the only way in. Owners: Task 3 `test_manager_transcript_needs_attendance_or_share`, Task 31 `test_manager_cannot_open_unattended_transcript`, Task 52 leak matrix.
2. **A rep learns about another rep's deal through an aggregate, a contact join or a prompt.** Expected: pipeline sums, contact lists and LLM prompts hold only rows inside the principal's `RowFilter`. Owners: Task 36 `test_aggregates_use_the_row_filter`, Task 52 `test_prompts_hold_no_canary_outside_scope`.
3. **A removed or demoted member, or a stale `authz_version`, executes a previously approved action.** Expected: the action is cancelled at execution with an audit row. Owners: Task 13 `test_role_change_cancels_pending_execution`.
4. **A duplicate, replayed or forged Fathom webhook.** Expected: bad signature rejected before any parse, replay window enforced, the same recording id creates one meeting and one proposal, and a retried approval posts one CRM note. Owners: Task 28 `test_forged_and_stale_signatures_rejected`, Task 30 `test_duplicate_recording_creates_one_proposal`, Task 32 `test_retry_posts_one_note`.
5. **Ambiguity is guessed instead of asked:** two open deals match a meeting, a CRM owner matches no member, a currency has no FX rate. Expected: the card asks, the deal is `owner_unmapped`, totals split per currency. Owners: Task 29 `test_ambiguous_match_asks`, Task 22 `test_unmapped_owner_is_org_visible_only`, Task 37 `test_missing_fx_rate_splits_per_currency`.

## Deviations from spec

1. **Seed matrix follows owner decision 17.** `transcript.read` for manager is `own` where "own" includes meetings the manager attended or that were shared (spec said `team`); `meeting.read` (summary, action items) stays `team`. Added actions: `risk.read` (owner org, admin org, manager team, rep own, viewer team) and `approval.decide` (owner org, admin org, manager team).
2. **`authorize` answers permission only; `decide()` adds approval.** The spec's `Decision.effect="require_approval"` is produced by `revintel.decision.decide()` which composes `authorize` with `approval.resolve` (Task 7). Tools call `decide`.
3. **`scope_filter(principal, action, *, snapshot)`** has no `resource_type` argument: the matrix is keyed by action, and a second argument could only disagree with it.
4. **One org first.** Org creation is `mavis org create` (CLI) by the platform owner, not `/admin org create` in chat; there is no self-serve path. Invites reuse the multi-user table (Task 14, waits for plan 11).
5. **Org knowledge starts as `org_notes` in Postgres** (Task 15), not graph `ns` properties. The graph/vector namespace work is Task 25 and waits for the connectors graph tasks; recall for org turns never reads the personal graph from Task 15 on.
6. **Salesforce, Outlook, Teams, Gong, Zoom, Fireflies are `DISABLED` stub specs** (Task 55). Market data is a refusal stub (Task 55).
7. **Rate budgets** are a pure `RateBudget` plus an in-memory/fakeredis bucket in `revintel/ratebudget.py` (Task 20); the SyncEngine hook waits for connectors Task 18.
8. **Revision ids are unnumbered** (see Global Constraints).

## Start gates and parallel execution

Branches in flight: `multiuser` (plan 11; invite codes, per-user isolation, shared limiter, migration `0022_multiuser_access`), `connectors` (plan 14; spec format, provider port, Composio/direct OAuth/MCP adapters, ingestion pipeline, provenance/trust, sealed token store; migrations 0017 to 0021), `ledger` (commitments, 0015), `programs` (0016). All shared-file hunks are additive; `git fetch && git rebase main` before every task that lists "Shared".

| Tasks | Start | Needs |
|---|---|---|
| 1 to 13 (Phase A seams: flags, domain, matrix, authorize, org tables, audit, org repo, approval policy, principal and spaces, tool hook, chat switching, RLS, manager approvals) | **Immediately on main** | nothing external. Task 10 uses `agents/commands.py` as it is on main; after plan 11 merges it re-registers on the command table (note in the task). |
| 14 (org invites, `/join`, `/org` commands) | After plan 11 merges | `invite_codes`, `users.status`, command gate |
| 15 (org notes, LEARN routing by space) | Immediately | nothing |
| 16 to 19 (rev_* tables, stage config, owner/team resolution, `rev_access`, projection of normalised dicts) | **Immediately** | nothing: pure tables and functions over plain dicts |
| 20 (rate budget) | **Immediately** (pure) | integrates later with connectors Task 18 |
| 21 to 27 (DEAL/ACCOUNT/CALL kinds, `org_id` on connector tables, HubSpot mappers, spec, binding, sync hooks, projection job, org graph namespace) | After connectors Tasks 7, 8, 9, 10, 16 to 18 | `domain/records.py`, `connectors/spec.py`, repo, SyncEngine, rate buckets |
| 28 (Standard Webhooks verifier) | **Immediately** (pure) | |
| 29 (meeting-to-deal resolver), 30 to 33 (Fathom spec, recap pipeline, card, execution) | 29 immediately; 30 to 33 after connectors Tasks 16, 17, 23 and Phase B tasks 21 to 27 | |
| 34 (relevance gate) | **Immediately** (pure) | |
| 35 to 38 (org-bound Google binding, Calendar, Gmail, identity-set rebuild) | After plan 11 (`composio_user_id`) and connectors Task 25 (Google bundle) | |
| 39 to 45 (metrics, forecast, narrator, risk, stakeholder map, tools and commands) | **Immediately** after Tasks 16 to 19 and 9 | pure SQL/Python over `rev_*` tables |
| 46 (follow-up drafts) | After Task 36 and connectors Task 25 | Workspace draft action |
| 47 to 48 (NBA, alerts) | Immediately after Task 43 | |
| 49 (ledger subject keys, owner registry, closers) | After `ledger` merges | `CommitmentLedger.propose/close_subject`, `ledger.keys.PREFIXES` |
| 50 (system wakeups, ping slots, digests, morning slot) | After `programs` merges | `register_system_wakeup`, `PING_SLOTS`, slot contributors |
| 51 to 57 (evals, isolation/load, offboarding, rollout, stubs) | Evals 51 to 54 as soon as their subject exists; 55 to 57 last | |

## File Structure

```
src/mavis/revintel/
  __init__.py, mode.py             flags: orgs_on(), feature_on()
  domain.py                        Role, Scope, Principal, ResourceRef, RowFilter, Decision, ApprovalReq
  authz.py                         SEED_MATRIX, PolicySnapshot, authorize, scope_filter
  models.py                        ORM: orgs, teams, org_memberships, role_permissions, org_role_overrides,
                                   territory_rules, resource_shares, approval_policies, org_connections,
                                   org_audit_log, org_notes (Task 4, 5, 15)
  audit.py                         hash-chained append and verify
  approval.py                      condition language, resolve(), SEED_POLICIES
  decision.py                      decide(): authorize + approval
  principal.py                     resolve_principal, Space switching helpers
  spaces.py                        active space, switch, @prefix, nudge matcher
  tooling.py                       authorize hook for ToolRegistry, coverage helpers
  rls.py                           org_tx(principal)
  knowledge.py                     org notes repo, learn_target()
  commands.py                      /space, /org, /join, /audit
  repo/orgs.py                     orgs, teams, members, snapshots
  repo/rev.py                      scoped repositories for rev_* (AuthorizedScope)
  ratebudget.py, relevance.py, standard_webhooks.py, meetings.py
  projection/{normalise,stages,owners,access,job}.py
  metrics/{__init__,pipeline,forecast,figures}.py
  narrator.py, risk.py, stakeholders.py, nba.py, alerts.py, followup.py
  recap/{extract,validate,changeset,card,pipeline,execute}.py
  ledger_port.py, wakeups.py, eval/{synth,oracle,leaks}.py
src/mavis/connectors/specs/hubspot.py, fathom.py (+ disabled stubs: salesforce.py outlook.py teams.py gong.py)
src/mavis/migrations/versions/<NN>_revintel_*.py
tests/revintel/...   tests/connectors/revintel/...   tests/fixtures/revintel/{hubspot,fathom}/*.json
```

**Shared files this plan touches** (additive hunks only): `src/mavis/config.py`, `src/mavis/store/models.py` (one import line registering `revintel.models`), `src/mavis/tools/registry.py` (`MavisTool.action`, authorizer hook), `src/mavis/policy/approvals.py` (approver role, revalidation), `src/mavis/agents/commands.py` (`/space`, `/org`), `src/mavis/agents/buttons.py` (prefixes `sp:` and `ra:`), `src/mavis/tools/__init__.py` and `tools/chat_tools.py` (`switch_space`, `rev_*` tools), `src/mavis/cli.py` (`mavis org`), `src/mavis/domain/records.py` (new Kinds), `src/mavis/connectors/specs/__init__.py`, `src/mavis/connectors/spec.py` (`rate_budget` field), `src/mavis/store/repo/connectors.py` (`org_id`), `src/mavis/domain/wakeups.py`, `src/mavis/policy/pings.py` (`PING_SLOTS`), `src/mavis/ledger/keys.py` (`PREFIXES`), `src/mavis/worker/handlers.py`, `docker-compose.prod.yml`, `tests/conftest.py`.

---

## Phase A: organisations, spaces, authorization, audit, approval policy

### Task 1: Flags, settings and the package skeleton

**Files:**
- Create: `src/mavis/revintel/__init__.py`, `src/mavis/revintel/mode.py`, `tests/revintel/__init__.py`, `tests/revintel/test_mode.py`
- Modify: `src/mavis/config.py` (new block before `# --- admin`), `tests/conftest.py` (`TEST_ENV` pin, `orgs_on` fixture)

**Interfaces:**
- Produces:
  - Settings: `orgs_enabled: bool = False`, `ri_body_retention_days: int = 30`, `ri_min_n: int = 20`, `ri_recap_min_words: int = 150`, `ri_match_margin: float = 0.25`, `org_alert_budget_per_day: int = 4`, `rev_work_cap_per_day: int = 15`, `org_slot_max_lines: int = 5`, `nba_per_day: int = 3`, `nba_suppress_days: int = 14`, `space_reset_hours: int = 8`, `audit_retention_days: int = 400`
  - `mavis.revintel.mode.orgs_on() -> bool`; `FEATURES: tuple[str, ...]`; `feature_on(features: Mapping[str, bool] | None, name: str) -> bool` (False unless `orgs_on()` and the org enables it; unknown name raises `KeyError`)
  - fixture `orgs_on` (sets `ORGS_ENABLED=true`, clears the settings cache)

- [ ] **Step 1: Write the failing test**

`tests/revintel/__init__.py` (empty) and `tests/revintel/test_mode.py`:
```python
"""Flags default to today's behaviour: orgs off, every feature off."""

from __future__ import annotations

import pytest

from mavis.revintel.mode import FEATURES, feature_on, orgs_on


def test_defaults_are_off(settings):
    assert settings.orgs_enabled is False and orgs_on() is False
    assert (settings.ri_min_n, settings.ri_body_retention_days, settings.nba_per_day) == (20, 30, 3)
    assert (settings.org_alert_budget_per_day, settings.space_reset_hours) == (4, 8)


def test_features_need_the_flag_and_the_org_switch(settings, orgs_on):
    assert feature_on({"recap": True}, "recap") is True
    assert feature_on({"recap": True}, "risk") is False
    assert feature_on(None, "recap") is False


def test_features_are_off_when_orgs_are_off(settings):
    assert all(feature_on({f: True for f in FEATURES}, f) is False for f in FEATURES)


def test_unknown_feature_is_a_bug(settings, orgs_on):
    with pytest.raises(KeyError):
        feature_on({"recap": True}, "teleport")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_mode.py -q`
Expected: FAIL (`ModuleNotFoundError: mavis.revintel`).

- [ ] **Step 3: Implement**

`src/mavis/config.py`, before `# --- admin`:
```python
    # --- revenue intelligence and organisations (Track 6) ---------------------------
    orgs_enabled: bool = False
    ri_body_retention_days: int = 30  # transcript and email bodies are nulled after this (org-settable 7..365)
    ri_min_n: int = 20  # fewest closed deals before a historical rate replaces a default
    ri_recap_min_words: int = 150  # shorter transcripts get no recap card
    ri_match_margin: float = 0.25  # score gap that lets a meeting pick one deal without asking
    org_alert_budget_per_day: int = 4  # per user, separate from ping_daily_budget
    rev_work_cap_per_day: int = 15  # promised recap and prep cards per user per day
    org_slot_max_lines: int = 5
    nba_per_day: int = 3
    nba_suppress_days: int = 14
    space_reset_hours: int = 8
    audit_retention_days: int = 400
```

`src/mavis/revintel/__init__.py`:
```python
"""Revenue intelligence and organisations (Track 6)."""
```

`src/mavis/revintel/mode.py`:
```python
"""Flags: ORGS_ENABLED gates the whole track; each org switches features on itself."""

from __future__ import annotations

from collections.abc import Mapping

from mavis.config import get_settings

FEATURES: tuple[str, ...] = ("recap", "risk", "forecast", "nba", "alerts", "writes")


def orgs_on() -> bool:
    return get_settings().orgs_enabled


def feature_on(features: Mapping[str, bool] | None, name: str) -> bool:
    if name not in FEATURES:
        raise KeyError(name)
    return bool(orgs_on() and features and features.get(name, False))
```

`tests/conftest.py`: add `"ORGS_ENABLED": "false",` to `TEST_ENV`, and append:
```python
@pytest.fixture
def orgs_on(settings, monkeypatch):
    """ORGS_ENABLED=true for one test (Settings is lru_cached, so the cache is cleared)."""
    from mavis.config import get_settings

    monkeypatch.setenv("ORGS_ENABLED", "true")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel/test_mode.py -q && uv run pytest -q`
Expected: PASS (defaults-unchanged: full suite green).

- [ ] **Step 5: Commit**

```bash
git add src/mavis/revintel src/mavis/config.py tests/conftest.py tests/revintel
git commit -m "feat(revintel): flags, settings and package skeleton"
```

---

### Task 2: Domain types (roles, scopes, principal, resource, row filter)

**Files:**
- Create: `src/mavis/revintel/domain.py`, `tests/revintel/test_domain.py`

**Interfaces:**
- Produces (exact):
  - `class Role(StrEnum)`: `OWNER="owner" ADMIN="admin" MANAGER="manager" REP="rep" VIEWER="viewer"`; `Role.rank` property (owner 4 .. viewer 0)
  - `class Scope(IntEnum)`: `NONE=0 OWN=1 TEAM=2 ORG=3`
  - `PERSONAL = "personal"`; `org_space(org_id: int) -> str` (`"org:7"`); `parse_space(space: str) -> int | None`
  - `@dataclass(frozen=True) class Principal`: `user_id: int`, `org_id: int | None = None`, `membership_id: int | None = None`, `role: Role | None = None`, `team_ids: frozenset[int] = frozenset()`, `authz_version: int = 0`, `actor: Literal["user","system"] = "user"`, `request_id: str = ""`; property `space`
  - `@dataclass(frozen=True) class ResourceRef`: `type: str`, `org_id: int`, `id: str | None = None`, `owner_user_id: int | None = None`, `team_id: int | None = None`, `shared_user_ids: frozenset[int] = frozenset()`, `shared_team_ids: frozenset[int] = frozenset()`, `attendee_user_ids: frozenset[int] = frozenset()`
  - `class FilterKind(StrEnum)`: `ALL TEAMS OWNER NONE`; `@dataclass(frozen=True) class RowFilter(kind, user_id=None, team_ids=frozenset())` with `matches(resource) -> bool`
  - `@dataclass(frozen=True) class ApprovalReq(mode: str, approver_role: Role | None, expires_after_s: int, policy_id: int | None, reason: str)`
  - `@dataclass(frozen=True) class Decision(effect, reason, matched_rule=None, approval=None, max_scope=Scope.NONE)` with `allowed` (True for allow and require_approval)

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_domain.py`:
```python
from __future__ import annotations

import pytest

from mavis.revintel.domain import (
    PERSONAL, Decision, FilterKind, Principal, ResourceRef, Role, RowFilter, Scope, org_space, parse_space,
)


def test_space_strings_round_trip():
    assert org_space(7) == "org:7" and parse_space("org:7") == 7
    assert parse_space(PERSONAL) is None and parse_space("org:x") is None and parse_space("") is None


def test_principal_space():
    assert Principal(user_id=1).space == PERSONAL
    assert Principal(user_id=1, org_id=9, role=Role.REP).space == "org:9"


def test_role_rank_orders_roles():
    assert Role.OWNER.rank > Role.ADMIN.rank > Role.MANAGER.rank > Role.REP.rank > Role.VIEWER.rank


def res(**kw):
    return ResourceRef(type="deal", org_id=1, **kw)


@pytest.mark.parametrize(("flt", "resource", "expected"), [
    (RowFilter(FilterKind.ALL), res(owner_user_id=5), True),
    (RowFilter(FilterKind.NONE), res(owner_user_id=1), False),
    (RowFilter(FilterKind.OWNER, user_id=1), res(owner_user_id=1), True),
    (RowFilter(FilterKind.OWNER, user_id=1), res(owner_user_id=2), False),
    (RowFilter(FilterKind.OWNER, user_id=1), res(owner_user_id=2, shared_user_ids=frozenset({1})), True),
    (RowFilter(FilterKind.OWNER, user_id=1), res(owner_user_id=2, attendee_user_ids=frozenset({1})), True),
    (RowFilter(FilterKind.OWNER, user_id=1, team_ids=frozenset({4})),
     res(owner_user_id=2, shared_team_ids=frozenset({4})), True),
    (RowFilter(FilterKind.TEAMS, user_id=1, team_ids=frozenset({4, 5})), res(owner_user_id=2, team_id=5), True),
    (RowFilter(FilterKind.TEAMS, user_id=1, team_ids=frozenset({4})), res(owner_user_id=2, team_id=9), False),
    (RowFilter(FilterKind.TEAMS, user_id=1, team_ids=frozenset({4})), res(owner_user_id=1, team_id=None), True),
])
def test_row_filter_matches(flt, resource, expected):
    assert flt.matches(resource) is expected


def test_decision_allowed():
    assert Decision("allow", "ok").allowed and Decision("require_approval", "x").allowed
    assert not Decision("deny", "role_lacks_action").allowed
    assert Decision("deny", "x").max_scope is Scope.NONE
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_domain.py -q`
Expected: FAIL (`cannot import name`).

- [ ] **Step 3: Implement**

`src/mavis/revintel/domain.py`:
```python
"""Pure types for organisations and authorization. No I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Literal

PERSONAL = "personal"


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MANAGER = "manager"
    REP = "rep"
    VIEWER = "viewer"

    @property
    def rank(self) -> int:
        return {"owner": 4, "admin": 3, "manager": 2, "rep": 1, "viewer": 0}[self.value]


class Scope(IntEnum):
    NONE = 0
    OWN = 1
    TEAM = 2
    ORG = 3


def org_space(org_id: int) -> str:
    return f"org:{org_id}"


def parse_space(space: str) -> int | None:
    head, _, tail = (space or "").partition(":")
    return int(tail) if head == "org" and tail.isdigit() else None


@dataclass(frozen=True)
class Principal:
    user_id: int
    org_id: int | None = None
    membership_id: int | None = None
    role: Role | None = None
    team_ids: frozenset[int] = frozenset()
    authz_version: int = 0
    actor: Literal["user", "system"] = "user"
    request_id: str = ""

    @property
    def space(self) -> str:
        return org_space(self.org_id) if self.org_id is not None else PERSONAL


@dataclass(frozen=True)
class ResourceRef:
    type: str
    org_id: int
    id: str | None = None
    owner_user_id: int | None = None
    team_id: int | None = None
    shared_user_ids: frozenset[int] = field(default_factory=frozenset)
    shared_team_ids: frozenset[int] = field(default_factory=frozenset)
    attendee_user_ids: frozenset[int] = field(default_factory=frozenset)


class FilterKind(StrEnum):
    ALL = "all"
    TEAMS = "teams"
    OWNER = "owner"
    NONE = "none"


@dataclass(frozen=True)
class RowFilter:
    """What a principal may see for one action, in a form the repository layer turns into SQL."""

    kind: FilterKind
    user_id: int | None = None
    team_ids: frozenset[int] = frozenset()

    def _mine(self, r: ResourceRef) -> bool:
        uid = self.user_id
        return (uid is not None and (r.owner_user_id == uid or uid in r.shared_user_ids
                                     or uid in r.attendee_user_ids)) or bool(self.team_ids & r.shared_team_ids)

    def matches(self, r: ResourceRef) -> bool:
        if self.kind is FilterKind.ALL:
            return True
        if self.kind is FilterKind.NONE:
            return False
        if self.kind is FilterKind.OWNER:
            return self._mine(r)
        return (r.team_id is not None and r.team_id in self.team_ids) or self._mine(r)


@dataclass(frozen=True)
class ApprovalReq:
    mode: str  # auto | confirm | manager | admin | deny
    approver_role: Role | None
    expires_after_s: int
    policy_id: int | None
    reason: str


@dataclass(frozen=True)
class Decision:
    effect: Literal["allow", "deny", "require_approval"]
    reason: str
    matched_rule: int | None = None
    approval: ApprovalReq | None = None
    max_scope: Scope = Scope.NONE

    @property
    def allowed(self) -> bool:
        return self.effect != "deny"
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel/test_domain.py -q && uv run ruff check src/mavis/revintel tests/revintel`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis/revintel/domain.py tests/revintel/test_domain.py
git commit -m "feat(revintel): domain types for roles, scopes, principals and row filters"
```

---

### Task 3: The permission matrix, `authorize` and `scope_filter`

**Files:**
- Create: `src/mavis/revintel/authz.py`, `tests/revintel/test_authz.py`
- Test: property test inside the same file

**Interfaces:**
- Consumes: Task 2 types.
- Produces:
  - `SEED_MATRIX: dict[str, dict[Role, Scope]]` (action to role to widest scope; absent role means `NONE`)
  - `@dataclass(frozen=True) class PolicySnapshot(org_id: int, version: int, matrix: Mapping[tuple[str, str], Scope])` with `max_scope(role: Role, action: str) -> Scope` (exact action, then the longest `prefix.*` wildcard, else NONE)
  - `seed_snapshot(org_id: int = 0, version: int = 0, overrides: Mapping[tuple[str, str], Scope] | None = None) -> PolicySnapshot`
  - `SYSTEM_PREFIXES = ("sync.", "project.", "score.", "snapshot.")`
  - `authorize(principal: Principal, action: str, resource: ResourceRef | None = None, *, snapshot: PolicySnapshot) -> Decision`
  - `scope_filter(principal: Principal, action: str, *, snapshot: PolicySnapshot) -> RowFilter`
- Reason codes: `no_org`, `cross_org`, `system_scope`, `no_role`, `role_lacks_action`, `out_of_scope`, `ok`.

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_authz.py`:
```python
"""Spec 4: default deny; one evaluator; authorize and scope_filter agree."""

from __future__ import annotations

import random

import pytest

from mavis.revintel.authz import SEED_MATRIX, authorize, scope_filter, seed_snapshot
from mavis.revintel.domain import Principal, ResourceRef, Role, Scope

SNAP = seed_snapshot(org_id=1)


def p(role, uid=10, teams=(1,), org=1, **kw):
    return Principal(user_id=uid, org_id=org, membership_id=uid, role=role, team_ids=frozenset(teams), **kw)


def r(type_="deal", owner=10, team=1, org=1, **kw):
    return ResourceRef(type=type_, org_id=org, id="x", owner_user_id=owner, team_id=team, **kw)


@pytest.mark.parametrize(("role", "action", "resource", "effect", "reason"), [
    (Role.REP, "deal.read", r(owner=10), "allow", "ok"),
    (Role.REP, "deal.read", r(owner=11), "deny", "out_of_scope"),
    (Role.REP, "deal.read", r(owner=11, shared_user_ids=frozenset({10})), "allow", "ok"),
    (Role.MANAGER, "deal.read", r(owner=11, team=1), "allow", "ok"),
    (Role.MANAGER, "deal.read", r(owner=11, team=2), "deny", "out_of_scope"),
    (Role.ADMIN, "deal.read", r(owner=11, team=2), "allow", "ok"),
    (Role.VIEWER, "deal.write", r(owner=10), "deny", "role_lacks_action"),
    (Role.ADMIN, "deal.write", r(owner=10), "deny", "role_lacks_action"),
    (Role.REP, "nothing.here", r(), "deny", "role_lacks_action"),
])
def test_matrix_cases(role, action, resource, effect, reason):
    d = authorize(p(role), action, resource, snapshot=SNAP)
    assert (d.effect, d.reason) == (effect, reason)


def test_manager_transcript_needs_attendance_or_share():
    """Owner decision 17: full transcripts only for meetings attended or shared."""
    mgr = p(Role.MANAGER, uid=20)
    not_there = r("transcript", owner=11, team=1)
    assert authorize(mgr, "transcript.read", not_there, snapshot=SNAP).reason == "out_of_scope"
    there = r("transcript", owner=11, team=1, attendee_user_ids=frozenset({20}))
    assert authorize(mgr, "transcript.read", there, snapshot=SNAP).allowed
    shared = r("transcript", owner=11, team=1, shared_user_ids=frozenset({20}))
    assert authorize(mgr, "transcript.read", shared, snapshot=SNAP).allowed
    summary = r("meeting", owner=11, team=1)
    assert authorize(mgr, "meeting.read", summary, snapshot=SNAP).allowed  # team-level summaries stay open


@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN, Role.VIEWER])
def test_no_one_but_managers_and_reps_read_transcripts(role):
    d = authorize(p(role), "transcript.read", r("transcript", owner=10), snapshot=SNAP)
    assert (d.effect, d.reason) == ("deny", "role_lacks_action")


def test_cross_org_is_denied_before_anything_else():
    d = authorize(p(Role.OWNER), "deal.read", r(org=2), snapshot=SNAP)
    assert (d.effect, d.reason) == ("deny", "cross_org")


def test_personal_space_and_suspended_members_fail_closed():
    assert authorize(Principal(user_id=1), "deal.read", None, snapshot=SNAP).reason == "no_org"
    assert authorize(p(None), "deal.read", None, snapshot=SNAP).reason == "no_role"


def test_list_check_returns_the_widest_scope():
    d = authorize(p(Role.REP), "deal.read", None, snapshot=SNAP)
    assert d.allowed and d.max_scope is Scope.OWN
    assert not authorize(p(Role.VIEWER), "transcript.read", None, snapshot=SNAP).allowed


def test_system_principal_only_holds_job_actions():
    sysp = Principal(user_id=0, org_id=1, actor="system")
    assert authorize(sysp, "sync.run", None, snapshot=SNAP).allowed
    assert authorize(sysp, "project.deals", r(), snapshot=SNAP).allowed
    for action in ("deal.read", "email.send", "deal.write", "transcript.read"):
        assert authorize(sysp, action, r(), snapshot=SNAP).reason == "system_scope"


def test_overrides_and_wildcards():
    snap = seed_snapshot(org_id=1, version=2, overrides={("manager", "deal.*"): Scope.NONE,
                                                         ("rep", "forecast.read"): Scope.TEAM})
    assert authorize(p(Role.MANAGER), "deal.read", r(), snapshot=snap).reason == "role_lacks_action"
    assert snap.max_scope(Role.REP, "forecast.read") is Scope.TEAM
    assert SNAP.max_scope(Role.REP, "forecast.read") is Scope.OWN


def test_authorize_and_scope_filter_agree_on_random_worlds():
    rng = random.Random(7)
    actions = sorted(SEED_MATRIX)
    for _ in range(3000):
        role = rng.choice(list(Role))
        uid = rng.randint(1, 6)
        principal = p(role, uid=uid, teams=rng.sample([1, 2, 3], rng.randint(1, 2)))
        resource = ResourceRef(
            type="deal", org_id=1, id="d", owner_user_id=rng.choice([None, 1, 2, 3, 4, 5, 6]),
            team_id=rng.choice([None, 1, 2, 3, 4]),
            shared_user_ids=frozenset(rng.sample(range(1, 7), rng.randint(0, 2))),
            shared_team_ids=frozenset(rng.sample([1, 2, 3, 4], rng.randint(0, 1))),
            attendee_user_ids=frozenset(rng.sample(range(1, 7), rng.randint(0, 2))))
        action = rng.choice(actions)
        allowed = authorize(principal, action, resource, snapshot=SNAP).allowed
        assert allowed == scope_filter(principal, action, snapshot=SNAP).matches(resource), (role, action)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_authz.py -q`
Expected: FAIL (`No module named 'mavis.revintel.authz'`).

- [ ] **Step 3: Implement**

`src/mavis/revintel/authz.py`:
```python
"""The one authorization function (spec 4.3). Pure over (principal, action, resource, snapshot).

Default is deny. `scope_filter` shares the matrix with `authorize`, so a list query and a single check can
never disagree (tests/revintel/test_authz.py runs 3000 random worlds through both)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from mavis.revintel.domain import Decision, FilterKind, Principal, ResourceRef, Role, RowFilter, Scope

O, A, M, R, V = Role.OWNER, Role.ADMIN, Role.MANAGER, Role.REP, Role.VIEWER
ORG, TEAM, OWN = Scope.ORG, Scope.TEAM, Scope.OWN

SEED_MATRIX: dict[str, dict[Role, Scope]] = {
    "deal.read": {O: ORG, A: ORG, M: TEAM, R: OWN, V: TEAM},
    "account.read": {O: ORG, A: ORG, M: TEAM, R: OWN, V: TEAM},
    "contact.read": {O: ORG, A: ORG, M: TEAM, R: OWN, V: TEAM},
    "meeting.read": {O: ORG, A: ORG, M: TEAM, R: OWN},
    "transcript.read": {M: OWN, R: OWN},  # decision 17: managers only for meetings attended or shared
    "deal.write": {M: TEAM, R: OWN},
    "deal.bulk_write": {A: ORG, M: TEAM},
    "email.draft": {M: OWN, R: OWN},
    "email.send": {M: OWN, R: OWN},
    "forecast.read": {O: ORG, A: ORG, M: TEAM, R: OWN, V: TEAM},
    "pipeline.read": {O: ORG, A: ORG, M: TEAM, R: OWN, V: TEAM},
    "risk.read": {O: ORG, A: ORG, M: TEAM, R: OWN, V: TEAM},
    "report.team": {O: ORG, A: ORG, M: TEAM, V: TEAM},
    "alert.configure": {O: OWN, A: OWN, M: OWN, R: OWN},
    "alert.configure_org": {O: ORG, A: ORG},
    "member.manage": {O: ORG, A: ORG},
    "team.manage": {O: ORG, A: ORG},
    "connector.manage": {O: ORG, A: ORG},
    "connector.use_own": {O: OWN, A: OWN, M: OWN, R: OWN},
    "policy.manage": {O: ORG},
    "role.manage": {O: ORG},
    "audit.read": {O: ORG, A: ORG},
    "org.memory.read": {O: ORG, A: ORG, M: TEAM, R: OWN, V: TEAM},
    "org.memory.write": {O: OWN, A: OWN, M: OWN, R: OWN},
    "approval.decide": {O: ORG, A: ORG, M: TEAM},
}

SYSTEM_PREFIXES = ("sync.", "project.", "score.", "snapshot.")


@dataclass(frozen=True)
class PolicySnapshot:
    org_id: int
    version: int
    matrix: Mapping[tuple[str, str], Scope]  # (role, action or "prefix.*") -> widest scope

    def max_scope(self, role: Role, action: str) -> Scope:
        exact = self.matrix.get((role.value, action))
        if exact is not None:
            return exact
        parts = action.split(".")
        for i in range(len(parts) - 1, 0, -1):
            wild = self.matrix.get((role.value, ".".join(parts[:i]) + ".*"))
            if wild is not None:
                return wild
        return Scope.NONE


def seed_snapshot(org_id: int = 0, version: int = 0,
                  overrides: Mapping[tuple[str, str], Scope] | None = None) -> PolicySnapshot:
    matrix: dict[tuple[str, str], Scope] = {
        (role.value, action): scope for action, by_role in SEED_MATRIX.items() for role, scope in by_role.items()
    }
    matrix.update(overrides or {})
    return PolicySnapshot(org_id=org_id, version=version, matrix=matrix)


def _own(p: Principal, r: ResourceRef) -> bool:
    return (r.owner_user_id == p.user_id or p.user_id in r.shared_user_ids
            or p.user_id in r.attendee_user_ids or bool(p.team_ids & r.shared_team_ids))


def _in_scope(scope: Scope, p: Principal, r: ResourceRef) -> bool:
    if scope is Scope.ORG:
        return True
    if scope is Scope.TEAM:
        return (r.team_id is not None and r.team_id in p.team_ids) or _own(p, r)
    return scope is Scope.OWN and _own(p, r)


def _system(action: str) -> bool:
    return action.startswith(SYSTEM_PREFIXES)


def authorize(principal: Principal, action: str, resource: ResourceRef | None = None, *,
              snapshot: PolicySnapshot) -> Decision:
    if principal.org_id is None:
        return Decision("deny", "no_org")
    if resource is not None and resource.org_id != principal.org_id:
        return Decision("deny", "cross_org")
    if principal.actor == "system":
        return Decision("allow", "ok", max_scope=Scope.ORG) if _system(action) else Decision("deny", "system_scope")
    if principal.role is None:
        return Decision("deny", "no_role")
    scope = snapshot.max_scope(principal.role, action)
    if scope is Scope.NONE:
        return Decision("deny", "role_lacks_action")
    if resource is None:
        return Decision("allow", "ok", max_scope=scope)
    if not _in_scope(scope, principal, resource):
        return Decision("deny", "out_of_scope", max_scope=scope)
    return Decision("allow", "ok", max_scope=scope)


def scope_filter(principal: Principal, action: str, *, snapshot: PolicySnapshot) -> RowFilter:
    if principal.org_id is None:
        return RowFilter(FilterKind.NONE)
    if principal.actor == "system":
        return RowFilter(FilterKind.ALL) if _system(action) else RowFilter(FilterKind.NONE)
    if principal.role is None:
        return RowFilter(FilterKind.NONE)
    scope = snapshot.max_scope(principal.role, action)
    if scope is Scope.ORG:
        return RowFilter(FilterKind.ALL)
    if scope is Scope.TEAM:
        return RowFilter(FilterKind.TEAMS, principal.user_id, principal.team_ids)
    if scope is Scope.OWN:
        return RowFilter(FilterKind.OWNER, principal.user_id, principal.team_ids)
    return RowFilter(FilterKind.NONE)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel/test_authz.py -q && uv run ruff check src/mavis/revintel tests/revintel`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis/revintel/authz.py tests/revintel/test_authz.py
git commit -m "feat(revintel): permission matrix with authorize and scope_filter"
```

---

### Task 4: Org tables, ORM and the first migration

**Files:**
- Create: `src/mavis/revintel/models.py`, `src/mavis/migrations/versions/<NN>_revintel_org_core.py`, `tests/revintel/conftest.py`, `tests/revintel/test_models.py`, `tests/revintel/test_migration_chain.py`
- Modify: `src/mavis/store/models.py` (one import line at the end)

**Interfaces:**
- Produces ORM classes (table names): `Org` (`orgs`), `Team` (`teams`), `OrgMembership` (`org_memberships`), `RolePermission` (`role_permissions`), `OrgRoleOverride` (`org_role_overrides`), `TerritoryRule` (`territory_rules`), `ResourceShare` (`resource_shares`), `ApprovalPolicy` (`approval_policies`), `OrgConnection` (`org_connections`). Columns as in spec 3.2 plus `orgs.features JSON`, `orgs.authz_version int default 1`. Unique constraints are explicitly named (`uq_org_memberships_org_user`, ...).
- Revision id `revintel_org_core`.

- [ ] **Step 1: Read the head and write the failing tests**

Run: `uv run alembic heads` and `ls src/mavis/migrations/versions | sort | tail -3`. Note the head revision id (after plan 11 merges this is `0022_multiuser_access`) and its number `H`; this task's file is `<H+1>_revintel_org_core.py` with `down_revision = "<that head id>"`.

`tests/revintel/test_migration_chain.py`:
```python
from alembic.config import Config
from alembic.script import ScriptDirectory

from mavis.store.migrate import MIGRATIONS_DIR


def test_single_migration_head():
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    assert len(ScriptDirectory.from_config(cfg).get_heads()) == 1  # re-run (and renumber) before merge
```

`tests/revintel/test_models.py`:
```python
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from mavis.revintel.models import Org, OrgMembership, Team
from mavis.store.db import Session


async def test_org_defaults_and_membership_uniqueness(db, user):
    async with Session() as s:
        org = Org(name="Acme", slug="acme", base_currency="INR")
        s.add(org)
        await s.flush()
        assert (org.status, org.authz_version, org.features, org.internal_domains) == ("active", 1, {}, [])
        team = Team(org_id=org.id, name="East")
        s.add(team)
        await s.flush()
        s.add(OrgMembership(org_id=org.id, user_id=user.id, role="rep", team_id=team.id))
        await s.commit()
        s.add(OrgMembership(org_id=org.id, user_id=user.id, role="viewer"))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_slug_is_unique(db):
    async with Session() as s:
        s.add(Org(name="A", slug="same", base_currency="USD"))
        await s.commit()
        s.add(Org(name="B", slug="same", base_currency="USD"))
        with pytest.raises(IntegrityError):
            await s.commit()
```

`tests/revintel/conftest.py`:
```python
"""The shared org fixture: Acme with a hierarchy and one person per role."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mavis.revintel import authz
from mavis.revintel.domain import Role


@pytest.fixture(autouse=True)
def _fresh_snapshots():
    from mavis.revintel.repo import orgs

    orgs.clear_cache()
    yield
    orgs.clear_cache()


@pytest.fixture
async def world(db):
    """Acme: owner Olu, admin Ada, manager Mei (East), reps Raj and Rin (East), manager Wen and rep Sol
    (West), viewer Vic. Teams: Sales > East, West. Everyone is a membership; ids are in `w.u[name]`."""
    from mavis.revintel.repo import orgs
    from mavis.store.repo import users

    u = {}
    for i, name in enumerate(["Olu", "Ada", "Mei", "Raj", "Rin", "Wen", "Sol", "Vic"]):
        u[name], _ = await users.get_or_create_by_chat(9000 + i, name)
    org = await orgs.create_org("Acme", owner_user_id=u["Olu"].id, base_currency="USD",
                                internal_domains=["acme.test"])
    sales = await orgs.add_team(org.id, "Sales")
    east = await orgs.add_team(org.id, "East", parent_team_id=sales.id, manager_user_id=u["Mei"].id)
    west = await orgs.add_team(org.id, "West", parent_team_id=sales.id, manager_user_id=u["Wen"].id)
    for name, role, team in [("Ada", Role.ADMIN, sales), ("Mei", Role.MANAGER, east), ("Raj", Role.REP, east),
                             ("Rin", Role.REP, east), ("Wen", Role.MANAGER, west), ("Sol", Role.REP, west),
                             ("Vic", Role.VIEWER, sales)]:
        await orgs.add_member(org.id, u[name].id, role, team_id=team.id)
    return SimpleNamespace(org=org, u=u, teams=SimpleNamespace(sales=sales, east=east, west=west),
                           snapshot=authz.seed_snapshot(org.id))
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_models.py tests/revintel/test_migration_chain.py -q`
Expected: FAIL (`No module named 'mavis.revintel.models'`).

- [ ] **Step 3: Implement the models**

`src/mavis/revintel/models.py`:
```python
"""Org tables. Registered on the shared Base by one import at the end of store/models.py."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy import true as sa_true
from sqlalchemy.orm import Mapped, mapped_column

from mavis.store.db import Base, utcnow


class Org(Base):
    __tablename__ = "orgs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | suspended | deleting
    base_currency: Mapped[str] = mapped_column(String(3))
    fiscal_start_month: Mapped[int] = mapped_column(Integer, default=1)
    internal_domains: Mapped[list[str]] = mapped_column(JSON, default=list)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    features: Mapped[dict[str, bool]] = mapped_column(JSON, default=dict)
    authz_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), index=True)
    parent_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"))
    name: Mapped[str] = mapped_column(String(120))
    manager_user_id: Mapped[int | None] = mapped_column(Integer)
    attrs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class OrgMembership(Base):
    __tablename__ = "org_memberships"
    __table_args__ = (UniqueConstraint("org_id", "user_id", name="uq_org_memberships_org_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"))
    status: Mapped[str] = mapped_column(String(16), default="active")  # invited | active | suspended | removed
    crm_owner_refs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    joined_at: Mapped[datetime] = mapped_column(default=utcnow)
    removed_at: Mapped[datetime | None]


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role", "action_pattern", name="uq_role_permissions_role_action"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(String(16))
    action_pattern: Mapped[str] = mapped_column(String(64))
    max_scope: Mapped[str] = mapped_column(String(8))  # none | own | team | org


class OrgRoleOverride(Base):
    __tablename__ = "org_role_overrides"
    __table_args__ = (UniqueConstraint("org_id", "role", "action_pattern", name="uq_org_role_overrides_cell"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    action_pattern: Mapped[str] = mapped_column(String(64))
    max_scope: Mapped[str] = mapped_column(String(8))
    set_by: Mapped[int] = mapped_column(Integer)
    set_at: Mapped[datetime] = mapped_column(default=utcnow)


class TerritoryRule(Base):
    __tablename__ = "territory_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    attr: Mapped[str] = mapped_column(String(64))
    op: Mapped[str] = mapped_column(String(16))  # eq | in | prefix
    value: Mapped[str] = mapped_column(String(200))
    priority: Mapped[int] = mapped_column(Integer, default=0)


class ResourceShare(Base):
    __tablename__ = "resource_shares"
    __table_args__ = (Index("ix_resource_shares_lookup", "org_id", "resource_type", "resource_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"))
    resource_type: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[str] = mapped_column(String(64))
    grantee_user_id: Mapped[int | None] = mapped_column(Integer)
    grantee_team_id: Mapped[int | None] = mapped_column(Integer)
    level: Mapped[str] = mapped_column(String(8), default="read")
    expires_at: Mapped[datetime | None]
    granted_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ApprovalPolicy(Base):
    __tablename__ = "approval_policies"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("orgs.id"), index=True)  # NULL: platform default
    action_class: Mapped[str] = mapped_column(String(64))
    role: Mapped[str | None] = mapped_column(String(16))
    conditions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    mode: Mapped[str] = mapped_column(String(16))  # auto | confirm | manager | admin | deny
    approver_role: Mapped[str | None] = mapped_column(String(16))
    expires_after_s: Mapped[int] = mapped_column(Integer, default=86400)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(default=True, server_default=sa_true())
    created_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class OrgConnection(Base):
    __tablename__ = "org_connections"
    __table_args__ = (UniqueConstraint("org_id", "connector", "mode", name="uq_org_connections_binding"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), index=True)
    connector: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))  # service | per_user
    connected_by_user_id: Mapped[int | None] = mapped_column(Integer)
    composio_user_id: Mapped[str | None] = mapped_column(String(120))
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="active")

```

Append to the end of `src/mavis/store/models.py`:
```python
from mavis.revintel import models as _revintel_models  # noqa: E402,F401  (registers the org tables)
```

- [ ] **Step 4: Write the migration**

`src/mavis/migrations/versions/<NN>_revintel_org_core.py`:
```python
"""Track 6: organisations, teams, memberships, role matrix, overrides, territory, shares, approval
policies and org connections."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "revintel_org_core"
down_revision = "<head id from `alembic heads`>"
branch_labels = None
depends_on = None


def _ts(name: str = "created_at", nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "orgs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("base_currency", sa.String(3), nullable=False),
        sa.Column("fiscal_start_month", sa.Integer, nullable=False),
        sa.Column("internal_domains", sa.JSON, nullable=False),
        sa.Column("settings", sa.JSON, nullable=False),
        sa.Column("features", sa.JSON, nullable=False),
        sa.Column("authz_version", sa.Integer, nullable=False),
        _ts(),
        sa.UniqueConstraint("slug", name="uq_orgs_slug"),
    )
    op.create_table(
        "teams",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("parent_team_id", sa.Integer, sa.ForeignKey("teams.id"), nullable=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("manager_user_id", sa.Integer, nullable=True),
        sa.Column("attrs", sa.JSON, nullable=False),
    )
    op.create_index("ix_teams_org_id", "teams", ["org_id"])
    op.create_table(
        "org_memberships",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("team_id", sa.Integer, sa.ForeignKey("teams.id"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("crm_owner_refs", sa.JSON, nullable=False),
        _ts("joined_at"),
        _ts("removed_at", nullable=True),
        sa.UniqueConstraint("org_id", "user_id", name="uq_org_memberships_org_user"),
    )
    op.create_index("ix_org_memberships_org_id", "org_memberships", ["org_id"])
    op.create_index("ix_org_memberships_user_id", "org_memberships", ["user_id"])
    op.create_table(
        "role_permissions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("action_pattern", sa.String(64), nullable=False),
        sa.Column("max_scope", sa.String(8), nullable=False),
        sa.UniqueConstraint("role", "action_pattern", name="uq_role_permissions_role_action"),
    )
    op.create_table(
        "org_role_overrides",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("action_pattern", sa.String(64), nullable=False),
        sa.Column("max_scope", sa.String(8), nullable=False),
        sa.Column("set_by", sa.Integer, nullable=False),
        _ts("set_at"),
        sa.UniqueConstraint("org_id", "role", "action_pattern", name="uq_org_role_overrides_cell"),
    )
    op.create_index("ix_org_role_overrides_org_id", "org_role_overrides", ["org_id"])
    op.create_table(
        "territory_rules",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("team_id", sa.Integer, sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("attr", sa.String(64), nullable=False),
        sa.Column("op", sa.String(16), nullable=False),
        sa.Column("value", sa.String(200), nullable=False),
        sa.Column("priority", sa.Integer, nullable=False),
    )
    op.create_index("ix_territory_rules_org_id", "territory_rules", ["org_id"])
    op.create_table(
        "resource_shares",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=False),
        sa.Column("grantee_user_id", sa.Integer, nullable=True),
        sa.Column("grantee_team_id", sa.Integer, nullable=True),
        sa.Column("level", sa.String(8), nullable=False),
        _ts("expires_at", nullable=True),
        sa.Column("granted_by", sa.Integer, nullable=False),
        _ts(),
    )
    op.create_index("ix_resource_shares_lookup", "resource_shares", ["org_id", "resource_type", "resource_id"])
    op.create_table(
        "approval_policies",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=True),
        sa.Column("action_class", sa.String(64), nullable=False),
        sa.Column("role", sa.String(16), nullable=True),
        sa.Column("conditions", sa.JSON, nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("approver_role", sa.String(16), nullable=True),
        sa.Column("expires_after_s", sa.Integer, nullable=False),
        sa.Column("priority", sa.Integer, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Integer, nullable=True),
        _ts(),
    )
    op.create_index("ix_approval_policies_org_id", "approval_policies", ["org_id"])
    op.create_table(
        "org_connections",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("connector", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("connected_by_user_id", sa.Integer, nullable=True),
        sa.Column("composio_user_id", sa.String(120), nullable=True),
        sa.Column("scopes", sa.JSON, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.UniqueConstraint("org_id", "connector", "mode", name="uq_org_connections_binding"),
    )
    op.create_index("ix_org_connections_org_id", "org_connections", ["org_id"])


def downgrade() -> None:
    for table in ("org_connections", "approval_policies", "resource_shares", "territory_rules",
                  "org_role_overrides", "role_permissions", "org_memberships", "teams", "orgs"):
        op.drop_table(table)
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/revintel/test_models.py tests/revintel/test_migration_chain.py tests/store/test_migrations.py -q`
Expected: PASS (`test_migrations_match_models` sees the new tables and finds no diff). `test_models.py::test_org_defaults...` needs Task 6's `create_org` only through `world`, which it does not use here.

- [ ] **Step 6: Commit**

```bash
git add src/mavis/revintel/models.py src/mavis/store/models.py src/mavis/migrations/versions tests/revintel
git commit -m "feat(revintel): org, team, membership, role, policy tables and migration"
```

---

### Task 5: The hash-chained audit log

**Files:**
- Modify: `src/mavis/revintel/models.py` (add `OrgAuditLog`)
- Create: `src/mavis/revintel/audit.py`, `src/mavis/migrations/versions/<NN+1>_revintel_audit.py`, `tests/revintel/test_audit.py`

**Interfaces:**
- Consumes: Task 4 `Org`.
- Produces:
  - `OrgAuditLog` (`org_audit_log`): `id, org_id, seq (unique per org), ts, actor_user_id, on_behalf_of, role, space, channel, action, resource_type, resource_ids JSON, resource_count, args_hash, decision, reason, policy_rule_id, approver_user_id, approval_id, task_id, request_id, detail JSON, prev_hash, row_hash`
  - `audit.DECISIONS = ("allow","deny","approval_required","approved","rejected","expired","executed","failed","cancelled_revalidation")`
  - `async audit.append(org_id: int, *, action: str, decision: str, actor_user_id: int | None = None, on_behalf_of: int | None = None, role: str | None = None, space: str | None = None, channel: str = "telegram", resource_type: str | None = None, resource_ids: Sequence[str | int] = (), args: Mapping | None = None, reason: str = "", policy_rule_id: int | None = None, approver_user_id: int | None = None, approval_id: int | None = None, task_id: int | None = None, request_id: str = "", detail: Mapping | None = None, session: AsyncSession | None = None) -> OrgAuditLog` (with `session`, the caller commits; without, it commits its own)
  - `@dataclass ChainReport(ok: bool, checked: int, first_bad_seq: int | None)`; `async audit.verify_chain(org_id: int, *, after_seq: int = 0) -> ChainReport`
  - `audit.args_hash(args: Mapping | None) -> str | None` (sha256 of canonical JSON; args are never stored)
- Rules: `detail` may hold only ints, floats, bools, None, lists of those, and strings of at most 64 characters (else `ValueError("audit detail holds counts and ids only")`); at most 50 `resource_ids` are stored, `resource_count` always holds the true count.

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_audit.py`:
```python
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import update

from mavis.revintel import audit
from mavis.revintel.models import OrgAuditLog
from mavis.store.db import Session


async def put(org_id, n, **kw):
    return await audit.append(org_id, action="deal.read", decision="allow", actor_user_id=1, role="rep",
                              space=f"org:{org_id}", resource_type="deal", resource_ids=[f"d{n}"], **kw)


async def test_rows_chain_and_verify(world):
    rows = [await put(world.org.id, i) for i in range(4)]
    assert [r.seq for r in rows] == [1, 2, 3, 4]
    assert rows[0].prev_hash == "0" * 64 and rows[1].prev_hash == rows[0].row_hash
    rep = await audit.verify_chain(world.org.id)
    assert (rep.ok, rep.checked, rep.first_bad_seq) == (True, 5, None)  # +1: the org_create row from the fixture


async def test_tampering_is_detected_at_the_first_bad_row(world):
    for i in range(3):
        await put(world.org.id, i)
    async with Session() as s:
        await s.execute(update(OrgAuditLog).where(OrgAuditLog.org_id == world.org.id, OrgAuditLog.seq == 3)
                        .values(reason="edited"))
        await s.commit()
    rep = await audit.verify_chain(world.org.id)
    assert rep.ok is False and rep.first_bad_seq == 3


async def test_chains_are_per_org(world):
    from mavis.revintel.repo import orgs

    other = await orgs.create_org("Globex", owner_user_id=world.u["Ada"].id, base_currency="EUR")
    await put(other.id, 1)
    assert (await audit.verify_chain(other.id)).ok and (await audit.verify_chain(world.org.id)).ok
    assert (await put(other.id, 2)).seq == 3  # org_create + one + this


async def test_detail_holds_counts_and_ids_only(world):
    with pytest.raises(ValueError, match="counts and ids only"):
        await put(world.org.id, 1, detail={"note": "x" * 65})
    with pytest.raises(ValueError):
        await put(world.org.id, 1, detail={"blob": {"a": 1}})
    ok = await put(world.org.id, 1, detail={"fields": ["next_step"], "records": 1, "dry": False})
    assert ok.detail["records"] == 1


async def test_args_are_hashed_never_stored(world):
    row = await put(world.org.id, 1, args={"text": "secret plan", "deal": 5})
    assert row.args_hash == audit.args_hash({"deal": 5, "text": "secret plan"}) and len(row.args_hash) == 64
    assert "secret" not in repr(row.__dict__)


async def test_resource_ids_are_capped_with_a_true_count(world):
    row = await audit.append(world.org.id, action="deal.bulk_write", decision="executed",
                             resource_type="deal", resource_ids=[str(i) for i in range(120)])
    assert len(row.resource_ids) == 50 and row.resource_count == 120


async def test_concurrent_appends_get_unique_sequential_numbers(world):
    rows = await asyncio.gather(*[put(world.org.id, i) for i in range(20)])
    assert sorted(r.seq for r in rows) == list(range(2, 22))
    assert (await audit.verify_chain(world.org.id)).ok
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_audit.py -q`
Expected: FAIL (`No module named 'mavis.revintel.audit'` / `create_org` missing; Task 6 supplies `create_org`, so this test file is first run green at the end of Task 6, see Step 4).

- [ ] **Step 3: Implement**

Append to `src/mavis/revintel/models.py`:
```python
class OrgAuditLog(Base):
    """Append-only, one hash chain per org (spec 6)."""

    __tablename__ = "org_audit_log"
    __table_args__ = (UniqueConstraint("org_id", "seq", name="uq_org_audit_log_org_seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(Integer, index=True)
    seq: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(default=utcnow)
    actor_user_id: Mapped[int | None] = mapped_column(Integer)
    on_behalf_of: Mapped[int | None] = mapped_column(Integer)
    role: Mapped[str | None] = mapped_column(String(16))
    space: Mapped[str | None] = mapped_column(String(32))
    channel: Mapped[str] = mapped_column(String(16), default="telegram")
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str | None] = mapped_column(String(32))
    resource_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    resource_count: Mapped[int] = mapped_column(Integer, default=0)
    args_hash: Mapped[str | None] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(64), default="")
    policy_rule_id: Mapped[int | None] = mapped_column(Integer)
    approver_user_id: Mapped[int | None] = mapped_column(Integer)
    approval_id: Mapped[int | None] = mapped_column(Integer)
    task_id: Mapped[int | None] = mapped_column(Integer)
    request_id: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    row_hash: Mapped[str] = mapped_column(String(64))
```

`src/mavis/revintel/audit.py`:
```python
"""Append-only, hash-chained org audit log (spec 6). Content never enters it: counts and ids only."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mavis.revintel.models import OrgAuditLog
from mavis.store.db import Session, utcnow

DECISIONS = ("allow", "deny", "approval_required", "approved", "rejected", "expired", "executed", "failed",
             "cancelled_revalidation")
GENESIS = "0" * 64
MAX_IDS = 50
_locks: dict[int, asyncio.Lock] = {}  # in-process ordering; Postgres adds an advisory lock per org


def _canon(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def args_hash(args: Mapping | None) -> str | None:
    return None if args is None else hashlib.sha256(_canon(args)).hexdigest()


def _scalar_ok(v: Any) -> bool:
    return v is None or isinstance(v, bool | int | float) or (isinstance(v, str) and len(v) <= 64)


def _check_detail(detail: Mapping | None) -> dict:
    out = dict(detail or {})
    for v in out.values():
        if not (_scalar_ok(v) or (isinstance(v, list) and all(_scalar_ok(x) for x in v))):
            raise ValueError("audit detail holds counts and ids only")
    return out


def _payload(row: OrgAuditLog) -> dict:
    return {
        "org_id": row.org_id, "seq": row.seq, "ts": row.ts.isoformat(), "actor_user_id": row.actor_user_id,
        "on_behalf_of": row.on_behalf_of, "role": row.role, "space": row.space, "channel": row.channel,
        "action": row.action, "resource_type": row.resource_type, "resource_ids": row.resource_ids,
        "resource_count": row.resource_count, "args_hash": row.args_hash, "decision": row.decision,
        "reason": row.reason, "policy_rule_id": row.policy_rule_id, "approver_user_id": row.approver_user_id,
        "approval_id": row.approval_id, "task_id": row.task_id, "request_id": row.request_id,
        "detail": row.detail,
    }


def _hash(prev: str, row: OrgAuditLog) -> str:
    return hashlib.sha256(prev.encode() + _canon(_payload(row))).hexdigest()


async def append(org_id: int, *, action: str, decision: str, actor_user_id: int | None = None,
                 on_behalf_of: int | None = None, role: str | None = None, space: str | None = None,
                 channel: str = "telegram", resource_type: str | None = None,
                 resource_ids: Sequence[str | int] = (), args: Mapping | None = None, reason: str = "",
                 policy_rule_id: int | None = None, approver_user_id: int | None = None,
                 approval_id: int | None = None, task_id: int | None = None, request_id: str = "",
                 detail: Mapping | None = None, session: AsyncSession | None = None) -> OrgAuditLog:
    if decision not in DECISIONS:
        raise ValueError(f"unknown audit decision {decision!r}")
    clean = _check_detail(detail)
    lock = _locks.setdefault(org_id, asyncio.Lock())
    async with lock:
        own = session is None
        s = session or Session()
        try:
            if s.bind is not None and s.bind.dialect.name == "postgresql":
                await s.execute(text("SELECT pg_advisory_xact_lock(15, :k)"), {"k": org_id})
            last = await s.scalar(select(OrgAuditLog).where(OrgAuditLog.org_id == org_id)
                                  .order_by(OrgAuditLog.seq.desc()).limit(1))
            row = OrgAuditLog(
                org_id=org_id, seq=(last.seq + 1) if last else 1, ts=utcnow(), actor_user_id=actor_user_id,
                on_behalf_of=on_behalf_of, role=role, space=space, channel=channel, action=action,
                resource_type=resource_type, resource_ids=[str(i) for i in list(resource_ids)[:MAX_IDS]],
                resource_count=len(resource_ids), args_hash=args_hash(args), decision=decision, reason=reason,
                policy_rule_id=policy_rule_id, approver_user_id=approver_user_id, approval_id=approval_id,
                task_id=task_id, request_id=request_id, detail=clean,
                prev_hash=last.row_hash if last else GENESIS, row_hash="")
            row.row_hash = _hash(row.prev_hash, row)
            s.add(row)
            if own:
                await s.commit()
            else:
                await s.flush()
            return row
        finally:
            if own:
                await s.close()


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    checked: int
    first_bad_seq: int | None


async def verify_chain(org_id: int, *, after_seq: int = 0) -> ChainReport:
    async with Session() as s:
        rows = list(await s.scalars(select(OrgAuditLog).where(OrgAuditLog.org_id == org_id)
                                    .order_by(OrgAuditLog.seq)))
    prev = GENESIS
    checked = 0
    for row in rows:
        good = row.prev_hash == prev and row.row_hash == _hash(prev, row)
        if row.seq > after_seq:
            checked += 1
            if not good:
                return ChainReport(False, checked, row.seq)
        prev = row.row_hash
    return ChainReport(True, checked, None)
```

Migration `<NN+1>_revintel_audit.py` (`revision = "revintel_audit"`, `down_revision = "revintel_org_core"`):
```python
"""Track 6: the append-only org audit log (the Postgres trigger and grants arrive in revintel_rls)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "revintel_audit"
down_revision = "revintel_org_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_audit_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("org_id", sa.Integer, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_user_id", sa.Integer, nullable=True),
        sa.Column("on_behalf_of", sa.Integer, nullable=True),
        sa.Column("role", sa.String(16), nullable=True),
        sa.Column("space", sa.String(32), nullable=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=True),
        sa.Column("resource_ids", sa.JSON, nullable=False),
        sa.Column("resource_count", sa.Integer, nullable=False),
        sa.Column("args_hash", sa.String(64), nullable=True),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("policy_rule_id", sa.Integer, nullable=True),
        sa.Column("approver_user_id", sa.Integer, nullable=True),
        sa.Column("approval_id", sa.Integer, nullable=True),
        sa.Column("task_id", sa.Integer, nullable=True),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("detail", sa.JSON, nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("row_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("org_id", "seq", name="uq_org_audit_log_org_seq"),
    )
    op.create_index("ix_org_audit_log_org_id", "org_audit_log", ["org_id"])


def downgrade() -> None:
    op.drop_table("org_audit_log")
```

- [ ] **Step 4: Run to verify it passes**

This file's `world` fixture needs Task 6's repo; if executing strictly in order, run Step 4 after Task 6 Step 3, or use this stub check now: `uv run pytest tests/store/test_migrations.py -q` (PASS: model and migration agree). Then after Task 6: `uv run pytest tests/revintel/test_audit.py -q` (PASS).

- [ ] **Step 5: Commit**

```bash
git add src/mavis/revintel/models.py src/mavis/revintel/audit.py src/mavis/migrations/versions tests/revintel/test_audit.py
git commit -m "feat(revintel): hash-chained org audit log"
```

---

### Task 6: Org repository, policy snapshots and `mavis org` commands

**Files:**
- Create: `src/mavis/revintel/repo/__init__.py`, `src/mavis/revintel/repo/orgs.py`, `tests/revintel/test_orgs_repo.py`
- Modify: `src/mavis/cli.py` (a `org` sub-app, additive)

**Interfaces:**
- Consumes: Tasks 2 to 5.
- Produces (`mavis.revintel.repo.orgs`):
  - `async ensure_seed() -> int` (inserts missing `role_permissions` rows from `SEED_MATRIX`; returns how many)
  - `async create_org(name: str, *, owner_user_id: int, slug: str | None = None, base_currency: str = "USD", internal_domains: Sequence[str] = (), fiscal_start_month: int = 1) -> Org` (seeds, adds the owner membership, writes an audit row `member.change` with `detail={"op":"org_create"}`)
  - `async get_org(org_id) -> Org | None`, `async get_by_slug(slug) -> Org | None`
  - `async add_team(org_id, name, *, parent_team_id=None, manager_user_id=None, attrs=None) -> Team`
  - `async add_member(org_id, user_id, role: Role, *, team_id=None, crm_owner_refs=None, actor_user_id=None) -> OrgMembership` (re-activates a removed one; bumps `authz_version`; audits)
  - `async set_role(org_id, user_id, role, *, actor_user_id) -> None`; `async remove_member(org_id, user_id, *, actor_user_id) -> bool`; both refuse to leave the org without an owner (`ValueError("an org needs at least one owner")`)
  - `async membership(org_id, user_id) -> OrgMembership | None` (active only); `async memberships_of(user_id) -> list[OrgMembership]` (active, org active)
  - `async team_subtree_ids(org_id, team_id) -> frozenset[int]`
  - `async set_override(org_id, role: Role, action_pattern: str, scope: Scope, *, actor_user_id: int) -> None` (widening beyond the current cell requires the actor to be an owner: `PermissionError`; audits `policy.change`)
  - `async load_snapshot(org_id) -> PolicySnapshot` (merge of `SEED_MATRIX`, `role_permissions`, overrides; cached by `authz_version`); `clear_cache() -> None`
  - `async bump_version(org_id) -> int`; `async set_feature(org_id, name, on: bool) -> None` (validated against `mode.FEATURES`)
- CLI: `mavis org create NAME --owner-chat-id N [--currency USD] [--domain acme.test ...]`, `mavis org list`, `mavis org feature SLUG NAME on|off`.

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_orgs_repo.py`:
```python
from __future__ import annotations

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from mavis.cli import app
from mavis.revintel import audit
from mavis.revintel.authz import SEED_MATRIX
from mavis.revintel.domain import Role, Scope
from mavis.revintel.models import OrgAuditLog, RolePermission
from mavis.revintel.repo import orgs
from mavis.store.db import Session


async def test_create_org_seeds_the_matrix_and_the_owner(world):
    async with Session() as s:
        n = await s.scalar(select(func.count()).select_from(RolePermission))
    assert n == sum(len(v) for v in SEED_MATRIX.values())
    assert (await orgs.membership(world.org.id, world.u["Olu"].id)).role == "owner"
    assert await orgs.ensure_seed() == 0  # idempotent


async def test_slug_collision_and_default(db, user):
    a = await orgs.create_org("Initech Corp", owner_user_id=user.id)
    assert a.slug == "initech-corp"
    with pytest.raises(ValueError, match="slug"):
        await orgs.create_org("Initech Corp", owner_user_id=user.id)


async def test_team_subtree(world):
    east_a = await orgs.add_team(world.org.id, "East A", parent_team_id=world.teams.east.id)
    assert await orgs.team_subtree_ids(world.org.id, world.teams.sales.id) == frozenset(
        {world.teams.sales.id, world.teams.east.id, world.teams.west.id, east_a.id})
    assert await orgs.team_subtree_ids(world.org.id, world.teams.east.id) == frozenset(
        {world.teams.east.id, east_a.id})


async def test_membership_changes_bump_the_version_and_audit(world):
    v0 = (await orgs.get_org(world.org.id)).authz_version
    await orgs.set_role(world.org.id, world.u["Rin"].id, Role.MANAGER, actor_user_id=world.u["Olu"].id)
    assert (await orgs.get_org(world.org.id)).authz_version == v0 + 1
    assert await orgs.remove_member(world.org.id, world.u["Rin"].id, actor_user_id=world.u["Olu"].id)
    assert await orgs.membership(world.org.id, world.u["Rin"].id) is None
    assert [m.org_id for m in await orgs.memberships_of(world.u["Rin"].id)] == []
    async with Session() as s:
        n = await s.scalar(select(func.count()).select_from(OrgAuditLog).where(
            OrgAuditLog.org_id == world.org.id, OrgAuditLog.action == "member.change"))
    assert n >= 10 and (await audit.verify_chain(world.org.id)).ok


async def test_an_org_always_has_an_owner(world):
    with pytest.raises(ValueError, match="at least one owner"):
        await orgs.set_role(world.org.id, world.u["Olu"].id, Role.ADMIN, actor_user_id=world.u["Olu"].id)
    with pytest.raises(ValueError, match="at least one owner"):
        await orgs.remove_member(world.org.id, world.u["Olu"].id, actor_user_id=world.u["Olu"].id)


async def test_readd_after_removal_reactivates(world):
    await orgs.remove_member(world.org.id, world.u["Sol"].id, actor_user_id=world.u["Olu"].id)
    m = await orgs.add_member(world.org.id, world.u["Sol"].id, Role.VIEWER)
    assert m.status == "active" and m.role == "viewer" and m.removed_at is None


async def test_overrides_restrict_freely_but_widen_only_for_owners(world):
    await orgs.set_override(world.org.id, Role.MANAGER, "deal.*", Scope.NONE, actor_user_id=world.u["Ada"].id)
    snap = await orgs.load_snapshot(world.org.id)
    assert snap.max_scope(Role.MANAGER, "deal.read") is Scope.NONE
    with pytest.raises(PermissionError):
        await orgs.set_override(world.org.id, Role.REP, "forecast.read", Scope.TEAM, actor_user_id=world.u["Ada"].id)
    await orgs.set_override(world.org.id, Role.REP, "forecast.read", Scope.TEAM, actor_user_id=world.u["Olu"].id)
    assert (await orgs.load_snapshot(world.org.id)).max_scope(Role.REP, "forecast.read") is Scope.TEAM


async def test_snapshot_is_cached_by_version(world, monkeypatch):
    a = await orgs.load_snapshot(world.org.id)
    assert await orgs.load_snapshot(world.org.id) is a
    await orgs.bump_version(world.org.id)
    assert (await orgs.load_snapshot(world.org.id)).version == a.version + 1


async def test_features_are_validated(world):
    await orgs.set_feature(world.org.id, "recap", True)
    assert (await orgs.get_org(world.org.id)).features == {"recap": True}
    with pytest.raises(KeyError):
        await orgs.set_feature(world.org.id, "teleport", True)


def test_cli_creates_and_lists_orgs(settings):
    runner = CliRunner()
    assert runner.invoke(app, ["migrate"]).exit_code == 0
    r = runner.invoke(app, ["org", "create", "Globex", "--owner-chat-id", "4242", "--currency", "EUR",
                            "--domain", "globex.test"])
    assert r.exit_code == 0, r.output
    assert "globex" in runner.invoke(app, ["org", "list"]).output
    assert runner.invoke(app, ["org", "feature", "globex", "recap", "on"]).exit_code == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_orgs_repo.py -q`
Expected: FAIL (`No module named 'mavis.revintel.repo'`).

- [ ] **Step 3: Implement**

`src/mavis/revintel/repo/__init__.py`: empty file.

`src/mavis/revintel/repo/orgs.py`:
```python
"""Orgs, teams, memberships and policy snapshots. Every change bumps `authz_version` and writes the audit
chain in the same transaction."""

from __future__ import annotations

import re
from collections.abc import Sequence

from sqlalchemy import func, select

from mavis.revintel import audit
from mavis.revintel.authz import SEED_MATRIX, PolicySnapshot, seed_snapshot
from mavis.revintel.domain import Role, Scope
from mavis.revintel.mode import FEATURES
from mavis.revintel.models import Org, OrgMembership, OrgRoleOverride, RolePermission, Team
from mavis.store.db import Session, utcnow

_SCOPES = {s.name.lower(): s for s in Scope}
_cache: dict[int, PolicySnapshot] = {}


def clear_cache() -> None:
    _cache.clear()


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:64] or "org"


async def ensure_seed() -> int:
    inserted = 0
    async with Session() as s:
        have = {(r.role, r.action_pattern) for r in await s.scalars(select(RolePermission))}
        for action, by_role in SEED_MATRIX.items():
            for role, scope in by_role.items():
                if (role.value, action) not in have:
                    s.add(RolePermission(role=role.value, action_pattern=action, max_scope=scope.name.lower()))
                    inserted += 1
        await s.commit()
    return inserted


async def get_org(org_id: int) -> Org | None:
    async with Session() as s:
        return await s.get(Org, org_id)


async def get_by_slug(slug: str) -> Org | None:
    async with Session() as s:
        return await s.scalar(select(Org).where(Org.slug == slug))


async def create_org(name: str, *, owner_user_id: int, slug: str | None = None, base_currency: str = "USD",
                     internal_domains: Sequence[str] = (), fiscal_start_month: int = 1) -> Org:
    await ensure_seed()
    slug = slug or slugify(name)
    async with Session() as s:
        if await s.scalar(select(Org.id).where(Org.slug == slug)):
            raise ValueError(f"org slug {slug!r} is taken")
        org = Org(name=name, slug=slug, base_currency=base_currency.upper(), fiscal_start_month=fiscal_start_month,
                  internal_domains=[d.lower() for d in internal_domains])
        s.add(org)
        await s.flush()
        s.add(OrgMembership(org_id=org.id, user_id=owner_user_id, role=Role.OWNER.value))
        await audit.append(org.id, action="member.change", decision="executed", actor_user_id=None,
                           space=f"org:{org.id}", detail={"op": "org_create", "owner": owner_user_id}, session=s)
        await s.commit()
        return org


async def add_team(org_id: int, name: str, *, parent_team_id: int | None = None,
                   manager_user_id: int | None = None, attrs: dict | None = None) -> Team:
    async with Session() as s:
        team = Team(org_id=org_id, name=name, parent_team_id=parent_team_id, manager_user_id=manager_user_id,
                    attrs=attrs or {})
        s.add(team)
        await s.flush()
        await _bump(s, org_id)
        await audit.append(org_id, action="team.manage", decision="executed", resource_type="team",
                           resource_ids=[team.id], detail={"op": "add_team"}, session=s)
        await s.commit()
        return team


async def _bump(s, org_id: int) -> int:
    org = await s.get(Org, org_id)
    org.authz_version += 1
    return org.authz_version


async def bump_version(org_id: int) -> int:
    async with Session() as s:
        v = await _bump(s, org_id)
        await s.commit()
        return v


async def _owner_count(s, org_id: int) -> int:
    return await s.scalar(select(func.count()).select_from(OrgMembership).where(
        OrgMembership.org_id == org_id, OrgMembership.role == Role.OWNER.value,
        OrgMembership.status == "active")) or 0


async def add_member(org_id: int, user_id: int, role: Role, *, team_id: int | None = None,
                     crm_owner_refs: dict | None = None, actor_user_id: int | None = None) -> OrgMembership:
    async with Session() as s:
        m = await s.scalar(select(OrgMembership).where(OrgMembership.org_id == org_id,
                                                       OrgMembership.user_id == user_id))
        if m is None:
            m = OrgMembership(org_id=org_id, user_id=user_id, role=role.value, team_id=team_id,
                              crm_owner_refs=crm_owner_refs or {})
            s.add(m)
        else:
            m.role, m.team_id, m.status, m.removed_at, m.joined_at = role.value, team_id, "active", None, utcnow()
            m.crm_owner_refs = crm_owner_refs or m.crm_owner_refs
        await s.flush()
        await _bump(s, org_id)
        await audit.append(org_id, action="member.change", decision="executed", actor_user_id=actor_user_id,
                           resource_type="member", resource_ids=[user_id],
                           detail={"op": "add", "role": role.value}, session=s)
        await s.commit()
        return m


async def set_role(org_id: int, user_id: int, role: Role, *, actor_user_id: int) -> None:
    async with Session() as s:
        m = await s.scalar(select(OrgMembership).where(OrgMembership.org_id == org_id,
                                                       OrgMembership.user_id == user_id,
                                                       OrgMembership.status == "active"))
        if m is None:
            raise LookupError("no such member")
        if m.role == Role.OWNER.value and role is not Role.OWNER and await _owner_count(s, org_id) <= 1:
            raise ValueError("an org needs at least one owner")
        old, m.role = m.role, role.value
        await _bump(s, org_id)
        await audit.append(org_id, action="member.change", decision="executed", actor_user_id=actor_user_id,
                           resource_type="member", resource_ids=[user_id],
                           detail={"op": "role", "from": old, "to": role.value}, session=s)
        await s.commit()


async def remove_member(org_id: int, user_id: int, *, actor_user_id: int) -> bool:
    async with Session() as s:
        m = await s.scalar(select(OrgMembership).where(OrgMembership.org_id == org_id,
                                                       OrgMembership.user_id == user_id,
                                                       OrgMembership.status == "active"))
        if m is None:
            return False
        if m.role == Role.OWNER.value and await _owner_count(s, org_id) <= 1:
            raise ValueError("an org needs at least one owner")
        m.status, m.removed_at = "removed", utcnow()
        await _bump(s, org_id)
        await audit.append(org_id, action="member.change", decision="executed", actor_user_id=actor_user_id,
                           resource_type="member", resource_ids=[user_id], detail={"op": "remove"}, session=s)
        await s.commit()
        return True


async def membership(org_id: int, user_id: int) -> OrgMembership | None:
    async with Session() as s:
        return await s.scalar(select(OrgMembership).where(
            OrgMembership.org_id == org_id, OrgMembership.user_id == user_id, OrgMembership.status == "active"))


async def memberships_of(user_id: int) -> list[OrgMembership]:
    async with Session() as s:
        rows = await s.scalars(select(OrgMembership).join(Org, Org.id == OrgMembership.org_id).where(
            OrgMembership.user_id == user_id, OrgMembership.status == "active", Org.status == "active")
            .order_by(OrgMembership.id))
        return list(rows)


async def team_subtree_ids(org_id: int, team_id: int) -> frozenset[int]:
    async with Session() as s:
        edges = (await s.execute(select(Team.id, Team.parent_team_id).where(Team.org_id == org_id))).all()
    children: dict[int | None, list[int]] = {}
    for tid, parent in edges:
        children.setdefault(parent, []).append(tid)
    seen, stack = set(), [team_id]
    while stack:
        t = stack.pop()
        if t not in seen:
            seen.add(t)
            stack.extend(children.get(t, []))
    return frozenset(seen)


async def load_snapshot(org_id: int) -> PolicySnapshot:
    async with Session() as s:
        version = await s.scalar(select(Org.authz_version).where(Org.id == org_id))
        if version is None:
            raise LookupError(f"no org {org_id}")
        cached = _cache.get(org_id)
        if cached is not None and cached.version == version:
            return cached
        matrix = dict(seed_snapshot().matrix)
        for r in await s.scalars(select(RolePermission)):
            matrix[(r.role, r.action_pattern)] = _SCOPES[r.max_scope]
        for o in await s.scalars(select(OrgRoleOverride).where(OrgRoleOverride.org_id == org_id)):
            matrix[(o.role, o.action_pattern)] = _SCOPES[o.max_scope]
    snap = PolicySnapshot(org_id=org_id, version=version, matrix=matrix)
    _cache[org_id] = snap
    return snap


async def set_override(org_id: int, role: Role, action_pattern: str, scope: Scope, *, actor_user_id: int) -> None:
    current = (await load_snapshot(org_id)).max_scope(role, action_pattern)
    if scope > current:
        actor = await membership(org_id, actor_user_id)
        if actor is None or actor.role != Role.OWNER.value:
            raise PermissionError("only an org owner can widen a role")
    async with Session() as s:
        row = await s.scalar(select(OrgRoleOverride).where(
            OrgRoleOverride.org_id == org_id, OrgRoleOverride.role == role.value,
            OrgRoleOverride.action_pattern == action_pattern))
        if row is None:
            s.add(OrgRoleOverride(org_id=org_id, role=role.value, action_pattern=action_pattern,
                                  max_scope=scope.name.lower(), set_by=actor_user_id))
        else:
            row.max_scope, row.set_by, row.set_at = scope.name.lower(), actor_user_id, utcnow()
        await _bump(s, org_id)
        await audit.append(org_id, action="policy.change", decision="executed", actor_user_id=actor_user_id,
                           resource_type="role", resource_ids=[f"{role.value}:{action_pattern}"],
                           detail={"scope": scope.name.lower()}, session=s)
        await s.commit()


async def set_feature(org_id: int, name: str, on: bool) -> None:
    if name not in FEATURES:
        raise KeyError(name)
    async with Session() as s:
        org = await s.get(Org, org_id)
        org.features = {**org.features, name: bool(on)}
        await audit.append(org_id, action="policy.change", decision="executed", resource_type="feature",
                           resource_ids=[name], detail={"on": bool(on)}, session=s)
        await s.commit()

```

`src/mavis/cli.py`: add after the `migrate` command (additive):
```python
org_app = typer.Typer(no_args_is_help=True, help="Organisations (revenue intelligence)")
app.add_typer(org_app, name="org")


def _org_run(coro_fn):
    async def go():
        await init_db()
        try:
            return await coro_fn()
        finally:
            await dispose_engine()

    return asyncio.run(go())


@org_app.command("create")
def org_create(name: str, owner_chat_id: int = typer.Option(..., "--owner-chat-id"),
               currency: str = "USD", domain: list[str] = typer.Option([], "--domain")) -> None:
    """Create an organisation; the owner is the user with this Telegram chat id."""
    from mavis.revintel.repo import orgs
    from mavis.store.repo import users

    async def go():
        owner, _ = await users.get_or_create_by_chat(owner_chat_id, None)
        return await orgs.create_org(name, owner_user_id=owner.id, base_currency=currency, internal_domains=domain)

    org = _org_run(go)
    typer.echo(f"created org {org.slug} (id {org.id})")


@org_app.command("list")
def org_list() -> None:
    from sqlalchemy import select

    from mavis.revintel.models import Org
    from mavis.store.db import Session

    async def go():
        async with Session() as s:
            return list(await s.scalars(select(Org).order_by(Org.id)))

    for o in _org_run(go):
        typer.echo(f"{o.id}\t{o.slug}\t{o.status}\t{','.join(k for k, v in o.features.items() if v)}")


@org_app.command("feature")
def org_feature(slug: str, name: str, state: str) -> None:
    """Switch an org feature on or off: recap, risk, forecast, nba, alerts, writes."""
    from mavis.revintel.repo import orgs

    async def go():
        org = await orgs.get_by_slug(slug)
        if org is None:
            raise typer.BadParameter(f"no org {slug}")
        await orgs.set_feature(org.id, name, state.lower() in ("on", "true", "1"))

    _org_run(go)
    typer.echo(f"{slug}: {name} {state}")
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel -q && uv run ruff check src tests`
Expected: PASS (this also turns the Task 5 audit tests green).

- [ ] **Step 5: Commit**

```bash
git add src/mavis/revintel/repo src/mavis/cli.py tests/revintel/test_orgs_repo.py
git commit -m "feat(revintel): org repository, policy snapshots and mavis org commands"
```

---

### Task 7: Approval policy resolution and `decide()`

**Files:**
- Create: `src/mavis/revintel/approval.py`, `src/mavis/revintel/decision.py`, `tests/revintel/test_approval_policy.py`

**Interfaces:**
- Consumes: Task 2 types, Task 3 `authorize`, `mavis.domain.policy.RiskClass`.
- Produces:
  - `MODES = ("auto","confirm","manager","admin","deny")`, `stricter(a, b) -> str`
  - `@dataclass(frozen=True) PolicyRow(id, org_id, action_class, role, conditions, mode, approver_role, expires_after_s, priority, enabled=True)`
  - `validate_conditions(cond: Mapping) -> None` (closed set: `amount_gt`, `record_count_gt`, `external_recipient`, `stage_category`, `field_in`, `over_threshold`; unknown key raises `ValueError`), `eval_conditions(cond, facts) -> bool` (a fact that is missing counts as "holds": the stricter row applies)
  - `SEED_POLICIES: tuple[PolicyRow, ...]` (negative ids; spec 5.1 table)
  - `resolve(rows, *, role: Role, action_class: str, facts: Mapping, risk: RiskClass, tainted: bool, org_allows_auto: frozenset[str] = frozenset()) -> ApprovalReq`
  - `async rows_for(org_id: int) -> list[PolicyRow]` (seeds plus enabled DB rows of that org or NULL)
  - `decision.decide(principal, action, resource, *, snapshot, rows, action_class="", risk=RiskClass.READ, facts=None, tainted=False, org_allows_auto=frozenset()) -> Decision` (`deny` with reason `policy_deny` when the strictest mode is deny; `allow` when `auto`; else `require_approval` with `approval` set)
- Rules: strictest mode among matching rows at the highest priority tier; any matching `deny` wins outright; result is `max_strict(policy_mode, RiskClass floor)`; a floor of `confirm` is lowered to `auto` only when the class is in `org_allows_auto` and the risk is not SPEND; taint raises `auto` to `confirm` for every non-READ risk and nothing can lower it.

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_approval_policy.py`:
```python
from __future__ import annotations

import pytest

from mavis.domain.policy import RiskClass
from mavis.revintel.approval import SEED_POLICIES, PolicyRow, eval_conditions, resolve, stricter, validate_conditions
from mavis.revintel.authz import seed_snapshot
from mavis.revintel.decision import decide
from mavis.revintel.domain import Principal, ResourceRef, Role

OUT = RiskClass.OUTWARD


def mode(role, cls, risk=OUT, tainted=False, facts=None, rows=SEED_POLICIES, allow=frozenset()):
    return resolve(rows, role=role, action_class=cls, facts=facts or {}, risk=risk, tainted=tainted,
                   org_allows_auto=allow)


@pytest.mark.parametrize(("role", "cls", "expected"), [
    (Role.REP, "crm.note", "confirm"), (Role.MANAGER, "crm.stage", "confirm"),
    (Role.ADMIN, "crm.note", "deny"), (Role.OWNER, "crm.field", "deny"), (Role.VIEWER, "crm.task", "deny"),
    (Role.REP, "crm.bulk", "deny"), (Role.MANAGER, "crm.bulk", "admin"),
    (Role.REP, "email.send_external", "confirm"), (Role.ADMIN, "email.send_internal", "deny"),
])
def test_seed_table(role, cls, expected):
    assert mode(role, cls).mode == expected


def test_close_goes_to_the_manager_only_over_the_threshold():
    assert mode(Role.REP, "crm.close", facts={"over_threshold": False}).mode == "confirm"
    big = mode(Role.REP, "crm.close", facts={"over_threshold": True})
    assert (big.mode, big.approver_role) == ("manager", Role.MANAGER)
    assert mode(Role.REP, "crm.close").mode == "manager"  # unknown amount: the stricter row applies


def test_strictest_wins_within_the_top_tier_and_deny_beats_everything():
    rows = [PolicyRow(1, 5, "crm.note", "rep", {}, "confirm", None, 3600, 0),
            PolicyRow(2, 5, "crm.note", "rep", {}, "manager", "manager", 3600, 0),
            PolicyRow(3, 5, "crm.note", "rep", {}, "auto", None, 3600, 9)]
    assert mode(Role.REP, "crm.note", rows=rows[:2]).mode == "manager"
    assert mode(Role.REP, "crm.note", rows=rows).mode == "confirm"  # tier 9 says auto, the OUTWARD floor holds
    rows.append(PolicyRow(4, 5, "crm.note", None, {}, "deny", None, 0, -5))
    assert mode(Role.REP, "crm.note", rows=rows).mode == "deny"


def test_floor_taint_and_org_auto():
    auto_row = [PolicyRow(1, 5, "crm.note", "rep", {}, "auto", None, 60, 0)]
    assert mode(Role.REP, "crm.note", rows=auto_row).mode == "confirm"  # OUTWARD floor
    assert mode(Role.REP, "crm.note", rows=auto_row, allow=frozenset({"crm.note"})).mode == "auto"
    assert mode(Role.REP, "crm.note", rows=auto_row, allow=frozenset({"crm.note"}), tainted=True).mode == "confirm"
    assert mode(Role.REP, "crm.note", rows=auto_row, allow=frozenset({"crm.note"}), risk=RiskClass.SPEND).mode == "confirm"
    assert mode(Role.REP, "unlisted", risk=RiskClass.READ).mode == "auto"
    assert mode(Role.REP, "unlisted", risk=RiskClass.WRITE_SELF, tainted=True).mode == "confirm"


def test_conditions_are_a_closed_language():
    assert eval_conditions({"amount_gt": 500}, {"amount_minor": 900}) and not eval_conditions(
        {"amount_gt": 500}, {"amount_minor": 100})
    assert eval_conditions({"field_in": ["amount", "stage"]}, {"field": "stage"})
    assert not eval_conditions({"external_recipient": True}, {"external_recipient": False})
    assert eval_conditions({"stage_category": ["won", "lost"]}, {"stage_category": "won"})
    assert eval_conditions({"record_count_gt": 3}, {})  # unknown fact: holds
    with pytest.raises(ValueError):
        validate_conditions({"eval": "1+1"})


def test_stricter_orders_modes():
    assert stricter("confirm", "manager") == "manager" and stricter("deny", "admin") == "deny"


def _p(role):
    return Principal(user_id=1, org_id=1, role=role, team_ids=frozenset({1}))


def test_decide_composes_authorize_and_policy():
    snap, res = seed_snapshot(1), ResourceRef("deal", 1, "d", owner_user_id=1, team_id=1)
    d = decide(_p(Role.REP), "deal.write", res, snapshot=snap, rows=SEED_POLICIES, action_class="crm.note", risk=OUT)
    assert (d.effect, d.approval.mode) == ("require_approval", "confirm")
    d = decide(_p(Role.REP), "deal.read", res, snapshot=snap, rows=SEED_POLICIES)
    assert d.effect == "allow"
    d = decide(_p(Role.ADMIN), "deal.write", res, snapshot=snap, rows=SEED_POLICIES, action_class="crm.note", risk=OUT)
    assert (d.effect, d.reason) == ("deny", "role_lacks_action")  # permission first
    d = decide(_p(Role.REP), "deal.bulk_write", res, snapshot=snap, rows=SEED_POLICIES, action_class="crm.bulk", risk=OUT)
    assert d.effect == "deny"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_approval_policy.py -q`
Expected: FAIL (`No module named 'mavis.revintel.approval'`).

- [ ] **Step 3: Implement**

`src/mavis/revintel/approval.py`:
```python
"""Approval policy by role and action class (spec 5). Pure resolution; rows are data."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import or_, select

from mavis.domain.policy import RiskClass
from mavis.revintel.domain import ApprovalReq, Role

MODES = ("auto", "confirm", "manager", "admin", "deny")
_APPROVER = {"manager": Role.MANAGER, "admin": Role.ADMIN}
_FLOOR = {RiskClass.READ: "auto", RiskClass.WRITE_SELF: "auto", RiskClass.OUTWARD: "confirm",
          RiskClass.SPEND: "confirm", RiskClass.DESTRUCTIVE: "confirm"}
# condition key -> the fact the calling tool must declare
_FACT = {"amount_gt": "amount_minor", "record_count_gt": "record_count", "external_recipient": "external_recipient",
         "stage_category": "stage_category", "field_in": "field", "over_threshold": "over_threshold"}
DEFAULT_EXPIRY_S = 86400


def stricter(a: str, b: str) -> str:
    return a if MODES.index(a) >= MODES.index(b) else b


@dataclass(frozen=True)
class PolicyRow:
    id: int | None
    org_id: int | None
    action_class: str
    role: str | None
    conditions: Mapping
    mode: str
    approver_role: str | None
    expires_after_s: int
    priority: int
    enabled: bool = True


def validate_conditions(cond: Mapping) -> None:
    bad = set(cond) - set(_FACT)
    if bad:
        raise ValueError(f"unknown policy condition {sorted(bad)}")


def eval_conditions(cond: Mapping, facts: Mapping) -> bool:
    for key, want in cond.items():
        fact = _FACT[key]
        if fact not in facts:
            continue  # unknown: the condition holds, so the stricter row applies
        got = facts[fact]
        if key in ("amount_gt", "record_count_gt"):
            ok = got > want
        elif key in ("external_recipient", "over_threshold"):
            ok = bool(got) == bool(want)
        elif key == "stage_category":
            ok = got in (want if isinstance(want, list) else [want])
        else:  # field_in
            ok = got in want
        if not ok:
            return False
    return True


def _seed() -> tuple[PolicyRow, ...]:
    rows: list[PolicyRow] = []

    def add(cls: str, role: str, mode: str, conditions: Mapping | None = None, priority: int = 0) -> None:
        rows.append(PolicyRow(-(len(rows) + 1), None, cls, role, conditions or {}, mode, _mode_role(mode),
                              DEFAULT_EXPIRY_S, priority))

    ordinary = (("rep", "confirm"), ("manager", "confirm"), ("admin", "deny"), ("owner", "deny"), ("viewer", "deny"))
    for cls in ("crm.note", "crm.task", "crm.field", "crm.stage", "email.send_external", "email.send_internal"):
        for role, mode in ordinary:
            add(cls, role, mode)
    for role, mode in ordinary:
        add("crm.close", role, mode)
    add("crm.close", "rep", "manager", {"over_threshold": True}, priority=1)
    for role, mode in (("rep", "deny"), ("manager", "admin"), ("admin", "admin"), ("owner", "admin"),
                       ("viewer", "deny")):
        add("crm.bulk", role, mode)
    return tuple(rows)


def _mode_role(mode: str) -> str | None:
    r = _APPROVER.get(mode)
    return r.value if r else None


SEED_POLICIES = _seed()


def resolve(rows: Sequence[PolicyRow], *, role: Role, action_class: str, facts: Mapping, risk: RiskClass,
            tainted: bool, org_allows_auto: frozenset[str] = frozenset()) -> ApprovalReq:
    hits = [r for r in rows if r.enabled and r.action_class == action_class and r.role in (None, role.value)
            and eval_conditions(r.conditions, facts)]
    floor = _FLOOR[risk]
    pick: PolicyRow | None = None
    if hits:
        denies = [r for r in hits if r.mode == "deny"]
        if denies:
            pick = denies[0]
        else:
            top = max(r.priority for r in hits)
            tier = [r for r in hits if r.priority == top]
            pick = max(tier, key=lambda r: MODES.index(r.mode))
    mode = pick.mode if pick else "auto"
    if mode == "auto" and floor == "confirm" and action_class in org_allows_auto and risk is not RiskClass.SPEND:
        floor = "auto"  # an org owner switched this class to auto on purpose
    final = stricter(mode, floor)
    reason = f"policy:{pick.id}" if pick and final == pick.mode else f"floor:{risk.value}"
    if tainted and risk is not RiskClass.READ and final == "auto":
        final, reason = "confirm", "taint"
    expiry = pick.expires_after_s if pick else DEFAULT_EXPIRY_S
    return ApprovalReq(final, _APPROVER.get(final), expiry, pick.id if pick else None, reason)


async def rows_for(org_id: int) -> list[PolicyRow]:
    from mavis.revintel.models import ApprovalPolicy
    from mavis.store.db import Session

    async with Session() as s:
        found = await s.scalars(select(ApprovalPolicy).where(
            ApprovalPolicy.enabled.is_(True), or_(ApprovalPolicy.org_id == org_id, ApprovalPolicy.org_id.is_(None))))
        db = [PolicyRow(r.id, r.org_id, r.action_class, r.role, r.conditions, r.mode, r.approver_role,
                        r.expires_after_s, r.priority, r.enabled) for r in found]
    for r in db:
        validate_conditions(r.conditions)
    return [*SEED_POLICIES, *db]
```

`src/mavis/revintel/decision.py`:
```python
"""authorize() plus approval policy: the one answer a tool needs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from mavis.domain.policy import RiskClass
from mavis.revintel.approval import PolicyRow, resolve
from mavis.revintel.authz import PolicySnapshot, authorize
from mavis.revintel.domain import Decision, Principal, ResourceRef


def decide(principal: Principal, action: str, resource: ResourceRef | None, *, snapshot: PolicySnapshot,
           rows: Sequence[PolicyRow], action_class: str = "", risk: RiskClass = RiskClass.READ,
           facts: Mapping | None = None, tainted: bool = False,
           org_allows_auto: frozenset[str] = frozenset()) -> Decision:
    base = authorize(principal, action, resource, snapshot=snapshot)
    if not base.allowed or principal.role is None or (risk is RiskClass.READ and not action_class):
        return base
    req = resolve(rows, role=principal.role, action_class=action_class, facts=facts or {}, risk=risk,
                  tainted=tainted, org_allows_auto=org_allows_auto)
    if req.mode == "deny":
        return Decision("deny", "policy_deny", req.policy_id, req, base.max_scope)
    if req.mode == "auto":
        return base
    return Decision("require_approval", req.reason, req.policy_id, req, base.max_scope)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel/test_approval_policy.py -q && uv run ruff check src/mavis/revintel tests/revintel`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis/revintel/approval.py src/mavis/revintel/decision.py tests/revintel/test_approval_policy.py
git commit -m "feat(revintel): approval policy by role and class with decide()"
```

---

### Task 8: Principals and spaces

**Files:**
- Create: `src/mavis/revintel/principal.py`, `src/mavis/revintel/spaces.py`, `tests/revintel/test_principal_spaces.py`

**Interfaces:**
- Consumes: Task 6 `orgs.membership/memberships_of/team_subtree_ids/get_org`, `users.get_state/update_state`.
- Produces:
  - `async principal.resolve_principal(user_id: int, *, space: str | None = None, request_id: str = "") -> Principal` (space `None` means the user's active space; a missing, removed or suspended membership or a non-active org returns the personal principal: fail closed). Team ids: the member's team; for managers also the subtree of that team and of every team they manage.
  - `principal.system_principal(org_id: int, request_id: str = "") -> Principal` (`user_id=0`, `actor="system"`)
  - `spaces.current_space: ContextVar[str]` (default `PERSONAL`)
  - `async spaces.spaces_of(user_id) -> list[SpaceInfo]` (`SpaceInfo(space, label, role)`; personal first)
  - `async spaces.active_space(user_id, *, now=None) -> str` (state `space.active`, valid membership only; falls back to `space.default` after `space_reset_hours` without a switch, else personal)
  - `async spaces.switch_space(user_id, space, *, now=None) -> str` (`ValueError` unless personal or an active membership); `async spaces.set_default(user_id, space)`
  - `spaces.parse_prefix(text: str, slugs: Mapping[str, int]) -> tuple[str | None, str]` (`"@acme how is Initech"` gives `("org:7", "how is Initech")`; only at the very start; case-insensitive; an unknown handle returns `(None, text)`)

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_principal_spaces.py`:
```python
from __future__ import annotations

from datetime import timedelta

import pytest

from mavis.domain import timeutil
from mavis.revintel import principal, spaces
from mavis.revintel.domain import PERSONAL, Role
from mavis.revintel.repo import orgs


async def test_rep_manager_and_system_principals(world):
    rep = await principal.resolve_principal(world.u["Raj"].id, space=f"org:{world.org.id}")
    assert (rep.role, rep.team_ids, rep.org_id) == (Role.REP, frozenset({world.teams.east.id}), world.org.id)
    mgr = await principal.resolve_principal(world.u["Mei"].id, space=f"org:{world.org.id}")
    assert mgr.role is Role.MANAGER and world.teams.east.id in mgr.team_ids
    sub = await orgs.add_team(world.org.id, "East A", parent_team_id=world.teams.east.id)
    mgr = await principal.resolve_principal(world.u["Mei"].id, space=f"org:{world.org.id}")
    assert sub.id in mgr.team_ids and world.teams.west.id not in mgr.team_ids
    assert principal.system_principal(world.org.id).actor == "system"


async def test_non_members_and_removed_members_get_the_personal_principal(world, user):
    p = await principal.resolve_principal(user.id, space=f"org:{world.org.id}")
    assert (p.org_id, p.role, p.space) == (None, None, PERSONAL)
    await orgs.remove_member(world.org.id, world.u["Sol"].id, actor_user_id=world.u["Olu"].id)
    assert (await principal.resolve_principal(world.u["Sol"].id, space=f"org:{world.org.id}")).org_id is None


async def test_switching_validates_membership_and_persists(world, user):
    uid = world.u["Raj"].id
    assert await spaces.active_space(uid) == PERSONAL
    assert await spaces.switch_space(uid, f"org:{world.org.id}") == f"org:{world.org.id}"
    assert await spaces.active_space(uid) == f"org:{world.org.id}"
    p = await principal.resolve_principal(uid)
    assert p.org_id == world.org.id
    with pytest.raises(ValueError):
        await spaces.switch_space(user.id, f"org:{world.org.id}")
    assert [s.space for s in await spaces.spaces_of(uid)] == [PERSONAL, f"org:{world.org.id}"]


async def test_active_space_falls_back_to_the_default_after_the_reset_window(world):
    uid, org = world.u["Rin"].id, f"org:{world.org.id}"
    t0 = timeutil.now()
    await spaces.switch_space(uid, org, now=t0)
    assert await spaces.active_space(uid, now=t0 + timedelta(hours=7)) == org
    assert await spaces.active_space(uid, now=t0 + timedelta(hours=9)) == PERSONAL
    await spaces.set_default(uid, org)
    assert await spaces.active_space(uid, now=t0 + timedelta(hours=9)) == org


async def test_losing_membership_resets_the_active_space(world):
    uid, org = world.u["Sol"].id, f"org:{world.org.id}"
    await spaces.switch_space(uid, org)
    await orgs.remove_member(world.org.id, uid, actor_user_id=world.u["Olu"].id)
    assert await spaces.active_space(uid) == PERSONAL


@pytest.mark.parametrize(("text", "expected"), [
    ("@acme how is the Initech deal?", ("org:7", "how is the Initech deal?")),
    ("@ACME   pipeline", ("org:7", "pipeline")),
    ("@globex hi", ("org:9", "hi")),
    ("@nobody hi", (None, "@nobody hi")),
    ("mail me at a@acme.test", (None, "mail me at a@acme.test")),
    ("hello @acme", (None, "hello @acme")),
    ("@acme", ("org:7", "")),
])
def test_one_shot_prefix(text, expected):
    assert spaces.parse_prefix(text, {"acme": 7, "globex": 9}) == expected
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_principal_spaces.py -q`
Expected: FAIL (`No module named 'mavis.revintel.principal'`).

- [ ] **Step 3: Implement**

`src/mavis/revintel/principal.py`:
```python
"""The only constructor of a Principal (spec 3.4). Fails closed to the personal principal."""

from __future__ import annotations

from sqlalchemy import select

from mavis.revintel.domain import Principal, Role, parse_space
from mavis.revintel.models import Team
from mavis.revintel.repo import orgs
from mavis.store.db import Session


async def _teams(org_id: int, member_team: int | None, role: Role, user_id: int) -> frozenset[int]:
    if member_team is None and role is not Role.MANAGER:
        return frozenset()
    roots: set[int] = {member_team} if member_team is not None else set()
    if role is Role.MANAGER:
        async with Session() as s:
            roots |= set(await s.scalars(select(Team.id).where(Team.org_id == org_id,
                                                                Team.manager_user_id == user_id)))
        out: set[int] = set()
        for root in roots:
            out |= await orgs.team_subtree_ids(org_id, root)
        return frozenset(out)
    return frozenset(roots)


async def resolve_principal(user_id: int, *, space: str | None = None, request_id: str = "") -> Principal:
    if space is None:
        from mavis.revintel.spaces import active_space

        space = await active_space(user_id)
    org_id = parse_space(space)
    personal = Principal(user_id=user_id, request_id=request_id)
    if org_id is None:
        return personal
    org = await orgs.get_org(org_id)
    member = await orgs.membership(org_id, user_id)
    if org is None or org.status != "active" or member is None:
        return personal
    role = Role(member.role)
    return Principal(user_id=user_id, org_id=org_id, membership_id=member.id, role=role,
                     team_ids=await _teams(org_id, member.team_id, role, user_id),
                     authz_version=org.authz_version, request_id=request_id)


def system_principal(org_id: int, request_id: str = "") -> Principal:
    return Principal(user_id=0, org_id=org_id, actor="system", request_id=request_id)
```

`src/mavis/revintel/spaces.py`:
```python
"""Which space a turn runs in (spec 3.5). Deterministic: no model decides this."""

from __future__ import annotations

import re
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta

from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.revintel.domain import PERSONAL, Role, org_space, parse_space
from mavis.revintel.repo import orgs
from mavis.store.repo import users

current_space: ContextVar[str] = ContextVar("current_space", default=PERSONAL)
STATE_KEY = "space"


@dataclass(frozen=True)
class SpaceInfo:
    space: str
    label: str
    role: Role | None


async def spaces_of(user_id: int) -> list[SpaceInfo]:
    out = [SpaceInfo(PERSONAL, "Personal", None)]
    for m in await orgs.memberships_of(user_id):
        org = await orgs.get_org(m.org_id)
        out.append(SpaceInfo(org_space(m.org_id), org.name, Role(m.role)))
    return out


async def _member_of(user_id: int, space: str) -> bool:
    oid = parse_space(space)
    return oid is not None and await orgs.membership(oid, user_id) is not None


async def active_space(user_id: int, *, now: datetime | None = None) -> str:
    st = (await users.get_state(user_id)).get(STATE_KEY) or {}
    now = now or timeutil.now()
    active, default = st.get("active", PERSONAL), st.get("default", PERSONAL)
    switched = st.get("last_switch_at")
    stale = switched is None or now - datetime.fromisoformat(switched) > timedelta(
        hours=get_settings().space_reset_hours)
    chosen = default if stale else active
    if chosen != PERSONAL and not await _member_of(user_id, chosen):
        return PERSONAL
    return chosen


async def switch_space(user_id: int, space: str, *, now: datetime | None = None) -> str:
    if space != PERSONAL and not await _member_of(user_id, space):
        raise ValueError("not a member of that space")
    now = now or timeutil.now()
    await users.update_nested(user_id, STATE_KEY, {"active": space, "last_switch_at": now.isoformat()})
    return space


async def set_default(user_id: int, space: str) -> None:
    if space != PERSONAL and not await _member_of(user_id, space):
        raise ValueError("not a member of that space")
    await users.update_nested(user_id, STATE_KEY, {"default": space})


_PREFIX = re.compile(r"^@([A-Za-z0-9_-]{2,64})(?:\s+(.*))?$", re.DOTALL)


def parse_prefix(text: str, slugs: Mapping[str, int]) -> tuple[str | None, str]:
    m = _PREFIX.match((text or "").strip())
    if m and m.group(1).lower() in slugs:
        return org_space(slugs[m.group(1).lower()]), (m.group(2) or "").strip()
    return None, text
```
Check `users.update_nested(user_id, key, patch)` merges into `state[key]` (see repo: `update_nested(user_id, key, patch)`), so the first call creates the dict. Verify with `sed -n 75,90p src/mavis/store/repo/users.py` when executing; if it does not create a missing key, use `modify_nested(user_id, key, lambda d: {**d, **patch})`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel/test_principal_spaces.py -q && uv run ruff check src/mavis/revintel tests/revintel`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis/revintel/principal.py src/mavis/revintel/spaces.py tests/revintel/test_principal_spaces.py
git commit -m "feat(revintel): principals and per-user active space"
```

---

### Task 9: The tool seam: `action`, `resource_from`, authorizer and revalidator hooks

**Files:**
- Modify: `src/mavis/tools/registry.py` (`MavisTool` fields, `ToolRegistry.authorizer/revalidator`, two hunks in `invoke`/`execute_approved`), `src/mavis/domain/errors.py` (`ApprovalRequired.org`)
- Create: `src/mavis/revintel/tooling.py`, `tests/revintel/test_tooling.py`
- Shared: `tools/registry.py` (ledger, connectors also edit it; hunks additive)

**Interfaces:**
- Consumes: Tasks 3, 7, 8.
- Produces:
  - `MavisTool.action: str | None = None`, `.resource_from: Callable[[BaseModel, int], Awaitable[ResourceRef | None]] | None = None`, `.action_class: str = ""`, `.exempt_reason: str = ""`
  - `ApprovalRequired(action, preview, arguments, org: dict | None = None)` with `.org`
  - `ToolRegistry.authorizer: Callable[[MavisTool, int, BaseModel, RiskClass, bool], Awaitable[AuthOutcome | None]] | None`, `ToolRegistry.revalidator: Callable[[MavisTool, int, BaseModel, dict | None], Awaitable[str | None]] | None` (returns a refusal sentence or None)
  - `tooling.AuthOutcome(refusal: str | None = None, approval: ApprovalReq | None = None, org: dict | None = None)` where `org = {"org_id", "authz_version", "action", "role"}`
  - `tooling.ORG_NEUTRAL_TOOLS = frozenset({"wake_me","list_tasks","cancel_task","acknowledge_failure","web_search","web_extract","pending"})`
  - `tooling.install(registry) -> None` (idempotent; wraps `registry.available` so personal turns see only tools without `action`, org turns see tools with `action` or in `ORG_NEUTRAL_TOOLS`; sets `authorizer` and `revalidator`)
  - `tooling.check_registry(registry) -> list[str]` (problems: unknown action key; `org_id`/`user_id` field anywhere in an `action` tool's argument schema; `exempt_reason` set on a tool with an action; a `rev_*`/`org_*`/`switch_space` tool with neither `action` nor `exempt_reason`; a neutral name that is not registered)
- Behaviour: in a personal space an `action` tool refuses with "That is a work action. Switch to your org space first."; in an org space `decide` runs with the tool's `action_class`, effective risk, facts from `ToolContext`-free `facts_from` (none yet: `{}`), and the run's taint; `deny` returns "I can't find that in your view." for `out_of_scope`/`cross_org` and "Your role doesn't allow that." for `role_lacks_action`/`policy_deny`; every deny writes an audit row `decision="deny"`.

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_tooling.py`:
```python
from __future__ import annotations

import pytest
from pydantic import BaseModel

from mavis.domain.errors import ApprovalRequired
from mavis.domain.policy import RiskClass
from mavis.revintel import audit, spaces, tooling
from mavis.revintel.domain import ResourceRef
from mavis.revintel.models import OrgAuditLog
from mavis.tools.registry import MavisTool, ToolRegistry


class DealArgs(BaseModel):
    deal_key: str


class BadArgs(BaseModel):
    org_id: int


async def _noop(user_id, args):
    return "done"


def make_registry(world, owner_of="Raj"):
    async def resource_from(args, user_id):
        return ResourceRef("deal", world.org.id, args.deal_key, owner_user_id=world.u[owner_of].id,
                           team_id=world.teams.east.id)

    reg = ToolRegistry()
    reg.register(MavisTool("rev_deal", "Read a deal.", DealArgs, RiskClass.READ, _noop, frozenset({"conversation"}),
                           action="deal.read", resource_from=resource_from))
    reg.register(MavisTool("rev_note", "Add a note.", DealArgs, RiskClass.OUTWARD, _noop, frozenset({"conversation"}),
                           action="deal.write", action_class="crm.note", resource_from=resource_from))
    reg.register(MavisTool("wake_me", "Remind.", DealArgs, RiskClass.WRITE_SELF, _noop, frozenset({"conversation"})))
    tooling.install(reg)
    return reg


async def test_personal_turn_refuses_work_actions_and_hides_them(world):
    reg = make_registry(world)
    names = {t.name for t in reg.all()}  # all() lists every tool; select/available hides by space
    assert reg.available(reg.get("rev_deal")) is False and reg.available(reg.get("wake_me")) is True
    tok = spaces.current_space.set("personal")
    try:
        out = await reg.invoke(reg.get("rev_deal"), world.u["Raj"].id, DealArgs(deal_key="d1"))
    finally:
        spaces.current_space.reset(tok)
    assert "Switch to your org space" in out and "rev_deal" in names


async def test_org_turn_hides_personal_tools_and_allows_neutral_ones(world):
    reg = make_registry(world)
    tok = spaces.current_space.set(f"org:{world.org.id}")
    try:
        assert reg.available(reg.get("rev_deal")) and reg.available(reg.get("wake_me"))
    finally:
        spaces.current_space.reset(tok)


async def test_own_deal_reads_other_reps_deal_is_hidden_and_audited(world):
    reg = make_registry(world)
    tok = spaces.current_space.set(f"org:{world.org.id}")
    try:
        assert await reg.invoke(reg.get("rev_deal"), world.u["Raj"].id, DealArgs(deal_key="d1")) == "done"
        out = await reg.invoke(reg.get("rev_deal"), world.u["Sol"].id, DealArgs(deal_key="d1"))  # Sol is West
    finally:
        spaces.current_space.reset(tok)
    assert out == "I can't find that in your view."
    from sqlalchemy import select

    from mavis.store.db import Session
    async with Session() as s:
        rows = list(await s.scalars(select(OrgAuditLog).where(OrgAuditLog.decision == "deny")))
    assert [(r.action, r.reason) for r in rows] == [("deal.read", "out_of_scope")]
    assert (await audit.verify_chain(world.org.id)).ok


async def test_writes_need_approval_and_carry_the_org_metadata(world):
    reg = make_registry(world)
    tok = spaces.current_space.set(f"org:{world.org.id}")
    try:
        with pytest.raises(ApprovalRequired) as exc:
            await reg.invoke(reg.get("rev_note"), world.u["Raj"].id, DealArgs(deal_key="d1"))
    finally:
        spaces.current_space.reset(tok)
    assert exc.value.org["action"] == "deal.write" and exc.value.org["org_id"] == world.org.id
    assert exc.value.org["role"] == "rep"


async def test_role_without_the_action_gets_a_role_message(world):
    reg = make_registry(world)
    tok = spaces.current_space.set(f"org:{world.org.id}")
    try:
        out = await reg.invoke(reg.get("rev_note"), world.u["Vic"].id, DealArgs(deal_key="d1"))
    finally:
        spaces.current_space.reset(tok)
    assert out == "Your role doesn't allow that."


def test_check_registry_finds_the_structural_problems(world):
    reg = ToolRegistry()
    reg.register(MavisTool("rev_open", "x", DealArgs, RiskClass.READ, _noop, frozenset({"conversation"})))
    reg.register(MavisTool("rev_bad", "x", BadArgs, RiskClass.READ, _noop, frozenset({"conversation"}),
                           action="deal.read"))
    reg.register(MavisTool("rev_typo", "x", DealArgs, RiskClass.READ, _noop, frozenset({"conversation"}),
                           action="deal.reed"))
    reg.register(MavisTool("switch_space", "x", DealArgs, RiskClass.WRITE_SELF, _noop, frozenset({"conversation"}),
                           exempt_reason="changes only the caller's own active space"))
    problems = "\n".join(tooling.check_registry(reg))
    assert "rev_open" in problems and "rev_bad" in problems and "rev_typo" in problems
    assert "switch_space" not in problems


def test_the_real_registry_passes_the_check(settings):
    from mavis.tools.registry import get_registry

    assert tooling.check_registry(get_registry()) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_tooling.py -q`
Expected: FAIL (`unexpected keyword argument 'action'`).

- [ ] **Step 3: Implement**

`src/mavis/domain/errors.py`, `ApprovalRequired`:
```python
class ApprovalRequired(MavisError):
    def __init__(self, action: str, preview: str, arguments: dict, org: dict | None = None) -> None:
        super().__init__(f"approval required for {action}")
        self.action = action
        self.preview = preview
        self.arguments = arguments
        self.org = org  # Track 6: {"org_id", "authz_version", "action", "role", "mode", "approver_role", ...}
```

`src/mavis/tools/registry.py`, `MavisTool` (append after `progress_label`):
```python
    # Track 6 (revenue intelligence). `action` is the authorize() key of the org action this tool performs;
    # None means a personal tool. `resource_from(args, user_id)` finds the record it touches (loaded through a
    # scoped repository, never from a model-supplied org or user id). `action_class` names the approval policy
    # family; `exempt_reason` states why an org-space tool has no action (switch_space).
    action: str | None = None
    resource_from: Callable[[BaseModel, int], Awaitable[Any]] | None = None
    action_class: str = ""
    exempt_reason: str = ""
```
`ToolRegistry.__init__` (append):
```python
        self.authorizer: Callable[..., Awaitable[Any]] | None = None  # Track 6: set by revintel.tooling.install
        self.revalidator: Callable[..., Awaitable[str | None]] | None = None
```
In `invoke`, immediately after `tainted = _run_tainted()` and before the `_gate_tainted` check, insert:
```python
        outcome = None
        if self.authorizer is not None and tool.action is not None:
            outcome = await self.authorizer(tool, user_id, args, risk, tainted)
            if outcome.refusal is not None:
                return outcome.refusal
```
and replace `if risk.needs_approval:` with:
```python
        if outcome is not None and outcome.approval is not None:
            preview = tool.render_preview(args, await tool_context(user_id)) + note
            raise ApprovalRequired(tool.name, preview, payload, org=outcome.org)
        if outcome is None and risk.needs_approval:
```
(An org tool whose policy says `auto` skips the personal `needs_approval` block because `decide` already applied the RiskClass floor and the taint rule.) In `execute_approved`, after `args = await _localized(...)`:
```python
        if self.revalidator is not None and tool.action is not None:
            refusal = await self.revalidator(tool, approval.user_id, args, getattr(approval, "org_meta", None))
            if refusal is not None:
                raise ActionFailed(refusal, reason=refusal)
```
(`approval.org_meta` arrives in Task 12; until then `getattr` returns None and the revalidator re-runs `decide` only.)

`src/mavis/revintel/tooling.py`:
```python
"""Enforcement seam: every org tool call goes through decide(); structural checks keep it that way."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from mavis.domain.policy import RiskClass
from mavis.revintel import approval, audit
from mavis.revintel.authz import SEED_MATRIX
from mavis.revintel.decision import decide
from mavis.revintel.domain import PERSONAL, ApprovalReq, Principal, parse_space
from mavis.revintel.principal import resolve_principal
from mavis.revintel.repo import orgs
from mavis.revintel.spaces import current_space

ORG_NEUTRAL_TOOLS = frozenset({"wake_me", "list_tasks", "cancel_task", "acknowledge_failure", "web_search",
                               "web_extract", "pending"})
KNOWN_ACTIONS = frozenset(SEED_MATRIX) | {"sync.run"}
_FORBIDDEN_ARGS = frozenset({"org_id", "user_id"})
_ORG_PREFIXES = ("rev_", "org_", "switch_space")
HIDDEN = "I can't find that in your view."
ROLE = "Your role doesn't allow that."
WRONG_SPACE = "That is a work action. Switch to your org space first."


@dataclass(frozen=True)
class AuthOutcome:
    refusal: str | None = None
    approval: ApprovalReq | None = None
    org: dict | None = None


def _visible(tool, space: str) -> bool:
    if parse_space(space) is None:
        return tool.action is None
    return tool.action is not None or tool.name in ORG_NEUTRAL_TOOLS or bool(tool.exempt_reason)


async def authorize_tool(tool, user_id: int, args: BaseModel, risk: RiskClass, tainted: bool) -> AuthOutcome:
    p: Principal = await resolve_principal(user_id, space=current_space.get())
    if p.org_id is None:
        return AuthOutcome(refusal=WRONG_SPACE)
    snapshot = await orgs.load_snapshot(p.org_id)
    resource = await tool.resource_from(args, user_id) if tool.resource_from else None
    rows = await approval.rows_for(p.org_id)
    d = decide(p, tool.action, resource, snapshot=snapshot, rows=rows, action_class=tool.action_class, risk=risk,
               tainted=tainted)
    base = dict(org_id=p.org_id, actor_user_id=user_id, role=p.role.value if p.role else None,
                space=p.space, action=tool.action, request_id=p.request_id)
    if d.effect == "deny":
        await audit.append(p.org_id, decision="deny", reason=d.reason, resource_type=getattr(resource, "type", None),
                           resource_ids=[resource.id] if resource and resource.id else [], **{
                               k: v for k, v in base.items() if k != "org_id"})
        hidden = d.reason in ("out_of_scope", "cross_org")
        return AuthOutcome(refusal=HIDDEN if hidden else ROLE)
    meta = {"org_id": p.org_id, "authz_version": p.authz_version, "action": tool.action,
            "role": p.role.value if p.role else None}
    if d.effect == "require_approval":
        await audit.append(p.org_id, decision="approval_required", reason=d.reason, policy_rule_id=d.matched_rule,
                           resource_type=getattr(resource, "type", None),
                           resource_ids=[resource.id] if resource and resource.id else [],
                           **{k: v for k, v in base.items() if k != "org_id"})
        return AuthOutcome(approval=d.approval, org={**meta, "mode": d.approval.mode,
                                                     "approver_role": d.approval.approver_role.value
                                                     if d.approval.approver_role else None,
                                                     "policy_id": d.matched_rule,
                                                     "expires_after_s": d.approval.expires_after_s})
    return AuthOutcome(org=meta)


def install(registry) -> None:
    if getattr(registry, "_revintel_installed", False):
        return
    inner = registry.available
    registry.available = lambda t: inner(t) and _visible(t, current_space.get())
    registry.authorizer = authorize_tool
    registry.revalidator = revalidate_tool
    registry._revintel_installed = True


async def revalidate_tool(tool, user_id: int, args: BaseModel, meta: dict | None) -> str | None:
    """Re-authorize at execution time (spec 4.5). Task 12 supplies the version check; here: still allowed?"""
    org_id = (meta or {}).get("org_id")
    p = await resolve_principal(user_id, space=f"org:{org_id}" if org_id else current_space.get())
    if p.org_id is None:
        return "You are no longer a member of that workspace, so I did not do it."
    resource = await tool.resource_from(args, user_id) if tool.resource_from else None
    d = decide(p, tool.action, resource, snapshot=await orgs.load_snapshot(p.org_id), rows=[], risk=RiskClass.READ)
    return None if d.allowed else "Your access changed, so I did not do it."


def _fields(model: type[BaseModel]) -> set[str]:
    found: set[str] = set()
    stack = [model.model_json_schema()]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            found |= set(node.get("properties", {}))
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def check_registry(registry) -> list[str]:
    problems: list[str] = []
    names = {t.name for t in registry.all()}
    for t in registry.all():
        if t.action is not None:
            if t.action not in KNOWN_ACTIONS:
                problems.append(f"{t.name}: unknown action {t.action!r}")
            if _fields(t.args_model) & _FORBIDDEN_ARGS:
                problems.append(f"{t.name}: argument model has an org_id or user_id field")
            if t.exempt_reason:
                problems.append(f"{t.name}: has both an action and an exempt_reason")
        elif t.name.startswith(_ORG_PREFIXES) and not t.exempt_reason:
            problems.append(f"{t.name}: org tool with neither action nor exempt_reason")
    return problems + [f"neutral tool {n} is not registered" for n in sorted(ORG_NEUTRAL_TOOLS - names)
                       if names and "wake_me" in names]


_ = PERSONAL
```
`ToolRegistry.all()` and `.get()` exist (verify with `grep -n "def all\|def get" src/mavis/tools/registry.py`; if `all()` is named differently use that name consistently here and in the test). Remove the trailing `_ = PERSONAL` and the `PERSONAL` import if ruff flags it.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel/test_tooling.py tests/tools -q && uv run ruff check src tests`
Expected: PASS (defaults-unchanged: existing registry tests green because `action` defaults to None and `authorizer` to None).

- [ ] **Step 5: Commit**

```bash
git add src/mavis/tools/registry.py src/mavis/domain/errors.py src/mavis/revintel/tooling.py tests/revintel/test_tooling.py
git commit -m "feat(revintel): authorize hook and structural checks for org tools"
```

---

### Task 10: Space switching in chat (`/space`, `switch_space`, `@org` prefix, nudge)

**Files:**
- Create: `src/mavis/revintel/commands.py`, `tests/revintel/test_space_chat.py`
- Modify: `src/mavis/agents/commands.py` (`run_command` first lines), `src/mavis/agents/conversation.py` (`run_turn`: two lines after the user is loaded), `src/mavis/tools/chat_tools.py` (register `switch_space`), `src/mavis/config.py` (`org_nudge_words`)
- Shared: `agents/commands.py`, `agents/conversation.py`, `tools/chat_tools.py`. After plan 11 merges, re-register `run_org_command` on its command table instead of the first-lines hook (the function stays the same).

**Interfaces:**
- Consumes: Task 8 spaces, Task 6 repo.
- Produces:
  - `commands.run_org_command(event: Event) -> bool` (handles `/space`; returns True when handled)
  - `commands.begin_turn(user_id: int, text: str) -> tuple[str, str]` (sets `current_space` for this turn and returns `(text_without_prefix, space)`; a one-shot `@acme` prefix wins over the active space for this turn only; nothing is persisted)
  - button prefix `sp:` : `sp:<space>` switches (`sp:personal`, `sp:org:7`)
  - `needs_org_nudge(text: str, entity_names: Iterable[str], words: Iterable[str]) -> bool` and the reply "That sounds like {org}. Switch for this question?" with buttons `[Yes, {org}]` (`sp:once:org:7:<hash>` runs once) and `[No]`
  - tool `switch_space(space: str)` (`RiskClass.WRITE_SELF`, `exempt_reason="changes only the caller's own active space"`, affects later turns only)
  - setting `org_nudge_words: list[str] = ["pipeline", "forecast", "my deals", "my reps", "quota", "deal risk"]`

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_space_chat.py`:
```python
from __future__ import annotations

import pytest

from mavis.domain.events import Event, EventType
from mavis.revintel import spaces
from mavis.revintel.commands import begin_turn, needs_org_nudge, run_org_command, switch_space_tool
from mavis.revintel.domain import PERSONAL


def ev(user_id, text):
    return Event(id=f"e-{abs(hash(text))}", type=EventType.MESSAGE_RECEIVED, user_id=user_id, payload={"text": text})


async def test_space_command_lists_spaces_with_buttons(world, orgs_on, sent):
    assert await run_org_command(ev(world.u["Raj"].id, "/space")) is True
    out = sent[-1]
    assert "Personal" in out.text and "Acme" in out.text
    data = [b.data for row in out.buttons for b in row]
    assert data == ["sp:personal", f"sp:org:{world.org.id}"]


async def test_space_command_is_ignored_when_orgs_are_off(world, settings):
    assert await run_org_command(ev(world.u["Raj"].id, "/space")) is False


async def test_not_a_command_falls_through(world, orgs_on):
    assert await run_org_command(ev(world.u["Raj"].id, "what is on today")) is False


async def test_begin_turn_prefix_is_one_shot(world, orgs_on):
    uid, org = world.u["Raj"].id, f"org:{world.org.id}"
    text, space = await begin_turn(uid, "@acme how is the Initech deal?")
    assert (text, space) == ("how is the Initech deal?", org) and spaces.current_space.get() == org
    assert await spaces.active_space(uid) == PERSONAL  # nothing persisted
    text, space = await begin_turn(uid, "@acme.com is a domain")
    assert space == PERSONAL


async def test_begin_turn_uses_the_active_space(world, orgs_on):
    uid = world.u["Rin"].id
    await spaces.switch_space(uid, f"org:{world.org.id}")
    assert (await begin_turn(uid, "pipeline please"))[1] == f"org:{world.org.id}"


async def test_switch_space_tool_changes_later_turns_and_checks_membership(world, orgs_on, user):
    uid = world.u["Raj"].id
    assert "Acme" in await switch_space_tool(uid, f"org:{world.org.id}")
    assert await spaces.active_space(uid) == f"org:{world.org.id}"
    assert "not part of" in await switch_space_tool(user.id, f"org:{world.org.id}")
    assert "Personal" in await switch_space_tool(uid, "personal")


@pytest.mark.parametrize(("text", "hit"), [
    ("how is Initech doing", True), ("what is my pipeline", True), ("show my reps", True),
    ("what is for dinner", False), ("remind me about the pipeline in the bathroom", True),
])
def test_nudge_matcher_is_deterministic(text, hit):
    assert needs_org_nudge(text, ["Initech", "Globex"], ["pipeline", "my reps"]) is hit


async def test_personal_turn_with_org_words_gets_a_one_tap_offer(world, orgs_on, sent):
    from mavis.revintel.commands import maybe_nudge

    assert await maybe_nudge(world.u["Raj"].id, "what is my pipeline", PERSONAL) is True
    out = sent[-1]
    assert "Switch for this question?" in out.text and out.buttons[0][0].data.startswith("sp:once:")
    assert await maybe_nudge(world.u["Raj"].id, "what is my pipeline", f"org:{world.org.id}") is False
```
(`sent` is the existing `tests/conftest.py` fixture: a list of every `Outbound` passed to `outbox.enqueue`.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_space_chat.py -q`
Expected: FAIL (`No module named 'mavis.revintel.commands'`).

- [ ] **Step 3: Implement**

`src/mavis/revintel/commands.py`:
```python
"""Chat commands for spaces. Deterministic; the model never guesses a space (spec 3.5)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from mavis.config import get_settings
from mavis.domain.events import Event
from mavis.domain.messages import Button, Outbound
from mavis.revintel import spaces
from mavis.revintel.domain import PERSONAL, parse_space
from mavis.revintel.mode import orgs_on
from mavis.revintel.repo import orgs
from mavis.store.repo import outbox


def _say(user_id: int, text: str, buttons=None, key: str | None = None) -> Outbound:
    return Outbound(user_id=user_id, text=text, buttons=buttons or [], dedupe_key=key)


async def run_org_command(event: Event) -> bool:
    if not orgs_on():
        return False
    text = str(event.payload.get("text", "")).strip().lower()
    if text.split("@")[0] != "/space":
        return False
    infos = await spaces.spaces_of(event.user_id)
    active = await spaces.active_space(event.user_id)
    rows = [[Button(label=("* " if i.space == active else "") + (f"{i.label} ({i.role.value})" if i.role else i.label),
                    data=f"sp:{i.space}")] for i in infos]
    await outbox.enqueue_now(_say(event.user_id, "Where do you want to work?", rows, key=f"space:{event.id}"))
    return True


async def begin_turn(user_id: int, text: str) -> tuple[str, str]:
    """Set this turn's space. A leading @handle runs just this turn there; nothing is stored."""
    if not orgs_on():
        return text, PERSONAL
    mine = {}
    for m in await orgs.memberships_of(user_id):
        org = await orgs.get_org(m.org_id)
        mine[org.slug] = m.org_id
    prefixed, rest = spaces.parse_prefix(text, mine)
    space = prefixed or await spaces.active_space(user_id)
    spaces.current_space.set(space)
    return (rest if prefixed else text), space


async def switch_space_tool(user_id: int, space: str) -> str:
    try:
        await spaces.switch_space(user_id, space)
    except ValueError:
        return "You are not part of that workspace."
    if space == PERSONAL:
        return "Switched to Personal."
    return f"Switched to {(await orgs.get_org(parse_space(space))).name}."


def needs_org_nudge(text: str, entity_names: Iterable[str], words: Iterable[str]) -> bool:
    low = (text or "").lower()
    terms = [n.lower() for n in entity_names if len(n) >= 3] + [w.lower() for w in words]
    return any(re.search(rf"(?<!\w){re.escape(t)}(?!\w)", low) for t in terms)


async def maybe_nudge(user_id: int, text: str, space: str) -> bool:
    """In personal space, an org-shaped request gets a one-tap offer instead of a personal answer."""
    if space != PERSONAL or not orgs_on():
        return False
    for m in await orgs.memberships_of(user_id):
        org = await orgs.get_org(m.org_id)
        if needs_org_nudge(text, await _entity_names(m.org_id), get_settings().org_nudge_words):
            tag = hashlib.sha1(text.encode()).hexdigest()[:8]
            await outbox.enqueue_now(_say(
                user_id, f"That sounds like {org.name}. Switch for this question?",
                [[Button(label=f"Yes, {org.name}", data=f"sp:once:org:{org.id}:{tag}"),
                  Button(label="No", data="sp:personal")]], key=f"nudge:{user_id}:{tag}"))
            return True
    return False


async def _entity_names(org_id: int) -> list[str]:
    try:
        from mavis.revintel.repo import rev

        return await rev.entity_names(org_id)
    except ImportError:  # Phase B tables arrive later; the fixed word list still works
        return []


async def space_button(event: Event, data: str) -> None:
    arg = data[len("sp:"):]
    if arg.startswith("once:"):  # run the remembered question once: handled by the turn that follows
        return
    try:
        await spaces.switch_space(event.user_id, arg)
    except ValueError:
        return
    await outbox.enqueue_now(_say(event.user_id, "Switched." if arg != PERSONAL else "Switched to Personal.",
                                  key=f"space-ack:{event.id}"))


def register() -> None:
    from mavis.agents.buttons import register_button_handler

    register_button_handler("sp:", space_button)
```

`src/mavis/config.py` (in the Track 6 block): `org_nudge_words: list[str] = ["pipeline", "forecast", "my deals", "my reps", "quota", "deal risk"]`.

`src/mavis/agents/commands.py`, first lines of `run_command(event, flow=None)`:
```python
    from mavis.revintel.commands import run_org_command  # Track 6: /space (a no-op unless ORGS_ENABLED)

    if await run_org_command(event):
        return True
```
`src/mavis/agents/conversation.py`, in `run_turn` right after `user = await users.get(event.user_id)`:
```python
    if orgs_on():  # Track 6: set this turn's space; an @handle prefix is stripped and applies to this turn only
        text, _space = await org_commands.begin_turn(user.id, text)
        if await org_commands.maybe_nudge(user.id, text, _space):
            return
```
with imports `from mavis.revintel import commands as org_commands` and `from mavis.revintel.mode import orgs_on` at the top of the module.

`src/mavis/tools/chat_tools.py`: register
```python
class SwitchSpaceArgs(ToolArgs):
    space: str = Field(description="'personal' or 'org:<id>' as listed by /space")


async def _switch_space(user_id: int, args: SwitchSpaceArgs) -> str:
    from mavis.revintel.commands import switch_space_tool

    return await switch_space_tool(user_id, args.space)
```
and `MavisTool("switch_space", "Switch the user's active workspace for later messages (personal or an org). Use only when the user asks to switch.", SwitchSpaceArgs, RiskClass.WRITE_SELF, _switch_space, _a("conversation"), exempt_reason="changes only the caller's own active space")` added to the chat tool list, guarded by `orgs_on()` in the `available` hook (`tooling.install` makes it visible in both spaces because of `exempt_reason`). Call `org_commands.register()` from `register_default_handlers()` in `worker/handlers.py` (one line, additive).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel/test_space_chat.py tests/agents -q && uv run ruff check src tests`
Expected: PASS (defaults-unchanged: with `ORGS_ENABLED=false` `run_org_command` returns False and `begin_turn` is never called).

- [ ] **Step 5: Commit**

```bash
git add src/mavis/revintel/commands.py src/mavis/agents src/mavis/tools/chat_tools.py src/mavis/config.py src/mavis/worker/handlers.py tests/revintel/test_space_chat.py
git commit -m "feat(revintel): switch spaces in chat with /space, @org prefix and switch_space"
```

---

### Task 11: Row-level security, `org_tx` and the append-only grants (Postgres)

**Files:**
- Create: `src/mavis/revintel/rls.py`, `src/mavis/migrations/versions/<NN+2>_revintel_rls.py`, `tests/revintel/pg.py`, `tests/revintel/test_rls.py`
- Modify: `pyproject.toml` (`pg` marker)

**Interfaces:**
- Produces:
  - `rls.rls_sql(table: str, col: str = "org_id", allow_null: bool = False) -> list[str]` (enable, force, policy `org_isolation`, grant to `mavis_app`)
  - `rls.ORG_TABLES: list[tuple[str, str, bool]]` (table, column, allow_null); later tasks append theirs and call `rls_sql` from their own migration
  - `async rls.org_tx(org_id: int)` async context manager yielding an `AsyncSession` inside one transaction; on Postgres it runs `SELECT set_config('app.org_id', :o, true)` first (transaction-local, so a pooled connection cannot carry it over); on SQLite it is a plain transaction
  - `tests/revintel/pg.py`: `pg` (skip marker unless `TEST_PG_URL` is set), `fresh_database() -> str` (drops and recreates schema `public`, runs `alembic upgrade head`, returns the sync DSN)
- Migration `revintel_rls` (Postgres only; a no-op elsewhere): creates NOLOGIN role `mavis_app` if missing; applies `rls_sql` to every table in `ORG_TABLES`; makes `org_audit_log` append-only with `REVOKE UPDATE, DELETE, TRUNCATE ... FROM mavis_app` and a trigger `org_audit_log_immutable` that raises on UPDATE, DELETE and TRUNCATE for every role.

- [ ] **Step 1: Write the failing test**

Add `"pg: needs TEST_PG_URL (a throwaway Postgres)"` to `markers` in `pyproject.toml`.

`tests/revintel/pg.py`:
```python
from __future__ import annotations

import os

import psycopg
import pytest

PG_URL = os.environ.get("TEST_PG_URL", "")  # e.g. postgresql+psycopg://postgres:pw@localhost/mavis_test
pg = pytest.mark.skipif(not PG_URL, reason="TEST_PG_URL not set")


def dsn() -> str:
    return PG_URL.replace("+psycopg", "")


def fresh_database() -> str:
    from mavis.store.migrate import upgrade

    with psycopg.connect(dsn(), autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
    upgrade(PG_URL, "head")
    return dsn()
```

`tests/revintel/test_rls.py`:
```python
from __future__ import annotations

import psycopg
import pytest

from tests.revintel.pg import fresh_database, pg

pytestmark = pg


@pytest.fixture(scope="module")
def conn_str():
    return fresh_database()


@pytest.fixture
def owner(conn_str):
    with psycopg.connect(conn_str, autocommit=True) as c:
        c.execute("TRUNCATE teams, org_memberships, org_audit_log, orgs RESTART IDENTITY CASCADE")
        c.execute("INSERT INTO orgs (name, slug, status, base_currency, fiscal_start_month, internal_domains,"
                  " settings, features, authz_version, created_at) VALUES"
                  " ('A','a','active','USD',1,'[]','{}','{}',1,now()),('B','b','active','USD',1,'[]','{}','{}',1,now())")
        c.execute("INSERT INTO teams (org_id, name, attrs) VALUES (1,'ta','{}'),(2,'tb','{}')")
        yield c


def as_app(conn_str):
    c = psycopg.connect(conn_str, autocommit=True)
    c.execute("SET ROLE mavis_app")
    return c


def test_no_org_setting_sees_nothing(owner, conn_str):
    with as_app(conn_str) as c:
        assert c.execute("SELECT count(*) FROM teams").fetchone()[0] == 0


def test_only_the_set_org_is_visible(owner, conn_str):
    with as_app(conn_str) as c:
        c.execute("SELECT set_config('app.org_id', '2', false)")
        assert [r[0] for r in c.execute("SELECT name FROM teams")] == ["tb"]
        assert c.execute("SELECT count(*) FROM orgs").fetchone()[0] == 1


def test_the_setting_is_transaction_local(owner, conn_str):
    with as_app(conn_str) as c:
        c.autocommit = False
        c.execute("SELECT set_config('app.org_id', '1', true)")
        assert c.execute("SELECT count(*) FROM teams").fetchone()[0] == 1
        c.commit()
        assert c.execute("SELECT count(*) FROM teams").fetchone()[0] == 0  # a pooled reuse sees nothing


def test_cross_org_insert_is_refused(owner, conn_str):
    with as_app(conn_str) as c:
        c.execute("SELECT set_config('app.org_id', '1', false)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("INSERT INTO teams (org_id, name, attrs) VALUES (2,'x','{}')")


def test_audit_log_is_append_only_for_every_role(owner, conn_str):
    owner.execute("INSERT INTO org_audit_log (org_id, seq, ts, channel, action, resource_ids, resource_count,"
                  " decision, reason, request_id, detail, prev_hash, row_hash) VALUES"
                  " (1,1,now(),'telegram','x','[]',0,'allow','','','{}','0','h')")
    for sql in ("UPDATE org_audit_log SET reason='z'", "DELETE FROM org_audit_log", "TRUNCATE org_audit_log"):
        with pytest.raises(psycopg.Error):
            owner.execute(sql)
    with as_app(conn_str) as c:
        c.execute("SELECT set_config('app.org_id', '1', false)")
        with pytest.raises(psycopg.Error):
            c.execute("UPDATE org_audit_log SET reason='z'")


def test_every_table_with_an_org_id_forces_rls(owner):
    rows = owner.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid"
        " WHERE a.attname = 'org_id' AND c.relkind = 'r' AND c.relnamespace = 'public'::regnamespace"
        " AND NOT c.relforcerowsecurity").fetchall()
    assert rows == []
```
and a SQLite-side test appended to `tests/revintel/test_orgs_repo.py`... instead put in `tests/revintel/test_rls_local.py`:
```python
from mavis.revintel.rls import ORG_TABLES, org_tx, rls_sql


async def test_org_tx_is_a_plain_transaction_on_sqlite(world):
    from sqlalchemy import text

    async with org_tx(world.org.id) as s:
        assert (await s.execute(text("SELECT 1"))).scalar() == 1


def test_rls_sql_shape():
    stmts = rls_sql("teams")
    assert any("FORCE ROW LEVEL SECURITY" in s for s in stmts) and any("app.org_id" in s for s in stmts)
    assert "IS NULL OR" in "".join(rls_sql("approval_policies", allow_null=True))
    assert ("orgs", "id", False) in ORG_TABLES
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_rls_local.py -q`
Expected: FAIL (`No module named 'mavis.revintel.rls'`). The `pg` tests skip without `TEST_PG_URL`; run them with `TEST_PG_URL=... uv run pytest tests/revintel/test_rls.py -q` against a throwaway database.

- [ ] **Step 3: Implement**

`src/mavis/revintel/rls.py`:
```python
"""Postgres row-level security on org tables (spec 7.1): a missing WHERE can never cross orgs."""

from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy import text

from mavis.store.db import Session

# (table, key column, org_id may be NULL for platform rows). Later tasks append their tables here and call
# rls_sql() from their own migration; tests/revintel/test_rls.py proves every org_id table is covered.
ORG_TABLES: list[tuple[str, str, bool]] = [
    ("orgs", "id", False), ("teams", "org_id", False), ("org_memberships", "org_id", False),
    ("org_role_overrides", "org_id", False), ("territory_rules", "org_id", False),
    ("resource_shares", "org_id", False), ("approval_policies", "org_id", True),
    ("org_connections", "org_id", False), ("org_audit_log", "org_id", False),
]


def rls_sql(table: str, col: str = "org_id", allow_null: bool = False) -> list[str]:
    cond = f"{col} = NULLIF(current_setting('app.org_id', true), '')::int"
    if allow_null:
        cond = f"{col} IS NULL OR {cond}"
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS org_isolation ON {table}",
        f"CREATE POLICY org_isolation ON {table} USING ({cond}) WITH CHECK ({cond})",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO mavis_app",
    ]


@asynccontextmanager
async def org_tx(org_id: int):
    """One transaction scoped to one org. `set_config(..., true)` is transaction-local on purpose."""
    async with Session() as s, s.begin():
        if s.bind is not None and s.bind.dialect.name == "postgresql":
            await s.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org_id)})
        yield s
```

`src/mavis/migrations/versions/<NN+2>_revintel_rls.py`:
```python
"""Track 6: row-level security on org tables and an immutable audit log (Postgres only)."""

from __future__ import annotations

from alembic import op

from mavis.revintel.rls import ORG_TABLES, rls_sql

revision = "revintel_rls"
down_revision = "revintel_audit"
branch_labels = None
depends_on = None

TRIGGER = """
CREATE OR REPLACE FUNCTION org_audit_log_immutable() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'org_audit_log is append-only'; END; $$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mavis_app') THEN "
               "CREATE ROLE mavis_app NOLOGIN; END IF; END $$")
    op.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mavis_app")
    for table, col, allow_null in ORG_TABLES:
        for stmt in rls_sql(table, col, allow_null):
            op.execute(stmt)
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON org_audit_log FROM mavis_app")
    op.execute(TRIGGER)
    op.execute("CREATE TRIGGER org_audit_log_rows BEFORE UPDATE OR DELETE ON org_audit_log "
               "FOR EACH ROW EXECUTE FUNCTION org_audit_log_immutable()")
    op.execute("CREATE TRIGGER org_audit_log_truncate BEFORE TRUNCATE ON org_audit_log "
               "FOR EACH STATEMENT EXECUTE FUNCTION org_audit_log_immutable()")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER IF EXISTS org_audit_log_truncate ON org_audit_log")
    op.execute("DROP TRIGGER IF EXISTS org_audit_log_rows ON org_audit_log")
    op.execute("DROP FUNCTION IF EXISTS org_audit_log_immutable()")
    for table, _col, _n in ORG_TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
```
Note: the audit tests `TRUNCATE teams, ..., org_audit_log` in the `owner` fixture would hit the trigger; the fixture therefore disables it for setup: use `ALTER TABLE org_audit_log DISABLE TRIGGER USER` before the TRUNCATE and `ENABLE TRIGGER USER` after (add these two statements to the fixture).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel/test_rls_local.py tests/store/test_migrations.py -q`; and, against a throwaway Postgres, `TEST_PG_URL=postgresql+psycopg://postgres:pw@localhost/mavis_test uv run pytest tests/revintel/test_rls.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/mavis/revintel/rls.py src/mavis/migrations/versions tests/revintel
git commit -m "feat(revintel): row-level security, org_tx and an immutable audit log"
```

---

### Task 12: Manager and admin approvals, self-approval refusal, escalation, revalidation

**Files:**
- Create: `src/mavis/revintel/approvals.py`, `src/mavis/migrations/versions/<NN+3>_revintel_approvals.py`, `tests/revintel/test_org_approvals.py`
- Modify: `src/mavis/store/models.py` (`PendingApproval` columns), `src/mavis/store/repo/approvals.py` (`create(..., org=None)`), `src/mavis/tools/registry.py` (`approvals.create(..., org=req.org)`), `src/mavis/policy/approvals.py` (`send_approval_prompt` first lines), `src/mavis/revintel/tooling.py` (`revalidate_tool` uses `approvals.revalidate`), `src/mavis/config.py` (`org_approval_escalate_s: int = 14400`)
- Shared: `policy/approvals.py`, `store/repo/approvals.py` (Track 1 and connectors also edit them; hunks additive)

**Interfaces:**
- Produces:
  - `pending_approvals` columns: `org_id int null (index)`, `org_meta JSON null`, `approver_role str(16) null`, `approver_user_id int null`
  - `repo.approvals.create(..., *, tainted=False, org: dict | None = None)` stores `org_id`, `org_meta`, `approver_role`
  - `approvals.approver_candidates(org_id, requester_id, mode) -> list[int]` (requester's team chain upward, nearest manager first, never the requester; then, if none, active members with a role that may decide this mode: `manager` mode takes manager/admin/owner, `admin` mode takes admin/owner)
  - `approvals.can_decide(role: Role, mode: str, *, approver_id: int, requester_id: int) -> bool` (pure)
  - `async approvals.take_over(approval) -> bool` (True when the approval has `org_meta.mode` of `manager` or `admin`: sends the approver card, tells the requester "That needs your manager's OK. I've asked {name}.", books escalation and expiry wakeups; False for `confirm`, which keeps the normal requester card)
  - button prefix `ra:<id>:ok|no|ask`; `async approvals.handle_button(event, data)`
  - `async approvals.revalidate(tool, user_id, args, meta) -> str | None`: refusal text when the membership is gone or its role differs from `meta["role"]`, when the action is no longer allowed, or when the policy now resolves stricter than `meta["mode"]`; writes an audit row `cancelled_revalidation`
  - wakeup kind `system_org_approval_escalate`

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_org_approvals.py`:
```python
from __future__ import annotations

from datetime import timedelta

import pytest

from mavis.domain.events import Event, EventType
from mavis.domain.policy import RiskClass
from mavis.revintel import approvals as oa
from mavis.revintel import audit
from mavis.revintel.domain import Role
from mavis.revintel.repo import orgs
from mavis.store.db import utcnow
from mavis.store.repo import approvals as repo


def meta(world, role="rep", mode="manager", action="deal.write", version=None):
    return {"org_id": world.org.id, "authz_version": version or 1, "action": action, "role": role, "mode": mode,
            "approver_role": "manager", "policy_id": -1, "expires_after_s": 86400}


async def make(world, requester="Raj", **kw):
    aid = await repo.create(world.u[requester].id, None, "rev_note", {"deal_key": "d1"}, "Add a note to Initech",
                            utcnow() + timedelta(hours=24), org=meta(world, **kw))
    return await repo.get(aid)


async def test_candidates_walk_the_team_chain_then_fall_back(world):
    assert await oa.approver_candidates(world.org.id, world.u["Raj"].id, "manager") == [world.u["Mei"].id]
    assert await oa.approver_candidates(world.org.id, world.u["Mei"].id, "manager") == [
        world.u["Ada"].id, world.u["Olu"].id]  # no manager above her: admins and owners
    assert await oa.approver_candidates(world.org.id, world.u["Raj"].id, "admin") == [
        world.u["Ada"].id, world.u["Olu"].id]


@pytest.mark.parametrize(("role", "mode", "ok"), [
    (Role.MANAGER, "manager", True), (Role.ADMIN, "manager", True), (Role.OWNER, "admin", True),
    (Role.MANAGER, "admin", False), (Role.REP, "manager", False), (Role.VIEWER, "manager", False),
])
def test_who_may_decide(role, mode, ok):
    assert oa.can_decide(role, mode, approver_id=2, requester_id=1) is ok
    assert oa.can_decide(role, mode, approver_id=1, requester_id=1) is False  # never your own request


async def test_take_over_asks_the_manager_and_tells_the_requester(world, orgs_on, sent):
    a = await make(world)
    assert await oa.take_over(a) is True
    to_mei = [m for m in sent if m.user_id == world.u["Mei"].id]
    assert "Raj" in to_mei[0].text and "Acme" in to_mei[0].text
    assert [b.data for b in to_mei[0].buttons[0]] == [f"ra:{a.id}:ok", f"ra:{a.id}:no", f"ra:{a.id}:ask"]
    assert any(m.user_id == world.u["Raj"].id and "I've asked Mei" in m.text for m in sent)


async def test_confirm_mode_keeps_the_requesters_own_card(world, orgs_on, sent):
    a = await make(world, mode="confirm")
    assert await oa.take_over(a) is False and sent == []


async def test_requester_cannot_approve_their_own_request(world, orgs_on, sent, monkeypatch):
    a = await make(world)
    resumed = []
    monkeypatch.setattr("mavis.policy.approvals._resume", lambda *x, **k: _rec(resumed, x))
    ev = Event(id="b1", type=EventType.BUTTON_PRESSED, user_id=world.u["Raj"].id, payload={"data": f"ra:{a.id}:ok"})
    await oa.handle_button(ev, f"ra:{a.id}:ok")
    assert resumed == [] and (await repo.get(a.id)).status == "pending"
    rows = await _deny_rows(world)
    assert [r.reason for r in rows] == ["self_approval"]


async def _rec(bucket, x):
    bucket.append(x)


async def _deny_rows(world):
    from sqlalchemy import select

    from mavis.revintel.models import OrgAuditLog
    from mavis.store.db import Session

    async with Session() as s:
        return list(await s.scalars(select(OrgAuditLog).where(OrgAuditLog.decision == "deny")))


async def test_manager_approval_resumes_once_as_the_requester(world, orgs_on, sent, monkeypatch):
    a = await make(world)
    resumed = []
    monkeypatch.setattr("mavis.policy.approvals._resume", lambda ap, d, *x: _rec(resumed, (ap.id, ap.user_id, d)))
    ev = Event(id="b2", type=EventType.BUTTON_PRESSED, user_id=world.u["Mei"].id, payload={})
    await oa.handle_button(ev, f"ra:{a.id}:ok")
    await oa.handle_button(ev, f"ra:{a.id}:ok")  # double tap
    assert resumed == [(a.id, world.u["Raj"].id, "ok")]  # runs for Raj, not Mei
    assert (await repo.get(a.id)).approver_user_id == world.u["Mei"].id
    assert (await audit.verify_chain(world.org.id)).ok


async def test_a_manager_outside_the_chain_is_refused(world, orgs_on, sent, monkeypatch):
    a = await make(world)  # Raj is East: Wen manages West
    resumed = []
    monkeypatch.setattr("mavis.policy.approvals._resume", lambda ap, d, *x: _rec(resumed, d))
    await oa.handle_button(Event(id="b3", type=EventType.BUTTON_PRESSED, user_id=world.u["Sol"].id, payload={}),
                           f"ra:{a.id}:ok")
    assert resumed == []


async def test_revalidation_cancels_after_a_role_change(world, orgs_on):
    a = await make(world, role="rep")
    tool = type("T", (), {"action": "deal.write", "resource_from": None, "name": "rev_note", "action_class": "crm.note"})()
    assert await oa.revalidate(tool, world.u["Raj"].id, None, a.org_meta) is None
    await orgs.set_role(world.org.id, world.u["Raj"].id, Role.VIEWER, actor_user_id=world.u["Olu"].id)
    assert "access changed" in await oa.revalidate(tool, world.u["Raj"].id, None, a.org_meta)
    await orgs.remove_member(world.org.id, world.u["Rin"].id, actor_user_id=world.u["Olu"].id)
    b = await make(world, requester="Rin")
    assert "no longer" in await oa.revalidate(tool, world.u["Rin"].id, None, b.org_meta)
    decisions = [r.decision for r in await _all_audit(world)]
    assert decisions.count("cancelled_revalidation") == 2


async def _all_audit(world):
    from sqlalchemy import select

    from mavis.revintel.models import OrgAuditLog
    from mavis.store.db import Session

    async with Session() as s:
        return list(await s.scalars(select(OrgAuditLog).where(OrgAuditLog.org_id == world.org.id)))
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_org_approvals.py -q`
Expected: FAIL (`create() got an unexpected keyword argument 'org'`).

- [ ] **Step 3: Implement**

Columns on `PendingApproval` (`store/models.py`), after `acknowledged_at`:
```python
    # Track 6: org approvals. org_meta = {"org_id","authz_version","action","role","mode","approver_role",...}
    org_id: Mapped[int | None] = mapped_column(Integer, index=True)
    org_meta: Mapped[dict | None] = mapped_column(JSON)
    approver_role: Mapped[str | None] = mapped_column(String(16))
    approver_user_id: Mapped[int | None] = mapped_column(Integer)
```
`store/repo/approvals.py` `create`: add `org: dict | None = None` and in the constructor `org_id=(org or {}).get("org_id"), org_meta=org, approver_role=(org or {}).get("approver_role")`. `tools/registry.py` `_call`: add `org=req.org,` to the `approvals.create(...)` call (where `req` is the caught `ApprovalRequired`). `policy/approvals.py`, first line of `send_approval_prompt` after the status check:
```python
    if approval.org_meta:  # Track 6: manager/admin approvals go to the approver, not the requester
        from mavis.revintel import approvals as org_approvals

        if await org_approvals.take_over(approval):
            return
```
Migration `<NN+3>_revintel_approvals.py` (`revision = "revintel_approvals"`, `down_revision = "revintel_rls"`):
```python
import sqlalchemy as sa
from alembic import op

revision = "revintel_approvals"
down_revision = "revintel_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("pending_approvals") as b:
        b.add_column(sa.Column("org_id", sa.Integer, nullable=True))
        b.add_column(sa.Column("org_meta", sa.JSON, nullable=True))
        b.add_column(sa.Column("approver_role", sa.String(16), nullable=True))
        b.add_column(sa.Column("approver_user_id", sa.Integer, nullable=True))
        b.create_index("ix_pending_approvals_org_id", ["org_id"])


def downgrade() -> None:
    with op.batch_alter_table("pending_approvals") as b:
        b.drop_index("ix_pending_approvals_org_id")
        for c in ("approver_user_id", "approver_role", "org_meta", "org_id"):
            b.drop_column(c)
```
`src/mavis/revintel/approvals.py`:
```python
"""Manager and admin approvals (spec 5.2): the approver decides, the requester's own account executes."""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import select, update

from mavis.config import get_settings
from mavis.domain.messages import Button, Outbound
from mavis.domain.policy import RiskClass
from mavis.revintel import approval as policy
from mavis.revintel import audit
from mavis.revintel.authz import authorize
from mavis.revintel.decision import decide
from mavis.revintel.domain import Role
from mavis.revintel.models import OrgMembership, Team
from mavis.revintel.principal import resolve_principal
from mavis.revintel.repo import orgs
from mavis.store.db import Session, utcnow
from mavis.store.models import PendingApproval, User
from mavis.store.repo import approvals as repo
from mavis.store.repo import outbox

_BTN = re.compile(r"^ra:(\d+):(ok|no|ask)$")
_DECIDERS = {"manager": {Role.MANAGER, Role.ADMIN, Role.OWNER}, "admin": {Role.ADMIN, Role.OWNER}}
ESCALATE_KIND = "system_org_approval_escalate"


def can_decide(role: Role | None, mode: str, *, approver_id: int, requester_id: int) -> bool:
    return approver_id != requester_id and role in _DECIDERS.get(mode, set())


async def approver_candidates(org_id: int, requester_id: int, mode: str) -> list[int]:
    out: list[int] = []
    async with Session() as s:
        me = await s.scalar(select(OrgMembership).where(OrgMembership.org_id == org_id,
                                                        OrgMembership.user_id == requester_id))
        teams = {t.id: t for t in await s.scalars(select(Team).where(Team.org_id == org_id))}
        active = {m.user_id: m for m in await s.scalars(select(OrgMembership).where(
            OrgMembership.org_id == org_id, OrgMembership.status == "active"))}
    team = teams.get(me.team_id) if me else None
    while team is not None and mode == "manager":
        m = active.get(team.manager_user_id or -1)
        if m and m.user_id != requester_id and m.user_id not in out and Role(m.role) in _DECIDERS["manager"]:
            out.append(m.user_id)
        team = teams.get(team.parent_team_id or -1)
    if out:
        return out
    order = {"admin": 0, "owner": 1, "manager": 2}
    pool = [m for m in active.values() if m.user_id != requester_id and Role(m.role) in _DECIDERS.get(mode, set())]
    return [m.user_id for m in sorted(pool, key=lambda m: (order.get(m.role, 9), m.user_id))]


async def _name(user_id: int) -> str:
    async with Session() as s:
        return (await s.get(User, user_id)).name or "A teammate"


async def _card(approval, to_user: int, org_name: str, requester: str) -> None:
    text = (f"[{org_name}] {requester} wants to do this and needs your OK:\n\n{approval.preview}\n\n"
            f"Why you: it needs your {approval.org_meta['mode']}'s sign-off.")
    await outbox.enqueue_now(Outbound(user_id=to_user, text=text, dedupe_key=f"ra:{approval.id}:{to_user}", buttons=[[
        Button(label="Approve", data=f"ra:{approval.id}:ok"), Button(label="Reject", data=f"ra:{approval.id}:no"),
        Button(label="Ask rep", data=f"ra:{approval.id}:ask")]]))


async def take_over(approval) -> bool:
    meta = approval.org_meta or {}
    if meta.get("mode") not in _DECIDERS:
        return False
    cands = await approver_candidates(meta["org_id"], approval.user_id, meta["mode"])
    org = await orgs.get_org(meta["org_id"])
    if not cands:
        await outbox.enqueue_now(Outbound(user_id=approval.user_id, dedupe_key=f"ra:{approval.id}:nobody",
                                          text="Nobody in your workspace can approve that yet, so I did not do it."))
        await repo.set_status(approval.id, "rejected", "no approver")
        return True
    await _card(approval, cands[0], org.name, await _name(approval.user_id))
    await outbox.enqueue_now(Outbound(user_id=approval.user_id, dedupe_key=f"ra:{approval.id}:asked",
                                      text=f"That needs your manager's OK. I've asked {await _name(cands[0])}."
                                      if meta["mode"] == "manager" else
                                      f"That needs an admin's OK. I've asked {await _name(cands[0])}."))
    await repo.mark_prompted(approval.id)
    from mavis.timers import service as timers_service

    wk = timers_service.WakeupService()
    now = utcnow()
    await wk.wake_me(approval.user_id, now + timedelta(seconds=get_settings().org_approval_escalate_s),
                     f"approval:{approval.id}", kind=ESCALATE_KIND, scale=False, dedupe_key=f"ra:{approval.id}:esc")
    await wk.wake_me(approval.user_id, now + timedelta(seconds=meta.get("expires_after_s", 86400)),
                     f"approval:{approval.id}", kind="system_approval_expire", scale=False,
                     dedupe_key=f"approval:{approval.id}:expire")
    return True


async def escalate(user_id: int, reason: str) -> None:
    aid = int(reason.split(":")[1])
    a = await repo.get(aid)
    if a is None or a.status != "pending" or not a.org_meta:
        return
    cands = await approver_candidates(a.org_meta["org_id"], a.user_id, a.org_meta["mode"])
    org = await orgs.get_org(a.org_meta["org_id"])
    for c in cands[1:2]:
        await _card(a, c, org.name, await _name(a.user_id))


async def handle_button(event, data: str) -> None:
    m = _BTN.match(data)
    if not m:
        return
    aid, act = int(m.group(1)), m.group(2)
    a = await repo.get(aid)
    if a is None or not a.org_meta or a.status != "pending":
        return
    meta, who = a.org_meta, event.user_id
    p = await resolve_principal(who, space=f"org:{meta['org_id']}")
    base = dict(actor_user_id=who, role=p.role.value if p.role else None, space=f"org:{meta['org_id']}",
                approval_id=aid, resource_type="approval", resource_ids=[aid])
    if who == a.user_id:
        await audit.append(meta["org_id"], action="approval.decide", decision="deny", reason="self_approval", **base)
        return
    ok = can_decide(p.role, meta["mode"], approver_id=who, requester_id=a.user_id) and who in (
        await approver_candidates(meta["org_id"], a.user_id, meta["mode"])) and authorize(
        p, "approval.decide", None, snapshot=await orgs.load_snapshot(meta["org_id"])).allowed
    if not ok:
        await audit.append(meta["org_id"], action="approval.decide", decision="deny", reason="not_an_approver", **base)
        return
    if act == "ask":
        await outbox.enqueue_now(Outbound(user_id=a.user_id, dedupe_key=f"ra:{aid}:ask:{who}",
                                          text=f"{await _name(who)} would like more detail before approving."))
        return
    if not await repo.claim(aid, {"pending"}, "resolving"):  # a double tap loses here
        return
    async with Session() as s:
        await s.execute(update(PendingApproval).where(PendingApproval.id == aid).values(approver_user_id=who))
        await s.commit()
    await audit.append(meta["org_id"], action="approval.decide", decision="approved" if act == "ok" else "rejected",
                       approver_user_id=who, **{k: v for k, v in base.items() if k != "actor_user_id"},
                       actor_user_id=who)
    from mavis.policy import approvals as flow

    await flow._resume(await repo.get(aid), "ok" if act == "ok" else "no")


async def revalidate(tool, user_id: int, args, meta: dict | None) -> str | None:
    """Spec 4.5: the action must still be allowed, for the same role, under a policy no stricter than approved."""
    meta = meta or {}
    org_id = meta.get("org_id")
    if org_id is None:
        return None
    p = await resolve_principal(user_id, space=f"org:{org_id}")
    why = None
    if p.org_id is None:
        why = "You are no longer a member of that workspace, so I did not do it."
    elif meta.get("role") and p.role.value != meta["role"]:
        why = "Your access changed, so I did not do it."
    else:
        resource = await tool.resource_from(args, user_id) if tool.resource_from else None
        d = decide(p, tool.action, resource, snapshot=await orgs.load_snapshot(org_id),
                   rows=await policy.rows_for(org_id), action_class=tool.action_class, risk=RiskClass.OUTWARD,
                   tainted=False)
        stricter = d.approval and policy.MODES.index(d.approval.mode) > policy.MODES.index(meta.get("mode", "confirm"))
        if not d.allowed or stricter:
            why = "Your access changed, so I did not do it."
    if why:
        await audit.append(org_id, action=meta.get("action", "unknown"), decision="cancelled_revalidation",
                           actor_user_id=user_id, reason="revalidation", space=f"org:{org_id}")
    return why


def register() -> None:
    from mavis.agents.buttons import register_button_handler
    from mavis.timers.system import register_system_wakeup

    register_button_handler("ra:", handle_button)
    register_system_wakeup(ESCALATE_KIND, escalate)
```
`tooling.py`: replace the body of `revalidate_tool` with `from mavis.revintel import approvals; return await approvals.revalidate(tool, user_id, args, meta)` (and drop its `decide`/`RiskClass` imports). `config.py`: `org_approval_escalate_s: int = 14400`. `worker/handlers.py`: call `revintel.approvals.register()` beside `org_commands.register()`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel tests/policy tests/store -q && uv run ruff check src tests`
Expected: PASS (defaults-unchanged: personal approvals have `org_meta=None`, so `take_over` is never called).

- [ ] **Step 5: Commit**

```bash
git add src/mavis tests/revintel/test_org_approvals.py
git commit -m "feat(revintel): manager and admin approvals with revalidation at execution"
```

---

### Task 13: `/audit` and read-only `/org` commands

**Files:**
- Modify: `src/mavis/revintel/commands.py` (extend `run_org_command`)
- Test: `tests/revintel/test_org_commands.py`

**Interfaces:**
- Consumes: Tasks 3, 5, 6, 8.
- Produces: `/audit [days=7] [user=<id>] [action=<x>] [decision=<d>]` (needs `audit.read`; replies with counts by decision and action and attaches a CSV `artifacts/o<org>/u<user>/audit-<yyyymmdd-hhmm>.csv`; the read is itself audited as `audit.read`/`allow`; a denied caller gets "Your role doesn't allow that." and a `deny` row), `/org` (status), `/org members`, `/org teams` (need `member.manage` or `team.manage` for the member list; `/org status` is open to every member: name, your role, member count, enabled features), `audit.csv_rows(org_id, days, user, action, decision) -> list[dict]`.

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_org_commands.py`:
```python
from __future__ import annotations

import csv

from mavis.domain.events import Event, EventType
from mavis.revintel import audit, spaces
from mavis.revintel.commands import run_org_command


def ev(uid, text):
    return Event(id=f"e{abs(hash((uid, text)))}", type=EventType.MESSAGE_RECEIVED, user_id=uid, payload={"text": text})


async def enter(world, name):
    await spaces.switch_space(world.u[name].id, f"org:{world.org.id}")
    return world.u[name].id


async def test_admin_reads_the_audit_and_the_read_is_audited(world, orgs_on, sent):
    uid = await enter(world, "Ada")
    await audit.append(world.org.id, action="deal.read", decision="deny", actor_user_id=5, reason="out_of_scope")
    assert await run_org_command(ev(uid, "/audit days=7")) is True
    msg = sent[-1]
    assert "deny: 1" in msg.text and msg.document_path
    rows = list(csv.DictReader(open(msg.document_path)))
    assert {"seq", "ts", "action", "decision", "reason"} <= set(rows[0]) and any(r["reason"] == "out_of_scope" for r in rows)
    assert (await audit.verify_chain(world.org.id)).ok
    last = (await _rows(world))[-1]
    assert (last.action, last.decision, last.actor_user_id) == ("audit.read", "allow", uid)


async def test_a_rep_cannot_read_the_audit(world, orgs_on, sent):
    uid = await enter(world, "Raj")
    await run_org_command(ev(uid, "/audit"))
    assert sent[-1].text == "Your role doesn't allow that." and sent[-1].document_path is None


async def test_audit_filters(world, orgs_on, sent):
    uid = await enter(world, "Olu")
    await audit.append(world.org.id, action="deal.read", decision="allow", actor_user_id=7)
    await audit.append(world.org.id, action="deal.write", decision="deny", actor_user_id=8)
    await run_org_command(ev(uid, "/audit decision=deny user=8"))
    rows = list(csv.DictReader(open(sent[-1].document_path)))
    assert [r["action"] for r in rows] == ["deal.write"]


async def test_org_status_is_open_and_members_is_not(world, orgs_on, sent):
    rep = await enter(world, "Raj")
    await run_org_command(ev(rep, "/org"))
    assert "Acme" in sent[-1].text and "rep" in sent[-1].text
    await run_org_command(ev(rep, "/org members"))
    assert sent[-1].text == "Your role doesn't allow that."
    owner = await enter(world, "Olu")
    await run_org_command(ev(owner, "/org members"))
    assert "Raj" in sent[-1].text and "rep" in sent[-1].text and "Mei" in sent[-1].text


async def test_commands_in_personal_space_point_to_the_switch(world, orgs_on, sent):
    await run_org_command(ev(world.u["Ada"].id, "/audit"))
    assert "Switch to your org space" in sent[-1].text


async def _rows(world):
    from sqlalchemy import select

    from mavis.revintel.models import OrgAuditLog
    from mavis.store.db import Session

    async with Session() as s:
        return list(await s.scalars(select(OrgAuditLog).where(OrgAuditLog.org_id == world.org.id)
                                    .order_by(OrgAuditLog.seq)))
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_org_commands.py -q`
Expected: FAIL (the commands return False).

- [ ] **Step 3: Implement**

In `src/mavis/revintel/audit.py` add:
```python
async def rows_since(org_id: int, days: int, *, user: int | None = None, action: str | None = None,
                     decision: str | None = None, limit: int = 5000) -> list[OrgAuditLog]:
    from datetime import timedelta

    cutoff = utcnow() - timedelta(days=days)
    q = select(OrgAuditLog).where(OrgAuditLog.org_id == org_id, OrgAuditLog.ts >= cutoff)
    if user is not None:
        q = q.where(OrgAuditLog.actor_user_id == user)
    if action:
        q = q.where(OrgAuditLog.action == action)
    if decision:
        q = q.where(OrgAuditLog.decision == decision)
    async with Session() as s:
        return list(await s.scalars(q.order_by(OrgAuditLog.seq).limit(limit)))
```
In `src/mavis/revintel/commands.py` replace the `/space`-only body of `run_org_command` with a dispatcher and add the handlers:
```python
import csv
from collections import Counter
from datetime import datetime

from mavis.revintel import audit
from mavis.revintel.authz import authorize
from mavis.revintel.domain import Role
from mavis.revintel.principal import resolve_principal
from mavis.revintel.tooling import ROLE, WRONG_SPACE


def _kv(args: list[str]) -> dict[str, str]:
    return dict(a.split("=", 1) for a in args if "=" in a)


async def run_org_command(event: Event) -> bool:
    if not orgs_on():
        return False
    parts = str(event.payload.get("text", "")).strip().split()
    if not parts or not parts[0].startswith("/"):
        return False
    name, args = parts[0][1:].split("@")[0].lower(), parts[1:]
    if name == "space":
        return await _space(event)
    if name not in ("audit", "org"):
        return False
    p = await resolve_principal(event.user_id)
    if p.org_id is None:
        await outbox.enqueue_now(_say(event.user_id, WRONG_SPACE, key=f"cmd:{event.id}"))
        return True
    snap = await orgs.load_snapshot(p.org_id)
    base = dict(actor_user_id=event.user_id, role=p.role.value if p.role else None, space=p.space)
    action = {"audit": "audit.read", "org": "org.status"}[name]
    if name == "org" and args and args[0] in ("members", "teams"):
        action = "member.manage" if args[0] == "members" else "team.manage"
    if action != "org.status" and not authorize(p, action, None, snapshot=snap).allowed:
        await audit.append(p.org_id, action=action, decision="deny", reason="role_lacks_action", **base)
        await outbox.enqueue_now(_say(event.user_id, ROLE, key=f"cmd:{event.id}"))
        return True
    if name == "audit":
        await _audit(event, p, base, _kv(args))
    else:
        await _org(event, p, args)
    return True


async def _audit(event, p, base, kv) -> None:
    rows = await audit.rows_since(p.org_id, int(kv.get("days", 7)), user=int(kv["user"]) if "user" in kv else None,
                                  action=kv.get("action"), decision=kv.get("decision"))
    await audit.append(p.org_id, action="audit.read", decision="allow", detail={"rows": len(rows)}, **base)
    from mavis.config import get_settings

    folder = get_settings().artifacts_dir / f"o{p.org_id}" / f"u{event.user_id}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"audit-{datetime.now():%Y%m%d-%H%M%S}.csv"
    cols = ["seq", "ts", "actor_user_id", "role", "action", "decision", "reason", "resource_type", "resource_count"]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: getattr(r, c) for c in cols})
    by = Counter(r.decision for r in rows)
    text = f"{len(rows)} audit rows. " + ", ".join(f"{k}: {v}" for k, v in sorted(by.items()))
    await outbox.enqueue_now(Outbound(user_id=event.user_id, text=text, document_path=str(path),
                                      dedupe_key=f"cmd:{event.id}"))


async def _org(event, p, args) -> None:
    from sqlalchemy import select

    from mavis.revintel.models import OrgMembership, Team
    from mavis.store.db import Session
    from mavis.store.models import User

    org = await orgs.get_org(p.org_id)
    async with Session() as s:
        members = list(await s.execute(select(User.name, OrgMembership.role, OrgMembership.team_id)
                                       .join(OrgMembership, OrgMembership.user_id == User.id)
                                       .where(OrgMembership.org_id == p.org_id, OrgMembership.status == "active")))
        teams = list(await s.scalars(select(Team.name).where(Team.org_id == p.org_id)))
    if args and args[0] == "members":
        text = "\n".join(f"{n or 'Someone'}: {r}" for n, r, _t in members)
    elif args and args[0] == "teams":
        text = "\n".join(teams) or "No teams yet."
    else:
        feats = ", ".join(k for k, v in org.features.items() if v) or "none yet"
        text = f"{org.name}. You are {p.role.value}. {len(members)} members. Features: {feats}."
    await outbox.enqueue_now(_say(event.user_id, text, key=f"cmd:{event.id}"))
```
Rename the old `/space` body to `_space(event)` (unchanged logic). Add `("org.status")` handling note: `org.status` is not in the matrix on purpose (open to every member); the code path above skips `authorize` for it.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel -q && uv run ruff check src tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis/revintel tests/revintel/test_org_commands.py
git commit -m "feat(revintel): /audit and read-only /org commands"
```

---

### Task 14: Org invites, `/join` and member management (waits for plan 11)

**Start gate:** plan 11 merged (`invite_codes`, `invites.mint/redeem`, `register_user_command`, access gate).

**Files:**
- Create: `src/mavis/revintel/join.py`, `src/mavis/migrations/versions/<NN+4>_revintel_invites.py`, `tests/revintel/test_join.py`
- Modify: `src/mavis/store/models.py` (`InviteCode.org_id/org_role/team_id`), `src/mavis/access/gate.py` (one call after a successful redeem), `src/mavis/worker/handlers.py` (register `org`/`join` user commands)

**Interfaces:**
- Consumes: plan 11 `invites.mint(*, created_by, uses=1, days=14, tier="standard", tz=None, currency=None, label="") -> tuple[InviteCode, str]`, `invites.redeem(code, user_id, now) -> InviteCode | None`, `codes.display/normalize`, `register_user_command(name, fn)` with `CommandFn = (Event, User, list[str]) -> str | None`.
- Produces:
  - `invite_codes` columns `org_id int null`, `org_role str(16) null`, `team_id int null`
  - `join.mint_org_invite(minter_id: int, org_id: int, role: Role, *, team_name: str | None = None, uses: int = 1, days: int = 7) -> tuple[InviteCode, str]` (needs `member.manage`; the role must rank strictly below the minter's, owner never mintable; the org's member cap applies)
  - `join.after_redeem(invite, user_id: int) -> str | None` (creates the membership for an org invite, sets the user's default space to the org, returns the welcome line "You're in {org} as {role}. Say /space to switch.")
  - user commands `/join <code>` (existing active user redeems an org invite; an invalid, revoked, expired or exhausted code gets the generic invalid reply) and `/org invite role=rep team=East [uses=1] [days=7]`, `/org role <name> <role>`, `/org remove <name>` (each needs the matching action; audited)

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_join.py`:
```python
from __future__ import annotations

import pytest

from mavis.access import codes
from mavis.revintel import join, spaces
from mavis.revintel.domain import Role
from mavis.revintel.repo import orgs
from mavis.store.db import utcnow
from mavis.store.repo import invites, users


async def test_admin_mints_a_rep_invite_for_a_team(world):
    inv, plain = await join.mint_org_invite(world.u["Ada"].id, world.org.id, Role.REP, team_name="West", uses=2)
    assert (inv.org_id, inv.org_role, inv.team_id, inv.max_uses) == (world.org.id, "rep", world.teams.west.id, 2)
    assert codes.normalize(plain)


@pytest.mark.parametrize(("minter", "role", "ok"), [
    ("Olu", Role.ADMIN, True), ("Ada", Role.ADMIN, False), ("Ada", Role.OWNER, False),
    ("Olu", Role.OWNER, False), ("Raj", Role.REP, False), ("Mei", Role.REP, False),
])
async def test_role_rules(world, minter, role, ok):
    if ok:
        await join.mint_org_invite(world.u[minter].id, world.org.id, role)
    else:
        with pytest.raises(PermissionError):
            await join.mint_org_invite(world.u[minter].id, world.org.id, role)


async def test_existing_user_joins_with_a_code_and_gets_the_org_as_default(world):
    newbie, _ = await users.get_or_create_by_chat(777, "Tomas")
    inv, plain = await join.mint_org_invite(world.u["Olu"].id, world.org.id, Role.REP, team_name="East")
    reply = await join.join_command(newbie.id, plain)
    assert "Acme" in reply and "rep" in reply
    m = await orgs.membership(world.org.id, newbie.id)
    assert (m.role, m.team_id) == ("rep", world.teams.east.id)
    assert await spaces.active_space(newbie.id) == f"org:{world.org.id}"


async def test_bad_codes_get_the_generic_reply_and_single_use_codes_burn(world):
    a, _ = await users.get_or_create_by_chat(778, "Aiko")
    b, _ = await users.get_or_create_by_chat(779, "Priya")
    inv, plain = await join.mint_org_invite(world.u["Olu"].id, world.org.id, Role.VIEWER)
    assert "Welcome" in await join.join_command(a.id, plain) or "in Acme" in await join.join_command(b.id, plain)
    generic = await join.join_command(b.id, "ZZZZZZZZZZ")
    assert await join.join_command(b.id, plain) == generic  # exhausted looks like unknown


async def test_a_personal_invite_does_not_create_a_membership(world):
    u, _ = await users.get_or_create_by_chat(780, "Rin2")
    inv, plain = await invites.mint(created_by=world.u["Olu"].id)
    assert await join.after_redeem(inv, u.id) is None
    assert await orgs.memberships_of(u.id) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_join.py -q`
Expected: FAIL (`No module named 'mavis.revintel.join'`).

- [ ] **Step 3: Implement**

Model (`store/models.py`, `InviteCode`):
```python
    org_id: Mapped[int | None] = mapped_column(Integer, default=None)  # Track 6: an org invite
    org_role: Mapped[str | None] = mapped_column(String(16), default=None)
    team_id: Mapped[int | None] = mapped_column(Integer, default=None)
```
Migration `<NN+4>_revintel_invites.py` (`revision = "revintel_invites"`, `down_revision = "revintel_approvals"`): `batch_alter_table("invite_codes")` adding the three nullable columns; downgrade drops them.

`src/mavis/revintel/join.py`:
```python
"""Org invites and /join (spec 3.3). A code is the Track 5 code plus three columns."""

from __future__ import annotations

from sqlalchemy import select, update

from mavis.access import codes
from mavis.revintel import spaces
from mavis.revintel.authz import authorize
from mavis.revintel.domain import Role
from mavis.revintel.models import Team
from mavis.revintel.principal import resolve_principal
from mavis.revintel.repo import orgs
from mavis.store.db import Session, utcnow
from mavis.store.models import InviteCode
from mavis.store.repo import invites

INVALID = "That code didn't work. Check it and try again."
MAX_MEMBERS = 200


async def mint_org_invite(minter_id: int, org_id: int, role: Role, *, team_name: str | None = None, uses: int = 1,
                          days: int = 7) -> tuple[InviteCode, str]:
    p = await resolve_principal(minter_id, space=f"org:{org_id}")
    if not authorize(p, "member.manage", None, snapshot=await orgs.load_snapshot(org_id)).allowed:
        raise PermissionError("member.manage required")
    if role is Role.OWNER or role.rank >= p.role.rank:
        raise PermissionError("you can only invite roles below your own")
    team_id = None
    if team_name:
        async with Session() as s:
            team_id = await s.scalar(select(Team.id).where(Team.org_id == org_id, Team.name == team_name))
        if team_id is None:
            raise LookupError(f"no team {team_name}")
    inv, plain = await invites.mint(created_by=minter_id, uses=uses, days=days, label=f"org:{org_id}:{role.value}")
    async with Session() as s:
        await s.execute(update(InviteCode).where(InviteCode.id == inv.id).values(
            org_id=org_id, org_role=role.value, team_id=team_id))
        await s.commit()
        inv = await s.get(InviteCode, inv.id)
    return inv, plain


async def after_redeem(invite: InviteCode, user_id: int) -> str | None:
    if invite.org_id is None:
        return None
    org = await orgs.get_org(invite.org_id)
    await orgs.add_member(invite.org_id, user_id, Role(invite.org_role), team_id=invite.team_id,
                          actor_user_id=invite.created_by_user_id)
    await spaces.set_default(user_id, f"org:{invite.org_id}")
    await spaces.switch_space(user_id, f"org:{invite.org_id}")
    return f"You're in {org.name} as {invite.org_role}. Say /space to switch."


async def join_command(user_id: int, text: str) -> str:
    code = codes.normalize(text)
    inv = await invites.redeem(code, user_id, utcnow()) if code else None
    if inv is None or inv.org_id is None:
        return INVALID
    return await after_redeem(inv, user_id) or INVALID
```
Wire-up: in `access/gate.py` after a successful redemption for a pending user, `extra = await join.after_redeem(invite, user.id)` and append `extra` to the welcome; in `worker/handlers.py` call `register_user_command("join", lambda ev, u, args: join.join_command(u.id, " ".join(args)))` and the `org` write subcommands (`invite`, `role`, `remove`) through a `register_user_command("org_admin", ...)` wrapper that parses with `access.commands.parse_kv`, calls `join.mint_org_invite` / `orgs.set_role` / `orgs.remove_member`, and replies "Invite code: {display(plain)}" (plaintext shown once to the minter) or the role message from Task 9 on `PermissionError`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel tests/access -q && uv run ruff check src tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis tests/revintel/test_join.py
git commit -m "feat(revintel): org invites, /join and member management"
```

---

### Task 15: Org notes and personal/org separation in LEARN

**Files:**
- Modify: `src/mavis/revintel/models.py` (`OrgNote`), `src/mavis/agents/turn_support.py` (`enqueue_learn` first lines), `src/mavis/tools/chat_tools.py` (register `org_note`, `org_recall`)
- Create: `src/mavis/revintel/knowledge.py`, `src/mavis/migrations/versions/<NN+5>_revintel_notes.py`, `tests/revintel/test_knowledge.py`

**Interfaces:**
- Produces:
  - `OrgNote` (`org_notes`): `id, org_id, owner_user_id, team_id, visibility (owner|team|org), text, source_ref, created_at`
  - `knowledge.add_note(p: Principal, text: str, *, visibility: str = "owner", source_ref: str = "") -> OrgNote` (action `org.memory.write`, own scope; the note inherits the writer's `team_id`)
  - `knowledge.recall(p: Principal, query: str, *, limit: int = 8) -> list[OrgNote]` (action `org.memory.read`; filter in SQL: scope ORG sees `org` visibility plus own and team-visible of everyone, TEAM sees `org` plus `team` of its teams plus own, OWN sees `org` visibility plus own; `owner` visibility is never visible to anyone but the owner, whatever the role)
  - `knowledge.learn_target(space: str) -> Literal["personal", "org"]`
  - `enqueue_learn` returns without queuing a LEARN job when `orgs_on()` and the turn's space is an org (org turns never feed the personal graph); tools `org_note(text, visibility)` (action `org.memory.write`, `RiskClass.WRITE_SELF`) and `org_recall(query)` (action `org.memory.read`, `RiskClass.READ`)
- Rules: notes are third-party-safe plain text written by users; no automatic flow from personal memory to org notes or back.

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_knowledge.py`:
```python
from __future__ import annotations

from mavis.revintel import knowledge
from mavis.revintel.principal import resolve_principal


async def p(world, name):
    return await resolve_principal(world.u[name].id, space=f"org:{world.org.id}")


async def test_owner_visibility_is_private_even_from_admins(world):
    await knowledge.add_note(await p(world, "Raj"), "Initech prefers quarterly billing", visibility="owner")
    assert [n.text for n in await knowledge.recall(await p(world, "Raj"), "billing")] == ["Initech prefers quarterly billing"]
    for other in ("Rin", "Mei", "Ada", "Olu", "Vic"):
        assert await knowledge.recall(await p(world, other), "billing") == []


async def test_team_and_org_visibility(world):
    await knowledge.add_note(await p(world, "Raj"), "Globex wants a pilot first", visibility="team")
    await knowledge.add_note(await p(world, "Raj"), "Acme style: short emails", visibility="org")
    texts = lambda notes: sorted(n.text for n in notes)  # noqa: E731
    assert texts(await knowledge.recall(await p(world, "Rin"), "e")) == ["Acme style: short emails", "Globex wants a pilot first"]
    assert texts(await knowledge.recall(await p(world, "Sol"), "e")) == ["Acme style: short emails"]  # West
    assert texts(await knowledge.recall(await p(world, "Mei"), "e")) == ["Acme style: short emails", "Globex wants a pilot first"]
    assert texts(await knowledge.recall(await p(world, "Ada"), "e")) == ["Acme style: short emails", "Globex wants a pilot first"]


async def test_viewers_cannot_write(world):
    import pytest

    with pytest.raises(PermissionError):
        await knowledge.add_note(await p(world, "Vic"), "x")


def test_learn_target_follows_the_space():
    assert knowledge.learn_target("personal") == "personal" and knowledge.learn_target("org:3") == "org"


async def test_org_turns_do_not_feed_personal_learn(world, orgs_on, monkeypatch):
    from mavis.agents import turn_support
    from mavis.domain.events import Event, EventType
    from mavis.revintel import spaces

    queued = []

    class Bus:
        async def enqueue(self, job):
            queued.append(job)

    monkeypatch.setattr(turn_support, "get_bus", lambda: Bus())
    ev = Event(id="e1", type=EventType.MESSAGE_RECEIVED, user_id=world.u["Raj"].id, payload={"text": "hi"})
    await turn_support.enqueue_learn(world.u["Raj"].id, ev, "my daughter is Asha", None)
    assert len(queued) == 1  # personal space learns as before
    tok = spaces.current_space.set(f"org:{world.org.id}")
    try:
        await turn_support.enqueue_learn(world.u["Raj"].id, ev, "Initech prefers email", None)
    finally:
        spaces.current_space.reset(tok)
    assert len(queued) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_knowledge.py -q`
Expected: FAIL (`No module named 'mavis.revintel.knowledge'`).

- [ ] **Step 3: Implement**

`OrgNote` in `revintel/models.py`:
```python
class OrgNote(Base):
    __tablename__ = "org_notes"
    __table_args__ = (Index("ix_org_notes_org_owner", "org_id", "owner_user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"))
    owner_user_id: Mapped[int] = mapped_column(Integer)
    team_id: Mapped[int | None] = mapped_column(Integer)
    visibility: Mapped[str] = mapped_column(String(8), default="owner")  # owner | team | org
    text: Mapped[str] = mapped_column(String(2000))
    source_ref: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
```
Migration `<NN+5>_revintel_notes.py` (`revision = "revintel_notes"`, `down_revision = "revintel_invites"`): `op.create_table("org_notes", ...)` with the same columns, `op.create_index("ix_org_notes_org_owner", "org_notes", ["org_id", "owner_user_id"])`; also add `("org_notes", "org_id", False)` to `rls.ORG_TABLES` and an `op.execute` loop of `rls_sql("org_notes")` guarded by the Postgres dialect (same pattern as Task 11).

`src/mavis/revintel/knowledge.py`:
```python
"""Org notes (spec 7.2, first slice): separate from the personal graph, filtered in SQL."""

from __future__ import annotations

from typing import Literal

from sqlalchemy import or_, select

from mavis.revintel.authz import authorize
from mavis.revintel.domain import Principal, Scope
from mavis.revintel.models import OrgNote
from mavis.revintel.domain import parse_space
from mavis.revintel.repo import orgs
from mavis.store.db import Session


def learn_target(space: str) -> Literal["personal", "org"]:
    return "org" if parse_space(space) is not None else "personal"


async def add_note(p: Principal, text: str, *, visibility: str = "owner", source_ref: str = "") -> OrgNote:
    d = authorize(p, "org.memory.write", None, snapshot=await orgs.load_snapshot(p.org_id))
    if not d.allowed:
        raise PermissionError("org.memory.write")
    team = next(iter(sorted(p.team_ids)), None)
    async with Session() as s:
        n = OrgNote(org_id=p.org_id, owner_user_id=p.user_id, team_id=team, visibility=visibility,
                    text=text[:2000], source_ref=source_ref)
        s.add(n)
        await s.commit()
        return n


async def recall(p: Principal, query: str, *, limit: int = 8) -> list[OrgNote]:
    d = authorize(p, "org.memory.read", None, snapshot=await orgs.load_snapshot(p.org_id))
    if not d.allowed:
        return []
    mine = OrgNote.owner_user_id == p.user_id
    wide = OrgNote.visibility == "org"
    team = (OrgNote.visibility == "team") & OrgNote.team_id.in_(p.team_ids or {-1})
    if d.max_scope is Scope.ORG:  # admins and owners read org and team notes, never someone's private ones
        scope = or_(mine, wide, OrgNote.visibility == "team")
    elif d.max_scope is Scope.TEAM:
        scope = or_(mine, wide, team)
    else:
        scope = or_(mine, wide, team)
    terms = [t for t in query.lower().split() if len(t) >= 1][:6]
    q = select(OrgNote).where(OrgNote.org_id == p.org_id, scope)
    for t in terms:
        q = q.where(OrgNote.text.ilike(f"%{t}%"))
    async with Session() as s:
        return list(await s.scalars(q.order_by(OrgNote.id.desc()).limit(limit)))
```
(For a rep, `team` visibility is readable by teammates through `team`; the `OWN` branch in the spec says "own", but a note explicitly marked `team` by its author is meant for the team, so all three scopes include `team`-visible notes of the reader's own teams. The test above pins this.)

`enqueue_learn` first lines in `agents/turn_support.py`:
```python
    from mavis.revintel.mode import orgs_on
    from mavis.revintel.spaces import current_space

    if orgs_on() and current_space.get() != "personal":
        return  # Track 6: an org turn never feeds the personal graph
```
Tools in `chat_tools.py`, registered next to `switch_space`:
```python
class OrgNoteArgs(ToolArgs):
    text: str = Field(max_length=1000)
    visibility: Literal["owner", "team", "org"] = "owner"


class OrgRecallArgs(ToolArgs):
    query: str = Field(max_length=200)


async def _org_note(user_id: int, a: OrgNoteArgs) -> str:
    from mavis.revintel import knowledge, spaces
    from mavis.revintel.principal import resolve_principal

    await knowledge.add_note(await resolve_principal(user_id, space=spaces.current_space.get()), a.text,
                             visibility=a.visibility)
    return "Noted."


async def _org_recall(user_id: int, a: OrgRecallArgs) -> str:
    from mavis.revintel import knowledge, spaces
    from mavis.revintel.principal import resolve_principal

    notes = await knowledge.recall(await resolve_principal(user_id, space=spaces.current_space.get()), a.query)
    return "\n".join(f"- {n.text}" for n in notes) or "Nothing noted about that."
```
with `MavisTool("org_note", "Save a work note about an account or deal (private by default).", OrgNoteArgs, RiskClass.WRITE_SELF, _org_note, _a("conversation"), action="org.memory.write")` and `MavisTool("org_recall", "Recall work notes you are allowed to see.", OrgRecallArgs, RiskClass.READ, _org_recall, _a("conversation"), action="org.memory.read")`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel tests/agents tests/tools -q && uv run ruff check src tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis tests/revintel/test_knowledge.py
git commit -m "feat(revintel): org notes and no personal LEARN in org turns"
```

---

## Phase B: HubSpot connector, revenue mirror and approval-gated writes

### Task 16: The `rev_*` revenue mirror tables

**Files:**
- Create: `src/mavis/revintel/rev_models.py`, `src/mavis/migrations/versions/<NN+6>_revintel_rev.py`, `tests/revintel/test_rev_models.py`
- Modify: `src/mavis/store/models.py` (one more import line), `src/mavis/revintel/rls.py` (`ORG_TABLES`)

**Interfaces:**
- Produces ORM classes, all with `org_id NOT NULL` leading an index and listed in `rls.ORG_TABLES`: `RevStageConfig` (`rev_stage_config`), `RevAccount` (`rev_accounts`), `RevContact` (`rev_contacts`), `RevDeal` (`rev_deals`), `RevDealContact` (`rev_deal_contacts`), `RevStageEvent` (`rev_deal_stage_events`), `RevCloseDateEvent` (`rev_deal_closedate_events`), `RevInteraction` (`rev_interactions`), `RevCallExtract` (`rev_call_extracts`), `RevSignal` (`rev_signals`), `RevSnapshot` (`rev_pipeline_snapshots`), `RevProposal` (`rev_proposals`), `RevAccess` (`rev_access`), `FxRate` (`fx_rates`). Columns follow spec 9.2; money is `BigInteger` minor units plus `currency String(3)`; `rev_deals.record_key` is unique per org; revision id `revintel_rev`.

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_rev_models.py`:
```python
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from mavis.revintel.rev_models import FxRate, RevAccount, RevDeal, RevStageConfig
from mavis.revintel.rls import ORG_TABLES
from mavis.store.db import Session, utcnow


def deal(org_id, key="hubspot:deal:1", **kw):
    return RevDeal(org_id=org_id, record_key=key, subject_key="deal:hubspot:1", name="Initech renewal",
                   amount_minor=12_500_00, currency="USD", stage_ref="s1", pipeline_ref="p1", is_open=True,
                   is_won=False, created_at=utcnow(), modified_at=utcnow(), owner_unmapped=False, **kw)


async def test_deal_key_is_unique_per_org_not_global(world):
    from mavis.revintel.repo import orgs

    other = await orgs.create_org("Globex", owner_user_id=world.u["Ada"].id, base_currency="EUR")
    async with Session() as s:
        s.add_all([deal(world.org.id), deal(other.id)])
        await s.commit()
        s.add(deal(world.org.id))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_money_is_integer_minor_units(world):
    async with Session() as s:
        d = deal(world.org.id, close_date=date(2026, 12, 31))
        s.add(d)
        await s.commit()
        assert isinstance(d.amount_minor, int) and d.close_date == date(2026, 12, 31)


async def test_stage_config_and_fx_rows(world):
    async with Session() as s:
        s.add(RevStageConfig(org_id=world.org.id, crm="hubspot", pipeline_ref="p", stage_ref="x", label="Etapa 2",
                             rank=2, category="open"))
        s.add(FxRate(org_id=world.org.id, ccy="EUR", base_ccy="USD", rate=1.08, as_of=date(2026, 10, 1), source="manual"))
        s.add(RevAccount(org_id=world.org.id, record_key="hubspot:company:9", name="Initech", domain="initech.test"))
        await s.commit()


def test_every_rev_table_is_under_rls():
    names = {t for t, _c, _n in ORG_TABLES}
    assert {"rev_deals", "rev_accounts", "rev_contacts", "rev_interactions", "rev_signals", "rev_access",
            "rev_pipeline_snapshots", "rev_proposals", "fx_rates", "rev_stage_config", "rev_call_extracts",
            "rev_deal_contacts", "rev_deal_stage_events", "rev_deal_closedate_events"} <= names
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_rev_models.py -q`
Expected: FAIL (`No module named 'mavis.revintel.rev_models'`).

- [ ] **Step 3: Implement**

`src/mavis/revintel/rev_models.py`:
```python
"""The revenue mirror (spec 9.2): typed, derived from connector_records, rebuildable, org-scoped."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, BigInteger, Float, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from mavis.store.db import Base, utcnow


class _Org:
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(Integer, index=True)


class RevStageConfig(_Org, Base):
    __tablename__ = "rev_stage_config"
    __table_args__ = (UniqueConstraint("org_id", "crm", "pipeline_ref", "stage_ref", name="uq_rev_stage_config_stage"),)
    crm: Mapped[str] = mapped_column(String(32))
    pipeline_ref: Mapped[str] = mapped_column(String(64))
    stage_ref: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(120))
    rank: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(8))  # open | won | lost: from CRM flags, never from names
    crm_probability: Mapped[float | None] = mapped_column(Float)
    weight_override: Mapped[float | None] = mapped_column(Float)
    expected_days: Mapped[int | None] = mapped_column(Integer)
    is_proposal_marker: Mapped[bool] = mapped_column(default=False)


class RevAccount(_Org, Base):
    __tablename__ = "rev_accounts"
    __table_args__ = (UniqueConstraint("org_id", "record_key", name="uq_rev_accounts_key"),)
    record_key: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str | None] = mapped_column(String(200), index=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer)
    team_id: Mapped[int | None] = mapped_column(Integer)
    attrs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class RevContact(_Org, Base):
    __tablename__ = "rev_contacts"
    __table_args__ = (UniqueConstraint("org_id", "record_key", name="uq_rev_contacts_key"),)
    record_key: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str | None] = mapped_column(String(200), index=True)
    account_id: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(String(200))
    role_ref: Mapped[str | None] = mapped_column(String(64))
    role_source: Mapped[str] = mapped_column(String(16), default="crm")
    is_internal: Mapped[bool] = mapped_column(default=False)


class RevDeal(_Org, Base):
    __tablename__ = "rev_deals"
    __table_args__ = (UniqueConstraint("org_id", "record_key", name="uq_rev_deals_key"),)
    record_key: Mapped[str] = mapped_column(String(200))
    subject_key: Mapped[str] = mapped_column(String(200), index=True)
    account_id: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(300))
    amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    stage_ref: Mapped[str] = mapped_column(String(64))
    pipeline_ref: Mapped[str] = mapped_column(String(64))
    is_open: Mapped[bool] = mapped_column(default=True)
    is_won: Mapped[bool] = mapped_column(default=False)
    close_date: Mapped[date | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    modified_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_activity_at: Mapped[datetime | None]
    next_step: Mapped[str | None] = mapped_column(String(500))
    next_step_at: Mapped[datetime | None]
    forecast_category: Mapped[str | None] = mapped_column(String(32))
    owner_user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    team_id: Mapped[int | None] = mapped_column(Integer, index=True)
    owner_unmapped: Mapped[bool] = mapped_column(default=False)
    crm_owner_ref: Mapped[str | None] = mapped_column(String(64))


class RevDealContact(_Org, Base):
    __tablename__ = "rev_deal_contacts"
    __table_args__ = (UniqueConstraint("deal_id", "contact_id", name="uq_rev_deal_contacts_pair"),)
    deal_id: Mapped[int] = mapped_column(Integer, index=True)
    contact_id: Mapped[int] = mapped_column(Integer)
    crm_role: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16), default="crm")


class RevStageEvent(_Org, Base):
    __tablename__ = "rev_deal_stage_events"
    deal_id: Mapped[int] = mapped_column(Integer, index=True)
    from_stage: Mapped[str | None] = mapped_column(String(64))
    to_stage: Mapped[str] = mapped_column(String(64))
    at: Mapped[datetime]
    observed: Mapped[bool] = mapped_column(default=True)  # True: only our poll saw it (age statistics flag it)


class RevCloseDateEvent(_Org, Base):
    __tablename__ = "rev_deal_closedate_events"
    deal_id: Mapped[int] = mapped_column(Integer, index=True)
    from_date: Mapped[date | None]
    to_date: Mapped[date | None]
    at: Mapped[datetime]
    observed: Mapped[bool] = mapped_column(default=True)


class RevInteraction(_Org, Base):
    __tablename__ = "rev_interactions"
    __table_args__ = (UniqueConstraint("org_id", "record_key", name="uq_rev_interactions_key"),)
    deal_id: Mapped[int | None] = mapped_column(Integer, index=True)
    account_id: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))  # call | meeting | email_in | email_out | note | task
    at: Mapped[datetime]
    direction: Mapped[str | None] = mapped_column(String(8))
    owner_user_id: Mapped[int | None] = mapped_column(Integer)
    record_key: Mapped[str] = mapped_column(String(200))
    participants: Mapped[list[Any]] = mapped_column(JSON, default=list)


class RevCallExtract(_Org, Base):
    __tablename__ = "rev_call_extracts"
    __table_args__ = (UniqueConstraint("org_id", "record_key", name="uq_rev_call_extracts_key"),)
    record_key: Mapped[str] = mapped_column(String(200))
    deal_id: Mapped[int | None] = mapped_column(Integer, index=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    extract: Mapped[dict[str, Any]] = mapped_column(JSON)
    validated_at: Mapped[datetime] = mapped_column(default=utcnow)
    model: Mapped[str] = mapped_column(String(64), default="")
    items_proposed: Mapped[int] = mapped_column(Integer, default=0)
    items_kept: Mapped[int] = mapped_column(Integer, default=0)


class RevSignal(_Org, Base):
    __tablename__ = "rev_signals"
    deal_id: Mapped[int] = mapped_column(Integer, index=True)
    rule_id: Mapped[str] = mapped_column(String(64))
    rule_version: Mapped[int] = mapped_column(Integer, default=1)
    fired_at: Mapped[datetime] = mapped_column(default=utcnow)
    cleared_at: Mapped[datetime | None]
    severity: Mapped[str] = mapped_column(String(8))
    weight: Mapped[int] = mapped_column(Integer)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class RevSnapshot(_Org, Base):
    __tablename__ = "rev_pipeline_snapshots"
    taken_on: Mapped[date] = mapped_column(index=True)
    team_id: Mapped[int | None] = mapped_column(Integer)
    owner_user_id: Mapped[int | None] = mapped_column(Integer)
    pipeline_ref: Mapped[str] = mapped_column(String(64))
    stage_ref: Mapped[str] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(3))
    deal_count: Mapped[int] = mapped_column(Integer)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    weighted_minor: Mapped[int] = mapped_column(BigInteger)
    weights_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class RevProposal(_Org, Base):
    __tablename__ = "rev_proposals"
    subject_key: Mapped[str] = mapped_column(String(200), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    requester_user_id: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    expires_at: Mapped[datetime]
    task_id: Mapped[int | None] = mapped_column(Integer)
    approval_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class RevAccess(_Org, Base):
    __tablename__ = "rev_access"
    __table_args__ = (UniqueConstraint("org_id", "record_type", "record_id", "user_id", "source",
                                       name="uq_rev_access_row"),)
    record_type: Mapped[str] = mapped_column(String(16))  # deal | account | contact | meeting
    record_id: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    source: Mapped[str] = mapped_column(String(16))  # owner | share | crm_visible | attendee


class FxRate(_Org, Base):
    __tablename__ = "fx_rates"
    __table_args__ = (UniqueConstraint("org_id", "ccy", "base_ccy", "as_of", name="uq_fx_rates_day"),)
    ccy: Mapped[str] = mapped_column(String(3))
    base_ccy: Mapped[str] = mapped_column(String(3))
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    as_of: Mapped[date]
    source: Mapped[str] = mapped_column(String(32), default="manual")
```
Append to `store/models.py`: `from mavis.revintel import rev_models as _rev_models  # noqa: E402,F401`. Append the 14 table names to `rls.ORG_TABLES` as `(name, "org_id", False)` (it is a plain list literal; add the entries after the existing ones).

Migration `<NN+6>_revintel_rev.py` (`revision = "revintel_rev"`, `down_revision = "revintel_invites"` or the latest Track 6 revision, here `revintel_notes`):
```python
"""Track 6: the revenue mirror."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from mavis.revintel.rls import rls_sql

revision = "revintel_rev"
down_revision = "revintel_notes"
branch_labels = None
depends_on = None

C, S, I, B, J = sa.Column, sa.String, sa.Integer, sa.BigInteger, sa.JSON
DT = sa.DateTime(timezone=True)
TABLES = [
    ("rev_stage_config", [C("crm", S(32), nullable=False), C("pipeline_ref", S(64), nullable=False),
                          C("stage_ref", S(64), nullable=False), C("label", S(120), nullable=False),
                          C("rank", I, nullable=False), C("category", S(8), nullable=False),
                          C("crm_probability", sa.Float), C("weight_override", sa.Float), C("expected_days", I),
                          C("is_proposal_marker", sa.Boolean, nullable=False)],
     [("uq_rev_stage_config_stage", "org_id", "crm", "pipeline_ref", "stage_ref")], []),
    ("rev_accounts", [C("record_key", S(200), nullable=False), C("name", S(200), nullable=False),
                      C("domain", S(200)), C("owner_user_id", I), C("team_id", I), C("attrs", J, nullable=False)],
     [("uq_rev_accounts_key", "org_id", "record_key")], ["domain"]),
    ("rev_contacts", [C("record_key", S(200), nullable=False), C("name", S(200), nullable=False), C("email", S(200)),
                      C("account_id", I), C("title", S(200)), C("role_ref", S(64)),
                      C("role_source", S(16), nullable=False), C("is_internal", sa.Boolean, nullable=False)],
     [("uq_rev_contacts_key", "org_id", "record_key")], ["email"]),
    ("rev_deals", [C("record_key", S(200), nullable=False), C("subject_key", S(200), nullable=False),
                   C("account_id", I), C("name", S(300), nullable=False), C("amount_minor", B),
                   C("currency", S(3), nullable=False), C("stage_ref", S(64), nullable=False),
                   C("pipeline_ref", S(64), nullable=False), C("is_open", sa.Boolean, nullable=False),
                   C("is_won", sa.Boolean, nullable=False), C("close_date", sa.Date),
                   C("created_at", DT, nullable=False), C("modified_at", DT, nullable=False),
                   C("last_activity_at", DT), C("next_step", S(500)), C("next_step_at", DT),
                   C("forecast_category", S(32)), C("owner_user_id", I), C("team_id", I),
                   C("owner_unmapped", sa.Boolean, nullable=False), C("crm_owner_ref", S(64))],
     [("uq_rev_deals_key", "org_id", "record_key")], ["subject_key", "owner_user_id", "team_id"]),
    ("rev_deal_contacts", [C("deal_id", I, nullable=False), C("contact_id", I, nullable=False),
                           C("crm_role", S(64)), C("source", S(16), nullable=False)],
     [("uq_rev_deal_contacts_pair", "deal_id", "contact_id")], ["deal_id"]),
    ("rev_deal_stage_events", [C("deal_id", I, nullable=False), C("from_stage", S(64)),
                               C("to_stage", S(64), nullable=False), C("at", DT, nullable=False),
                               C("observed", sa.Boolean, nullable=False)], [], ["deal_id"]),
    ("rev_deal_closedate_events", [C("deal_id", I, nullable=False), C("from_date", sa.Date), C("to_date", sa.Date),
                                   C("at", DT, nullable=False), C("observed", sa.Boolean, nullable=False)],
     [], ["deal_id"]),
    ("rev_interactions", [C("deal_id", I), C("account_id", I), C("kind", S(16), nullable=False),
                          C("at", DT, nullable=False), C("direction", S(8)), C("owner_user_id", I),
                          C("record_key", S(200), nullable=False), C("participants", J, nullable=False)],
     [("uq_rev_interactions_key", "org_id", "record_key")], ["deal_id"]),
    ("rev_call_extracts", [C("record_key", S(200), nullable=False), C("deal_id", I),
                           C("schema_version", I, nullable=False), C("extract", J, nullable=False),
                           C("validated_at", DT, nullable=False), C("model", S(64), nullable=False),
                           C("items_proposed", I, nullable=False), C("items_kept", I, nullable=False)],
     [("uq_rev_call_extracts_key", "org_id", "record_key")], ["deal_id"]),
    ("rev_signals", [C("deal_id", I, nullable=False), C("rule_id", S(64), nullable=False),
                     C("rule_version", I, nullable=False), C("fired_at", DT, nullable=False), C("cleared_at", DT),
                     C("severity", S(8), nullable=False), C("weight", I, nullable=False),
                     C("evidence", J, nullable=False)], [], ["deal_id"]),
    ("rev_pipeline_snapshots", [C("taken_on", sa.Date, nullable=False), C("team_id", I), C("owner_user_id", I),
                                C("pipeline_ref", S(64), nullable=False), C("stage_ref", S(64), nullable=False),
                                C("currency", S(3), nullable=False), C("deal_count", I, nullable=False),
                                C("amount_minor", B, nullable=False), C("weighted_minor", B, nullable=False),
                                C("weights_json", J, nullable=False)], [], ["taken_on"]),
    ("rev_proposals", [C("subject_key", S(200), nullable=False), C("kind", S(32), nullable=False),
                       C("requester_user_id", I, nullable=False), C("payload", J, nullable=False),
                       C("status", S(16), nullable=False), C("expires_at", DT, nullable=False), C("task_id", I),
                       C("approval_id", I), C("created_at", DT, nullable=False)], [], ["subject_key"]),
    ("rev_access", [C("record_type", S(16), nullable=False), C("record_id", I, nullable=False),
                    C("user_id", I, nullable=False), C("source", S(16), nullable=False)],
     [("uq_rev_access_row", "org_id", "record_type", "record_id", "user_id", "source")], ["user_id"]),
    ("fx_rates", [C("ccy", S(3), nullable=False), C("base_ccy", S(3), nullable=False),
                  C("rate", sa.Numeric(18, 8), nullable=False), C("as_of", sa.Date, nullable=False),
                  C("source", S(32), nullable=False)], [("uq_fx_rates_day", "org_id", "ccy", "base_ccy", "as_of")], []),
]


def upgrade() -> None:
    for name, cols, uniques, indexed in TABLES:
        op.create_table(name, C("id", I, primary_key=True), C("org_id", I, nullable=False), *cols,
                        *[sa.UniqueConstraint(*u[1:], name=u[0]) for u in uniques])
        for col in ["org_id", *indexed]:
            op.create_index(f"ix_{name}_{col}", name, [col])
    if op.get_bind().dialect.name == "postgresql":
        for name, *_ in TABLES:
            for stmt in rls_sql(name):
                op.execute(stmt)


def downgrade() -> None:
    for name, *_ in reversed(TABLES):
        op.drop_table(name)
```
(`op.create_index` for `taken_on` in `rev_pipeline_snapshots` is named `ix_rev_pipeline_snapshots_taken_on`, matching the model's `index=True`.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel/test_rev_models.py tests/store/test_migrations.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis tests/revintel/test_rev_models.py
git commit -m "feat(revintel): revenue mirror tables with org scoping and RLS"
```

---

### Task 17: Stage configuration from CRM flags

**Files:**
- Create: `src/mavis/revintel/projection/__init__.py`, `src/mavis/revintel/projection/stages.py`, `tests/revintel/test_stages.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) StageIn(pipeline_ref, stage_ref, label, rank: int, is_closed: bool, is_won: bool, probability: float | None)` (the vendor-neutral shape every CRM mapper emits)
  - `category_of(s: StageIn) -> str` (`won` when closed and won, `lost` when closed and not won, else `open`; the label is never read)
  - `async import_stages(org_id: int, crm: str, stages: Sequence[StageIn]) -> int` (upserts; keeps `weight_override`, `expected_days`, `is_proposal_marker` set by an admin; returns rows written)
  - `async stage_map(org_id: int, crm: str) -> dict[tuple[str, str], RevStageConfig]`
  - `async set_stage_marker(org_id, crm, pipeline_ref, stage_ref, *, proposal: bool) -> None`, `async set_weight_override(org_id, crm, pipeline_ref, stage_ref, weight: float | None) -> None`

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_stages.py`:
```python
from __future__ import annotations

import pytest

from mavis.revintel.projection.stages import (
    StageIn, category_of, import_stages, set_stage_marker, set_weight_override, stage_map,
)


@pytest.mark.parametrize(("closed", "won", "expected"), [(False, False, "open"), (True, True, "won"), (True, False, "lost")])
def test_category_comes_from_flags_not_names(closed, won, expected):
    for label in ("Closed Won", "Etapa 2", "Qualified", "ganado"):  # a stage named "Closed Won" can still be open
        assert category_of(StageIn("p", "s", label, 1, closed, won, None)) == expected


async def test_import_keeps_admin_settings_on_reimport(world):
    first = [StageIn("p1", "a", "Discovery", 1, False, False, 0.1), StageIn("p1", "b", "Won it", 5, True, True, 1.0),
             StageIn("p1", "c", "Etapa perdida", 6, True, False, 0.0)]
    assert await import_stages(world.org.id, "hubspot", first) == 3
    await set_stage_marker(world.org.id, "hubspot", "p1", "a", proposal=True)
    await set_weight_override(world.org.id, "hubspot", "p1", "a", 0.33)
    await import_stages(world.org.id, "hubspot", [StageIn("p1", "a", "Discovery v2", 2, False, False, 0.2)])
    cfg = (await stage_map(world.org.id, "hubspot"))[("p1", "a")]
    assert (cfg.label, cfg.rank, cfg.crm_probability, cfg.is_proposal_marker, cfg.weight_override) == (
        "Discovery v2", 2, 0.2, True, 0.33)
    cats = {k[1]: v.category for k, v in (await stage_map(world.org.id, "hubspot")).items()}
    assert cats == {"a": "open", "b": "won", "c": "lost"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_stages.py -q` -> FAIL (`No module named 'mavis.revintel.projection'`).

- [ ] **Step 3: Implement**

`src/mavis/revintel/projection/__init__.py` (empty) and `stages.py`:
```python
"""Stage meaning comes from the CRM's closed/won flags and org config, never from stage names (spec G10)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select

from mavis.revintel.rev_models import RevStageConfig
from mavis.store.db import Session


@dataclass(frozen=True)
class StageIn:
    pipeline_ref: str
    stage_ref: str
    label: str
    rank: int
    is_closed: bool
    is_won: bool
    probability: float | None


def category_of(s: StageIn) -> str:
    return "open" if not s.is_closed else ("won" if s.is_won else "lost")


async def import_stages(org_id: int, crm: str, stages: Sequence[StageIn]) -> int:
    n = 0
    async with Session() as s:
        have = {(r.pipeline_ref, r.stage_ref): r for r in await s.scalars(
            select(RevStageConfig).where(RevStageConfig.org_id == org_id, RevStageConfig.crm == crm))}
        for st in stages:
            row = have.get((st.pipeline_ref, st.stage_ref))
            if row is None:
                row = RevStageConfig(org_id=org_id, crm=crm, pipeline_ref=st.pipeline_ref, stage_ref=st.stage_ref)
                s.add(row)
            row.label, row.rank, row.category, row.crm_probability = st.label, st.rank, category_of(st), st.probability
            n += 1
        await s.commit()
    return n


async def stage_map(org_id: int, crm: str) -> dict[tuple[str, str], RevStageConfig]:
    async with Session() as s:
        rows = await s.scalars(select(RevStageConfig).where(RevStageConfig.org_id == org_id,
                                                            RevStageConfig.crm == crm))
        return {(r.pipeline_ref, r.stage_ref): r for r in rows}


async def _one(s, org_id, crm, pipeline_ref, stage_ref) -> RevStageConfig:
    return await s.scalar(select(RevStageConfig).where(
        RevStageConfig.org_id == org_id, RevStageConfig.crm == crm, RevStageConfig.pipeline_ref == pipeline_ref,
        RevStageConfig.stage_ref == stage_ref))


async def set_stage_marker(org_id: int, crm: str, pipeline_ref: str, stage_ref: str, *, proposal: bool) -> None:
    async with Session() as s:
        (await _one(s, org_id, crm, pipeline_ref, stage_ref)).is_proposal_marker = proposal
        await s.commit()


async def set_weight_override(org_id: int, crm: str, pipeline_ref: str, stage_ref: str,
                              weight: float | None) -> None:
    async with Session() as s:
        (await _one(s, org_id, crm, pipeline_ref, stage_ref)).weight_override = weight
        await s.commit()
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel/test_stages.py -q && uv run ruff check src tests` -> PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis/revintel/projection tests/revintel/test_stages.py
git commit -m "feat(revintel): stage configuration from CRM flags"
```

---

### Task 18: Owner mapping, territory teams and `rev_access`

**Files:**
- Create: `src/mavis/revintel/projection/owners.py`, `src/mavis/revintel/repo/rev.py`, `tests/revintel/test_owners_access.py`

**Interfaces:**
- Consumes: Tasks 6, 16.
- Produces:
  - `owners.map_owner(org_id: int, crm: str, owner: Mapping | None) -> int | None` (matches `org_memberships.crm_owner_refs[crm]` by `id`, else by lower-cased `email`; zero or more than one match gives `None`)
  - `owners.team_for(org_id: int, owner_user_id: int | None, attrs: Mapping[str, str]) -> int | None` (territory rules first, highest `priority` then lowest id; ops `eq`, `in` (comma list), `prefix`; else the owner's membership team; else `None`)
  - `owners.set_crm_owner(org_id, user_id, crm, *, owner_id: str, email: str | None, actor_user_id: int) -> None` (the `/org map-owner` backend; audited)
  - `rev.deal_ref(org_id, deal_id) -> ResourceRef | None`, `rev.account_ref`, `rev.contact_ref` (shares and attendees loaded; contacts and accounts inherit access from `rev_access`), `rev.meeting_ref(org_id, record_key)`
  - `rev.rebuild_access(org_id, deal_ids: Sequence[int] | None = None) -> int` (writes `owner` rows for the deal owner on the deal, its account and its contacts, and `share` rows for unexpired `resource_shares`; idempotent)
  - `rev.entity_names(org_id) -> list[str]` (account names, for the space nudge)
  - `rev.scoped_deals(session, flt: RowFilter, org_id) -> Select` (SQL for list queries: `ALL` no filter, `TEAMS` team in ids or owner = me or in `rev_access`, `OWNER` owner = me or in `rev_access` or shared team, `NONE` empty)

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_owners_access.py`:
```python
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from mavis.revintel import authz
from mavis.revintel.domain import Role
from mavis.revintel.principal import resolve_principal
from mavis.revintel.projection import owners
from mavis.revintel.repo import orgs, rev
from mavis.revintel.rev_models import RevAccount, RevContact, RevDeal, RevDealContact
from mavis.revintel.models import ResourceShare, TerritoryRule
from mavis.store.db import Session, utcnow


async def add_deal(world, owner="Raj", team=None, unmapped=False, name="Initech renewal", key="hubspot:deal:1"):
    async with Session() as s:
        d = RevDeal(org_id=world.org.id, record_key=key, subject_key=f"deal:{key}", name=name, amount_minor=100,
                    currency="USD", stage_ref="a", pipeline_ref="p", is_open=True, is_won=False,
                    created_at=utcnow(), modified_at=utcnow(), owner_unmapped=unmapped,
                    owner_user_id=None if unmapped else world.u[owner].id,
                    team_id=team if team is not None else (None if unmapped else world.teams.east.id))
        s.add(d)
        await s.commit()
        return d


async def test_map_owner_by_id_then_email_and_never_when_ambiguous(world):
    admin = world.u["Olu"].id
    await owners.set_crm_owner(world.org.id, world.u["Raj"].id, "hubspot", owner_id="501", email="raj@acme.test", actor_user_id=admin)
    await owners.set_crm_owner(world.org.id, world.u["Rin"].id, "hubspot", owner_id="502", email="rin@acme.test", actor_user_id=admin)
    assert await owners.map_owner(world.org.id, "hubspot", {"id": "501"}) == world.u["Raj"].id
    assert await owners.map_owner(world.org.id, "hubspot", {"id": "999", "email": "RIN@acme.test"}) == world.u["Rin"].id
    assert await owners.map_owner(world.org.id, "hubspot", {"id": "999"}) is None
    assert await owners.map_owner(world.org.id, "hubspot", None) is None
    await owners.set_crm_owner(world.org.id, world.u["Sol"].id, "hubspot", owner_id="501", email=None, actor_user_id=admin)
    assert await owners.map_owner(world.org.id, "hubspot", {"id": "501"}) is None  # two members claim it


async def test_territory_beats_the_owners_team_by_priority(world):
    async with Session() as s:
        s.add_all([TerritoryRule(org_id=world.org.id, team_id=world.teams.west.id, attr="region", op="in",
                                 value="emea, apac", priority=5),
                   TerritoryRule(org_id=world.org.id, team_id=world.teams.east.id, attr="region", op="prefix",
                                 value="em", priority=1)])
        await s.commit()
    raj = world.u["Raj"].id
    assert await owners.team_for(world.org.id, raj, {"region": "emea"}) == world.teams.west.id
    assert await owners.team_for(world.org.id, raj, {"region": "latam"}) == world.teams.east.id  # owner's team
    assert await owners.team_for(world.org.id, None, {}) is None


async def test_unmapped_owner_is_org_visible_only(world):
    d = await add_deal(world, unmapped=True)
    ref = await rev.deal_ref(world.org.id, d.id)
    snap = world.snapshot
    for name, expected in [("Raj", False), ("Mei", False), ("Ada", True), ("Olu", True), ("Vic", False)]:
        p = await resolve_principal(world.u[name].id, space=f"org:{world.org.id}")
        assert authz.authorize(p, "deal.read", ref, snapshot=snap).allowed is expected, name


async def test_shares_grant_a_rep_access_until_they_expire(world):
    d = await add_deal(world)
    async with Session() as s:
        s.add(ResourceShare(org_id=world.org.id, resource_type="deal", resource_id=str(d.id),
                            grantee_user_id=world.u["Sol"].id, level="read", granted_by=world.u["Raj"].id,
                            expires_at=utcnow() + timedelta(days=1)))
        s.add(ResourceShare(org_id=world.org.id, resource_type="deal", resource_id=str(d.id),
                            grantee_user_id=world.u["Rin"].id, level="read", granted_by=world.u["Raj"].id,
                            expires_at=utcnow() - timedelta(days=1)))
        await s.commit()
    ref = await rev.deal_ref(world.org.id, d.id)
    assert ref.shared_user_ids == frozenset({world.u["Sol"].id})


async def test_contacts_and_accounts_inherit_access_from_deals(world):
    d = await add_deal(world)
    async with Session() as s:
        a = RevAccount(org_id=world.org.id, record_key="hubspot:company:1", name="Initech", domain="initech.test")
        c = RevContact(org_id=world.org.id, record_key="hubspot:contact:1", name="Anita", email="anita@initech.test")
        s.add_all([a, c])
        await s.flush()
        s.add(RevDealContact(org_id=world.org.id, deal_id=d.id, contact_id=c.id, source="crm"))
        (await s.get(RevDeal, d.id)).account_id = a.id
        await s.commit()
        cid, aid = c.id, a.id
    assert await rev.rebuild_access(world.org.id) >= 3
    assert await rev.rebuild_access(world.org.id) >= 0  # idempotent
    raj = await resolve_principal(world.u["Raj"].id, space=f"org:{world.org.id}")
    rin = await resolve_principal(world.u["Rin"].id, space=f"org:{world.org.id}")
    cref, aref = await rev.contact_ref(world.org.id, cid), await rev.account_ref(world.org.id, aid)
    assert authz.authorize(raj, "contact.read", cref, snapshot=world.snapshot).allowed
    assert authz.authorize(raj, "account.read", aref, snapshot=world.snapshot).allowed
    assert not authz.authorize(rin, "contact.read", cref, snapshot=world.snapshot).allowed


async def test_scoped_deal_query_matches_authorize(world):
    mine, theirs = await add_deal(world, "Raj", key="hubspot:deal:1"), await add_deal(world, "Sol", team=world.teams.west.id, key="hubspot:deal:2")
    for name in ("Raj", "Mei", "Wen", "Ada", "Vic", "Sol"):
        p = await resolve_principal(world.u[name].id, space=f"org:{world.org.id}")
        flt = authz.scope_filter(p, "deal.read", snapshot=world.snapshot)
        async with Session() as s:
            got = set(await s.scalars(rev.scoped_deals(flt, world.org.id).with_only_columns(RevDeal.id)))
        want = {d.id for d in (mine, theirs)
                if authz.authorize(p, "deal.read", await rev.deal_ref(world.org.id, d.id), snapshot=world.snapshot).allowed}
        assert got == want, name
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_owners_access.py -q` -> FAIL (`No module named 'mavis.revintel.projection.owners'`).

- [ ] **Step 3: Implement**

`src/mavis/revintel/projection/owners.py`:
```python
"""CRM owner to member, and record to team (spec 4.2, 8.2). Ambiguity resolves to 'unknown', never a guess."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select

from mavis.revintel import audit
from mavis.revintel.models import OrgMembership, TerritoryRule
from mavis.store.db import Session


async def map_owner(org_id: int, crm: str, owner: Mapping | None) -> int | None:
    if not owner:
        return None
    async with Session() as s:
        members = list(await s.scalars(select(OrgMembership).where(OrgMembership.org_id == org_id,
                                                                   OrgMembership.status == "active")))
    refs = [(m.user_id, (m.crm_owner_refs or {}).get(crm) or {}) for m in members]
    oid, email = str(owner.get("id") or ""), str(owner.get("email") or "").lower()
    by_id = [u for u, r in refs if oid and str(r.get("id") or "") == oid]
    if len(by_id) == 1:
        return by_id[0]
    if by_id:
        return None
    by_mail = [u for u, r in refs if email and str(r.get("email") or "").lower() == email]
    return by_mail[0] if len(by_mail) == 1 else None


def _rule_hits(rule: TerritoryRule, attrs: Mapping[str, str]) -> bool:
    got = str(attrs.get(rule.attr, "")).lower()
    want = rule.value.lower()
    if rule.op == "eq":
        return got == want
    if rule.op == "in":
        return got in {v.strip() for v in want.split(",")}
    return rule.op == "prefix" and bool(got) and got.startswith(want)


async def team_for(org_id: int, owner_user_id: int | None, attrs: Mapping[str, str]) -> int | None:
    async with Session() as s:
        rules = list(await s.scalars(select(TerritoryRule).where(TerritoryRule.org_id == org_id)
                                     .order_by(TerritoryRule.priority.desc(), TerritoryRule.id)))
        for r in rules:
            if _rule_hits(r, attrs):
                return r.team_id
        if owner_user_id is None:
            return None
        return await s.scalar(select(OrgMembership.team_id).where(
            OrgMembership.org_id == org_id, OrgMembership.user_id == owner_user_id, OrgMembership.status == "active"))


async def set_crm_owner(org_id: int, user_id: int, crm: str, *, owner_id: str, email: str | None,
                        actor_user_id: int) -> None:
    async with Session() as s:
        m = await s.scalar(select(OrgMembership).where(OrgMembership.org_id == org_id,
                                                       OrgMembership.user_id == user_id))
        m.crm_owner_refs = {**(m.crm_owner_refs or {}), crm: {"id": owner_id, "email": email}}
        await audit.append(org_id, action="member.change", decision="executed", actor_user_id=actor_user_id,
                           resource_type="member", resource_ids=[user_id], detail={"op": "map_owner", "crm": crm},
                           session=s)
        await s.commit()
```

`src/mavis/revintel/repo/rev.py`:
```python
"""Scoped access to the revenue mirror. Org tables are reached only through here (spec 4.4)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select, String, cast, false, or_, select

from mavis.revintel.domain import FilterKind, ResourceRef, RowFilter
from mavis.revintel.models import ResourceShare
from mavis.revintel.rev_models import RevAccess, RevAccount, RevContact, RevDeal, RevDealContact
from mavis.store.db import Session, utcnow


async def _extras(s, org_id: int, rtype: str, rid: int) -> tuple[frozenset[int], frozenset[int], frozenset[int]]:
    now = utcnow()
    shares = list(await s.scalars(select(ResourceShare).where(
        ResourceShare.org_id == org_id, ResourceShare.resource_type == rtype, ResourceShare.resource_id == str(rid),
        or_(ResourceShare.expires_at.is_(None), ResourceShare.expires_at > now))))
    users = frozenset(x.grantee_user_id for x in shares if x.grantee_user_id is not None)
    teams = frozenset(x.grantee_team_id for x in shares if x.grantee_team_id is not None)
    attendees = frozenset(await s.scalars(select(RevAccess.user_id).where(
        RevAccess.org_id == org_id, RevAccess.record_type == rtype, RevAccess.record_id == rid,
        RevAccess.source.in_(("attendee", "crm_visible")))))
    return users, teams, attendees


async def deal_ref(org_id: int, deal_id: int) -> ResourceRef | None:
    async with Session() as s:
        d = await s.get(RevDeal, deal_id)
        if d is None or d.org_id != org_id:
            return None
        users, teams, att = await _extras(s, org_id, "deal", deal_id)
        return ResourceRef("deal", org_id, str(deal_id), d.owner_user_id, d.team_id, users, teams, att)


async def _inherited(s, org_id: int, rtype: str, rid: int) -> ResourceRef:
    users, teams, att = await _extras(s, org_id, rtype, rid)
    owners = frozenset(await s.scalars(select(RevAccess.user_id).where(
        RevAccess.org_id == org_id, RevAccess.record_type == rtype, RevAccess.record_id == rid,
        RevAccess.source == "owner")))
    return ResourceRef(rtype, org_id, str(rid), None, None, users | owners, teams, att)


async def account_ref(org_id: int, account_id: int) -> ResourceRef | None:
    async with Session() as s:
        a = await s.get(RevAccount, account_id)
        return None if a is None or a.org_id != org_id else await _inherited(s, org_id, "account", account_id)


async def contact_ref(org_id: int, contact_id: int) -> ResourceRef | None:
    async with Session() as s:
        c = await s.get(RevContact, contact_id)
        return None if c is None or c.org_id != org_id else await _inherited(s, org_id, "contact", contact_id)


async def meeting_ref(org_id: int, record_key: str) -> ResourceRef | None:
    """Filled in by the recap pipeline (Task 30); declared here so tools import one module."""
    from mavis.revintel.meetings import meeting_ref as impl

    return await impl(org_id, record_key)


async def rebuild_access(org_id: int, deal_ids: Sequence[int] | None = None) -> int:
    n = 0
    async with Session() as s:
        q = select(RevDeal).where(RevDeal.org_id == org_id)
        if deal_ids is not None:
            q = q.where(RevDeal.id.in_(list(deal_ids)))
        have = {(r.record_type, r.record_id, r.user_id, r.source) for r in await s.scalars(
            select(RevAccess).where(RevAccess.org_id == org_id))}

        def put(rtype: str, rid: int, uid: int, src: str) -> None:
            nonlocal n
            if (rtype, rid, uid, src) not in have:
                have.add((rtype, rid, uid, src))
                s.add(RevAccess(org_id=org_id, record_type=rtype, record_id=rid, user_id=uid, source=src))
                n += 1

        for d in await s.scalars(q):
            if d.owner_user_id is None:
                continue
            put("deal", d.id, d.owner_user_id, "owner")
            if d.account_id:
                put("account", d.account_id, d.owner_user_id, "owner")
            for cid in await s.scalars(select(RevDealContact.contact_id).where(RevDealContact.deal_id == d.id)):
                put("contact", cid, d.owner_user_id, "owner")
        await s.commit()
    return n


async def entity_names(org_id: int) -> list[str]:
    async with Session() as s:
        return list(await s.scalars(select(RevAccount.name).where(RevAccount.org_id == org_id).limit(500)))


def scoped_deals(flt: RowFilter, org_id: int) -> Select:
    q = select(RevDeal).where(RevDeal.org_id == org_id)
    if flt.kind is FilterKind.ALL:
        return q
    if flt.kind is FilterKind.NONE or flt.user_id is None:
        return q.where(false())
    seen = select(RevAccess.record_id).where(RevAccess.org_id == org_id, RevAccess.record_type == "deal",
                                             RevAccess.user_id == flt.user_id,
                                             RevAccess.source.in_(("share", "attendee", "crm_visible")))
    shared_team = select(ResourceShare.resource_id).where(
        ResourceShare.org_id == org_id, ResourceShare.resource_type == "deal",
        ResourceShare.grantee_team_id.in_(flt.team_ids or {-1}),
        or_(ResourceShare.expires_at.is_(None), ResourceShare.expires_at > utcnow()))
    shared_user = select(ResourceShare.resource_id).where(
        ResourceShare.org_id == org_id, ResourceShare.resource_type == "deal",
        ResourceShare.grantee_user_id == flt.user_id,
        or_(ResourceShare.expires_at.is_(None), ResourceShare.expires_at > utcnow()))
    mine = or_(RevDeal.owner_user_id == flt.user_id, RevDeal.id.in_(seen),
               cast(RevDeal.id, String).in_(shared_user), cast(RevDeal.id, String).in_(shared_team))
    if flt.kind is FilterKind.TEAMS:
        return q.where(or_(RevDeal.team_id.in_(flt.team_ids or {-1}), mine))
    return q.where(mine)
```
(`shared_user`/`shared_team` select string resource ids, so the comparison casts `RevDeal.id` to `String`. The `rebuild_access` function also writes `share` rows for unexpired shares into `rev_access` when asked; the SQL above reads shares directly so correctness never depends on that cache.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel/test_owners_access.py -q && uv run ruff check src tests` -> PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis/revintel tests/revintel/test_owners_access.py
git commit -m "feat(revintel): owner mapping, territory teams and scoped access to deals"
```

---

### Task 19: Projection of vendor-neutral deals, accounts, contacts and interactions

**Files:**
- Create: `src/mavis/revintel/projection/normalise.py`, `src/mavis/revintel/projection/project.py`, `tests/revintel/test_project.py`

**Interfaces:**
- Consumes: Tasks 16, 17, 18.
- Produces (every CRM mapper and the projection job speak this shape; no vendor field names past this point):
  - dataclasses `NormAccount(crm, external_id, name, domain, owner: Mapping | None, attrs: Mapping[str, str])`, `NormContact(crm, external_id, name, email, account_key: str | None, title, role: str | None)`, `NormDeal(crm, external_id, name, amount_minor: int | None, currency, stage_ref, pipeline_ref, close_date: date | None, created_at, modified_at, owner: Mapping | None, account_key: str | None, contact_keys: tuple[str, ...], next_step, next_step_at, forecast_category, last_activity_at, attrs: Mapping[str, str], stage_history: tuple[tuple[str | None, str, datetime], ...] = ())`, `NormInteraction(crm, external_id, kind, at, direction, owner, deal_keys: tuple[str, ...], account_key, participants: tuple[str, ...])`; `record_key(crm, type_, external_id) -> str` (`"hubspot:deal:42"`); `deal_subject_key(crm, external_id) -> str` (`"deal:hubspot:42"`)
  - `async project_account(org_id, na) -> int`, `async project_contact(org_id, nc) -> int` (`is_internal` when the e-mail domain is in `orgs.internal_domains`; e-mail lower-cased), `async project_deal(org_id, nd) -> ProjectResult`, `async project_interaction(org_id, ni) -> int | None`
  - `@dataclass ProjectResult(deal_id: int, created: bool, changed: bool, stage_changed: bool, close_date_moved: bool)`
- Rules: idempotent per record version (same `modified_at` and content: `changed=False`, no new events); a stage change writes a `rev_deal_stage_events` row with `observed=True`, supplied `stage_history` writes `observed=False` rows once; a changed close date writes a `rev_deal_closedate_events` row; `is_open`/`is_won` come from `rev_stage_config` (an unknown stage stays open); an owner that does not map sets `owner_unmapped=True` and `owner_user_id=None`; `last_activity_at` is the later of the supplied value and the newest linked interaction; `rebuild_access` runs for the deal.

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_project.py`:
```python
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from mavis.revintel.projection import owners, project
from mavis.revintel.projection.normalise import (
    NormAccount, NormContact, NormDeal, NormInteraction, deal_subject_key, record_key,
)
from mavis.revintel.projection.stages import StageIn, import_stages
from mavis.revintel.rev_models import RevCloseDateEvent, RevDeal, RevDealContact, RevStageEvent
from mavis.store.db import Session

T0 = datetime(2026, 10, 1, 9, tzinfo=UTC)


def nd(**kw):
    base = dict(crm="hubspot", external_id="42", name="Initech renewal", amount_minor=50_000_00, currency="usd",
                stage_ref="a", pipeline_ref="p", close_date=date(2026, 12, 15), created_at=T0 - timedelta(days=30),
                modified_at=T0, owner={"id": "501"}, account_key=None, contact_keys=(), next_step=None,
                next_step_at=None, forecast_category=None, last_activity_at=None, attrs={})
    return NormDeal(**{**base, **kw})


@pytest.fixture
async def ready(world):
    await import_stages(world.org.id, "hubspot", [StageIn("p", "a", "Etapa 1", 1, False, False, 0.2),
                                                  StageIn("p", "b", "Etapa 2", 2, False, False, 0.5),
                                                  StageIn("p", "w", "Ganado", 9, True, True, 1.0),
                                                  StageIn("p", "l", "Perdido", 10, True, False, 0.0)])
    await owners.set_crm_owner(world.org.id, world.u["Raj"].id, "hubspot", owner_id="501", email=None,
                               actor_user_id=world.u["Olu"].id)
    return world


async def one(world):
    async with Session() as s:
        return await s.scalar(select(RevDeal).where(RevDeal.org_id == world.org.id))


def test_keys():
    assert record_key("hubspot", "deal", "42") == "hubspot:deal:42" and deal_subject_key("hubspot", "42") == "deal:hubspot:42"


async def test_new_deal_resolves_owner_team_and_state(ready):
    r = await project.project_deal(ready.org.id, nd())
    d = await one(ready)
    assert (r.created, r.changed, d.currency, d.owner_user_id, d.team_id) == (True, True, "USD", ready.u["Raj"].id, ready.teams.east.id)
    assert (d.is_open, d.is_won, d.subject_key, d.amount_minor) == (True, False, "deal:hubspot:42", 50_000_00)
    r2 = await project.project_deal(ready.org.id, nd())
    assert (r2.created, r2.changed) == (False, False)


async def test_closed_stages_come_from_config_and_unknown_stages_stay_open(ready):
    await project.project_deal(ready.org.id, nd(stage_ref="w"))
    d = await one(ready)
    assert (d.is_open, d.is_won) == (False, True)
    await project.project_deal(ready.org.id, nd(stage_ref="zzz", modified_at=T0 + timedelta(hours=1)))
    d = await one(ready)
    assert (d.is_open, d.is_won) == (True, False)


async def test_stage_and_close_date_changes_write_events_once(ready):
    await project.project_deal(ready.org.id, nd())
    r = await project.project_deal(ready.org.id, nd(stage_ref="b", close_date=date(2027, 1, 20), modified_at=T0 + timedelta(days=2)))
    assert (r.stage_changed, r.close_date_moved) == (True, True)
    await project.project_deal(ready.org.id, nd(stage_ref="b", close_date=date(2027, 1, 20), modified_at=T0 + timedelta(days=2)))
    async with Session() as s:
        stage = list(await s.scalars(select(RevStageEvent)))
        moved = list(await s.scalars(select(RevCloseDateEvent)))
    assert [(e.from_stage, e.to_stage, e.observed) for e in stage] == [("a", "b", True)]
    assert [(e.from_date, e.to_date) for e in moved] == [(date(2026, 12, 15), date(2027, 1, 20))]


async def test_supplied_history_is_recorded_as_not_observed_once(ready):
    hist = ((None, "a", T0 - timedelta(days=20)), ("a", "b", T0 - timedelta(days=5)))
    await project.project_deal(ready.org.id, nd(stage_ref="b", stage_history=hist))
    await project.project_deal(ready.org.id, nd(stage_ref="b", stage_history=hist, modified_at=T0 + timedelta(hours=1)))
    async with Session() as s:
        rows = list(await s.scalars(select(RevStageEvent).order_by(RevStageEvent.at)))
    assert [(e.to_stage, e.observed) for e in rows] == [("a", False), ("b", False)]


async def test_unmapped_owner_flag(ready):
    await project.project_deal(ready.org.id, nd(owner={"id": "nobody"}))
    d = await one(ready)
    assert (d.owner_unmapped, d.owner_user_id, d.crm_owner_ref) == (True, None, "nobody")


async def test_contacts_accounts_interactions_link_up(ready):
    aid = await project.project_account(ready.org.id, NormAccount("hubspot", "9", "Initech", "initech.test", {"id": "501"}, {}))
    c1 = await project.project_contact(ready.org.id, NormContact("hubspot", "7", "Anita", "ANITA@Initech.test", "hubspot:company:9", "CFO", None))
    c2 = await project.project_contact(ready.org.id, NormContact("hubspot", "8", "Jo", "jo@acme.test", None, None, None))
    async with Session() as s:
        from mavis.revintel.rev_models import RevContact

        assert (await s.get(RevContact, c1)).email == "anita@initech.test" and (await s.get(RevContact, c2)).is_internal
    await project.project_deal(ready.org.id, nd(account_key="hubspot:company:9", contact_keys=("hubspot:contact:7",)))
    await project.project_interaction(ready.org.id, NormInteraction("hubspot", "n1", "note", T0 + timedelta(days=3), None,
                                                                    {"id": "501"}, ("hubspot:deal:42",), None, ()))
    d = await one(ready)
    assert d.account_id == aid and d.last_activity_at == T0 + timedelta(days=3)
    async with Session() as s:
        assert await s.scalar(select(func.count()).select_from(RevDealContact)) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_project.py -q` -> FAIL (`No module named 'mavis.revintel.projection.normalise'`).

- [ ] **Step 3: Implement**

`src/mavis/revintel/projection/normalise.py`:
```python
"""The vendor-neutral shapes every CRM mapper emits and the projector consumes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime


def record_key(crm: str, type_: str, external_id: str) -> str:
    return f"{crm}:{type_}:{external_id}"


def deal_subject_key(crm: str, external_id: str) -> str:
    return f"deal:{crm}:{external_id}"


@dataclass(frozen=True)
class NormAccount:
    crm: str
    external_id: str
    name: str
    domain: str | None
    owner: Mapping | None
    attrs: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class NormContact:
    crm: str
    external_id: str
    name: str
    email: str | None
    account_key: str | None
    title: str | None
    role: str | None


@dataclass(frozen=True)
class NormDeal:
    crm: str
    external_id: str
    name: str
    amount_minor: int | None
    currency: str
    stage_ref: str
    pipeline_ref: str
    close_date: date | None
    created_at: datetime
    modified_at: datetime
    owner: Mapping | None
    account_key: str | None
    contact_keys: tuple[str, ...]
    next_step: str | None
    next_step_at: datetime | None
    forecast_category: str | None
    last_activity_at: datetime | None
    attrs: Mapping[str, str] = field(default_factory=dict)
    stage_history: tuple[tuple[str | None, str, datetime], ...] = ()


@dataclass(frozen=True)
class NormInteraction:
    crm: str
    external_id: str
    kind: str
    at: datetime
    direction: str | None
    owner: Mapping | None
    deal_keys: tuple[str, ...]
    account_key: str | None
    participants: tuple[str, ...]
```

`src/mavis/revintel/projection/project.py`:
```python
"""Idempotent upserts from normalised records into the revenue mirror (spec 9.1)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select

from mavis.revintel.models import Org
from mavis.revintel.projection import owners
from mavis.revintel.projection.normalise import (
    NormAccount, NormContact, NormDeal, NormInteraction, deal_subject_key, record_key,
)
from mavis.revintel.projection.stages import stage_map
from mavis.revintel.repo import rev
from mavis.revintel.rev_models import (
    RevAccount, RevCloseDateEvent, RevContact, RevDeal, RevDealContact, RevInteraction, RevStageEvent,
)
from mavis.store.db import Session


@dataclass(frozen=True)
class ProjectResult:
    deal_id: int
    created: bool
    changed: bool
    stage_changed: bool
    close_date_moved: bool


async def project_account(org_id: int, na: NormAccount) -> int:
    key = record_key(na.crm, "company", na.external_id)
    owner = await owners.map_owner(org_id, na.crm, na.owner)
    team = await owners.team_for(org_id, owner, na.attrs)
    async with Session() as s:
        row = await s.scalar(select(RevAccount).where(RevAccount.org_id == org_id, RevAccount.record_key == key))
        if row is None:
            row = RevAccount(org_id=org_id, record_key=key, name=na.name)
            s.add(row)
        row.name, row.domain, row.owner_user_id, row.team_id = na.name, (na.domain or "").lower() or None, owner, team
        row.attrs = dict(na.attrs)
        await s.commit()
        return row.id


async def _account_id(s, org_id: int, key: str | None) -> int | None:
    if not key:
        return None
    return await s.scalar(select(RevAccount.id).where(RevAccount.org_id == org_id, RevAccount.record_key == key))


async def project_contact(org_id: int, nc: NormContact) -> int:
    key = record_key(nc.crm, "contact", nc.external_id)
    email = (nc.email or "").strip().lower() or None
    async with Session() as s:
        org = await s.get(Org, org_id)
        row = await s.scalar(select(RevContact).where(RevContact.org_id == org_id, RevContact.record_key == key))
        if row is None:
            row = RevContact(org_id=org_id, record_key=key)
            s.add(row)
        row.name, row.email, row.title = nc.name, email, nc.title
        row.account_id = await _account_id(s, org_id, nc.account_key)
        if nc.role:
            row.role_ref, row.role_source = nc.role, "crm"
        row.is_internal = bool(email and email.split("@")[-1] in {d.lower() for d in org.internal_domains})
        await s.commit()
        return row.id


async def project_deal(org_id: int, nd: NormDeal) -> ProjectResult:
    key = record_key(nd.crm, "deal", nd.external_id)
    cfg = (await stage_map(org_id, nd.crm)).get((nd.pipeline_ref, nd.stage_ref))
    is_open, is_won = (cfg.category == "open", cfg.category == "won") if cfg else (True, False)
    owner = await owners.map_owner(org_id, nd.crm, nd.owner)
    team = await owners.team_for(org_id, owner, nd.attrs)
    async with Session() as s:
        row = await s.scalar(select(RevDeal).where(RevDeal.org_id == org_id, RevDeal.record_key == key))
        created = row is None
        stage_changed = close_moved = False
        if created:
            row = RevDeal(org_id=org_id, record_key=key, subject_key=deal_subject_key(nd.crm, nd.external_id))
            s.add(row)
        elif row.modified_at >= nd.modified_at and nd.stage_history == ():
            return ProjectResult(row.id, False, False, False, False)
        else:
            stage_changed = row.stage_ref != nd.stage_ref
            close_moved = row.close_date != nd.close_date
        prev_stage, prev_close = (row.stage_ref, row.close_date) if not created else (None, None)
        row.name, row.amount_minor, row.currency = nd.name, nd.amount_minor, nd.currency.upper()
        row.stage_ref, row.pipeline_ref, row.is_open, row.is_won = nd.stage_ref, nd.pipeline_ref, is_open, is_won
        row.close_date, row.created_at, row.modified_at = nd.close_date, nd.created_at, nd.modified_at
        row.next_step, row.next_step_at, row.forecast_category = nd.next_step, nd.next_step_at, nd.forecast_category
        row.owner_user_id, row.team_id = owner, team
        row.owner_unmapped = nd.owner is not None and owner is None
        row.crm_owner_ref = str((nd.owner or {}).get("id") or "") or None
        row.account_id = await _account_id(s, org_id, nd.account_key)
        await s.flush()
        if stage_changed:
            s.add(RevStageEvent(org_id=org_id, deal_id=row.id, from_stage=prev_stage, to_stage=nd.stage_ref,
                                at=nd.modified_at, observed=True))
        if close_moved:
            s.add(RevCloseDateEvent(org_id=org_id, deal_id=row.id, from_date=prev_close, to_date=nd.close_date,
                                    at=nd.modified_at, observed=True))
        have = {(e.to_stage, e.at) for e in await s.scalars(select(RevStageEvent).where(RevStageEvent.deal_id == row.id))}
        for frm, to, at in nd.stage_history:
            if (to, at) not in have:
                s.add(RevStageEvent(org_id=org_id, deal_id=row.id, from_stage=frm, to_stage=to, at=at, observed=False))
        newest = await s.scalar(select(func.max(RevInteraction.at)).where(RevInteraction.deal_id == row.id))
        times = [t for t in (nd.last_activity_at, newest) if t is not None]
        row.last_activity_at = max(times) if times else None
        for ck in nd.contact_keys:
            cid = await s.scalar(select(RevContact.id).where(RevContact.org_id == org_id, RevContact.record_key == ck))
            if cid and not await s.scalar(select(RevDealContact.id).where(RevDealContact.deal_id == row.id,
                                                                            RevDealContact.contact_id == cid)):
                s.add(RevDealContact(org_id=org_id, deal_id=row.id, contact_id=cid, source="crm"))
        await s.commit()
        deal_id = row.id
    await rev.rebuild_access(org_id, [deal_id])
    return ProjectResult(deal_id, created, True, stage_changed, close_moved)


async def project_interaction(org_id: int, ni: NormInteraction) -> int | None:
    key = record_key(ni.crm, "engagement", ni.external_id)
    owner = await owners.map_owner(org_id, ni.crm, ni.owner)
    async with Session() as s:
        deal = await s.scalar(select(RevDeal).where(RevDeal.org_id == org_id, RevDeal.record_key.in_(ni.deal_keys or ("",))))
        row = await s.scalar(select(RevInteraction).where(RevInteraction.org_id == org_id, RevInteraction.record_key == key))
        if row is None:
            row = RevInteraction(org_id=org_id, record_key=key, kind=ni.kind, at=ni.at)
            s.add(row)
        row.kind, row.at, row.direction, row.owner_user_id = ni.kind, ni.at, ni.direction, owner
        row.deal_id, row.participants = (deal.id if deal else None), list(ni.participants)
        row.account_id = await _account_id(s, org_id, ni.account_key) or (deal.account_id if deal else None)
        if deal is not None and (deal.last_activity_at is None or deal.last_activity_at < ni.at):
            deal.last_activity_at = ni.at
        await s.commit()
        return deal.id if deal else None
```
`rev_models.RevInteraction.at` and `RevDeal.last_activity_at` are `UTCDateTime`, so comparisons use aware datetimes.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel/test_project.py -q && uv run ruff check src tests` -> PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis/revintel/projection tests/revintel/test_project.py
git commit -m "feat(revintel): idempotent projection of normalised CRM records"
```

---

### Task 20: Declarative rate budgets (pure)

**Files:**
- Create: `src/mavis/revintel/ratebudget.py`, `tests/revintel/test_ratebudget.py`
- Modify: `src/mavis/config.py` (`hubspot_per_second: float = 10.0`, `hubspot_per_day: int = 250000`; both "verify" values)

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) RateBudget(per_second: float, per_day: int, share_sync: float = 0.6, share_interactive: float = 0.3, share_reserve: float = 0.1, degrade_at: float = 0.8, poll_multiplier: float = 3.0, backoff_floor_s: float = 1.0, backoff_cap_s: float = 3600.0)`
  - `@dataclass(frozen=True) Grant(allowed: bool, wait_s: float, reason: str)`
  - `class BudgetBucket(rb: RateBudget, clock: Callable[[], datetime])` with `acquire(kind: Literal["sync","interactive","backfill"]) -> Grant`, `note_rate_limited(retry_after_s: float | None = None) -> float` (returns the pause; doubles from `backoff_floor_s` to `backoff_cap_s` on consecutive 429s, honours `Retry-After`, resets on `note_ok()`), `note_ok() -> None`, `day_fraction` property, `poll_every(base_minutes: int) -> int`
- Rules: `sync` may use up to `share_sync * per_day` of the UTC day; `interactive` up to `(share_interactive + share_reserve)`; once `day_fraction >= degrade_at` `backfill` is refused with reason `degraded` and `poll_every` multiplies; per-second spacing is enforced by `wait_s`; the day counter resets at UTC midnight; nothing is a vendor constant.

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_ratebudget.py`:
```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mavis.revintel.ratebudget import BudgetBucket, RateBudget


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 8, 12, tzinfo=UTC)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def bucket(per_day=100, per_second=1000.0, **kw):
    clock = Clock()
    return BudgetBucket(RateBudget(per_second=per_second, per_day=per_day, **kw), clock), clock


def test_sync_stops_at_its_share_but_interactive_still_works():
    b, _ = bucket()
    assert all(b.acquire("sync").allowed for _ in range(60))
    g = b.acquire("sync")
    assert (g.allowed, g.reason) == (False, "share_exhausted")
    assert all(b.acquire("interactive").allowed for _ in range(40))  # 30 own + 10 reserve
    assert b.acquire("interactive").reason == "day_exhausted"


def test_interactive_borrows_the_reserve_only():
    b, _ = bucket()
    assert sum(b.acquire("interactive").allowed for _ in range(100)) == 40


def test_backfill_pauses_and_polling_slows_when_degraded():
    b, _ = bucket()
    for _ in range(50):
        b.acquire("sync")
    assert b.acquire("backfill").allowed
    for _ in range(10):
        b.acquire("sync")
    assert b.acquire("sync").reason == "share_exhausted"
    for _ in range(20):
        b.acquire("interactive")
    assert b.day_fraction >= 0.8
    assert b.acquire("backfill").reason == "degraded" and b.poll_every(5) == 15


def test_day_counter_resets_at_utc_midnight():
    b, c = bucket()
    for _ in range(60):
        b.acquire("sync")
    c.advance(hours=13)
    assert b.acquire("sync").allowed and b.day_fraction == pytest.approx(0.01)


def test_per_second_spacing_returns_a_wait():
    b, c = bucket(per_second=2.0, per_day=10_000)
    assert b.acquire("sync").wait_s == 0.0
    assert b.acquire("sync").wait_s == pytest.approx(0.5)
    c.advance(seconds=1)
    assert b.acquire("sync").wait_s == 0.0


def test_rate_limit_backoff_honours_retry_after_then_doubles_to_the_cap():
    b, c = bucket(backoff_floor_s=1, backoff_cap_s=8)
    assert b.note_rate_limited(retry_after_s=30) == 30
    assert b.acquire("sync").reason == "paused" and b.acquire("sync").wait_s == pytest.approx(30)
    c.advance(seconds=31)
    assert [b.note_rate_limited(), b.note_rate_limited(), b.note_rate_limited(), b.note_rate_limited(), b.note_rate_limited()] == [1, 2, 4, 8, 8]
    b.note_ok()
    assert b.note_rate_limited() == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_ratebudget.py -q` -> FAIL (`No module named 'mavis.revintel.ratebudget'`).

- [ ] **Step 3: Implement**

`src/mavis/revintel/ratebudget.py`:
```python
"""Declarative per-account call budgets (spec 8.5). Numbers come from settings, never constants."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

Kind = Literal["sync", "interactive", "backfill"]


@dataclass(frozen=True)
class RateBudget:
    per_second: float
    per_day: int
    share_sync: float = 0.6
    share_interactive: float = 0.3
    share_reserve: float = 0.1
    degrade_at: float = 0.8
    poll_multiplier: float = 3.0
    backoff_floor_s: float = 1.0
    backoff_cap_s: float = 3600.0


@dataclass(frozen=True)
class Grant:
    allowed: bool
    wait_s: float = 0.0
    reason: str = "ok"


class BudgetBucket:
    def __init__(self, rb: RateBudget, clock: Callable[[], datetime]) -> None:
        self.rb, self._clock = rb, clock
        self._day: date | None = None
        self._used = {"sync": 0, "interactive": 0}
        self._next_ok: datetime | None = None
        self._paused_until: datetime | None = None
        self._strikes = 0

    def _roll(self) -> datetime:
        now = self._clock()
        if self._day != now.date():
            self._day, self._used = now.date(), {"sync": 0, "interactive": 0}
        return now

    @property
    def day_fraction(self) -> float:
        self._roll()
        return sum(self._used.values()) / self.rb.per_day

    def acquire(self, kind: Kind) -> Grant:
        now = self._roll()
        if self._paused_until and now < self._paused_until:
            return Grant(False, (self._paused_until - now).total_seconds(), "paused")
        if kind == "backfill" and self.day_fraction >= self.rb.degrade_at:
            return Grant(False, 0.0, "degraded")
        bucket = "interactive" if kind == "interactive" else "sync"
        cap = self.rb.share_interactive + self.rb.share_reserve if bucket == "interactive" else self.rb.share_sync
        if sum(self._used.values()) >= self.rb.per_day:
            return Grant(False, 0.0, "day_exhausted")
        if self._used[bucket] >= int(cap * self.rb.per_day):
            return Grant(False, 0.0, "share_exhausted" if bucket == "sync" else "day_exhausted")
        wait = 0.0
        if self._next_ok and self._next_ok > now:
            wait = (self._next_ok - now).total_seconds()
        self._next_ok = max(now, self._next_ok or now) + timedelta(seconds=1.0 / self.rb.per_second)
        self._used[bucket] += 1
        return Grant(True, wait)

    def note_rate_limited(self, retry_after_s: float | None = None) -> float:
        if retry_after_s:
            pause = retry_after_s  # the server's word wins and is not a doubling step
        else:
            pause = min(self.rb.backoff_cap_s, self.rb.backoff_floor_s * 2 ** self._strikes)
            self._strikes += 1
        self._paused_until = self._clock() + timedelta(seconds=pause)
        return pause

    def note_ok(self) -> None:
        self._strikes, self._paused_until = 0, None

    def poll_every(self, base_minutes: int) -> int:
        return int(base_minutes * self.rb.poll_multiplier) if self.day_fraction >= self.rb.degrade_at else base_minutes
```
Interactive test arithmetic check: per_day 100 → interactive cap `int(0.4*100)=40` ok; sync cap 60.
`test_backfill_pauses...`: after 50 sync + 10 sync = 60 sync (fraction .6), then 20 interactive = 80 → fraction .8 → degraded. ok. In the first assertion `b.acquire("backfill").allowed` at 50 used: backfill counts as `sync` bucket (50 used < 60) → allowed and increments used to 51; then loop `for _ in range(10): sync` → total 61? sync used 51+10 = 61 but cap 60 → 9 allowed, 10th refused; then `b.acquire("sync").reason == "share_exhausted"` ok; total used = 60, interactive 20 → 80 → fraction 0.8 OK.
The paused test: `note_rate_limited(retry_after_s=30)` returns 30; `strikes` increments to 1 — then the doubling test expects [1,2,4,8,8] after `advance` — strikes already 1 → first returns 2. Fix: `retry_after` path should not count a strike: only increment `_strikes` in the exponential branch. Patch the code: 
```python
        if retry_after_s:
            pause = retry_after_s
        else:
            pause = min(cap, floor * 2 ** self._strikes); self._strikes += 1
```
Also in the paused test two `acquire` calls: second `b.acquire("sync").wait_s == approx(30)` fine (paused returns without consuming).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel/test_ratebudget.py -q` -> PASS (apply the strike fix first).

- [ ] **Step 5: Commit**

```bash
git add src/mavis/revintel/ratebudget.py src/mavis/config.py tests/revintel/test_ratebudget.py
git commit -m "feat(revintel): declarative rate budgets with shares, degrade and backoff"
```

---

### Task 21: Kinds `DEAL`, `ACCOUNT`, `ENGAGEMENT`, `CALL`, `CONFIG`, their field schemas and money helpers

**Start gate:** connectors Task 7 merged (`domain/records.py`: `Kind`, `Record`, `FIELD_SCHEMAS`, `validate_fields`).

**Files:**
- Modify: `src/mavis/domain/records.py` (append; shared with connectors), `tests/domain/test_records.py` is untouched
- Create: `src/mavis/revintel/money.py`, `tests/revintel/test_kinds_money.py`

**Interfaces:**
- Produces:
  - `Kind.DEAL="deal"`, `Kind.ACCOUNT="account"`, `Kind.ENGAGEMENT="engagement"` (a CRM-logged note, call, e-mail, meeting or task; `ACTIVITY` stays the fitness kind), `Kind.CALL="call"` (a recorded meeting with transcript), `Kind.CONFIG="config"` (CRM pipelines and owners)
  - pydantic schemas registered in `FIELD_SCHEMAS`: `DealFields`, `AccountFields`, `EngagementFields`, `CallFields`, `ConfigFields` (fields below)
  - `money.minor(amount: Decimal | str | int | float, ccy: str) -> int` (ISO 4217 exponent: 0 for JPY KRW VND CLP ISK UGX XAF XOF PYG RWF, 3 for BHD KWD OMR JOD TND IQD, else 2; half-up; floats are refused with `TypeError` so money never rides a float), `money.exponent(ccy) -> int`, `money.fmt(minor: int, ccy: str) -> str` (`"USD 1,250.50"`)
- Schemas: `DealFields(amount: Decimal | None, currency: str ^[A-Z]{3}$, stage_ref, pipeline_ref, close_date: date | None, owner_ref, owner_email, is_closed: bool, is_won: bool, created_at: datetime, modified_at: datetime, next_step, next_step_at, forecast_category, last_activity_at, account_ref, contact_refs: list[str], stage_history: list[dict], attrs: dict[str, str])`; `AccountFields(domain, owner_ref, attrs)`; `EngagementFields(type: Literal["note","call","email","meeting","task"], direction, deal_refs: list[str], account_ref, owner_ref, participants: list[str], status)`; `CallFields(started_at: datetime, duration_s: int, attendees: list[dict], summary_source: str, recorder_email: str | None, share_url: str | None, has_transcript: bool)`; `ConfigFields(type: Literal["pipeline","owner"], payload: dict)`.

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_kinds_money.py`:
```python
from __future__ import annotations

from decimal import Decimal

import pytest

from mavis.domain.records import FIELD_SCHEMAS, InvalidRecord, Kind, Record, validate_fields
from mavis.revintel import money


@pytest.mark.parametrize(("amount", "ccy", "expected"), [
    ("1250.50", "USD", 125050), (Decimal("99.995"), "EUR", 10000), (1250, "JPY", 1250), ("1.234", "KWD", 1234),
    ("0", "INR", 0), (7, "inr", 700),
])
def test_minor_units(amount, ccy, expected):
    assert money.minor(amount, ccy) == expected


def test_floats_never_become_money():
    with pytest.raises(TypeError):
        money.minor(12.5, "USD")


@pytest.mark.parametrize(("minor", "ccy", "text"), [(125050, "USD", "USD 1,250.50"), (1250, "JPY", "JPY 1,250"),
                                                    (1234, "KWD", "KWD 1.234"), (-500, "EUR", "EUR -5.00")])
def test_fmt(minor, ccy, text):
    assert money.fmt(minor, ccy) == text


def test_new_kinds_have_schemas():
    assert {Kind.DEAL, Kind.ACCOUNT, Kind.ENGAGEMENT, Kind.CALL, Kind.CONFIG} <= set(FIELD_SCHEMAS)


def deal_fields(**kw):
    return {"amount": "1250.50", "currency": "USD", "stage_ref": "a", "pipeline_ref": "p",
            "created_at": "2026-10-01T09:00:00+00:00", "modified_at": "2026-10-02T09:00:00+00:00", **kw}


def test_deal_fields_normalise_and_validate():
    out = validate_fields(Kind.DEAL, deal_fields(close_date="2026-12-31"))
    assert out["amount"] == "1250.50" and out["close_date"] == "2026-12-31"
    with pytest.raises(InvalidRecord):
        validate_fields(Kind.DEAL, deal_fields(currency="dollars"))
    with pytest.raises(InvalidRecord):
        validate_fields(Kind.DEAL, deal_fields(amount="lots"))


def test_call_and_config_fields():
    ok = validate_fields(Kind.CALL, {"started_at": "2026-10-05T10:00:00+00:00", "duration_s": 2400,
                                     "attendees": [{"email": "a@x.test", "name": "A", "is_internal": False}],
                                     "summary_source": "fathom", "has_transcript": True})
    assert ok["duration_s"] == 2400
    with pytest.raises(InvalidRecord):
        validate_fields(Kind.CONFIG, {"type": "weather", "payload": {}})
    r = Record(user_id=1, connector="hubspot", kind=Kind.ACCOUNT, external_id="9", title="Initech",
               fields={"domain": "initech.test"})
    assert r.record_key == "hubspot:account:9"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_kinds_money.py -q` -> FAIL (`module 'mavis.revintel.money' not found`).

- [ ] **Step 3: Implement**

`src/mavis/revintel/money.py`:
```python
"""Money is integer minor units plus an ISO code. Floats never carry it (spec 9.3)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_ZERO = frozenset("JPY KRW VND CLP ISK UGX XAF XOF PYG RWF".split())
_THREE = frozenset("BHD KWD OMR JOD TND IQD".split())


def exponent(ccy: str) -> int:
    c = ccy.upper()
    return 0 if c in _ZERO else 3 if c in _THREE else 2


def minor(amount: Decimal | str | int, ccy: str) -> int:
    if isinstance(amount, float):
        raise TypeError("money must not be a float")
    q = Decimal(str(amount)) * (Decimal(10) ** exponent(ccy))
    return int(q.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def fmt(minor_units: int, ccy: str) -> str:
    e = exponent(ccy)
    major = Decimal(minor_units) / (Decimal(10) ** e)
    return f"{ccy.upper()} {major:,.{e}f}"
```
Append to `src/mavis/domain/records.py`, in `Kind` add the five members, and after `FIELD_SCHEMAS` is defined:
```python
class DealFields(BaseModel):
    amount: Decimal | None = None
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    stage_ref: str
    pipeline_ref: str
    close_date: date | None = None
    owner_ref: str | None = None
    owner_email: str | None = None
    is_closed: bool = False
    is_won: bool = False
    created_at: datetime
    modified_at: datetime
    next_step: str | None = None
    next_step_at: datetime | None = None
    forecast_category: str | None = None
    last_activity_at: datetime | None = None
    account_ref: str | None = None
    contact_refs: list[str] = Field(default_factory=list)
    stage_history: list[dict] = Field(default_factory=list)
    attrs: dict[str, str] = Field(default_factory=dict)


class AccountFields(BaseModel):
    domain: str | None = None
    owner_ref: str | None = None
    attrs: dict[str, str] = Field(default_factory=dict)


class EngagementFields(BaseModel):
    type: Literal["note", "call", "email", "meeting", "task"]
    direction: str | None = None
    deal_refs: list[str] = Field(default_factory=list)
    account_ref: str | None = None
    owner_ref: str | None = None
    participants: list[str] = Field(default_factory=list)
    status: str | None = None


class CallFields(BaseModel):
    started_at: datetime
    duration_s: int = Field(ge=0)
    attendees: list[dict] = Field(default_factory=list)
    summary_source: str
    recorder_email: str | None = None
    share_url: str | None = None
    has_transcript: bool = False


class ConfigFields(BaseModel):
    type: Literal["pipeline", "owner"]
    payload: dict


FIELD_SCHEMAS.update({Kind.DEAL: DealFields, Kind.ACCOUNT: AccountFields, Kind.ENGAGEMENT: EngagementFields,
                      Kind.CALL: CallFields, Kind.CONFIG: ConfigFields})
```
(`date`, `Decimal`, `Literal`, `Field` imports exist or are added at the top of the module. `_jsonable` already turns `Decimal` into a string, `date` and `datetime` into ISO strings.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel/test_kinds_money.py tests/domain -q && uv run ruff check src tests` -> PASS (if a connectors test enumerates every `Kind`, extend its expected set in the same commit).

- [ ] **Step 5: Commit**

```bash
git add src/mavis/domain/records.py src/mavis/revintel/money.py tests/revintel/test_kinds_money.py
git commit -m "feat(revintel): CRM and call record kinds with typed fields and money helpers"
```

---

### Task 22: An `org_id` dimension on connector storage, spec additions, org-bound identity

**Start gate:** connectors Tasks 8, 9, 10, 16 to 19 merged and plan 11 merged (`composio_user_id`).

**Files:**
- Create: `src/mavis/migrations/versions/<NN+7>_revintel_connector_org.py`, `tests/revintel/test_connector_org.py`
- Modify: `src/mavis/store/models.py` (`ConnectorRecordRow`, `ConnectorCursorRow`, `ConnectorTokenRow`: `org_id`), `src/mavis/store/repo/connectors.py` (every query takes `org_id`), `src/mavis/domain/records.py` (`Record.org_id: int = 0`), `src/mavis/connectors/spec.py` (`MapContext.org_id: int = 0`, `MapContext.org_currency: str = "USD"`, `ConnectorSpec.rate_budget: Any = None`, `.extra_prefixes: tuple[str, ...] = ()`, `.org_capable: bool = False`), `src/mavis/connectors/registry.py` (subject keys may start with `subject_prefix` or any `extra_prefixes`; all prefixes are collision-checked), `src/mavis/connectors/ingest.py` (`Ingestor.ingest` skips org rows), `src/mavis/connectors/identity.py` (`composio_user_id(user_id, org_id=None)`)
- Shared: all of the above are connectors-owned; hunks are additive and defaults (`org_id=0`) keep personal behaviour byte-identical.

**Interfaces:**
- Produces:
  - `org_id int not null default 0` on `connector_records`, `connector_cursors`, `connector_tokens` (0 means personal); uniques widened to include it: records `(user_id, org_id, record_key)`, cursors `(user_id, org_id, connector, stream)`, tokens `(user_id, org_id, connector)`; the migration finds the existing unique constraints with `sa.inspect(bind).get_unique_constraints(table)` and rebuilds them, so it does not depend on their generated names
  - repo functions gain `org_id: int = 0` (keyword): `upsert_record` (reads `record.org_id`), `get_cursor`, `records_for`, `record_keys_for`, `counts_for`, `tombstone`, `is_tombstoned`, `mark_deleted`, `mark_invalid`, `purge_records`, `null_expired_bodies` (all orgs)
  - `Ingestor.ingest`: a row with `org_id != 0` is marked `processed` without graph or vector writes and without free-text extraction (org knowledge never enters the personal graph, spec 7.2)
  - `identity.composio_user_id(user_id, org_id=None)`: `f"{base}-o{org_id}"` when `org_id` is given; `identity.parse_bound_id(provider_id) -> tuple[int, int] | None` (`mavis-<anything>-<uid>-o<org>` gives `(uid, org)`)

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_connector_org.py`:
```python
from __future__ import annotations

import pytest

from mavis.connectors import identity
from mavis.domain.records import Kind, Record
from mavis.store import db as dbm
from mavis.store.repo import connectors as repo


def rec(org_id=0, **kw):
    return Record(user_id=1, org_id=org_id, connector="hubspot", kind=Kind.DEAL, external_id="42", title="Deal",
                  fields={"currency": "USD", "stage_ref": "a", "pipeline_ref": "p",
                          "created_at": "2026-10-01T09:00:00+00:00", "modified_at": "2026-10-01T09:00:00+00:00"}, **kw)


async def test_same_key_in_two_spaces_does_not_collide(db):
    async with dbm.Session() as s:
        a, _ = await repo.upsert_record(s, rec(0), body_expires_at=None)
        b, _ = await repo.upsert_record(s, rec(7), body_expires_at=None)
        await s.commit()
    assert a.name == "NEW" and b.name == "NEW"
    assert await repo.record_keys_for(1, "hubspot", org_id=7) == ["hubspot:deal:42"]
    assert await repo.counts_for(1, org_id=0) != {} and await repo.counts_for(1, org_id=9) == {}


async def test_purging_one_space_leaves_the_other(db):
    async with dbm.Session() as s:
        await repo.upsert_record(s, rec(0), body_expires_at=None)
        await repo.upsert_record(s, rec(7), body_expires_at=None)
        await s.commit()
        assert await repo.purge_records(s, 1, "hubspot", org_id=7) == 1
        await s.commit()
    assert await repo.record_keys_for(1, "hubspot", org_id=0) == ["hubspot:deal:42"]
    assert await repo.record_keys_for(1, "hubspot", org_id=7) == []


async def test_cursors_are_per_space(db):
    a = await repo.get_cursor(1, "hubspot", "deals", org_id=0)
    b = await repo.get_cursor(1, "hubspot", "deals", org_id=7)
    assert a.id != b.id and b.org_id == 7


async def test_org_rows_never_reach_the_personal_graph(db, connectors_on, graph, memory):
    from mavis.connectors.ingest import Ingestor

    async with dbm.Session() as s:
        _, rid = await repo.upsert_record(s, rec(7), body_expires_at=None)
        await s.commit()
    out = await Ingestor(memory=memory).ingest(rid)
    row = await repo.get_record(rid)
    assert row.status == "processed" and out.graph_ops == 0
    assert await graph.entities(1) == []


async def test_bound_identity_round_trip(db, user):
    pid = await identity.composio_user_id(user.id, org_id=3)
    assert pid.endswith(f"-o3") and identity.parse_bound_id(pid) == (user.id, 3)
    assert await identity.composio_user_id(user.id) == pid[: -len("-o3")]
    assert identity.parse_bound_id("mavis-12") is None
    assert identity.parse_bound_id("mavis-prod-12-o5") == (12, 5)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_connector_org.py -q` -> FAIL (`unexpected keyword argument 'org_id'`).

- [ ] **Step 3: Implement**

Model columns (`store/models.py`), added to the three connector classes:
```python
    org_id: Mapped[int] = mapped_column(Integer, default=0, server_default="0", index=True)  # Track 6: 0 = personal
```
In each class replace the unique constraint that currently lists `user_id, record_key` (records), `user_id, connector, stream` (cursors) or `user_id, connector` (tokens) by the same columns with `org_id` inserted after `user_id`; keep the constraint names the connectors migration gave them.

Migration `<NN+7>_revintel_connector_org.py` (`revision = "revintel_connector_org"`, `down_revision = "revintel_rev"`):
```python
"""Track 6: org_id dimension on connector records, cursors and tokens."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "revintel_connector_org"
down_revision = "revintel_rev"
branch_labels = None
depends_on = None

TABLES = {"connector_records": ("user_id", "record_key"), "connector_cursors": ("user_id", "connector", "stream"),
          "connector_tokens": ("user_id", "connector")}


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    for table, key in TABLES.items():
        old = [u for u in insp.get_unique_constraints(table) if list(u["column_names"]) == list(key)]
        with op.batch_alter_table(table) as b:
            b.add_column(sa.Column("org_id", sa.Integer, nullable=False, server_default="0"))
            for u in old:
                b.drop_constraint(u["name"], type_="unique")
            b.create_unique_constraint(f"uq_{table}_scope", [key[0], "org_id", *key[1:]])
            b.create_index(f"ix_{table}_org_id", ["org_id"])


def downgrade() -> None:
    for table, key in TABLES.items():
        with op.batch_alter_table(table) as b:
            b.drop_index(f"ix_{table}_org_id")
            b.drop_constraint(f"uq_{table}_scope", type_="unique")
            b.create_unique_constraint(f"uq_{table}_key", list(key))
            b.drop_column("org_id")
```
Name the model constraints the same: `UniqueConstraint("user_id","org_id","record_key", name="uq_connector_records_scope")` etc. so `test_migrations_match_models` agrees.

Repo (`store/repo/connectors.py`), pattern shown for the four load-bearing functions; every other listed function gets the same `org_id: int = 0` keyword and `Row.org_id == org_id` in its `where`:
```python
async def upsert_record(session, record: Record, *, body_expires_at, status: str = "new"):
    ...  # existing lookup: add .where(ConnectorRecordRow.org_id == record.org_id); the new row sets org_id=record.org_id

async def get_cursor(user_id: int, connector: str, stream: str, *, org_id: int = 0) -> ConnectorCursorRow:
    ...  # lookup and create both use (user_id, org_id, connector, stream)

async def purge_records(session, user_id: int, connector: str, *, org_id: int = 0) -> int:
    ...  # deletes only rows of this org_id and writes tombstones with the same org_id

async def record_keys_for(user_id: int, connector: str, *, org_id: int = 0) -> list[str]:
    ...
```
`Record`: `org_id: int = 0` (excluded from `content_hash`; included in nothing else). `MapContext` and `ConnectorSpec` additions are plain defaulted fields. Registry: where `validate` checks `subject_key_fn` results start with `subject_prefix`, accept `tuple((spec.subject_prefix, *spec.extra_prefixes))` and add every extra prefix to the collision set against `LEDGER_BUILTIN_PREFIXES` and other specs.

`connectors/ingest.py`, first lines of `Ingestor.ingest(record_id)`:
```python
        row = await repo.get_record(record_id)
        if row is not None and row.org_id != 0:  # Track 6: org records feed the typed mirror, not the personal graph
            await repo.set_status(record_id, "processed")
            return IngestOutcome(graph_ops=0)
```
(use the actual status setter and outcome class names from connectors Task 19; the test pins `status == "processed"` and `graph_ops == 0`.)

`connectors/identity.py`:
```python
_BOUND = re.compile(r".*?-(\d+)-o(\d+)$")


async def composio_user_id(user_id: int, org_id: int | None = None) -> str:
    base = ...  # unchanged body (users.composio_user_id or UserRef(...).provider_id)
    return base if org_id is None else f"{base}-o{org_id}"


def parse_bound_id(provider_id: str) -> tuple[int, int] | None:
    m = _BOUND.fullmatch(provider_id or "")
    return (int(m.group(1)), int(m.group(2))) if m else None
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel/test_connector_org.py tests/connectors tests/store -q && uv run ruff check src tests` -> PASS (defaults-unchanged: `org_id=0` everywhere keeps every connectors test green).

- [ ] **Step 5: Commit**

```bash
git add src/mavis tests/revintel/test_connector_org.py
git commit -m "feat(revintel): org_id dimension on connector storage and org-bound identity"
```

---

### Task 23: HubSpot mappers (pure) and the record-to-projection bridge

**Start gate:** Tasks 21 and 22 merged (connectors `Record`, `MapContext`).

**Files:**
- Create: `src/mavis/connectors/specs/hubspot_map.py`, `src/mavis/revintel/projection/from_record.py`, `tests/revintel/hubspot_fixtures.py`, `tests/revintel/test_hubspot_map.py`

**Interfaces:**
- Consumes: `Record`, `MapContext(user_id, connector, self_ids, tz, activated_at, org_id, org_currency)`, Task 19 `Norm*`, Task 17 `StageIn`, Task 21 `money.minor`.
- Produces:
  - `hubspot_map.parse_ts(value) -> datetime | None` (ISO with or without `Z`, or epoch milliseconds as str or int)
  - mappers `map_deal(raw, ctx) -> Record`, `map_company(raw, ctx)`, `map_contact(raw, ctx)`, `map_engagement(raw, ctx)` (note, call, email, meeting, task; `raw["_type"]` names the object type), `map_pipeline(raw, ctx)` (one `CONFIG` record per pipeline with its stages), `map_owner(raw, ctx)`; all over HubSpot CRM v3 objects (`{"id", "properties": {...}, "associations": {...}, "propertiesWithHistory": {...}}`); every record carries `org_id=ctx.org_id`
  - `from_record.to_norm(record: Record) -> NormDeal | NormAccount | NormContact | NormInteraction | list[StageIn] | dict | None` (vendor-neutral: reads only the typed fields from Task 21; a `CONFIG` pipeline gives `list[StageIn]`, an owner gives `{"id", "email", "name"}`)
- Rules: amounts become `Decimal` strings in `fields.amount` (no floats); `is_closed`/`is_won` come from HubSpot's `hs_is_closed`/`hs_is_closed_won` flags, and for pipelines from each stage's `metadata.isClosed`/`metadata.probability`; a missing currency falls back to `ctx.org_currency`; free text (`dealname`, note bodies) is third-party content and only enters `title`/`body`.

- [ ] **Step 1: Write the failing test**

`tests/revintel/hubspot_fixtures.py`:
```python
DEAL = {"id": "42", "properties": {
    "dealname": "Initech renewal", "amount": "50000.5", "deal_currency_code": "EUR", "dealstage": "contractsent",
    "pipeline": "default", "closedate": "2026-12-15T00:00:00.000Z", "createdate": "2026-09-01T08:00:00.000Z",
    "hs_lastmodifieddate": "2026-10-02T10:30:00.000Z", "hubspot_owner_id": "501", "hs_next_step": "Send MSA",
    "hs_is_closed": "false", "hs_is_closed_won": "false", "hs_forecast_category": "COMMIT"},
    "associations": {"companies": {"results": [{"id": "9"}]}, "contacts": {"results": [{"id": "7"}, {"id": "8"}]}},
    "propertiesWithHistory": {"dealstage": [{"value": "contractsent", "timestamp": "2026-09-20T10:00:00.000Z"},
                                            {"value": "appointmentscheduled", "timestamp": "2026-09-01T08:00:00.000Z"}]}}
DEAL_WON = {"id": "43", "properties": {"dealname": "Globex pilot", "amount": "1200", "dealstage": "closedwon",
                                       "pipeline": "default", "createdate": "1759305600000",
                                       "hs_lastmodifieddate": "1759392000000", "hs_is_closed": "true",
                                       "hs_is_closed_won": "true"}}
COMPANY = {"id": "9", "properties": {"name": "Initech", "domain": "Initech.test", "hubspot_owner_id": "501",
                                     "industry": "SOFTWARE", "country": "India", "hs_lastmodifieddate": "2026-10-01T00:00:00Z"}}
CONTACT = {"id": "7", "properties": {"email": "Anita@Initech.test", "firstname": "Anita", "lastname": "Rao",
                                     "jobtitle": "CFO", "associatedcompanyid": "9", "hs_lastmodifieddate": "2026-10-01T00:00:00Z"}}
NOTE = {"_type": "note", "id": "n1", "properties": {"hs_timestamp": "2026-10-03T09:00:00.000Z",
                                                      "hs_note_body": "Call went well", "hubspot_owner_id": "501"},
        "associations": {"deals": {"results": [{"id": "42"}]}}}
PIPELINE = {"id": "default", "label": "Sales", "stages": [
    {"id": "appointmentscheduled", "label": "Etapa 1", "displayOrder": 0, "metadata": {"isClosed": "false", "probability": "0.2"}},
    {"id": "closedwon", "label": "Closed Won", "displayOrder": 5, "metadata": {"isClosed": "true", "probability": "1.0"}},
    {"id": "closedlost", "label": "Closed Lost", "displayOrder": 6, "metadata": {"isClosed": "true", "probability": "0.0"}}]}
OWNER = {"id": "501", "email": "raj@acme.test", "firstName": "Raj", "lastName": "S", "userId": 77}
```

`tests/revintel/test_hubspot_map.py`:
```python
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from mavis.connectors.spec import MapContext
from mavis.connectors.specs import hubspot_map as hm
from mavis.domain.records import Kind
from mavis.revintel.projection import from_record as fr
from mavis.revintel.projection.normalise import NormAccount, NormContact, NormDeal, NormInteraction
from mavis.revintel.projection.stages import StageIn
from tests.revintel import hubspot_fixtures as fx

CTX = MapContext(user_id=3, connector="hubspot", self_ids=frozenset(), tz="UTC", activated_at=None,
                 org_id=7, org_currency="INR")


def test_parse_ts_accepts_iso_and_epoch_ms():
    assert hm.parse_ts("2026-10-02T10:30:00.000Z") == datetime(2026, 10, 2, 10, 30, tzinfo=UTC)
    assert hm.parse_ts("1759392000000") == datetime(2025, 10, 2, 8, 0, tzinfo=UTC)
    assert hm.parse_ts(None) is None and hm.parse_ts("") is None


def test_deal_maps_to_typed_fields():
    r = hm.map_deal(fx.DEAL, CTX)
    f = r.fields
    assert (r.kind, r.external_id, r.org_id, r.title, r.record_key) == (Kind.DEAL, "42", 7, "Initech renewal", "hubspot:deal:42")
    assert (f["amount"], f["currency"], f["stage_ref"], f["pipeline_ref"], f["close_date"]) == ("50000.5", "EUR", "contractsent", "default", "2026-12-15")
    assert (f["owner_ref"], f["account_ref"], f["contact_refs"], f["is_closed"]) == ("501", "hubspot:company:9", ["hubspot:contact:7", "hubspot:contact:8"], False)
    assert [(h["to"]) for h in f["stage_history"]] == ["appointmentscheduled", "contractsent"]  # oldest first


def test_closed_flags_and_default_currency():
    f = hm.map_deal(fx.DEAL_WON, CTX).fields
    assert (f["is_closed"], f["is_won"], f["currency"]) == (True, True, "INR")


def test_deal_to_norm_converts_money_once():
    nd = fr.to_norm(hm.map_deal(fx.DEAL, CTX))
    assert isinstance(nd, NormDeal) and nd.amount_minor == 5000050 and nd.currency == "EUR"
    assert nd.close_date == date(2026, 12, 15) and nd.account_key == "hubspot:company:9"
    assert nd.stage_history[0][:2] == (None, "appointmentscheduled") and nd.stage_history[1][:2] == ("appointmentscheduled", "contractsent")
    assert nd.owner == {"id": "501"}


def test_company_contact_engagement_norms():
    a = fr.to_norm(hm.map_company(fx.COMPANY, CTX))
    c = fr.to_norm(hm.map_contact(fx.CONTACT, CTX))
    n = fr.to_norm(hm.map_engagement(fx.NOTE, CTX))
    assert isinstance(a, NormAccount) and a.domain == "initech.test" and a.attrs == {"industry": "SOFTWARE", "country": "India"}
    assert isinstance(c, NormContact) and (c.email, c.name, c.account_key) == ("anita@initech.test", "Anita Rao", "hubspot:company:9")
    assert isinstance(n, NormInteraction) and (n.kind, n.deal_keys, n.external_id) == ("note", ("hubspot:deal:42",), "n1")


def test_pipeline_and_owner():
    stages = fr.to_norm(hm.map_pipeline(fx.PIPELINE, CTX))
    assert stages[0] == StageIn("default", "appointmentscheduled", "Etapa 1", 0, False, False, 0.2)
    assert [(s.stage_ref, s.is_closed, s.is_won) for s in stages[1:]] == [("closedwon", True, True), ("closedlost", True, False)]
    assert fr.to_norm(hm.map_owner(fx.OWNER, CTX)) == {"id": "501", "email": "raj@acme.test", "name": "Raj S"}


def test_a_float_amount_in_the_payload_is_still_exact():
    raw = {**fx.DEAL, "properties": {**fx.DEAL["properties"], "amount": 0.1 + 0.2}}
    assert fr.to_norm(hm.map_deal(raw, CTX)).amount_minor == 30
```
(The last test pins that a JSON float is passed through `Decimal(str(round(x, 8)))` once at the mapper boundary, so `0.30000000000000004` becomes `0.3`.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_hubspot_map.py -q` -> FAIL (`No module named 'mavis.connectors.specs.hubspot_map'`).

- [ ] **Step 3: Implement**

`src/mavis/connectors/specs/hubspot_map.py`:
```python
"""HubSpot CRM v3 payloads to canonical Records. Pure: no I/O, no clock. Free text is third-party content."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from mavis.connectors.spec import MapContext
from mavis.domain.records import Actor, Kind, Record

CRM = "hubspot"


def parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    s = str(value)
    if s.isdigit():
        return datetime.fromtimestamp(int(s) / 1000, tz=UTC)
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def _truthy(v: Any) -> bool:
    return str(v).lower() == "true"


def _amount(v: Any) -> str | None:
    if v in (None, ""):
        return None
    return str(Decimal(str(round(v, 8))) if isinstance(v, float) else Decimal(str(v)))


def _ids(raw: dict, kind: str) -> list[str]:
    return [str(x["id"]) for x in (raw.get("associations", {}).get(kind, {}) or {}).get("results", [])]


def _ref(kind: str, ids: list[str]) -> list[str]:
    return [f"{CRM}:{kind}:{i}" for i in ids]


def _base(raw: dict, ctx: MapContext, kind: Kind, **kw) -> Record:
    p = raw.get("properties", {})
    ts = parse_ts(p.get("hs_lastmodifieddate") or p.get("lastmodifieddate") or p.get("hs_timestamp"))
    return Record(user_id=ctx.user_id, org_id=ctx.org_id, connector=CRM, kind=kind, external_id=str(raw["id"]),
                  occurred_at=ts, updated_at=ts, **kw)


def map_deal(raw: dict, ctx: MapContext) -> Record:
    p = raw["properties"]
    hist = sorted(((parse_ts(h["timestamp"]), h["value"]) for h in
                   raw.get("propertiesWithHistory", {}).get("dealstage", [])), key=lambda x: x[0])
    stage_history = [{"from": hist[i - 1][1] if i else None, "to": v, "at": t.isoformat()} for i, (t, v) in enumerate(hist)]
    close = parse_ts(p.get("closedate"))
    fields = {
        "amount": _amount(p.get("amount")), "currency": (p.get("deal_currency_code") or ctx.org_currency).upper(),
        "stage_ref": p["dealstage"], "pipeline_ref": p["pipeline"], "close_date": close.date().isoformat() if close else None,
        "owner_ref": p.get("hubspot_owner_id"), "is_closed": _truthy(p.get("hs_is_closed")),
        "is_won": _truthy(p.get("hs_is_closed_won")), "created_at": parse_ts(p["createdate"]).isoformat(),
        "modified_at": parse_ts(p["hs_lastmodifieddate"]).isoformat(), "next_step": p.get("hs_next_step") or None,
        "next_step_at": None, "forecast_category": p.get("hs_forecast_category"),
        "last_activity_at": (parse_ts(p.get("notes_last_updated")) or None) and parse_ts(p["notes_last_updated"]).isoformat(),
        "account_ref": (_ref("company", _ids(raw, "companies")) or [None])[0],
        "contact_refs": _ref("contact", _ids(raw, "contacts")), "stage_history": stage_history, "attrs": {},
    }
    return _base(raw, ctx, Kind.DEAL, title=p.get("dealname"), fields=fields)


def map_company(raw: dict, ctx: MapContext) -> Record:
    p = raw["properties"]
    attrs = {k: p[src] for k, src in (("industry", "industry"), ("country", "country")) if p.get(src)}
    return _base(raw, ctx, Kind.ACCOUNT, title=p.get("name"), fields={
        "domain": (p.get("domain") or "").lower() or None, "owner_ref": p.get("hubspot_owner_id"), "attrs": attrs})


def map_contact(raw: dict, ctx: MapContext) -> Record:
    p = raw["properties"]
    name = " ".join(x for x in (p.get("firstname"), p.get("lastname")) if x)
    email = (p.get("email") or "").lower() or None
    return _base(raw, ctx, Kind.CONTACT, title=name, actors=[Actor(role="contact", name=name, email=email)],
                 fields={"email": email, "title": p.get("jobtitle"),
                         "account_ref": f"{CRM}:company:{p['associatedcompanyid']}" if p.get("associatedcompanyid") else None})


def map_engagement(raw: dict, ctx: MapContext) -> Record:
    p, typ = raw["properties"], raw["_type"]
    body = p.get("hs_note_body") or p.get("hs_call_body") or p.get("hs_email_text") or p.get("hs_meeting_body")
    title = p.get("hs_email_subject") or p.get("hs_task_subject") or p.get("hs_meeting_title")
    return _base(raw, ctx, Kind.ENGAGEMENT, title=title, body=body, fields={
        "type": typ, "direction": p.get("hs_email_direction") or p.get("hs_call_direction"),
        "deal_refs": _ref("deal", _ids(raw, "deals")), "account_ref": (_ref("company", _ids(raw, "companies")) or [None])[0],
        "owner_ref": p.get("hubspot_owner_id"), "participants": [], "status": p.get("hs_task_status")})


def map_pipeline(raw: dict, ctx: MapContext) -> Record:
    return Record(user_id=ctx.user_id, org_id=ctx.org_id, connector=CRM, kind=Kind.CONFIG, external_id=f"pipeline:{raw['id']}",
                  title=raw.get("label"), fields={"type": "pipeline", "payload": raw})


def map_owner(raw: dict, ctx: MapContext) -> Record:
    name = " ".join(x for x in (raw.get("firstName"), raw.get("lastName")) if x)
    return Record(user_id=ctx.user_id, org_id=ctx.org_id, connector=CRM, kind=Kind.CONFIG, external_id=f"owner:{raw['id']}",
                  title=name, fields={"type": "owner", "payload": {"id": str(raw["id"]), "email": (raw.get("email") or "").lower(), "name": name}})


_ = date
```
(Delete the trailing `_ = date` and the unused `date` import if ruff flags them.)

`src/mavis/revintel/projection/from_record.py`:
```python
"""Canonical Record (typed fields) to the projector's vendor-neutral shapes."""

from __future__ import annotations

from datetime import date, datetime

from mavis.domain.records import Kind, Record
from mavis.revintel import money
from mavis.revintel.projection.normalise import NormAccount, NormContact, NormDeal, NormInteraction
from mavis.revintel.projection.stages import StageIn


def _dt(v) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


def to_norm(r: Record):
    f, crm, ext = r.fields, r.connector, r.external_id
    if r.kind is Kind.DEAL:
        hist = tuple((h["from"], h["to"], _dt(h["at"])) for h in f.get("stage_history", []))
        return NormDeal(
            crm=crm, external_id=ext, name=r.title or "", currency=f["currency"], stage_ref=f["stage_ref"],
            amount_minor=money.minor(f["amount"], f["currency"]) if f.get("amount") is not None else None,
            pipeline_ref=f["pipeline_ref"], close_date=date.fromisoformat(f["close_date"]) if f.get("close_date") else None,
            created_at=_dt(f["created_at"]), modified_at=_dt(f["modified_at"]),
            owner={"id": f["owner_ref"]} if f.get("owner_ref") else None, account_key=f.get("account_ref"),
            contact_keys=tuple(f.get("contact_refs", [])), next_step=f.get("next_step"),
            next_step_at=_dt(f.get("next_step_at")), forecast_category=f.get("forecast_category"),
            last_activity_at=_dt(f.get("last_activity_at")), attrs=f.get("attrs", {}), stage_history=hist)
    if r.kind is Kind.ACCOUNT:
        return NormAccount(crm, ext, r.title or "", f.get("domain"), {"id": f["owner_ref"]} if f.get("owner_ref") else None,
                           f.get("attrs", {}))
    if r.kind is Kind.CONTACT:
        return NormContact(crm, ext, r.title or "", f.get("email"), f.get("account_ref"), f.get("title"), None)
    if r.kind is Kind.ENGAGEMENT:
        return NormInteraction(crm, ext, f["type"], r.occurred_at, f.get("direction"),
                               {"id": f["owner_ref"]} if f.get("owner_ref") else None, tuple(f.get("deal_refs", [])),
                               f.get("account_ref"), tuple(f.get("participants", [])))
    if r.kind is Kind.CONFIG and f["type"] == "pipeline":
        pl = f["payload"]
        return [StageIn(pl["id"], s["id"], s["label"], int(s["displayOrder"]), _t(s["metadata"].get("isClosed")),
                        _won(s), float(s["metadata"]["probability"]) if s["metadata"].get("probability") else None)
                for s in pl["stages"]]
    if r.kind is Kind.CONFIG:
        return dict(f["payload"])
    return None


def _t(v) -> bool:
    return str(v).lower() == "true"


def _won(stage: dict) -> bool:
    m = stage["metadata"]
    return _t(m.get("isClosed")) and float(m.get("probability") or 0) >= 1.0
```
(HubSpot marks won vs lost only by probability 1.0 vs 0.0 on closed stages in the pipelines API, which is a CRM flag, not a name; `test_pipeline_and_owner` pins both.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel/test_hubspot_map.py -q && uv run ruff check src tests` -> PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis/connectors/specs/hubspot_map.py src/mavis/revintel/projection/from_record.py tests/revintel
git commit -m "feat(revintel): HubSpot mappers and the record-to-projection bridge"
```

---

### Task 24: The HubSpot connector spec (streams, triggers, polling, rate budget, write actions)

**Start gate:** Tasks 21 to 23 merged; connectors Tasks 8, 9, 12, 23 merged.

**Files:**
- Create: `src/mavis/connectors/specs/hubspot.py`, `tests/connectors/revintel/__init__.py`, `tests/connectors/revintel/test_hubspot_spec.py`
- Modify: `src/mavis/config.py` (`hubspot_poll_minutes: int = 5`, `hubspot_reconcile_hours: int = 6`, `hubspot_backfill_days: int = 365`, `hubspot_backfill_max: int = 5000`), `src/mavis/connectors/specs/__init__.py` (import), `docs/` none

**Interfaces:**
- Consumes: connectors `ConnectorSpec`, `Stream`, `Backfill`, `Poll`, `Webhook`, `Paginate`, `ComposioManaged`; Task 23 mappers; Task 20 `RateBudget`; existing `ActionSpec`.
- Produces:
  - `SPEC: ConnectorSpec` with `id="hubspot"`, `category=Category.WORK`, `provider=Provider.COMPOSIO`, `auth=ComposioManaged("hubspot")`, `status=Status.BETA`, `sensitivity=Sensitivity.NORMAL`, `subject_prefix="deal"`, `extra_prefixes=("acct",)`, `org_capable=True`, `graph=()`, `extract_text=False`, `rate_budget=RateBudget(per_second=settings.hubspot_per_second, per_day=settings.hubspot_per_day)`
  - ten streams: deals (`Kind.DEAL`), companies (`ACCOUNT`), contacts (`CONTACT`), notes, calls, emails, meetings, tasks (`ENGAGEMENT`, each with its own mapper closure fixing `_type`), pipelines and owners (`CONFIG`)
  - deals, contacts and companies: `Webhook(<stage-updated or created trigger>) | Poll(every_minutes=settings.hubspot_poll_minutes, cursor="hs_lastmodifieddate")`; engagements: `Poll` only; pipelines and owners: `Poll(every_minutes=60 * settings.hubspot_reconcile_hours)`
  - `SLUGS: dict[str, str]` (Composio action slugs and trigger names; every value is a "verify" value, checked nightly by `scripts/verify_composio.py` which already walks every Composio spec's `list_action`, `fetch_action` and `Webhook.triggers`)
  - three `ActionSpec`s `hubspot.add_note` (OUTWARD), `hubspot.add_task` (OUTWARD), `hubspot.update_deal` (OUTWARD) with `agents=frozenset({"revintel"})`: they exist for `provider.execute` from the org write tools (Task 27) and are never offered to a chat agent

- [ ] **Step 1: Write the failing test**

`tests/connectors/revintel/test_hubspot_spec.py`:
```python
from __future__ import annotations

import pytest

from mavis.connectors.registry import ConnectorRegistry
from mavis.connectors.specs.hubspot import SLUGS, SPEC
from mavis.domain.policy import RiskClass
from mavis.domain.records import Kind


def test_the_spec_passes_registry_validation():
    reg = ConnectorRegistry([SPEC])
    assert reg.get("hubspot") is SPEC and SPEC.org_capable and SPEC.graph == ()


def test_streams_cover_the_crm_objects_with_the_right_kinds():
    kinds = {s.list_action: s.kind for s in SPEC.streams}
    assert len(SPEC.streams) == 10
    assert {SLUGS["list_deals"]: Kind.DEAL, SLUGS["list_companies"]: Kind.ACCOUNT, SLUGS["list_contacts"]: Kind.CONTACT,
            SLUGS["list_pipelines"]: Kind.CONFIG, SLUGS["list_owners"]: Kind.CONFIG}.items() <= kinds.items()
    assert sum(1 for s in SPEC.streams if s.kind is Kind.ENGAGEMENT) == 5


def test_incremental_modes(settings):
    by = {s.list_action: s for s in SPEC.streams}
    deals = by[SLUGS["list_deals"]]
    assert deals.incremental.poll.every_minutes == settings.hubspot_poll_minutes == 5
    assert deals.incremental.webhook.triggers == (SLUGS["trigger_deal_stage"],)
    assert deals.backfill.window_days == settings.hubspot_backfill_days
    assert by[SLUGS["list_owners"]].incremental.webhook is None
    assert all(s.incremental.webhook is None for s in SPEC.streams if s.kind is Kind.ENGAGEMENT)


def test_rate_budget_comes_from_settings_not_constants(settings, monkeypatch):
    assert SPEC.rate_budget.per_day == settings.hubspot_per_day and SPEC.rate_budget.per_second == settings.hubspot_per_second


def test_write_actions_are_outward_and_hidden_from_chat_agents():
    names = {a.name: a for a in SPEC.actions}
    assert set(names) == {"hubspot.add_note", "hubspot.add_task", "hubspot.update_deal"}
    assert all(a.risk is RiskClass.OUTWARD and a.agents == frozenset({"revintel"}) for a in names.values())


def test_mappers_are_the_hubspot_ones():
    from tests.revintel import hubspot_fixtures as fx
    from mavis.connectors.spec import MapContext

    ctx = MapContext(user_id=1, connector="hubspot", self_ids=frozenset(), tz="UTC", activated_at=None, org_id=2)
    deals = next(s for s in SPEC.streams if s.kind is Kind.DEAL)
    assert deals.map(fx.DEAL, ctx).external_id == "42"
    note = next(s for s in SPEC.streams if s.list_action == SLUGS["list_notes"])
    assert note.map({**fx.NOTE}, ctx).fields["type"] == "note"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/connectors/revintel/test_hubspot_spec.py -q` -> FAIL (`No module named 'mavis.connectors.specs.hubspot'`).

- [ ] **Step 3: Implement**

`config.py` (Track 6 block): `hubspot_poll_minutes: int = 5`, `hubspot_reconcile_hours: int = 6`, `hubspot_backfill_days: int = 365`, `hubspot_backfill_max: int = 5000`.

`src/mavis/connectors/specs/hubspot.py`:
```python
"""HubSpot as a declarative connector (spec 8.3 row 1): managed OAuth through Composio, org service sync."""

from __future__ import annotations

from functools import partial

from pydantic import Field

from mavis.config import get_settings
from mavis.connectors.spec import (
    Backfill, Category, ComposioManaged, ConnectorSpec, Deletes, Paginate, Poll, Provider, Sensitivity, Status,
    Stream, Webhook,
)
from mavis.connectors.specs import hubspot_map as hm
from mavis.domain.args import ToolArgs
from mavis.domain.policy import RiskClass
from mavis.domain.records import Kind
from mavis.revintel.ratebudget import RateBudget
from mavis.tools.integrations.actions import ActionSpec

# Composio slugs and trigger names. Every value is a "verify" value (spec 8.3): scripts/verify_composio.py walks
# these through the spec nightly. If a slug differs on the live catalogue, change it here and nowhere else.
SLUGS = {
    "list_deals": "HUBSPOT_SEARCH_DEALS", "list_companies": "HUBSPOT_SEARCH_COMPANIES",
    "list_contacts": "HUBSPOT_SEARCH_CONTACTS", "list_notes": "HUBSPOT_LIST_NOTES", "list_calls": "HUBSPOT_LIST_CALLS",
    "list_emails": "HUBSPOT_LIST_EMAILS", "list_meetings": "HUBSPOT_LIST_MEETINGS", "list_tasks": "HUBSPOT_LIST_TASKS",
    "list_pipelines": "HUBSPOT_LIST_PIPELINES", "list_owners": "HUBSPOT_LIST_OWNERS",
    "trigger_deal_stage": "HUBSPOT_DEAL_STAGE_UPDATED_TRIGGER", "trigger_contact_created": "HUBSPOT_CONTACT_CREATED_TRIGGER",
    "add_note": "HUBSPOT_CREATE_NOTE", "add_task": "HUBSPOT_CREATE_TASK", "update_deal": "HUBSPOT_UPDATE_DEAL",
}
_s = get_settings()
_PAGE = Paginate(kind="cursor", field="after", page_param="after", next_path="data.paging.next.after")


def _stream(kind: Kind, slug: str, mapper, *, poll_min: int | None, trigger: str | None = None) -> Stream:
    poll = Poll(every_minutes=poll_min, cursor="hs_lastmodifieddate") if poll_min else None
    inc = (Webhook(trigger) | poll) if trigger else (poll | None)
    return Stream(kind=kind, list_action=SLUGS[slug], map=mapper, paginate=_PAGE,
                  backfill=Backfill(window_days=_s.hubspot_backfill_days, max_records=_s.hubspot_backfill_max),
                  incremental=inc, deletes=Deletes.IGNORED)


def _eng(kind_name: str):
    return lambda raw, ctx: hm.map_engagement({**raw, "_type": kind_name}, ctx)


STREAMS = (
    _stream(Kind.DEAL, "list_deals", hm.map_deal, poll_min=_s.hubspot_poll_minutes, trigger=SLUGS["trigger_deal_stage"]),
    _stream(Kind.ACCOUNT, "list_companies", hm.map_company, poll_min=_s.hubspot_poll_minutes),
    _stream(Kind.CONTACT, "list_contacts", hm.map_contact, poll_min=_s.hubspot_poll_minutes,
            trigger=SLUGS["trigger_contact_created"]),
    *(_stream(Kind.ENGAGEMENT, f"list_{n}s", _eng(n), poll_min=_s.hubspot_poll_minutes)
      for n in ("note", "call", "email", "meeting", "task")),
    _stream(Kind.CONFIG, "list_pipelines", hm.map_pipeline, poll_min=60 * _s.hubspot_reconcile_hours),
    _stream(Kind.CONFIG, "list_owners", hm.map_owner, poll_min=60 * _s.hubspot_reconcile_hours),
)


class AddNoteArgs(ToolArgs):
    deal_id: str
    body: str = Field(max_length=4000)


class AddTaskArgs(ToolArgs):
    deal_id: str
    title: str = Field(max_length=300)
    due: str | None = None


class UpdateDealArgs(ToolArgs):
    deal_id: str
    properties: dict[str, str]


_AGENTS = frozenset({"revintel"})  # an agent name nothing else uses: provider.execute only, never a chat tool
ACTIONS = (
    ActionSpec("hubspot.add_note", "hubspot", "Add a note to a HubSpot deal.", AddNoteArgs, RiskClass.OUTWARD, _AGENTS),
    ActionSpec("hubspot.add_task", "hubspot", "Create a HubSpot task on a deal.", AddTaskArgs, RiskClass.OUTWARD, _AGENTS),
    ActionSpec("hubspot.update_deal", "hubspot", "Update properties of a HubSpot deal.", UpdateDealArgs,
               RiskClass.OUTWARD, _AGENTS),
)

SPEC = ConnectorSpec(
    id="hubspot", name="HubSpot", category=Category.WORK, provider=Provider.COMPOSIO, auth=ComposioManaged("hubspot"),
    status=Status.BETA, sensitivity=Sensitivity.NORMAL, subject_prefix="deal", extra_prefixes=("acct",),
    streams=STREAMS, graph=(), extract_text=False, actions=ACTIONS, org_capable=True,
    reads="your deals, companies, contacts and logged activity", does="adds notes and tasks and updates deals after you approve",
    rate_budget=RateBudget(per_second=_s.hubspot_per_second, per_day=_s.hubspot_per_day),
    subject_key_fn=lambda r: f"deal:hubspot:{r.external_id}" if r.kind is Kind.DEAL else f"acct:hubspot:{r.external_id}",
)

_ = partial
```
(Drop `_ = partial` and the import if unused.) Add `from mavis.connectors.specs import hubspot  # noqa: F401` to `connectors/specs/__init__.py` so the registry imports it; keep `status=BETA` and the connector hidden for non-org users by `connector_visible` (it is `org_capable` and not personal: `identity.connector_visible` returns False for `org_capable` specs when `org_id == 0`; add that one rule and its test line).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/connectors tests/revintel -q && uv run ruff check src tests` -> PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis tests/connectors/revintel
git commit -m "feat(revintel): HubSpot connector spec with triggers, polling, rate budget and actions"
```

---

### Task 25: Org connection binding (service and per-user)

**Start gate:** Task 22 merged; connectors Tasks 12, 22 merged (`connect_flow`, provider port); plan 11 merged.

**Files:**
- Create: `src/mavis/revintel/bindings.py`, `tests/revintel/test_bindings.py`
- Modify: `src/mavis/domain/integrations.py` (`BoundRef`)

**Interfaces:**
- Consumes: Task 6 repo, Task 9 audit, Task 22 `composio_user_id(user_id, org_id)`, connectors `IntegrationProvider.connect_link/status/execute`.
- Produces:
  - `class BoundRef(UserRef)` with `org_id: int` and `bound_id: str`; `provider_id` returns `bound_id` (so every Composio call made through it uses the org-bound grant)
  - `async bindings.ref_for(user_id: int, org_id: int) -> BoundRef`
  - `async bindings.begin_connect(provider, org_id: int, user_id: int, connector: str, *, mode: Literal["service","per_user"], callback_url: str) -> str` (service needs `connector.manage`, per_user needs `connector.use_own`; the spec must be `org_capable`; returns the consent link; audits `connector.change`)
  - `async bindings.on_connected(org_id: int, user_id: int, connector: str, *, mode) -> None` (sets `org_connections.status="active"`, records `connected_by_user_id` for service, starts the org sync via `engine.start(user_id, connector, org_id=org_id)`)
  - `async bindings.service_binding(org_id, connector) -> OrgConnection | None`, `async bindings.disconnect(org_id, connector, mode, *, actor_user_id) -> None` (revokes, purges by origin, audits)
  - `async bindings.removal_blocked(org_id, user_id) -> list[str]` (connectors for which this user is the only service binding: removing them needs a re-bind first)

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_bindings.py`:
```python
from __future__ import annotations

import pytest

from mavis.domain.integrations import BoundRef
from mavis.revintel import bindings
from mavis.revintel.models import OrgAuditLog
from mavis.revintel.repo import orgs


class Prov:
    def __init__(self):
        self.links, self.revoked = [], []

    async def connect_link(self, user, toolkit, callback_url):
        self.links.append((user.provider_id, toolkit))
        return f"https://connect.test/{user.provider_id}"

    async def revoke(self, user, connector):
        self.revoked.append((user.provider_id, connector))


async def test_ref_uses_the_org_bound_identity(world):
    ref = await bindings.ref_for(world.u["Raj"].id, world.org.id)
    assert isinstance(ref, BoundRef) and ref.provider_id == f"mavis-{world.u['Raj'].id}-o{world.org.id}"


async def test_only_admins_connect_the_service_account(world, connectors_on):
    prov = Prov()
    link = await bindings.begin_connect(prov, world.org.id, world.u["Ada"].id, "hubspot", mode="service", callback_url="https://x/cb")
    assert link.endswith(f"-o{world.org.id}") and prov.links[0][1] == "hubspot"
    with pytest.raises(PermissionError):
        await bindings.begin_connect(prov, world.org.id, world.u["Raj"].id, "hubspot", mode="service", callback_url="https://x/cb")


async def test_every_member_may_connect_their_own_account_but_viewers_may_not(world, connectors_on):
    prov = Prov()
    await bindings.begin_connect(prov, world.org.id, world.u["Raj"].id, "hubspot", mode="per_user", callback_url="https://x/cb")
    with pytest.raises(PermissionError):
        await bindings.begin_connect(prov, world.org.id, world.u["Vic"].id, "hubspot", mode="per_user", callback_url="https://x/cb")


async def test_a_connector_that_is_not_org_capable_is_refused(world, connectors_on):
    with pytest.raises(LookupError):
        await bindings.begin_connect(Prov(), world.org.id, world.u["Ada"].id, "strava", mode="service", callback_url="https://x/cb")


async def test_on_connected_records_the_binding_and_starts_the_sync(world, connectors_on, monkeypatch):
    started = []

    class Eng:
        async def start(self, user_id, connector, *, org_id=0):
            started.append((user_id, connector, org_id))

    monkeypatch.setattr(bindings, "engine", lambda: Eng())
    await bindings.on_connected(world.org.id, world.u["Ada"].id, "hubspot", mode="service")
    b = await bindings.service_binding(world.org.id, "hubspot")
    assert (b.status, b.connected_by_user_id, b.mode) == ("active", world.u["Ada"].id, "service")
    assert started == [(world.u["Ada"].id, "hubspot", world.org.id)]
    assert await bindings.removal_blocked(world.org.id, world.u["Ada"].id) == ["hubspot"]
    assert await bindings.removal_blocked(world.org.id, world.u["Raj"].id) == []


async def test_disconnect_revokes_and_audits(world, connectors_on, monkeypatch):
    prov = Prov()
    monkeypatch.setattr(bindings, "engine", lambda: type("E", (), {"start": lambda *a, **k: None})())
    await bindings.on_connected(world.org.id, world.u["Ada"].id, "hubspot", mode="service")
    await bindings.disconnect(prov, world.org.id, "hubspot", "service", actor_user_id=world.u["Olu"].id)
    assert prov.revoked == [(f"mavis-{world.u['Ada'].id}-o{world.org.id}", "hubspot")]
    assert (await bindings.service_binding(world.org.id, "hubspot")) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_bindings.py -q` -> FAIL (`cannot import name 'BoundRef'`).

- [ ] **Step 3: Implement**

`domain/integrations.py`:
```python
class BoundRef(UserRef):
    """A user's org-bound grant: every provider call made through it uses the org-bound Composio identity."""

    org_id: int
    bound_id: str

    @property
    def provider_id(self) -> str:
        return self.bound_id
```
`src/mavis/revintel/bindings.py`:
```python
"""Org connections (spec 8.2): a service account for the CRM mirror, or a member's own org-bound account."""

from __future__ import annotations

from typing import Literal

from sqlalchemy import select

from mavis.connectors import identity
from mavis.connectors.registry import get_registry
from mavis.domain.integrations import BoundRef
from mavis.revintel import audit
from mavis.revintel.authz import authorize
from mavis.revintel.models import OrgConnection
from mavis.revintel.principal import resolve_principal
from mavis.revintel.repo import orgs
from mavis.store.db import Session

Mode = Literal["service", "per_user"]


def engine():
    from mavis.connectors.wiring import current_engine

    return current_engine()


async def ref_for(user_id: int, org_id: int) -> BoundRef:
    return BoundRef(user_id=user_id, org_id=org_id, bound_id=await identity.composio_user_id(user_id, org_id=org_id))


async def begin_connect(provider, org_id: int, user_id: int, connector: str, *, mode: Mode, callback_url: str) -> str:
    spec = get_registry().get(connector)
    if spec is None or not spec.org_capable:
        raise LookupError(f"{connector} cannot be bound to an org")
    p = await resolve_principal(user_id, space=f"org:{org_id}")
    action = "connector.manage" if mode == "service" else "connector.use_own"
    if not authorize(p, action, None, snapshot=await orgs.load_snapshot(org_id)).allowed:
        await audit.append(org_id, action=action, decision="deny", actor_user_id=user_id, reason="role_lacks_action")
        raise PermissionError(action)
    ref = await ref_for(user_id, org_id)
    link = await provider.connect_link(ref, spec.auth.toolkit, callback_url)
    await audit.append(org_id, action=action, decision="executed", actor_user_id=user_id, role=p.role.value,
                       resource_type="connector", resource_ids=[connector], detail={"mode": mode, "op": "connect"})
    return link


async def on_connected(org_id: int, user_id: int, connector: str, *, mode: Mode) -> None:
    async with Session() as s:
        row = await s.scalar(select(OrgConnection).where(OrgConnection.org_id == org_id, OrgConnection.connector == connector,
                                                         OrgConnection.mode == mode))
        if row is None:
            row = OrgConnection(org_id=org_id, connector=connector, mode=mode)
            s.add(row)
        row.status, row.connected_by_user_id = "active", user_id
        row.composio_user_id = await identity.composio_user_id(user_id, org_id=org_id)
        await s.commit()
    await engine().start(user_id, connector, org_id=org_id)


async def service_binding(org_id: int, connector: str) -> OrgConnection | None:
    async with Session() as s:
        return await s.scalar(select(OrgConnection).where(
            OrgConnection.org_id == org_id, OrgConnection.connector == connector, OrgConnection.mode == "service",
            OrgConnection.status == "active"))


async def disconnect(provider, org_id: int, connector: str, mode: Mode, *, actor_user_id: int) -> None:
    async with Session() as s:
        row = await s.scalar(select(OrgConnection).where(OrgConnection.org_id == org_id, OrgConnection.connector == connector,
                                                         OrgConnection.mode == mode))
        if row is None:
            return
        who = row.connected_by_user_id
        await s.delete(row)
        await audit.append(org_id, action="connector.manage", decision="executed", actor_user_id=actor_user_id,
                           resource_type="connector", resource_ids=[connector], detail={"mode": mode, "op": "disconnect"}, session=s)
        await s.commit()
    await provider.revoke(await ref_for(who, org_id), connector)
    from mavis.connectors.purge import purge_connector

    await purge_connector(who, connector, org_id=org_id)


async def removal_blocked(org_id: int, user_id: int) -> list[str]:
    async with Session() as s:
        rows = await s.scalars(select(OrgConnection.connector).where(
            OrgConnection.org_id == org_id, OrgConnection.mode == "service", OrgConnection.status == "active",
            OrgConnection.connected_by_user_id == user_id))
        return sorted(rows)
```
(`current_engine()` and `purge_connector(user_id, connector, *, org_id=0)` are the connectors wiring/purge entry points of plan 14 Tasks 16 and 20; add the `org_id` keyword there if Task 22 has not, with `org_id=0` defaults.) Wire `removal_blocked` into `orgs.remove_member`: when it returns a non-empty list the removal raises `ValueError("rebind first: hubspot")` (extend `test_membership_changes...` with one case once connectors are merged).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel tests/connectors -q && uv run ruff check src tests` -> PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis tests/revintel/test_bindings.py
git commit -m "feat(revintel): org service and per-user connection bindings"
```

---

### Task 26: Org sync wiring: budgeted provider, projection job, hygiene lists

**Start gate:** Tasks 19, 20, 22 to 25 merged; connectors Tasks 16 to 18 merged.

**Files:**
- Create: `src/mavis/revintel/syncing.py`, `src/mavis/revintel/projection/job.py`, `src/mavis/revintel/hygiene.py`, `tests/revintel/test_org_sync.py`
- Modify: `src/mavis/connectors/sync.py` (thread `org_id` through the engine), `src/mavis/connectors/wiring.py` (register the projection handler), `src/mavis/revintel/commands.py` (`/org hygiene`)
- Shared: `connectors/sync.py` and `wiring.py` (connectors-owned; edits are signature additions with `org_id: int = 0` defaults)

**Interfaces:**
- Produces:
  - SyncEngine threading (every change keeps `org_id=0` behaviour identical): `start(user_id, connector, *, org_id=0)`, `backfill(user_id, connector, kind, *, org_id=0)`, `poll(...)`, `ingest_page(user_id, spec, stream, items, *, historical_before, org_id=0)`; job payloads carry `"org_id"`; `get_cursor(..., org_id=org_id)`; `MapContext(..., org_id=org_id, org_currency=<org base currency>)`; each mapped `Record` gets `org_id=org_id`; `CONNECTOR_RECORD` event payload gains `"org_id"`
  - `syncing.BudgetedProvider(inner, buckets: BucketRegistry)`: implements `list_records` and `execute` by calling `acquire("sync"|"backfill"|"interactive")` first; a refused grant raises connectors' `RateLimited(retry_after_s=grant.wait_s or 60)` (the engine already pauses the stream and leaves the cursor); a provider `RateLimited` calls `note_rate_limited`; success calls `note_ok`; `BucketRegistry.get(org_id, connector) -> BudgetBucket` (from `spec.rate_budget`); `poll_every(org_id, connector, base)` for the poll wakeup
  - `job.project_record(record_id: int) -> ProjectResult | None`: loads the connector row, `to_norm`, calls the Task 19 projector (CONFIG pipelines go to `import_stages`; CONFIG owners fill `crm_owner_refs` when the e-mail matches exactly one member's connected e-mail, else are stored for the admin list); `job.on_connector_record(event)` registered on `CONNECTOR_RECORD` for `org_id != 0`; `job.reproject(org_id) -> int` (rebuild from records after a spec version bump); deal-change listeners `job.on_deal_changed(fn)` (used by alerts, Task 48)
  - `hygiene.unmapped_owners(org_id) -> list[tuple[str, int]]` (CRM owner ref, deal count), `hygiene.deals_without_next_step(org_id, flt) -> int`, `/org hygiene` (needs `member.manage`) listing both
  - `per_user` sync mode: records synced by a member's own org-bound token add `rev_access(source="crm_visible")` rows for that member (service mode adds none)

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_org_sync.py`:
```python
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from mavis.connectors.registry import get_registry
from mavis.connectors.sync import SyncEngine
from mavis.revintel import hygiene, syncing
from mavis.revintel.projection import job, owners
from mavis.revintel.ratebudget import RateBudget
from mavis.revintel.rev_models import RevAccess, RevDeal, RevStageConfig
from mavis.store.db import Session
from mavis.tools.integrations.base import Page
from tests.revintel import hubspot_fixtures as fx
from tests.tools.integrations.fakes import FakeProvider

NOW = datetime(2026, 10, 8, 9, tzinfo=UTC)


@pytest.fixture
def hub(connectors_on, world, recording_bus, monkeypatch):
    from mavis.connectors.specs.hubspot import SLUGS

    prov = FakeProvider()
    prov.pages[SLUGS["list_pipelines"]] = [Page(items=[fx.PIPELINE], next_cursor=None, has_more=False)]
    prov.pages[SLUGS["list_deals"]] = [Page(items=[fx.DEAL, fx.DEAL_WON], next_cursor=None, has_more=False)]
    eng = SyncEngine(prov, get_registry(), recording_bus, clock=lambda: NOW)
    return eng, prov, recording_bus


async def test_service_sync_projects_deals_for_the_org(hub, world):
    eng, prov, bus = hub
    admin = world.u["Ada"].id
    await owners.set_crm_owner(world.org.id, world.u["Raj"].id, "hubspot", owner_id="501", email=None, actor_user_id=admin)
    await eng.start(admin, "hubspot", org_id=world.org.id)
    for kind in ("config", "deal"):
        await eng.backfill(admin, "hubspot", kind, org_id=world.org.id)
    for ev in [e for e in bus.events if e.payload.get("org_id") == world.org.id]:
        await job.on_connector_record(ev)
    async with Session() as s:
        deals = {d.record_key: d for d in await s.scalars(select(RevDeal))}
        stages = await s.scalar(select(func.count()).select_from(RevStageConfig))
    assert set(deals) == {"hubspot:deal:42", "hubspot:deal:43"} and stages == 3
    assert deals["hubspot:deal:42"].owner_user_id == world.u["Raj"].id
    assert deals["hubspot:deal:43"].owner_unmapped is False or deals["hubspot:deal:43"].owner_user_id is None
    assert deals["hubspot:deal:43"].is_won is True  # closedwon is a won-category stage from the pipeline flags


async def test_personal_records_are_not_projected(hub, world):
    eng, prov, bus = hub
    await eng.start(world.u["Raj"].id, "hubspot")  # org_id 0
    await eng.backfill(world.u["Raj"].id, "hubspot", "deal")
    for ev in bus.events:
        assert await job.on_connector_record(ev) is None


class Clock:
    t = NOW


async def test_budgeted_provider_turns_a_refusal_into_rate_limited(world):
    from mavis.domain.errors import RateLimited

    inner = FakeProvider()
    reg = syncing.BucketRegistry(clock=lambda: NOW, budgets={"hubspot": RateBudget(per_second=1000, per_day=10)})
    prov = syncing.BudgetedProvider(inner, reg)
    from mavis.connectors.specs.hubspot import SLUGS
    from mavis.domain.integrations import BoundRef
    from mavis.tools.integrations.base import StreamRef

    ref = BoundRef(user_id=1, org_id=world.org.id, bound_id="mavis-1-o1")
    inner.pages[SLUGS["list_deals"]] = [Page(items=[], next_cursor=None, has_more=False)] * 20
    stream = StreamRef(connector="hubspot", kind="deal", action=SLUGS["list_deals"], paginate={"kind": "cursor"})
    for _ in range(6):
        await prov.list_records(ref, stream, None, 50)  # sync share is 60 percent of 10 calls
    with pytest.raises(RateLimited):
        await prov.list_records(ref, stream, None, 50)


async def test_hygiene_lists_unmapped_owners_and_missing_next_steps(world):
    from mavis.revintel.projection import project
    from mavis.revintel.projection.normalise import NormDeal

    t = NOW
    for i, owner in enumerate(["ghost", "ghost", None]):
        await project.project_deal(world.org.id, NormDeal("hubspot", str(i), f"D{i}", 100, "USD", "a", "p", None, t, t,
                                                          {"id": owner} if owner else None, None, (), None, None, None, None))
    assert await hygiene.unmapped_owners(world.org.id) == [("ghost", 2)]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_org_sync.py -q` -> FAIL (`start() got an unexpected keyword argument 'org_id'`).

- [ ] **Step 3: Implement**

`connectors/sync.py` edits (each is a signature or argument change; confirm the line with `grep -n "def start\|def backfill\|def poll\|def ingest_page\|get_cursor\|MapContext(\|Record(" src/mavis/connectors/sync.py`): add `*, org_id: int = 0` to `start`, `backfill`, `poll`, `ingest_page`, `reconcile`; include `"org_id": org_id` in every enqueued job payload and read it back in the handlers; pass `org_id=org_id` to `get_cursor` and `upsert_record`; build `MapContext(..., org_id=org_id, org_currency=await _org_currency(org_id))` where `_org_currency` returns `Org.base_currency` for `org_id != 0` else `"USD"`; after mapping set `record = record.model_copy(update={"org_id": org_id})`; add `"org_id": org_id` to the `CONNECTOR_RECORD` event payload.

`src/mavis/revintel/syncing.py`:
```python
"""Per-account call budgets around the provider (spec 8.5). The engine already pauses on RateLimited."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from mavis.domain.errors import RateLimited
from mavis.revintel.ratebudget import BudgetBucket, RateBudget


class BucketRegistry:
    def __init__(self, clock: Callable[[], datetime], budgets: dict[str, RateBudget] | None = None) -> None:
        self._clock, self._budgets, self._buckets = clock, budgets, {}

    def get(self, org_id: int, connector: str) -> BudgetBucket:
        key = (org_id, connector)
        if key not in self._buckets:
            rb = (self._budgets or {}).get(connector)
            if rb is None:
                from mavis.connectors.registry import get_registry

                rb = get_registry().get(connector).rate_budget
            self._buckets[key] = BudgetBucket(rb, self._clock)
        return self._buckets[key]

    def poll_every(self, org_id: int, connector: str, base_minutes: int) -> int:
        return self.get(org_id, connector).poll_every(base_minutes)


class BudgetedProvider:
    def __init__(self, inner, buckets: BucketRegistry) -> None:
        self._inner, self._buckets = inner, buckets

    def __getattr__(self, name):  # everything else passes straight through
        return getattr(self._inner, name)

    def _bucket(self, user):
        return self._buckets.get(getattr(user, "org_id", 0), "hubspot")

    async def list_records(self, user, stream, cursor, page_size):
        b = self._bucket(user)
        grant = b.acquire("backfill" if cursor is None and stream.since is None else "sync")
        if not grant.allowed:
            raise RateLimited(f"budget: {grant.reason}", retry_after_s=grant.wait_s or 60.0)
        try:
            page = await self._inner.list_records(user, stream, cursor, page_size)
        except RateLimited as exc:
            b.note_rate_limited(exc.retry_after_s)
            raise
        b.note_ok()
        return page

    async def execute(self, user, action, args):
        grant = self._bucket(user).acquire("interactive")
        if not grant.allowed:
            raise RateLimited(f"budget: {grant.reason}", retry_after_s=grant.wait_s or 60.0)
        return await self._inner.execute(user, action, args)
```
(The connector name is taken from the stream/action in real use: replace `"hubspot"` with `stream.connector` in `list_records` and with the connector prefix of `action` (`action.split(".")[0]`) in `execute`; the test above pins `list_records`. Both lines are in the code as written once `_bucket(user, connector)` takes the connector argument; make that signature change while implementing.)

`src/mavis/revintel/projection/job.py`:
```python
"""CONNECTOR_RECORD (org rows only) to the revenue mirror."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from mavis.domain.events import Event
from mavis.revintel.projection import project
from mavis.revintel.projection.from_record import to_norm
from mavis.revintel.projection.normalise import NormAccount, NormContact, NormDeal, NormInteraction
from mavis.revintel.projection.stages import StageIn, import_stages
from mavis.revintel.repo import rev
from mavis.store.repo import connectors as repo

_listeners: list[Callable[[int, project.ProjectResult], Awaitable[None]]] = []


def on_deal_changed(fn) -> None:
    _listeners.append(fn)


async def project_record(record_id: int):
    row = await repo.get_record(record_id)
    if row is None or row.org_id == 0:
        return None
    record = repo.to_record(row)
    norm = to_norm(record)
    org = row.org_id
    if isinstance(norm, NormDeal):
        res = await project.project_deal(org, norm)
        if res.changed:
            for fn in _listeners:
                await fn(org, res)
        return res
    if isinstance(norm, NormAccount):
        return await project.project_account(org, norm)
    if isinstance(norm, NormContact):
        return await project.project_contact(org, norm)
    if isinstance(norm, NormInteraction):
        return await project.project_interaction(org, norm)
    if isinstance(norm, list) and norm and isinstance(norm[0], StageIn):
        return await import_stages(org, record.connector, norm)
    return None


async def on_connector_record(event: Event):
    rid = event.payload.get("record_id")
    if rid is None or not event.payload.get("org_id"):
        return None
    return await project_record(int(rid))


async def reproject(org_id: int) -> int:
    n = 0
    for connector in ("hubspot",):
        for row in await repo.records_for_org(org_id, connector):
            n += 1 if await project_record(row.id) is not None else 0
    await rev.rebuild_access(org_id)
    return n
```
(`repo.to_record(row)` and `repo.records_for_org(org_id, connector)` are two small helpers added to `store/repo/connectors.py` in this task: the first rebuilds a `Record` from a row, the second lists rows with `org_id == org_id`; pipelines are projected before deals because `records_for_org` orders by `kind='config'` first.) Register `on_connector_record` for `EventType.CONNECTOR_RECORD` in `connectors/wiring.py::register_connectors` next to the existing handler (both run; the connectors handler ignores org rows after Task 22).

`src/mavis/revintel/hygiene.py`:
```python
from __future__ import annotations

from sqlalchemy import func, select

from mavis.revintel.domain import RowFilter
from mavis.revintel.repo import rev
from mavis.revintel.rev_models import RevDeal
from mavis.store.db import Session


async def unmapped_owners(org_id: int) -> list[tuple[str, int]]:
    async with Session() as s:
        rows = await s.execute(select(RevDeal.crm_owner_ref, func.count()).where(
            RevDeal.org_id == org_id, RevDeal.owner_unmapped.is_(True), RevDeal.is_open.is_(True))
            .group_by(RevDeal.crm_owner_ref).order_by(func.count().desc()))
        return [(r, n) for r, n in rows if r]


async def deals_without_next_step(org_id: int, flt: RowFilter) -> int:
    q = rev.scoped_deals(flt, org_id).where(RevDeal.is_open.is_(True), RevDeal.next_step.is_(None))
    async with Session() as s:
        return await s.scalar(select(func.count()).select_from(q.subquery())) or 0
```
`commands.py::_org`: add a branch for `args[0] == "hygiene"` (action `member.manage`) printing "N deals have owners I can't match: {ref} ({n}). Say /org map-owner <name> <hubspot owner id> to fix." (copy has no dashes).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel tests/connectors -q && uv run ruff check src tests` -> PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis tests/revintel/test_org_sync.py
git commit -m "feat(revintel): org-aware sync engine, call budgets, projection job and hygiene lists"
```

---

### Task 27: Approval-gated CRM writes: notes, tasks, field updates, stage moves, closes

**Start gate:** Tasks 9, 12, 24, 25 merged.

**Files:**
- Create: `src/mavis/revintel/crm_write.py`, `src/mavis/revintel/tools_write.py`, `tests/revintel/test_crm_write.py`
- Modify: `src/mavis/tools/__init__.py` (register the four tools when `orgs_on()`)

**Interfaces:**
- Consumes: Task 7 `decide`, Task 9 tool hook, Task 12 approvals, Task 24 `ActionSpec`s, Task 25 `ref_for`, Task 16 `RevProposal`.
- Produces:
  - tools (all `RiskClass.OUTWARD`, `action="deal.write"`, org-only): `rev_add_note(deal_id: int, text: str)` class `crm.note`; `rev_add_task(deal_id: int, title: str, due: str | None)` class `crm.task`; `rev_set_field(deal_id: int, field: Literal["next_step","next_step_at","close_date","forecast_category"], value: str)` class `crm.field`; `rev_move_stage(deal_id: int, stage_ref: str)` class `crm.stage`; `rev_close_deal(deal_id: int, outcome: Literal["won","lost"])` class `crm.close`; arguments never hold org or user ids; `deal_id` is the mirror id and is loaded through `rev.deal_ref(org_id, deal_id)`
  - previews rendered from typed args: "Add a note to {deal}: {text up to 200 chars}", "Create a task on {deal}: {title}{ (due {date})}", "Set {field} on {deal} to {value}", "Move {deal} to {stage label}", "Mark {deal} as {won|lost}"
  - `crm_write.execute(user_id, org_id, op: str, deal_id: int, payload: dict, *, idem: str) -> WriteResult(ok: bool, text: str, crm_ids: list[str])`: runs `provider.execute(await ref_for(user_id, org_id), "hubspot.add_note" | ..., args)` with the member's OWN org-bound grant; idempotent on `idem` through `rev_proposals(subject_key=idem, kind="write")` (a second call with the same `idem` returns the stored result without calling the provider); a provider permission error maps to "Your HubSpot login doesn't allow that." and a missing grant to the connect offer "Connect HubSpot to save this."; the audit row `executed` or `failed` carries counts and ids only
  - `crm_write.idem_key(op, deal_key, ref) -> str` (`"crm.note|deal=<record_key>|ref=<x>"`)
- Rules: the tool's `fn` (run only after approval, by `execute_approved`) calls `crm_write.execute`; a note never runs un-approved because every `crm.*` seed policy resolves to at least `confirm`; stage and close are never proposed from a transcript (Task 32) but remain available on request.

- [ ] **Step 1: Write the failing test**

`tests/revintel/test_crm_write.py`:
```python
from __future__ import annotations

import pytest

from mavis.domain.errors import ApprovalRequired
from mavis.domain.integrations import ToolResult
from mavis.revintel import audit, crm_write, spaces
from mavis.revintel.models import OrgAuditLog
from mavis.revintel.projection import project
from mavis.revintel.projection.normalise import NormDeal
from mavis.revintel.projection.stages import StageIn, import_stages
from mavis.revintel.repo import orgs
from mavis.revintel.tools_write import build_tools
from mavis.store.db import Session
from mavis.tools.registry import ToolRegistry
from datetime import UTC, datetime

T = datetime(2026, 10, 1, tzinfo=UTC)


class Prov:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or ToolResult(ok=True, data={"id": "n-9"})

    async def execute(self, user, action, args):
        self.calls.append((user.provider_id, action, args))
        return self.result


@pytest.fixture
async def deal_world(world, orgs_on, monkeypatch):
    await import_stages(world.org.id, "hubspot", [StageIn("p", "a", "Discovery", 1, False, False, 0.2), StageIn("p", "w", "Won", 9, True, True, 1.0)])
    from mavis.revintel.projection import owners

    await owners.set_crm_owner(world.org.id, world.u["Raj"].id, "hubspot", owner_id="501", email=None, actor_user_id=world.u["Olu"].id)
    res = await project.project_deal(world.org.id, NormDeal("hubspot", "42", "Initech renewal", 100, "USD", "a", "p", None, T, T, {"id": "501"}, None, (), None, None, None, None))
    prov = Prov()
    monkeypatch.setattr(crm_write, "provider", lambda: prov)
    world.deal_id, world.prov = res.deal_id, prov
    return world


def reg():
    r = ToolRegistry()
    for t in build_tools():
        r.register(t)
    from mavis.revintel import tooling

    tooling.install(r)
    return r


async def test_a_rep_note_needs_approval_and_carries_org_metadata(deal_world):
    w, r = deal_world, reg()
    tok = spaces.current_space.set(f"org:{w.org.id}")
    try:
        with pytest.raises(ApprovalRequired) as exc:
            await r.invoke(r.get("rev_add_note"), w.u["Raj"].id, r.get("rev_add_note").args_model(deal_id=w.deal_id, text="Pricing was fine"))
    finally:
        spaces.current_space.reset(tok)
    assert exc.value.preview.startswith("Add a note to Initech renewal: Pricing was fine")
    assert exc.value.org["action"] == "deal.write" and w.prov.calls == []


async def test_another_reps_deal_cannot_be_written(deal_world):
    w, r = deal_world, reg()
    tok = spaces.current_space.set(f"org:{w.org.id}")
    try:
        out = await r.invoke(r.get("rev_add_note"), w.u["Sol"].id, r.get("rev_add_note").args_model(deal_id=w.deal_id, text="x"))
    finally:
        spaces.current_space.reset(tok)
    assert out == "I can't find that in your view." and w.prov.calls == []


async def test_admins_cannot_write_by_default(deal_world):
    w, r = deal_world, reg()
    tok = spaces.current_space.set(f"org:{w.org.id}")
    try:
        out = await r.invoke(r.get("rev_add_note"), w.u["Ada"].id, r.get("rev_add_note").args_model(deal_id=w.deal_id, text="x"))
    finally:
        spaces.current_space.reset(tok)
    assert out == "Your role doesn't allow that."


async def test_execute_uses_the_members_own_grant_and_is_idempotent(deal_world):
    w = deal_world
    idem = crm_write.idem_key("crm.note", "hubspot:deal:42", "call-1")
    a = await crm_write.execute(w.u["Raj"].id, w.org.id, "add_note", w.deal_id, {"body": "Pricing fine"}, idem=idem)
    b = await crm_write.execute(w.u["Raj"].id, w.org.id, "add_note", w.deal_id, {"body": "Pricing fine"}, idem=idem)
    assert a.ok and b.ok and b.crm_ids == a.crm_ids == ["n-9"]
    assert len(w.prov.calls) == 1
    assert w.prov.calls[0][0] == f"mavis-{w.u['Raj'].id}-o{w.org.id}" and w.prov.calls[0][1] == "hubspot.add_note"
    assert w.prov.calls[0][2]["deal_id"] == "42"


async def test_provider_refusal_is_mapped_and_can_be_retried(deal_world):
    w = deal_world
    w.prov.result = ToolResult(ok=False, error="403 forbidden: missing scope crm.objects.deals.write")
    r1 = await crm_write.execute(w.u["Raj"].id, w.org.id, "add_note", w.deal_id, {"body": "x"}, idem="k1")
    assert (r1.ok, r1.text) == (False, "Your HubSpot login doesn't allow that.")
    w.prov.result = ToolResult(ok=True, data={"id": "n-1"})
    assert (await crm_write.execute(w.u["Raj"].id, w.org.id, "add_note", w.deal_id, {"body": "x"}, idem="k1")).ok
    from sqlalchemy import select
    async with Session() as s:
        decisions = [x.decision for x in await s.scalars(select(OrgAuditLog).where(OrgAuditLog.action == "deal.write"))]
    assert decisions == ["failed", "executed"]


async def test_no_grant_offers_the_connect_flow(deal_world, monkeypatch):
    from mavis.domain.errors import ConnectionRequired
    w = deal_world

    class NoGrant(Prov):
        async def execute(self, user, action, args):
            raise ConnectionRequired("hubspot", "connect")

    monkeypatch.setattr(crm_write, "provider", lambda: NoGrant())
    out = await crm_write.execute(w.u["Raj"].id, w.org.id, "add_note", w.deal_id, {"body": "x"}, idem="k2")
    assert (out.ok, out.text) == (False, "Connect HubSpot to save this.")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/revintel/test_crm_write.py -q` -> FAIL (`No module named 'mavis.revintel.crm_write'`).

- [ ] **Step 3: Implement**

`src/mavis/revintel/crm_write.py`:
```python
"""Executing an approved CRM write as the requester, once (spec 4.5, 10.1 step 7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select

from mavis.domain.errors import ConnectionRequired
from mavis.revintel import audit, bindings
from mavis.revintel.rev_models import RevDeal, RevProposal
from mavis.store.db import Session, utcnow

NO_PERMISSION = "Your HubSpot login doesn't allow that."
CONNECT = "Connect HubSpot to save this."
_ACTION = {"add_note": "hubspot.add_note", "add_task": "hubspot.add_task", "update_deal": "hubspot.update_deal"}
_CLASS = {"add_note": "crm.note", "add_task": "crm.task", "update_deal": "crm.field"}


@dataclass
class WriteResult:
    ok: bool
    text: str
    crm_ids: list[str] = field(default_factory=list)


def idem_key(op: str, deal_key: str, ref: str) -> str:
    return f"{op}|deal={deal_key}|ref={ref}"


def provider():
    from mavis.tools.integrations.wiring import get_provider

    return get_provider()


def _denied(error: str | None) -> bool:
    low = (error or "").lower()
    return any(w in low for w in ("403", "forbidden", "scope", "permission", "unauthor", "401"))


async def execute(user_id: int, org_id: int, op: str, deal_id: int, payload: dict, *, idem: str) -> WriteResult:
    async with Session() as s:
        deal = await s.get(RevDeal, deal_id)
        if deal is None or deal.org_id != org_id:
            return WriteResult(False, "I can't find that in your view.")
        done = await s.scalar(select(RevProposal).where(RevProposal.org_id == org_id, RevProposal.subject_key == idem,
                                                        RevProposal.kind == "write"))
        if done is not None and done.status == "executed":
            return WriteResult(True, "Already done.", list(done.payload.get("crm_ids", [])))
        ext = deal.record_key.rsplit(":", 1)[-1]
        if done is None:
            done = RevProposal(org_id=org_id, subject_key=idem, kind="write", requester_user_id=user_id,
                               payload={"op": op}, status="executing", expires_at=utcnow() + timedelta(days=30))
            s.add(done)
        done.status = "executing"
        await s.commit()
    base = dict(actor_user_id=user_id, on_behalf_of=user_id, space=f"org:{org_id}", action="deal.write",
                resource_type="deal", resource_ids=[deal_id], detail={"op": op, "records": 1})
    try:
        res = await provider().execute(await bindings.ref_for(user_id, org_id), _ACTION[op], {"deal_id": ext, **payload})
    except ConnectionRequired:
        await _finish(org_id, idem, "failed", [])
        await audit.append(org_id, decision="failed", reason="no_grant", **base)
        return WriteResult(False, CONNECT)
    if not res.ok:
        await _finish(org_id, idem, "failed", [])
        await audit.append(org_id, decision="failed", reason="provider", **base)
        return WriteResult(False, NO_PERMISSION if _denied(res.error) else "HubSpot did not accept that, so nothing was saved.")
    ids = [str(res.data.get("id"))] if isinstance(res.data, dict) and res.data.get("id") else []
    await _finish(org_id, idem, "executed", ids)
    await audit.append(org_id, decision="executed", **base)
    return WriteResult(True, "Done.", ids)


async def _finish(org_id: int, idem: str, status: str, ids: list[str]) -> None:
    async with Session() as s:
        row = await s.scalar(select(RevProposal).where(RevProposal.org_id == org_id, RevProposal.subject_key == idem,
                                                       RevProposal.kind == "write"))
        row.status, row.payload = status, {**row.payload, "crm_ids": ids}
        await s.commit()
```
(Add `UniqueConstraint("org_id", "subject_key", "kind", name="uq_rev_proposals_subject")` to `RevProposal` and the same line to the `rev_proposals` entry of the Task 16 migration's `uniques` list, so concurrent executions of one idempotency key collide in the database.)

`src/mavis/revintel/tools_write.py`:
```python
"""The org write tools. They only run after the approval policy says so (Task 7, Task 12)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from mavis.domain.args import ToolArgs
from mavis.domain.policy import RiskClass
from mavis.revintel import crm_write
from mavis.revintel.repo import rev
from mavis.revintel.rev_models import RevDeal, RevStageConfig
from mavis.revintel.spaces import current_space
from mavis.revintel.domain import parse_space
from mavis.store.db import Session
from mavis.tools.registry import MavisTool


class NoteArgs(ToolArgs):
    deal_id: int
    text: str = Field(max_length=2000)


class TaskArgs(ToolArgs):
    deal_id: int
    title: str = Field(max_length=300)
    due: str | None = None


class FieldArgs(ToolArgs):
    deal_id: int
    field: Literal["next_step", "next_step_at", "close_date", "forecast_category"]
    value: str = Field(max_length=500)


class StageArgs(ToolArgs):
    deal_id: int
    stage_ref: str


class CloseArgs(ToolArgs):
    deal_id: int
    outcome: Literal["won", "lost"]


async def _resource(args, user_id):
    return await rev.deal_ref(parse_space(current_space.get()), args.deal_id)


async def _name(deal_id: int) -> str:
    async with Session() as s:
        d = await s.get(RevDeal, deal_id)
        return d.name if d else "that deal"


async def _stage_label(deal_id: int, stage_ref: str) -> str:
    async with Session() as s:
        d = await s.get(RevDeal, deal_id)
        from sqlalchemy import select

        row = await s.scalar(select(RevStageConfig).where(RevStageConfig.org_id == d.org_id,
                                                          RevStageConfig.pipeline_ref == d.pipeline_ref,
                                                          RevStageConfig.stage_ref == stage_ref))
        return row.label if row else stage_ref


def _org() -> int:
    return parse_space(current_space.get())


async def _note(user_id, a: NoteArgs) -> str:
    r = await crm_write.execute(user_id, _org(), "add_note", a.deal_id, {"body": a.text},
                                idem=crm_write.idem_key("crm.note", f"d{a.deal_id}", str(hash(a.text))))
    return r.text


async def _task(user_id, a: TaskArgs) -> str:
    r = await crm_write.execute(user_id, _org(), "add_task", a.deal_id, {"title": a.title, "due": a.due},
                                idem=crm_write.idem_key("crm.task", f"d{a.deal_id}", a.title + str(a.due)))
    return r.text


_FIELD_PROP = {"next_step": "hs_next_step", "next_step_at": "hs_next_step_date", "close_date": "closedate",
               "forecast_category": "hs_forecast_category"}


async def _field(user_id, a: FieldArgs) -> str:
    r = await crm_write.execute(user_id, _org(), "update_deal", a.deal_id, {"properties": {_FIELD_PROP[a.field]: a.value}},
                                idem=crm_write.idem_key("crm.field", f"d{a.deal_id}", f"{a.field}={a.value}"))
    return r.text


async def _stage(user_id, a: StageArgs) -> str:
    r = await crm_write.execute(user_id, _org(), "update_deal", a.deal_id, {"properties": {"dealstage": a.stage_ref}},
                                idem=crm_write.idem_key("crm.stage", f"d{a.deal_id}", a.stage_ref))
    return r.text


async def _close(user_id, a: CloseArgs) -> str:
    stage = {"won": "closedwon", "lost": "closedlost"}[a.outcome]
    r = await crm_write.execute(user_id, _org(), "update_deal", a.deal_id, {"properties": {"dealstage": stage}},
                                idem=crm_write.idem_key("crm.close", f"d{a.deal_id}", a.outcome))
    return r.text


def build_tools() -> list[MavisTool]:
    common = dict(risk=RiskClass.OUTWARD, agents=frozenset({"conversation"}), action="deal.write", resource_from=_resource)
    return [
        MavisTool("rev_add_note", "Add a note to a deal in the CRM (asks for approval).", NoteArgs, fn=_note,
                  action_class="crm.note", identity=("deal_id", "text"), target=("deal_id",),
                  preview=lambda a: f"Add a note to deal {a.deal_id}: {a.text[:200]}", **common),
        MavisTool("rev_add_task", "Create a task on a deal in the CRM (asks for approval).", TaskArgs, fn=_task,
                  action_class="crm.task", identity=("deal_id", "title"), preview=lambda a: f"Create a task on deal {a.deal_id}: {a.title}", **common),
        MavisTool("rev_set_field", "Set next step, close date or forecast category on a deal (asks for approval).",
                  FieldArgs, fn=_field, action_class="crm.field", identity=("deal_id", "field"),
                  preview=lambda a: f"Set {a.field} on deal {a.deal_id} to {a.value}", **common),
        MavisTool("rev_move_stage", "Move a deal to another stage (asks for approval).", StageArgs, fn=_stage,
                  action_class="crm.stage", identity=("deal_id",), preview=lambda a: f"Move deal {a.deal_id} to {a.stage_ref}", **common),
        MavisTool("rev_close_deal", "Mark a deal won or lost (asks for approval).", CloseArgs, fn=_close,
                  action_class="crm.close", identity=("deal_id",), preview=lambda a: f"Mark deal {a.deal_id} as {a.outcome}", **common),
    ]
```
The test's first preview assertion needs the deal name, so give each tool `preview_needs_ctx` free names: replace the lambdas by `render` functions that read the name synchronously from a per-turn cache filled by `_resource` (`_NAMES[deal_id] = deal.name` when the resource is loaded, which always happens before the preview in `invoke`); the lambdas become `lambda a: f"Add a note to {_NAMES.get(a.deal_id, 'that deal')}: {a.text[:200]}"` and so on, with `_NAMES: dict[int, str]` bounded to 256 entries. Register in `tools/__init__.py`: `if orgs_on(): for t in build_tools(): registry.register(t)`; the other `crm.*` previews follow the same pattern ("Create a task on {name}: {title}", "Set {field} on {name} to {value}", "Move {name} to {stage}", "Mark {name} as {outcome}").

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/revintel -q && uv run ruff check src tests` -> PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mavis tests/revintel/test_crm_write.py
git commit -m "feat(revintel): approval-gated CRM writes executed as the requester"
```

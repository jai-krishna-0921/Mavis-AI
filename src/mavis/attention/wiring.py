"""Composition root for the attention layer (one instance per process).

register_attention() is safe to call repeatedly: event handlers replace or dedupe, system wakeups and button
prefixes overwrite, and hook / brief-source / context-provider appends are membership-guarded. It must run
after wire_initiative() and register_integrations(): it replaces EMAIL_RECEIVED and appends to
TASK_COMPLETED. With ATTENTION_ENABLED=false it registers nothing and the Phase 5 email path stays as is."""

from __future__ import annotations

from functools import lru_cache

import structlog
from qdrant_client import AsyncQdrantClient

from mavis.agents import context_hooks
from mavis.agents.buttons import dispatch_button, register_button_handler
from mavis.attention.baselines import Baselines
from mavis.attention.digest import Digest
from mavis.attention.feedback import FeedbackHandler
from mavis.attention.index import AttentionIndex
from mavis.attention.intake import Intake
from mavis.attention.learning import Thresholds
from mavis.attention.pipeline import AttentionPipeline
from mavis.attention.rhythm import AttentionBrief, EveningWrap, FirstLook, Retention, register_evening_source
from mavis.attention.speaker import PREFIX, Speaker
from mavis.attention.understand import Understander
from mavis.attention.workspace import PREFIX as WORKSPACE_PREFIX
from mavis.attention.workspace import WorkspaceIntake
from mavis.attention.workspace_rhythm import WorkspaceBrief, workspace_evening
from mavis.config import get_settings
from mavis.domain.events import Event, EventType
from mavis.domain.policy import Capability
from mavis.domain.wakeups import WakeupKind
from mavis.initiative import hooks, routines
from mavis.initiative import wiring as initiative_wiring
from mavis.memory import embeddings
from mavis.policy.pings import PingPolicy
from mavis.store.repo import attention as repo
from mavis.store.repo import users
from mavis.timers.service import WakeupService
from mavis.timers.system import register_system_wakeup
from mavis.tools.integrations.actions import workspace_enabled
from mavis.tools.integrations.first_sync import register_first_sync_handler
from mavis.tools.integrations.poller import WORKSPACE_POLL_KIND
from mavis.worker.runner import register_event_handler, register_startup_hook

log = structlog.get_logger(__name__)

SYSTEM_KINDS = (
    WakeupKind.SYSTEM_ATTENTION_DRAIN,
    WakeupKind.SYSTEM_ATTENTION_SPEAK,
    WakeupKind.SYSTEM_ATTENTION_BACKFILL,
    WakeupKind.SYSTEM_EVENING_WRAP,
    WakeupKind.SYSTEM_ATTENTION_RETENTION,
)

# A Qdrant client this module opened itself (only when memory has none to share); closed on shutdown.
_private_clients: list[AsyncQdrantClient] = []


def _executor():
    return initiative_wiring.current().executor


async def _forward(event: Event) -> None:
    await initiative_wiring.current().handler.handle(event)


@lru_cache
def get_index() -> AttentionIndex:
    """Shares memory's Qdrant client and embedder: one embedding model per process on the 2 GB box."""
    from mavis.memory.service import get_memory

    memory = get_memory()
    client = getattr(getattr(memory, "vector", None), "client", None)
    if client is None:  # embedded Qdrant cannot be opened twice; never open a second on-disk client
        log.warning("attention.index_private_client")
        client = AsyncQdrantClient(location=":memory:")
        _private_clients.append(client)
    embedder = getattr(memory, "embedder", None) or embeddings.get_embedder()
    return AttentionIndex(client, embedder, remote=bool(get_settings().qdrant_url))


@lru_cache
def get_thresholds() -> Thresholds:
    return Thresholds()


@lru_cache
def get_baselines() -> Baselines:
    return Baselines()


@lru_cache
def get_pipeline() -> AttentionPipeline:
    return AttentionPipeline(
        understander=Understander(),
        baselines=get_baselines(),
        index=get_index(),
        speaker=Speaker(_executor, PingPolicy(), WakeupService()),
        thresholds=get_thresholds(),
    )


@lru_cache
def get_first_look() -> FirstLook:
    return FirstLook(_executor, get_thresholds())


@lru_cache
def get_intake() -> Intake:
    from mavis.tools.integrations import get_provider

    return Intake(
        pipeline=get_pipeline(),
        loops=initiative_wiring.current().loops,
        wakeups=WakeupService(),
        thresholds=get_thresholds(),
        forward=_forward,
        provider=get_provider(),
        on_backlog_empty=get_first_look().maybe_send,
        connectors=get_connector_ingest(),
    )


@lru_cache
def get_connector_ingest():
    from mavis.attention.connector_ingest import ConnectorIngest

    return ConnectorIngest(directory=slack_directory)


async def slack_directory(user_id: int, team: str, ids: frozenset[str]) -> dict[str, dict[str, str]]:
    """Display names for Slack ids via the native Slack executor's cached users.info."""
    from mavis.tools.integrations import get_provider
    from mavis.tools.integrations.native.base import NativeProvider

    executor = getattr(get_provider(), "executors", {}).get(NativeProvider.SLACK)
    if executor is None:
        return {}
    out: dict[str, dict[str, str]] = {}
    for sid in sorted(ids)[:10]:
        info = await executor.user_info(user_id, team, sid)
        if info and not info.get("is_bot"):
            out[sid] = {"name": info.get("name", ""), "email": info.get("email", "")}
    return out


@lru_cache
def get_feedback() -> FeedbackHandler:
    return FeedbackHandler(
        loops=initiative_wiring.current().loops,
        wakeups=WakeupService(),
        baselines=get_baselines(),
        index=get_index(),
        thresholds=get_thresholds(),
    )


@lru_cache
def get_digest() -> Digest:
    return Digest(get_index())


@lru_cache
def get_evening() -> EveningWrap:
    return EveningWrap(_executor, WakeupService())


@lru_cache
def get_retention() -> Retention:
    return Retention(get_index, WakeupService())


@lru_cache
def get_workspace() -> WorkspaceIntake:
    from mavis.tools.integrations import get_connection_cache, get_provider
    from mavis.tools.integrations.wiring import reconnect_prompt, wakeup_schedule

    return WorkspaceIntake(provider=get_provider(), executor_of=_executor,
                           loops=initiative_wiring.current().loops, schedule=wakeup_schedule,
                           cache=get_connection_cache(), policy=PingPolicy(), on_auth_failed=reconnect_prompt)


ATTENTION_GETTERS = (
    get_connector_ingest,
    get_index,
    get_thresholds,
    get_baselines,
    get_pipeline,
    get_first_look,
    get_intake,
    get_feedback,
    get_digest,
    get_evening,
    get_retention,
    get_workspace,
)


async def watched(user_id: int) -> bool:
    """Daily attention chains only for users whose Gmail is watched or who still have observations."""
    polling = (await users.get_state(user_id)).get("polling") or {}
    return bool(polling.get(Capability.GMAIL.value)) or await repo.has_any(user_id)


async def _ensure_daily(user_id: int) -> None:
    if not await watched(user_id):
        return
    for name, chain in (("evening", get_evening()), ("retention", get_retention())):
        try:
            await chain.ensure(user_id)
        except Exception as exc:  # noqa: BLE001 - one chain must not block the other
            log.warning("attention.heal_failed", chain=name, user_id=user_id, error=type(exc).__name__)


async def morning_maintenance(user_id: int) -> None:
    """Rides on the daily check-in: self-heal the drain/backfill, evening wrap and retention chains.
    Each step is isolated, so one failure never skips the others."""
    try:
        await get_intake().heal(user_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("attention.heal_failed", chain="intake", user_id=user_id, error=type(exc).__name__)
    try:
        await _ensure_daily(user_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("attention.heal_failed", chain="daily", user_id=user_id, error=type(exc).__name__)


async def heal_all() -> None:
    """Worker startup: everything attention keeps in the wakeups table is re-armed."""
    await get_intake().heal_all()
    for user_id in await users.all_ids():
        try:
            await _ensure_daily(user_id)
        except Exception as exc:  # noqa: BLE001 - one user must not block the rest
            log.warning("attention.heal_failed", chain="daily", user_id=user_id, error=type(exc).__name__)


async def heal_workspace() -> None:
    """Worker startup: every Google-synced user has its Tasks and Drive poll chains armed."""
    for user_id in await users.all_ids():
        try:
            await get_workspace().ensure_chains(user_id)
        except Exception as exc:  # noqa: BLE001 - one user must not block the rest
            log.warning("workspace.heal_failed", user_id=user_id, error=type(exc).__name__)


def register_workspace() -> None:
    """Drive shares, Docs comments and Google Tasks (GOOGLE_WORKSPACE_ENABLED and ATTENTION_ENABLED)."""
    workspace = get_workspace()
    register_event_handler(EventType.WORKSPACE_SIGNAL, workspace.on_event, replace=True)
    register_button_handler(WORKSPACE_PREFIX, workspace.on_button)
    register_system_wakeup(WORKSPACE_POLL_KIND, workspace.on_wakeup)
    register_startup_hook(heal_workspace)
    routines.register_morning_hook(workspace.ensure_chains)
    if "workspace" not in {s.name for s in routines.brief_sources()}:
        routines.register_brief_source(WorkspaceBrief(workspace))
    register_evening_source(workspace_evening)
    register_first_sync_handler(Capability.TASKS, workspace.first_sync_tasks)
    register_first_sync_handler(Capability.DRIVE, workspace.first_sync_drive)
    register_first_sync_handler(Capability.CONTACTS, workspace.first_sync_contacts)


async def close_attention() -> None:
    """Shutdown: close a Qdrant client this module opened itself (the shared one belongs to memory)."""
    while _private_clients:
        client = _private_clients.pop()
        try:
            await client.close()
        except Exception as exc:  # noqa: BLE001 - shutdown must go on
            log.warning("attention.close_failed", error=type(exc).__name__)


def register_attention() -> None:
    if not get_settings().attention_enabled:
        log.info("attention.disabled")  # the Phase 5 email path stays as it is
        # The knowledge graph does not depend on triage: mail and Slack records still reach the guard.
        ingest = get_connector_ingest()
        register_event_handler(EventType.SLACK_MESSAGE, ingest.on_slack_event)
        register_event_handler(EventType.EMAIL_RECEIVED, ingest.on_email_event)
        return
    intake, pipeline = get_intake(), get_pipeline()
    register_event_handler(EventType.EMAIL_RECEIVED, intake.on_email, replace=True)
    register_event_handler(EventType.SLACK_MESSAGE, get_connector_ingest().on_slack_event)
    register_event_handler(EventType.TASK_COMPLETED, intake.on_task_completed)
    # the one shared dispatcher (deduped: register_integrations already registered this same function)
    register_event_handler(EventType.BUTTON_PRESSED, dispatch_button)
    register_button_handler(PREFIX, get_feedback().on_button)
    register_system_wakeup(WakeupKind.SYSTEM_ATTENTION_DRAIN.value, intake.drain)
    register_system_wakeup(WakeupKind.SYSTEM_ATTENTION_SPEAK.value, pipeline.speak_deferred)
    register_system_wakeup(WakeupKind.SYSTEM_ATTENTION_BACKFILL.value, intake.backfill)
    register_system_wakeup(WakeupKind.SYSTEM_EVENING_WRAP.value, get_evening().run)
    register_system_wakeup(WakeupKind.SYSTEM_ATTENTION_RETENTION.value, get_retention().run)
    register_startup_hook(heal_all)
    routines.register_morning_hook(morning_maintenance)
    if pipeline.enrich not in hooks.ENRICHERS:
        hooks.ENRICHERS.append(pipeline.enrich)
    routines.unregister_brief_source("inbox")
    if "attention" not in {s.name for s in routines.brief_sources()}:
        routines.register_brief_source(AttentionBrief())
    context_hooks.register_context_provider(get_digest().context)
    if workspace_enabled():
        register_workspace()

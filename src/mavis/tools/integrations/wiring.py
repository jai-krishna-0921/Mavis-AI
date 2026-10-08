"""Production wiring for integrations: the only module that binds concrete deps to the flows.

`register_integrations()` is safe to call any number of times: handler, job and wakeup registration
runs on every call (tests clear the registries between runs), and hook / brief-source appends are
guarded by membership checks.
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import TYPE_CHECKING

import structlog

from mavis.agents.buttons import dispatch_button, register_button_handler
from mavis.bus import get_bus
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.decisions import NotifyIntent
from mavis.domain.errors import ConnectionRequired, IntegrationError
from mavis.domain.events import Event, EventType, Job, JobKind
from mavis.domain.integrations import ConnectionState
from mavis.domain.messages import Outbound
from mavis.domain.policy import Capability
from mavis.domain.wakeups import WakeupKind
from mavis.initiative import hooks, routines
from mavis.initiative import wiring as initiative_wiring
from mavis.initiative.briefs_integrations import CalendarBrief, InboxBrief
from mavis.initiative.email_triage import EmailTriage, email_prefilter
from mavis.initiative.untrusted import wrap_untrusted
from mavis.timers.system import register_system_wakeup
from mavis.tools.integrations import get_connection_cache, get_provider
from mavis.tools.integrations.actions import (
    CAPABILITY_PURPOSE,
    GOOGLE_CAPABILITIES,
    active_capabilities,
    display_name,
    workspace_enabled,
)
from mavis.tools.integrations.activation import Activator
from mavis.tools.integrations.connect_flow import CHECK_KIND, ConnectFlow, RepoUserState
from mavis.tools.integrations.first_sync import FirstSync
from mavis.tools.integrations.poller import POLL_KIND, WORKSPACE_POLL_KIND, Poller
from mavis.worker.runner import register_event_handler, register_job_handler, register_startup_hook

if TYPE_CHECKING:
    from mavis.tools.registry import MavisTool, ToolRegistry

log = structlog.get_logger(__name__)


class _LazyBus:
    """Resolves the process bus at use time, so cached flows never hold a stale bus."""

    async def publish(self, event: Event) -> bool:
        return await get_bus().publish(event)

    async def enqueue(self, job: Job) -> None:
        await get_bus().enqueue(job)


class JobLearner:
    """FirstSync's memory port: third-party text goes through an untrusted LEARN job, never inline."""

    async def learn(self, user_id: int, text: str, source_ref: str) -> None:
        await get_bus().enqueue(Job(
            id=f"learn:{source_ref}", user_id=user_id, kind=JobKind.LEARN,
            payload={"text": text, "source_ref": source_ref, "trust": "untrusted", "conversation": False,
                     # gathered now; each line carries its own date for the extractor to anchor on
                     "anchor_at": timeutil.now().isoformat()},
        ))


async def outbox_notify(msg: Outbound) -> None:
    from mavis.store.db import Session
    from mavis.store.repo import outbox

    async with Session() as session:
        await outbox.enqueue(session, msg)
        await session.commit()


async def wakeup_schedule(user_id: int, at: datetime, reason: str, kind: str) -> int:
    """Schedule a system wakeup. Poll wakeups (and Workspace polls and deferred Workspace pings) collapse
    onto an existing pending one with the same reason (one chain per user and capability); connection
    checks do not dedupe."""
    from mavis.timers.service import WakeupService

    service = WakeupService()
    if kind in (POLL_KIND, WORKSPACE_POLL_KIND):
        now = timeutil.now()
        for w in await service.pending(user_id, WakeupKind(kind)):
            if w.reason != reason:
                continue
            # Any pending poll absorbs an immediate request (the Activator asks for "now"). A request
            # for a later time (the poller's own reschedule) must not dedupe onto the row that is
            # firing right now (due, still PENDING until commit), or the chain would die.
            if w.due_at > now or at <= now:
                return w.id
    # plumbing, not agent intent: never compressed by DEMO_TIME_SCALE
    return await service.wake_me(user_id, at, reason, kind=kind, scale=False)


async def google_activated(user_id: int) -> None:
    """googlesuper is live for this user: drop the legacy triggers and start the Workspace polls, the
    first one a full interval out so first sync sets the quiet baseline before any poll speaks (A3)."""
    try:
        await get_activator().retire_legacy(user_id)
    finally:  # leftover legacy triggers only duplicate events; the polls must start regardless
        if get_settings().attention_enabled and workspace_enabled():
            from mavis.attention.wiring import get_workspace  # lazy: attention is wired after integrations

            await get_workspace().ensure_chains(user_id, later=True)


async def connection_checks_pending(user_id: int, pending_id: int) -> bool:
    from mavis.timers.service import WakeupService

    pending = await WakeupService().pending(user_id, WakeupKind.SYSTEM_CONNECTION_CHECK)
    return any(w.reason == str(pending_id) for w in pending)


async def cancel_connection_checks(user_id: int, pending_id: int) -> None:
    from mavis.timers.service import WakeupService

    service = WakeupService()
    for w in await service.pending(user_id, WakeupKind.SYSTEM_CONNECTION_CHECK):
        if w.reason == str(pending_id):
            await service.cancel(w.id)


async def reconnect_prompt(user_id: int, capability: Capability) -> object:
    """Expired or revoked access seen by the poller, a brief or an action: one prompt per day."""
    return await get_connect_flow().prompt_reconnect(user_id, capability)


async def nudge_upgrade(user_id: int) -> None:
    """Morning hook: a legacy-only Google user gets the one upgrade nudge (Workspace flag on)."""
    try:
        await get_connect_flow().maybe_nudge_upgrade(user_id)
    except Exception as exc:  # noqa: BLE001 - the morning check-in must go on
        log.warning("integrations.nudge_failed", user_id=user_id, error=type(exc).__name__)


async def all_user_ids() -> list[int]:
    from mavis.store.repo import users

    return await users.all_ids()


async def heal_poll_chains(user_id: int) -> None:
    await get_poller().ensure_chains(user_id)


async def check_provider_key() -> None:
    """A non-prod stack must not share a Composio key that holds prod accounts (spec 6.2)."""
    from mavis.tools.integrations.identity import check_shared_key

    s = get_settings()
    if s.integration_provider == "composio" and s.composio_api_key:
        await check_shared_key(get_provider())


async def heal_all_poll_chains() -> None:
    armed = await get_poller().ensure_all_chains()
    log.info("poller.chains_ensured", armed=armed)


async def user_timezone(user_id: int) -> str:
    from mavis.store.repo import users

    return (await users.get(user_id)).timezone


async def known_names(user_id: int) -> set[str]:
    from mavis.memory.service import get_memory

    names: set[str] = set()
    for entity in await get_memory().graph.entities(user_id):
        names.add(entity.name.lower())
        names.update(a.lower() for a in entity.aliases)
    return names


@lru_cache
def get_activator() -> Activator:
    s = get_settings()
    # Without a webhook secret every webhook is rejected, so polling is the only inbound path.
    return Activator(provider=get_provider(), state=RepoUserState(), schedule=wakeup_schedule,
                     polling_forced=s.integration_polling or not s.composio_webhook_secret)


@lru_cache
def get_connect_flow() -> ConnectFlow:
    return ConnectFlow(
        provider=get_provider(), cache=get_connection_cache(), bus=_LazyBus(), notify=outbox_notify,
        schedule=wakeup_schedule, state=RepoUserState(), base_url=get_settings().public_base_url,
        on_active=get_activator().on_active, has_checks=connection_checks_pending,
        cancel_checks=cancel_connection_checks, on_google_active=google_activated,
        on_google_begin=get_activator().begin_google,
    )


@lru_cache
def get_poller() -> Poller:
    return Poller(provider=get_provider(), cache=get_connection_cache(), bus=_LazyBus(),
                  state=RepoUserState(), schedule=wakeup_schedule, on_failed=reconnect_prompt,
                  user_ids=all_user_ids)


@lru_cache
def get_first_sync() -> FirstSync:
    from mavis.loops.service import LoopService

    # Phase 4 / later: loops stay unused, third-party content never creates loops.
    return FirstSync(provider=get_provider(), memory=JobLearner(), loops=LoopService(get_bus()),
                     bus=_LazyBus(), tz_of=user_timezone)


@lru_cache
def get_email_triage() -> EmailTriage:
    return EmailTriage(known_names)


WIRING_GETTERS = (get_activator, get_connect_flow, get_poller, get_first_sync, get_email_triage)


async def _connection_check_job(job: Job) -> None:
    await get_connect_flow().check(int(job.payload["pending_id"]))


async def _first_sync_job(job: Job) -> None:
    await get_first_sync().run(job.user_id, Capability(job.payload["capability"]))


async def _poll_job(job: Job) -> None:
    await get_poller().poll(job.user_id, Capability(job.payload["capability"]))


async def _notify_first_sync(event: Event) -> None:
    from mavis.store.repo import users

    noticed = [str(n) for n in event.payload.get("noticed") or []]
    if not noticed:
        return
    capability = str(event.payload.get("capability", ""))
    try:
        name = display_name(Capability(capability))
    except ValueError:
        name = capability
    user = await users.get(event.user_id)
    intent = NotifyIntent(
        urgency=3,
        intent=f"Tell the user what you noticed after connecting {name}:\n"
               + wrap_untrusted("\n".join(noticed), "first_sync"),
        dedupe_key=f"first_sync:{event.user_id}:{capability}",
    )
    # untrusted: the lines carry email subjects; the executor caps urgency
    await initiative_wiring.current().executor.notify(user, intent, untrusted=True)


async def dispatch_task_completed(event: Event) -> None:
    """The single TASK_COMPLETED owner: first sync, orchestrator task results, else the initiative agent."""
    if event.payload.get("kind") == "first_sync":
        await _notify_first_sync(event)
        return
    if "task_id" in event.payload:  # a finished task: deliver its result, no reasoner LLM call
        from mavis.initiative import task_delivery

        await task_delivery.deliver_task_result(event)
        return
    await initiative_wiring.current().handler.handle(event)


# --- tool registry policy hooks (Phase 4) ----------------------------------------------------------


async def capability_check(user_id: int, capability: Capability) -> bool:
    """Is this capability usable right now? Checked before a tool runs or asks for approval, so the
    user is asked to connect first. WEB and SANDBOX always pass. An expired or revoked account raises
    ConnectionRequired(revoked=True) itself, so the prompt says "reconnect". If the provider cannot be
    reached the check passes, and the tool reports "unreachable" instead of sending a connect link."""
    if capability not in active_capabilities():
        return True
    from mavis.tools import integrations  # module lookup at call time (tests swap the singletons)

    try:
        state = (await integrations.get_connection_cache().status(user_id)).get(capability.value)
    except IntegrationError as exc:
        log.warning("integrations.capability_check_failed", capability=capability.value,
                    error=type(exc).__name__)
        return True
    if state is ConnectionState.ACTIVE:
        return True
    if state is ConnectionState.FAILED:
        raise ConnectionRequired(capability, capability_reason(capability), revoked=True)
    return False


def capability_reason(capability: Capability) -> str:
    """Completes "To <reason>, I need access to your <name>" in the connect prompt."""
    return CAPABILITY_PURPOSE.get(capability, f"use your {capability.value}")


def tool_available(tool: MavisTool) -> bool:
    """Integration tools are offered only when a provider is configured (a dev box without a key
    does not waste tool rounds on them)."""
    if tool.requires is Capability.WEB:
        return get_settings().web_search_enabled  # web tools exist only when WEB is configured
    if tool.requires not in active_capabilities():
        return tool.requires is None or tool.requires not in GOOGLE_CAPABILITIES
    from mavis.tools import integrations

    try:
        return bool(getattr(integrations.get_provider(), "configured", True))
    except IntegrationError:
        return False


def register_integrations(registry: ToolRegistry | None = None) -> None:
    """Hook integrations into the worker. With the Phase 4 ToolRegistry, also set its policy hooks."""
    from mavis.agents.interrupts import register_interrupt_handler

    if registry is not None:
        registry.capability_check = capability_check
        registry.capability_reason = capability_reason
        registry.available = tool_available
    flow = get_connect_flow()
    # A task that needs an account pauses on a connect interrupt; ConnectFlow sends the link and
    # resumes the task (RESUME_TASK) once the account is active, declined or failed.
    register_interrupt_handler("connect", flow.on_connect_interrupt)
    register_event_handler(EventType.BUTTON_PRESSED, dispatch_button)
    register_button_handler("conn:", flow.on_button)
    register_event_handler(EventType.CONNECTION_CHANGED, flow.on_connection_changed, replace=True)
    register_event_handler(EventType.TASK_COMPLETED, dispatch_task_completed, replace=True)
    register_job_handler(JobKind.CONNECTION_CHECK, _connection_check_job)
    register_job_handler(JobKind.FIRST_SYNC, _first_sync_job)
    register_job_handler(JobKind.POLL_PROVIDER, _poll_job)
    register_system_wakeup(CHECK_KIND, flow.on_check_wakeup)
    register_system_wakeup(POLL_KIND, get_poller().on_wakeup)
    # Self-healing: the poll chain lives in the wakeups table, so re-arm it on start and each morning.
    register_startup_hook(heal_all_poll_chains)
    register_startup_hook(check_provider_key)
    routines.register_morning_hook(heal_poll_chains)
    if workspace_enabled():
        routines.register_morning_hook(nudge_upgrade)

    triage = get_email_triage()
    if email_prefilter not in hooks.PREFILTERS:
        hooks.PREFILTERS.append(email_prefilter)
    if triage.enrich not in hooks.ENRICHERS:
        hooks.ENRICHERS.append(triage.enrich)
    if triage.apply_policy not in hooks.DECISION_POLICIES:
        hooks.DECISION_POLICIES.append(triage.apply_policy)
    # Phase 4: connect suggestions enrich/apply_policy hooks

    present = {s.name for s in routines.brief_sources()}
    if "calendar" not in present:
        routines.register_brief_source(
            CalendarBrief(get_provider(), get_connection_cache(), user_timezone, on_failed=reconnect_prompt)
        )
    if "inbox" not in present:
        routines.register_brief_source(
            InboxBrief(get_provider(), get_connection_cache(), on_failed=reconnect_prompt)
        )

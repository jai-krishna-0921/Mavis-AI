"""Ask for a connection exactly when it's needed, then pick the interrupted work back up.

Flow: interrupt(connect) -> start(): link + 'Not now' -> checks at +1/+3/+10/+30/+60 min, then every 6 h
until the pending expires, and on callback -> CONNECTION_CHANGED -> resume waiting runs, first sync,
trigger/poller activation. /connect and /connections reconcile provider state with local state, so a
connection that went ACTIVE unseen is still activated.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog

from mavis.bus.base import EventBus
from mavis.domain import timeutil
from mavis.domain.errors import IntegrationError, NoSuchConnection
from mavis.domain.events import Event, EventType, Job, JobKind, Trust
from mavis.domain.integrations import ConnectionState, PendingStatus, UserRef
from mavis.domain.messages import Button, Outbound
from mavis.domain.policy import Capability
from mavis.store.repo import connections
from mavis.tools.integrations.actions import (
    BRANDS,
    CAPABILITY_PURPOSE,
    GOOGLE_ABILITIES,
    GOOGLE_ANCHOR,
    GOOGLE_CAPABILITIES,
    WORKSPACE_ROW,
    active_capabilities,
    consent_notice,
    display_name,
    is_google,
    workspace_enabled,
)
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.connections import ConnectionCache
from mavis.worker.locks import claim, release

log = structlog.get_logger()

CHECK_KIND = "system_connection_check"
PENDING_TTL = timedelta(hours=24)
CHECK_DELAYS = (
    timedelta(minutes=1), timedelta(minutes=3), timedelta(minutes=10), timedelta(minutes=30),
    timedelta(minutes=60), timedelta(hours=6), timedelta(hours=12), timedelta(hours=18),
    PENDING_TTL + timedelta(minutes=1),  # the last one is past the TTL, so the expiry is reachable
)
REUSE_WINDOW = timedelta(minutes=10)
# Composio expires an unfinished connect link after about 10 minutes.
LINK_LIFETIME = timedelta(minutes=10)
# A task that needs a capability while a connect prompt for it is open (a link sent within this
# window) joins that request instead of sending a new prompt: silently while the last link is still
# alive, otherwise with the link sent again once (it is dead after LINK_LIFETIME).
TASK_JOIN_WINDOW = timedelta(hours=2)
LINK_SENT_KEY = "connect_link_sent"  # user state: {capability: iso time the last link went out}
NOT_NOW_PREFIX = "conn:no:"
START_PREFIX = "conn:start:"
RETRY_PREFIX = "conn:retry:"

Notify = Callable[[Outbound], Awaitable[None]]
Schedule = Callable[[int, datetime, str, str], Awaitable[int]]  # (user_id, at, reason, kind)
OnActive = Callable[[int, Capability], Awaitable[None]]
OnGoogleBegin = Callable[[int], object]  # a googlesuper fan-out starts (reset per-activation state)
OnGoogleActive = Callable[[int], Awaitable[object]]  # once per googlesuper activation (retire old triggers)
HasChecks = Callable[[int, int], Awaitable[bool]]  # (user_id, pending_id) -> a check is still scheduled
CancelChecks = Callable[[int, int], Awaitable[object]]  # (user_id, pending_id): drop its scheduled checks
FIRST_SYNC_CLAIM_TTL_S = 600
MORE_ACCESS_KEY = "more_access_offered"  # user state: {service: day an "add the missing permission" link went out}
GOOGLE_KEY = "google"  # one reconnect prompt per day for all eight Google capabilities
NUDGE_KEY = "workspace_nudged"
UPGRADE_TEXT = ("I can now work with your Drive, Docs, Sheets and Tasks too. "
                "Tap to upgrade your Google connection.")


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


ForgetSource = Callable[[int, str], Awaitable[int]]


class UserState(Protocol):
    async def get(self, user_id: int) -> dict: ...
    async def update(self, user_id: int, patch: dict) -> dict: ...


class RepoUserState:
    async def get(self, user_id: int) -> dict:
        from mavis.store.repo import users

        return await users.get_state(user_id)

    async def update(self, user_id: int, patch: dict) -> dict:
        from mavis.store.repo import users

        return await users.update_state(user_id, patch)


class ReplyScope:
    """Gives every reply sent inside it a dedupe key derived from the triggering event, and records it."""

    def __init__(self, event_id: str) -> None:
        self.event_id, self.texts = event_id, []

    def next_key(self, text: str) -> str:
        self.texts.append(text)
        return f"cmdreply:{self.event_id}:{len(self.texts) - 1}"


_reply_scope: ContextVar[ReplyScope | None] = ContextVar("connect_reply_scope", default=None)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class ConnectFlow:
    def __init__(
        self,
        *,
        provider: IntegrationProvider,
        cache: ConnectionCache,
        bus: EventBus,
        notify: Notify,
        schedule: Schedule,
        state: UserState,
        base_url: str,
        on_active: OnActive | None = None,
        has_checks: HasChecks | None = None,
        cancel_checks: CancelChecks | None = None,
        clock: Callable[[], datetime] = timeutil.now,
        on_google_active: OnGoogleActive | None = None,
        on_google_begin: OnGoogleBegin | None = None,
        forget_source: ForgetSource | None = None,
    ) -> None:
        self.forget_source = forget_source
        self.provider, self.cache, self.bus = provider, cache, bus
        self.notify, self.schedule, self.state = notify, schedule, state
        self.base_url = base_url.rstrip("/")
        self.on_active = on_active
        self.on_google_active = on_google_active
        self.on_google_begin = on_google_begin
        self.has_checks = has_checks
        self.cancel_checks = cancel_checks
        self.clock = clock

    @contextlib.contextmanager
    def reply_scope(self, event_id: str) -> Iterator[ReplyScope]:
        scope = ReplyScope(event_id)
        token = _reply_scope.set(scope)
        try:
            yield scope
        finally:
            _reply_scope.reset(token)

    async def send(self, user_id: int, text: str, buttons: list[list[Button]] | None = None) -> None:
        scope = _reply_scope.get()
        key = scope.next_key(text) if scope else None
        await self.notify(Outbound(user_id=user_id, text=text, buttons=buttons or [], dedupe_key=key))

    async def _resume(self, task_id: str, user_id: int, connected: bool, tag: str) -> None:
        await self.bus.enqueue(Job(
            id=f"resume:{task_id}:{tag}", user_id=user_id, kind=JobKind.RESUME_TASK,
            payload={"task_id": task_id, "value": {"connected": connected}},
        ))

    # --- starting a connection ---------------------------------------------------------------------

    async def start(
        self, user_id: int, capability: Capability, reason: str = "", task_id: str | None = None,
        revoked: bool = False,
    ) -> int | None:
        name = display_name(capability)
        if not revoked and await self.cache.is_active(user_id, capability, fresh=True):
            # The provider says ACTIVE: make sure the local side (first sync, polling) agrees.
            activated = await self.reconcile(user_id, capability, announce=task_id is None)
            if task_id:
                await self._resume(task_id, user_id, True, f"already:{capability.value}")
            elif not activated:
                await self.send(user_id, f"{name} is already connected ✓")
            return None

        now = self.clock()
        recent = await self._open_prompt(user_id, capability)
        window = TASK_JOIN_WINDOW if task_id else REUSE_WINDOW
        sent_at = await self._link_sent_at(user_id, capability, recent)
        if recent is not None and sent_at is not None and now - sent_at < window:
            # A link went out recently: remember this run, hand the link over again if needed, and
            # make sure something is still watching for the sign-in to finish.
            if task_id:
                # Dated like the pending it waits on: joining never extends the prompt's window, and
                # that pending's decline / expiry closes this row too.
                await connections.create_pending(user_id, capability, reason, task_id,
                                                 now=_aware(recent.created_at))
            await self._ensure_checks(user_id, recent.id, now)
            if task_id is None or now - sent_at >= LINK_LIFETIME:
                await self._send_link(user_id, capability, recent.id, again=True)
            return recent.id

        pending_id = await connections.create_pending(user_id, capability, reason, task_id, now=now)
        if not await self._send_link(user_id, capability, pending_id, reason=reason, revoked=revoked):
            await connections.resolve(pending_id, PendingStatus.FAILED, now=now)
            if task_id:
                await self._resume(task_id, user_id, False, f"linkfail:{pending_id}")
            return None
        await self._ensure_checks(user_id, pending_id, now)
        return pending_id

    async def _open_prompt(self, user_id: int, capability: Capability):
        """The newest open prompt: the first row of the newest date (joined rows share their prompt's
        date, so this is the pending the link and its checks belong to)."""
        rows = await connections.open_for(user_id, capability)
        if not rows:
            return None
        newest = max(_aware(r.created_at) for r in rows)
        return next(r for r in rows if _aware(r.created_at) == newest)

    async def _link_sent_at(self, user_id: int, capability: Capability, recent) -> datetime | None:
        """When the last link for this capability went out (falls back to the open pending's time)."""
        if recent is None:
            return None
        raw = (await self.state.get(user_id)).get(LINK_SENT_KEY, {}).get(capability.value)
        anchor = _aware(recent.created_at)
        try:
            sent = _aware(datetime.fromisoformat(raw)) if raw else anchor
        except ValueError:
            sent = anchor
        return max(sent, anchor)

    async def _note_link_sent(self, user_id: int, capability: Capability) -> None:
        sent = dict((await self.state.get(user_id)).get(LINK_SENT_KEY, {}))
        sent[capability.value] = self.clock().isoformat()
        await self.state.update(user_id, {LINK_SENT_KEY: sent})

    async def _send_link(
        self, user_id: int, capability: Capability, pending_id: int, *, reason: str = "",
        revoked: bool = False, again: bool = False,
    ) -> bool:
        name = display_name(capability)
        callback = f"{self.base_url}/connect/callback?p={pending_id}"
        try:
            url = await self.provider.connect_link(UserRef(user_id=user_id), capability.value, callback)
        except IntegrationError as exc:
            log.warning("connect.link_failed", capability=capability.value, error=str(exc))
            await self.send(user_id, f"I'd need {name} for that, but I can't open a connection right now. "
                                     "Mind if we try again in a bit?")
            return False
        if again:
            lead = "Here's your link again:"
        elif revoked:
            lead = f"Your {name} access has expired. One tap to reconnect:"
        elif reason:
            lead = f"To {reason}, I need access to your {name}. One tap here:"
        else:
            lead = f"Let's connect your {name}. One tap here:"
        text = (f"{lead}\nYou'll sign in on {BRANDS[capability]}'s own page. No password comes to me, "
                "and you can revoke access anytime.")
        if notice := consent_notice(capability):
            text += f"\n{notice}"
        await self.send(user_id, text, [
            [Button(label=f"Connect {name}", url=url)],
            [Button(label="Not now", data=f"{NOT_NOW_PREFIX}{pending_id}")],
        ])
        await self._note_link_sent(user_id, capability)
        return True

    async def _close(self, user_id: int, pending_id: int, status: PendingStatus) -> None:
        """Resolve a pending and drop the check wakeups that would only fire as no-ops."""
        await connections.resolve(pending_id, status, now=self.clock())
        if self.cancel_checks is not None:
            try:
                await self.cancel_checks(user_id, pending_id)
            except Exception as exc:  # noqa: BLE001 - leftover checks are harmless no-ops
                log.warning("connect.cancel_checks_failed", error=type(exc).__name__)

    async def _ensure_checks(self, user_id: int, pending_id: int, now: datetime) -> None:
        if self.has_checks is not None and await self.has_checks(user_id, pending_id):
            return
        created = await connections.get_pending(pending_id)
        origin = _aware(created.created_at) if created is not None else now
        for delay in CHECK_DELAYS:
            if origin + delay > now:  # a reused pending skips the checks that are already in the past
                await self.schedule(user_id, max(origin + delay, now + timedelta(seconds=30)),
                                    str(pending_id), CHECK_KIND)

    async def on_connect_interrupt(self, task_id: int | str, user_id: int, payload: dict[str, Any]) -> None:
        """Phase 4 interrupt handler for {"type": "connect"} (dormant in this slice; Phase 4 registers it)."""
        capability = Capability(payload["capability"])
        await self.start(
            user_id, capability, CAPABILITY_PURPOSE.get(capability, ""), task_id=str(task_id),
            revoked=bool(payload.get("revoked")),
        )

    # --- learning the outcome --------------------------------------------------------------------

    async def check(self, pending_id: int) -> None:
        p = await connections.get_pending(pending_id)
        if p is None or p.status not in (PendingStatus.PENDING.value, PendingStatus.DECLINED.value):
            return
        declined = p.status == PendingStatus.DECLINED.value
        capability = Capability(p.capability)
        if not declined and self.clock() - _aware(p.created_at) > PENDING_TTL:
            # Every run that joined this prompt (rows dated like it) expires with it and is resumed.
            for row in await self._joined(p):
                await self._close(p.user_id, row.id, PendingStatus.EXPIRED)
                if row.task_id:
                    await self._resume(row.task_id, p.user_id, False, f"expired:{row.id}")
            return
        states = await self.cache.status(p.user_id, fresh=True)
        state = self._state_of(capability, states)
        if state is ConnectionState.FAILED:
            st = await self.state.get(p.user_id)
            if st.get("synced", {}).get(capability.value) or st.get("reconnect_prompted", {}).get(
                capability.value
            ):
                return  # a reconnect after expiry: FAILED is still the old account, keep waiting for ACTIVE
        # After "Not now" the user may still have finished the sign-in, so ACTIVE still counts.
        accepted = (ConnectionState.ACTIVE,) if declined else (ConnectionState.ACTIVE, ConnectionState.FAILED)
        if state not in accepted:
            return
        await self.bus.publish(Event(
            id=f"conn:{p.user_id}:{capability.value}:{state.value.lower()}:{pending_id}",
            user_id=p.user_id, type=EventType.CONNECTION_CHANGED, occurred_at=self.clock(),
            source="integrations", trust=Trust.SYSTEM,
            payload={"capability": capability.value, "state": state.value, "pending_id": pending_id},
        ))

    @staticmethod
    def _state_of(capability: Capability, states: dict[str, ConnectionState]) -> ConnectionState:
        """The state a pending for `capability` waits on. One Google consent covers every Google service, and
        a person may leave some boxes unticked, so for Google it is ACTIVE as soon as any service is."""
        if not is_google(capability):
            return states.get(capability.value, ConnectionState.NONE)
        mine = [states.get(c.value, ConnectionState.NONE) for c in GOOGLE_CAPABILITIES]
        for wanted in (ConnectionState.ACTIVE, ConnectionState.FAILED, ConnectionState.INITIATED):
            if wanted in mine:
                return wanted
        return ConnectionState.NONE

    async def on_check_wakeup(self, user_id: int, reason: str) -> None:
        try:
            pending_id = int(reason)
        except ValueError:
            return
        await self.check(pending_id)

    async def on_connection_changed(self, event: Event) -> None:
        capability = Capability(event.payload["capability"])
        state = ConnectionState(event.payload["state"])
        user_id = event.user_id
        name = display_name(capability)
        self.cache.invalidate(user_id)
        waiting = await connections.open_for(user_id, capability)

        if state is ConnectionState.FAILED:
            for p in waiting:
                await self._close(user_id, p.id, PendingStatus.FAILED)
                if p.task_id:
                    await self._resume(p.task_id, user_id, False, f"failed:{p.id}")
            if waiting:
                await self.send(user_id, f"Hmm, the {name} connection didn't go through. Want a fresh link?",
                                [[Button(label="Try again", data=f"{RETRY_PREFIX}{capability.value}")]])
            return
        if state is not ConnectionState.ACTIVE:
            return

        resumed = False
        for p in waiting:
            await self._close(user_id, p.id, PendingStatus.ACTIVE)
            if p.task_id:
                await self._resume(p.task_id, user_id, True, f"conn:{p.id}")
                resumed = True

        activated = await self._activate(user_id, capability)
        # One announcement per activation: later duplicate events (other pendings) stay quiet.
        if not resumed and (waiting or activated):
            await self._announce(user_id, capability)

    async def _announce(self, user_id: int, capability: Capability) -> None:
        if is_google(capability):
            await self.send(user_id, await self._google_announcement(user_id))
            return
        text = (f"Connected ✓ I can see your {display_name(capability)} now. "
                "Give me a minute to get familiar with it.")
        if capability is Capability.SLACK and await self._slack_chat_ready(user_id):
            text += " You can also message me right in Slack: open the Mavis AI app under Apps."
        await self.send(user_id, text)

    async def _slack_chat_ready(self, user_id: int) -> bool:
        ready = getattr(self.provider, "slack_chat_ready", None)
        if ready is None:
            return False
        try:
            return bool(await ready(user_id))
        except Exception as exc:  # noqa: BLE001 - a courtesy line, never a reason to fail the announcement
            log.warning("connect.slack_chat_check_failed", error=type(exc).__name__)
            return False

    async def _google_announcement(self, user_id: int) -> str:
        full = f"Connected ✓ I can now work with your {GOOGLE_ABILITIES}."
        access = getattr(self.provider, "google_access", None)
        if access is None:
            return full
        try:
            found = await access(user_id)
        except Exception as exc:  # noqa: BLE001 - the announcement must not fail the activation
            log.warning("connect.google_access_failed", error=type(exc).__name__)
            return full
        if found is None or not found[1]:
            return full
        can, cannot = found
        return (f"Connected ✓ I can {_join(can)}. Not allowed on Google's screen: {', '.join(cannot)}. "
                "To add them, send /connect google and leave every box ticked.")

    async def _google_fan_out(self, user_id: int, capability: Capability) -> list[Capability]:
        """The Google account is googlesuper (the anchor is ACTIVE): every Google capability activates.
        A legacy Gmail/Calendar activation stays on its own capability."""
        if not is_google(capability):
            return [capability]
        states = await self.cache.status(user_id, fresh=True)
        live = [c for c in GOOGLE_CAPABILITIES if states.get(c.value) is ConnectionState.ACTIVE]
        if not live or (len(live) == 1 and live[0] is capability):
            return [capability]  # a lone legacy Gmail or Calendar activation stays on its own capability
        if self.on_google_begin is not None:
            self.on_google_begin(user_id)
        return live  # only what the consent actually allowed

    async def _activate(self, user_id: int, capability: Capability) -> bool:
        """Activate `capability`, or all eight Google capabilities when the Google account is googlesuper.
        True if a first sync ran for any of them."""
        targets = await self._google_fan_out(user_id, capability)
        ran = False
        for target in targets:
            ran = await self._activate_one(user_id, target) or ran
        if len(targets) > 1 and ran and self.on_google_active is not None:
            try:
                await self.on_google_active(user_id)
            except Exception as exc:  # noqa: BLE001 - leftover legacy triggers only duplicate events
                log.warning("connect.google_active_hook_failed", error=type(exc).__name__)
        return ran

    async def _activate_one(self, user_id: int, capability: Capability) -> bool:
        """First sync (once), polling or triggers, and a cleared reconnect prompt. True if first sync ran."""
        st = await self.state.get(user_id)
        synced = dict(st.get("synced", {}))
        first_time = not synced.get(capability.value)
        if first_time:
            # claim(): two paths (command and event handler) can both see "not synced" at once
            if not await claim(f"first_sync:{user_id}:{capability.value}", FIRST_SYNC_CLAIM_TTL_S):
                first_time = False
            else:
                await self.bus.enqueue(Job(id=f"first_sync:{user_id}:{capability.value}", user_id=user_id,
                                           kind=JobKind.FIRST_SYNC, payload={"capability": capability.value}))
                synced[capability.value] = self.clock().isoformat()
                await self.state.update(user_id, {"synced": synced})
        prompted = dict(st.get("reconnect_prompted", {}))
        cleared = prompted.pop(capability.value, None) is not None
        if is_google(capability):
            cleared = prompted.pop(GOOGLE_KEY, None) is not None or cleared
        if cleared:
            await self.state.update(user_id, {"reconnect_prompted": prompted})
        if self.on_active is not None:
            await self.on_active(user_id, capability)
        return first_time

    async def reconcile(self, user_id: int, capability: Capability, *, announce: bool = False) -> bool:
        """The provider says ACTIVE. If the local side never noticed (no first sync, no activation
        state), run activation now. True if it ran and the user was told."""
        st = await self.state.get(user_id)
        if st.get("synced", {}).get(capability.value) and capability.value in st.get("polling", {}):
            return False
        for p in await connections.open_for(user_id, capability):  # the sign-in finished unseen
            await self._close(user_id, p.id, PendingStatus.ACTIVE)
            if p.task_id:
                await self._resume(p.task_id, user_id, True, f"conn:{p.id}")
        first_time = await self._activate(user_id, capability)
        if announce and first_time:
            await self._announce(user_id, capability)
            return True
        return False

    async def prompt_reconnect(self, user_id: int, capability: Capability) -> bool:
        """Tell the user once per capability per day that access expired or was revoked, with a
        reconnect button. True if a prompt went out."""
        st = await self.state.get(user_id)
        today = self.clock().date().isoformat()
        prompted = dict(st.get("reconnect_prompted", {}))
        key = GOOGLE_KEY if is_google(capability) else capability.value
        if prompted.get(key) == today:
            return False
        if await self.start(user_id, capability, "", revoked=True) is None:
            return False  # no link went out: try again next time
        prompted[key] = today
        await self.state.update(user_id, {"reconnect_prompted": prompted})
        return True

    async def offer_more_access(self, user_id: int, capability: Capability) -> bool:
        """The account is connected but lacks a permission the user just needed (they unticked it on the
        consent screen). Send the way to add it, once per service per day. True if it went out. No pending
        request is made: nothing is "connected" until they sign in again, and then the callback does it."""
        st = await self.state.get(user_id)
        today = self.clock().date().isoformat()
        offered = dict(st.get(MORE_ACCESS_KEY, {}))
        key = GOOGLE_KEY if is_google(capability) else capability.value
        if offered.get(key) == today:
            return False
        name = display_name(capability)
        try:
            url = await self.provider.connect_link(UserRef(user_id=user_id), capability.value,
                                                   f"{self.base_url}/connect/callback")
        except IntegrationError as exc:
            log.warning("connect.more_access_link_failed", capability=capability.value, error=str(exc))
            return False
        text = (f"That needs more access to your {name} than you allowed. One tap here, and leave every box "
                f"ticked:\nYou'll sign in on {BRANDS[capability]}'s own page. No password comes to me, and "
                "you can revoke access anytime.")
        if notice := consent_notice(capability):
            text += f"\n{notice}"
        await self.send(user_id, text, [[Button(label=f"Reconnect {name}", url=url)]])
        offered[key] = today
        await self.state.update(user_id, {MORE_ACCESS_KEY: offered})
        return True

    # --- user controls ----------------------------------------------------------------------------

    async def _joined(self, p) -> list:
        """`p` plus every open row for the same user and capability dated no later than it (the runs
        that joined its prompt)."""
        rows = await connections.open_for(p.user_id, Capability(p.capability))
        cutoff = _aware(p.created_at)
        return [r for r in rows if r.id == p.id or _aware(r.created_at) <= cutoff]

    async def decline(self, user_id: int, pending_id: int) -> None:
        p = await connections.get_pending(pending_id)
        if p is None or p.user_id != user_id or p.status != PendingStatus.PENDING.value:
            return
        # "Not now" answers the capability: every waiting run is told, not only the one on the button.
        rows = await connections.open_for(user_id, Capability(p.capability))
        resumed = False
        for row in rows:
            # resolve, not _close: the prompt's scheduled checks stay, so a sign-in finished after
            # "Not now" is still found (check() accepts ACTIVE on a declined row).
            await connections.resolve(row.id, PendingStatus.DECLINED, now=self.clock())
            if row.task_id:
                await self._resume(row.task_id, user_id, False, f"declined:{row.id}")
                resumed = True
        not_now = dict((await self.state.get(user_id)).get("not_now", {}))
        not_now[p.capability] = self.clock().isoformat()
        await self.state.update(user_id, {"not_now": not_now})
        if not resumed:
            await self.send(user_id, "No problem. Just say the word whenever.")

    async def on_button(self, event: Event, data: str) -> None:
        try:
            if data.startswith(NOT_NOW_PREFIX):
                await self.decline(event.user_id, int(data.removeprefix(NOT_NOW_PREFIX)))
            elif data.startswith((START_PREFIX, RETRY_PREFIX)):
                capability = Capability(data.rsplit(":", 1)[1])
                if capability in active_capabilities():
                    await self.start(event.user_id, capability, "")
        except ValueError:
            log.warning("connect.bad_button", data=data)

    async def _reconcile_active(self, user_id: int, states: dict[str, ConnectionState]) -> None:
        for c in active_capabilities():
            if states.get(c.value) is ConnectionState.ACTIVE:
                await self.reconcile(user_id, c)

    def _menu(self) -> list[tuple[Capability, str]]:
        """(capability, label) per menu row: with Workspace on, one Google row instead of Gmail + Calendar."""
        if not workspace_enabled():
            return [(c, display_name(c)) for c in active_capabilities()]
        others = [c for c in active_capabilities() if c not in GOOGLE_CAPABILITIES]
        return [(GOOGLE_ANCHOR, WORKSPACE_ROW), *((c, display_name(c)) for c in others)]

    async def offer_menu(self, user_id: int) -> None:
        states = await self.cache.status(user_id, fresh=True)
        await self._reconcile_active(user_id, states)
        menu = self._menu()
        rows = [
            [Button(label=f"Connect {label}", data=f"{START_PREFIX}{c.value}")]
            for c, label in menu if states.get(c.value) is not ConnectionState.ACTIVE
        ]
        if not rows:
            labels = [label.replace("Google Calendar", "Calendar") for _, label in menu]
            listed = f"{', '.join(labels[:-1])} and {labels[-1]}"
            await self.send(user_id, f"Everything's already connected: {listed}.")
            return
        await self.send(user_id, "Which one should I hook up?", rows)

    async def status_text(self, user_id: int) -> str:
        states = await self.cache.status(user_id, fresh=True)
        await self._reconcile_active(user_id, states)
        lines = []
        for c, label in self._menu():
            state = states.get(c.value)
            if state is ConnectionState.ACTIVE:
                mark, word = "✅", "connected"
            elif state is ConnectionState.FAILED:
                mark, word = "⚠️", "needs reconnecting"
            elif c is GOOGLE_ANCHOR and workspace_enabled() and any(
                states.get(g.value) is ConnectionState.ACTIVE for g in (Capability.GMAIL, Capability.CALENDAR)
            ):
                mark, word = "⚪", "Gmail and Calendar only (send /connect google to add the rest)"
            else:
                mark, word = "⚪", "not connected"
            lines.append(f"{mark} {label}: {word}")
        return "\n".join(lines)

    async def maybe_nudge_upgrade(self, user_id: int) -> bool:
        """Workspace on, Gmail/Calendar on the legacy connection only: one upgrade nudge, ever."""
        if not workspace_enabled():
            return False
        st = await self.state.get(user_id)
        if st.get(NUDGE_KEY):
            return False
        states = await self.cache.status(user_id)
        if states.get(GOOGLE_ANCHOR.value) is ConnectionState.ACTIVE:
            return False
        legacy = (Capability.GMAIL, Capability.CALENDAR)
        if not any(states.get(c.value) is ConnectionState.ACTIVE for c in legacy):
            return False
        await self.state.update(user_id, {NUDGE_KEY: self.clock().isoformat()})
        await self.send(user_id, UPGRADE_TEXT,
                        [[Button(label="Upgrade Google", data=f"{START_PREFIX}{GOOGLE_ANCHOR.value}")]])
        return True

    async def disconnect(self, user_id: int, capability: Capability, *, forget: bool = True) -> bool:
        """True when the connection is gone. `forget=False` keeps what was learned from it."""
        name = display_name(capability)
        try:
            await self.provider.disconnect(UserRef(user_id=user_id), capability.value)
        except NoSuchConnection:
            if not (workspace_enabled() and is_google(capability)):
                await self.send(user_id, f"There's no {name} connection to remove.")
                return False
            try:
                removed = await self._disconnect_legacy_accounts(user_id)
            except IntegrationError as exc:
                log.warning("connect.disconnect_failed", capability="legacy", error=str(exc))
                await self.send(user_id, f"I couldn't disconnect {name} just now. "
                                         "Mind trying again in a bit?")
                return False
            if not removed:
                await self.send(user_id, f"There's no {name} connection to remove.")
                return False
        except IntegrationError as exc:
            log.warning("connect.disconnect_failed", capability=capability.value, error=str(exc))
            await self.send(user_id, f"I couldn't disconnect {name} just now. Mind trying again in a bit?")
            return False
        self.cache.invalidate(user_id)
        dropped = [c.value for c in (GOOGLE_CAPABILITIES if is_google(capability) else (capability,))]
        st = await self.state.get(user_id)
        synced = {k: v for k, v in st.get("synced", {}).items() if k not in dropped}
        polling = {k: v for k, v in st.get("polling", {}).items() if k not in dropped}
        if synced != st.get("synced", {}):
            await self.state.update(user_id, {"synced": synced})
        for dropped_name in dropped:  # the "first sync is running" claim must not outlive the connection
            await release(f"first_sync:{user_id}:{dropped_name}")
        if polling != st.get("polling", {}):
            await self.state.update(user_id, {"polling": polling})
        forgotten = await self._forget_learned(user_id, capability) if forget else 0
        await self.send(user_id, f"Disconnected {name}. I can't see it anymore."
                        + (" I also removed what I had learned from it." if forgotten else ""))
        return True

    async def _forget_learned(self, user_id: int, capability: Capability) -> int:
        """Forget is the default: what was learned from mail or Slack records goes with the connection."""
        prefix = ""
        if capability is Capability.GMAIL or is_google(capability):
            prefix = "gmail:"
        elif capability is Capability.SLACK:
            prefix = "slack:"
        if not prefix or self.forget_source is None:
            return 0
        try:
            return int(await self.forget_source(user_id, prefix))
        except Exception as exc:  # noqa: BLE001 - the disconnect itself already succeeded
            log.warning("connect.forget_source_failed", prefix=prefix, error=type(exc).__name__)
            return 0

    async def _disconnect_legacy_accounts(self, user_id: int) -> int:
        """No googlesuper account: remove the old Gmail and Calendar accounts instead. Returns how many."""
        removed = 0
        for alias in ("gmail-legacy", "calendar-legacy"):
            try:
                await self.provider.disconnect(UserRef(user_id=user_id), alias)
            except NoSuchConnection:
                continue
            removed += 1
        return removed

    async def disconnect_legacy(self, user_id: int, alias: str) -> None:
        """/disconnect gmail-legacy or calendar-legacy: remove an old pre-Workspace account only."""
        label = "Gmail" if alias.startswith("gmail") else "Calendar"
        try:
            await self.provider.disconnect(UserRef(user_id=user_id), alias)
        except NoSuchConnection:
            await self.send(user_id, f"There's no old {label} connection to remove.")
            return
        except IntegrationError as exc:
            log.warning("connect.disconnect_failed", capability=alias, error=str(exc))
            await self.send(user_id, f"I couldn't remove the old {label} connection just now. "
                                     "Mind trying again in a bit?")
            return
        self.cache.invalidate(user_id)
        await self.send(user_id, f"Removed the old {label} connection. Your Google connection is untouched.")

"""Google Workspace signal intake (spec 2026-10-03 section 5).

Webhooks (comment, task, share) and two self-rescheduling polls (Tasks due/overdue, Drive shared-with-me)
become Signals; `workspace_signals.decide` scores them; each lands once in attention_observations (source
drive/docs/tasks). Asks and notifies first pass the ping policy (quiet hours, daily budget, dedupe), like
attention's own Speaker; a deferred one is re-spoken by a one-off wakeup. Ask: fixed text with Keep/Drop.
Share that closes a loop: fixed text (amendment A8). Comment: one line composed by the executor (untrusted,
background LLM priority), fixed text when the LLM is busy. Brief and log rows feed the morning brief and
evening wrap. Polls stay quiet (brief rows only) until first sync has set BASELINE_KEY (amendment A3).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.exc import NoResultFound

from mavis.attention.feedback import reply
from mavis.attention.schema import Verdict
from mavis.attention.workspace_signals import (
    SOURCE_OF,
    Signal,
    SignalKind,
    WorkspaceDecision,
    comment_signal,
    decide,
    match_loop,
    safe_title,
    shared_file_signal,
    task_signal,
)
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.decisions import NotifyIntent
from mavis.domain.errors import ConnectionRequired
from mavis.domain.events import Event
from mavis.domain.integrations import UserRef
from mavis.domain.loops import LoopStatus
from mavis.domain.messages import Button
from mavis.domain.policy import Capability
from mavis.initiative.untrusted import wrap_untrusted
from mavis.policy.pings import PingPolicy
from mavis.store.repo import attention as repo
from mavis.store.repo import audit, users
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.connections import ConnectionCache
from mavis.tools.integrations.normalize import extract_list, pick, to_datetime
from mavis.tools.integrations.poller import WORKSPACE_POLL_KIND
from mavis.tools.integrations.workspace_guard import (
    STATE_KEY,
    file_meta,
    modify_workspace_state,
    my_email,
    update_workspace_state,
)
from mavis.tools.registry import ToolContext
from mavis.worker.locks import lock

log = structlog.get_logger()

PREFIX = "ws:"
MUTE, KEEP, DROP = "ws:m:", "ws:k:", "ws:d:"
SPEAK = "speak:"  # WORKSPACE_POLL_KIND reason of a deferred ask or notify: speak:<observation id>
BASELINE_KEY = "baselined_at"  # users.state["workspace"]: set by first sync (Task 13); polls speak after it
SHARED_LOOKBACK = timedelta(minutes=30)  # a missing cursor starts here: no backfill, no gap alarm
MAX_LIST = 200
MAX_CONTACTS = 1000
NEW, DUPLICATE = "new", "duplicate"
SENT, DEFERRED, DROPPED, NONE = "sent", "deferred", "dropped", "none"
POLL_REASONS: dict[str, Capability] = {"tasks": Capability.TASKS, "drive": Capability.DRIVE}
WORKSPACE_SOURCES = frozenset(SOURCE_OF.values())
Schedule = Callable[[int, datetime, str, str], Awaitable[int]]


def _appended(items: Any, value: str) -> list[str]:
    """`value` moved to the end of the list, at most MAX_LIST kept."""
    kept = [x for x in items or [] if x != value]
    return [*kept[-(MAX_LIST - 1):], value]


class WorkspaceIntake:
    def __init__(
        self,
        *,
        provider: IntegrationProvider,
        executor_of: Callable[[], Any],
        loops: Any,
        schedule: Schedule,
        clock: Callable[[], datetime] = timeutil.now,
        cache: ConnectionCache | None = None,
        policy: PingPolicy | None = None,
    ) -> None:
        self.provider, self.executor_of, self.loops = provider, executor_of, loops
        self.schedule, self.clock = schedule, clock
        self.cache = cache or ConnectionCache(provider)
        self.policy = policy or PingPolicy()

    # --- state (all writes inside the users row lock, amendment A5) ------------------------------------

    async def state(self, user_id: int) -> dict:
        return dict((await users.get_state(user_id)).get(STATE_KEY) or {})

    async def patch(self, user_id: int, **kv: Any) -> None:
        await update_workspace_state(user_id, kv)

    async def _append(self, user_id: int, key: str, value: str) -> None:
        await modify_workspace_state(user_id, lambda st: {**st, key: _appended(st.get(key), value)})

    async def _advance_cursor(self, user_id: int, newest: datetime) -> None:
        def change(st: dict) -> dict:
            current = to_datetime(st.get("shared_after"))
            return {**st, "shared_after": max(current or newest, newest).isoformat()}

        await modify_workspace_state(user_id, change)

    async def mark_baselined(self, user_id: int) -> None:
        """First sync calls this once its quiet baseline is in: from now on polls may ask and notify."""
        await self.patch(user_id, **{BASELINE_KEY: self.clock().isoformat()})

    async def _execute(self, user_id: int, action: str, args: dict) -> Any:
        res = await self.provider.execute(UserRef(user_id=user_id), action, args)
        if not res.ok:
            log.warning("workspace.provider_failed", action=action, error=(res.error or "")[:120])
            return None
        return res.data

    def _today(self, tz: str) -> date:
        return timeutil.to_local(self.clock(), tz).date()

    async def known(self, user_id: int, actor: str) -> bool:
        """Seen in Contacts (seeded at first sync) or in mail history (attention_senders). A Drive sharer's
        address is the Google account that shared, so a spoofed From line cannot be replayed here."""
        address = actor.strip().lower()
        if "@" not in address:
            return False
        if address in set((await self.state(user_id)).get("contacts") or []):
            return True
        return await repo.sender_known(user_id, address)

    async def my_address(self, user_id: int) -> str:
        """The user's Google address, looked up lazily and cached (amendment A4); "" when unknown."""
        try:
            return await my_email(ToolContext(user_id=user_id), provider=self.provider, cache=self.cache)
        except ConnectionRequired:
            return ""

    # --- inputs ---------------------------------------------------------------------------------------

    async def on_event(self, event: Event) -> None:
        kind = event.payload.get("kind")
        raw = event.payload.get("raw") if isinstance(event.payload.get("raw"), dict) else {}
        try:
            user = await users.get(event.user_id)
        except NoResultFound:
            return
        if kind == "share":
            await self.poll_shared(user.id)  # the share payload names no sharer: list shared-with-me
            return
        signal: Signal | None = None
        quiet = False
        if kind == "comment":
            me = await self.my_address(user.id)
            signal = comment_signal(raw, me=me)
            if signal is not None:
                signal = await self._with_file(user.id, signal)
        elif kind == "task":
            task = raw.get("task") if isinstance(raw.get("task"), dict) else raw
            signal = task_signal(task, self._today(user.timezone))
            quiet = not await self._baselined(user.id)
        if signal is not None:
            await self.handle(user, signal, quiet=quiet)

    async def _with_file(self, user_id: int, s: Signal) -> Signal:
        """Title and ownership from workspace_guard.file_meta; an unreadable file is not the user's."""
        try:
            meta = await file_meta(ToolContext(user_id=user_id), s.object_id, provider=self.provider,
                                   cache=self.cache)
        except ConnectionRequired:
            meta = None
        title = safe_title(meta.name if meta is not None else "") or "a document"
        return s.model_copy(update={"object_title": title, "owned_by_me": bool(meta and meta.owned_by_me)})

    async def _baselined(self, user_id: int) -> bool:
        return bool((await self.state(user_id)).get(BASELINE_KEY))

    async def poll_shared(self, user_id: int) -> int:
        st = await self.state(user_id)
        after = to_datetime(st.get("shared_after")) or (self.clock() - SHARED_LOOKBACK)
        data = await self._execute(user_id, "drive.list_recent", {"shared_with_me": True, "max_results": 25})
        if data is None:
            return 0
        user = await users.get(user_id)
        quiet = not st.get(BASELINE_KEY)
        newest, handled = after, 0
        for f in extract_list(data, "files", "data.files"):
            when = to_datetime(f.get("sharedWithMeTime"))
            if when is None or when <= after:
                continue
            newest = max(newest, when)
            signal = shared_file_signal(f)
            if signal is not None and await self.handle(user, signal, quiet=quiet) == NEW:
                handled += 1
        await self._advance_cursor(user_id, newest)
        return handled

    async def poll_tasks(self, user_id: int, *, quiet: bool = False) -> int:
        """`quiet=True` is first sync's baseline: nothing is spoken and overdue tasks count as asked, so no
        keep-or-drop question ever comes for a backlog the user already had. Before the baseline marker a
        poll is quiet too, but asks nothing away (the baseline still owns that backlog)."""
        user = await users.get(user_id)
        backlog = quiet
        quiet = quiet or not await self._baselined(user_id)
        local = timeutil.to_local(self.clock(), user.timezone)
        end = local.replace(hour=23, minute=59, second=59, microsecond=0)
        query = {"due_before": end.isoformat(), "max_results": 100}
        data = await self._execute(user_id, "tasks.list", query)
        if data is None:
            return 0
        handled = 0
        for t in extract_list(data, "tasks", "data.tasks"):
            signal = task_signal(t, local.date())
            if signal is not None and await self.handle(user, signal, quiet=quiet, backlog=backlog) == NEW:
                handled += 1
        return handled

    # --- decide, persist, speak -----------------------------------------------------------------------

    async def handle(self, user: Any, s: Signal, *, quiet: bool = False, backlog: bool = False) -> str:
        """One row per signal. `quiet`: before the first-sync baseline, asks and notifies become brief
        rows and nothing is closed (the user has not seen Mavis watch this account yet). `backlog` (first
        sync): a demoted ask also marks the task asked, so it is never asked about later."""
        if s.kind is SignalKind.FILE_SHARED:
            s = s.model_copy(update={"actor_known": await self.known(user.id, s.actor)})
        st = await self.state(user.id)
        loop_id, loop_title = None, ""
        if s.kind is SignalKind.FILE_SHARED and s.actor_known:
            active = list(await self.loops.active(user.id))
            loop_id = match_loop(s.object_title, active)
            loop_title = next((safe_title(lp.title) for lp in active if lp.id == loop_id), "")
        muted = f"{s.kind.value}:{s.actor.lower()}" in (st.get("muted") or [])
        asked = s.object_id in (st.get("asked") or [])
        d = decide(s, loop_id=loop_id, muted=muted, asked=asked)
        if quiet and d.verdict in (Verdict.NOTIFY, Verdict.ASK):
            if backlog and d.verdict is Verdict.ASK:
                await self._append(user.id, "asked", s.object_id)
            d = WorkspaceDecision(Verdict.BRIEF, 0, d.reason, d.security)
        facts = {**s.model_dump(mode="json"), "reason": d.reason, "security": d.security,
                 "loop_title": loop_title if d.close_loop is not None else ""}
        obs, created = await repo.insert_signal(
            user.id, s.message_id, source=s.source, kind=s.kind.value, verdict=d.verdict.value,
            urgency=d.urgency, summary=s.object_title, facts=facts, received_at=self.clock(),
        )
        if not created:
            return DUPLICATE
        if d.close_loop is not None:
            await self.loops.close(d.close_loop, LoopStatus.DONE)
        if d.verdict in (Verdict.NOTIFY, Verdict.ASK):
            await self._speak(user, obs, s, d.verdict, d.urgency, facts["loop_title"])
        return NEW

    def _ping_key(self, obs: Any, s: Signal, verdict: Verdict) -> str:
        # one keep-or-drop question per task per day, whichever day's row carries it
        key = f"ws:ask:{s.object_id}" if verdict is Verdict.ASK else f"ws:{obs.message_id}"
        return key[:150]

    async def _speak(
        self, user: Any, obs: Any, s: Signal, verdict: Verdict, urgency: int, loop_title: str
    ) -> None:
        """Amendment A2: the ping policy decides first (quiet hours, daily budget, dedupe); a deferral
        becomes a one-off wakeup that speaks this row later, so the buttons survive."""
        if verdict is Verdict.ASK and s.object_id in ((await self.state(user.id)).get("asked") or []):
            await repo.set_fields(obs.id, delivery=NONE)
            return
        key = self._ping_key(obs, s, verdict)
        allowed = await self.policy.check(user, max(1, urgency), key, self.clock())
        if not allowed.allow:
            if allowed.defer_until is not None:
                await repo.set_fields(obs.id, delivery=DEFERRED)
                await self.schedule(user.id, allowed.defer_until, f"{SPEAK}{obs.id}", WORKSPACE_POLL_KIND)
            else:
                await repo.set_fields(obs.id, delivery=NONE if allowed.reason == "duplicate" else DROPPED)
            log.info("workspace.held", obs_id=obs.id, reason=allowed.reason)
            return
        if verdict is Verdict.ASK:
            await self._ask(user, obs, s, key)
        elif s.kind is SignalKind.COMMENT:
            await self._notify_comment(user, obs, s, key, urgency)
        else:
            await self._notify_share(user, obs, s, key, urgency, loop_title)

    def _mute_button(self, obs: Any, s: Signal) -> list[list[Button]] | None:
        """None without an actor: an empty mute key ("comment:") would silence every nameless comment."""
        if not s.actor.strip():
            return None
        return [[Button(label="Not useful", data=f"{MUTE}{obs.id}")]]

    async def _notify_share(
        self, user: Any, obs: Any, s: Signal, key: str, urgency: int, loop_title: str
    ) -> None:
        """Fixed text, no LLM (amendment A8). The title is scrubbed third-party text: tainted."""
        loop = f' ("{loop_title}")' if loop_title else ""
        text = (f'{s.actor or "Someone"} shared "{s.object_title}". It looks like what you were waiting '
                f"for{loop}, so I marked that done.")
        await self.executor_of().deliver(user, [text], key, urgency, buttons=self._mute_button(obs, s),
                                         tainted=True)
        await repo.set_fields(obs.id, delivery=SENT)

    def _comment_text(self, s: Signal) -> str:
        whose = "your doc" if s.owned_by_me else "a doc you follow"
        who = s.actor or "Someone"
        return f'{who} commented on {whose} "{s.object_title}". Open it in Google Docs to reply.'

    async def _notify_comment(self, user: Any, obs: Any, s: Signal, key: str, urgency: int) -> None:
        """One composed line (the executor's composer runs at background priority, so chat is never
        starved); the fixed line when the LLM is busy."""
        who = wrap_untrusted(s.actor or "someone", "actor")
        title = wrap_untrusted(s.object_title, "file_title")
        whose = "their doc" if s.owned_by_me else "a doc they follow"
        intent = (f"In one short line, tell the user {who} commented on {whose} {title}. "
                  f"Say what the comment asks:\n{wrap_untrusted(s.preview, 'comment')}")
        notice = NotifyIntent(urgency=max(1, urgency), intent=intent, dedupe_key=key)
        executor = self.executor_of()
        try:
            sent = await executor.notify(user, notice, untrusted=True, buttons=self._mute_button(obs, s))
        except Exception as exc:  # noqa: BLE001 - LLM busy or any composer failure: the row is still spoken
            log.warning("workspace.notify_fallback_text", obs_id=obs.id, error=type(exc).__name__)
            await executor.deliver(user, [self._comment_text(s)], key, notice.urgency,
                                   buttons=self._mute_button(obs, s), tainted=True)
            sent = True
        await repo.set_fields(obs.id, delivery=SENT if sent else NONE)

    async def _ask(self, user: Any, obs: Any, s: Signal, key: str) -> None:
        days = (self._today(user.timezone) - s.due).days if s.due is not None else s.overdue_days
        text = f'"{s.object_title}" is {days} days past its due date. Keep it or drop it?'
        await self.executor_of().deliver(
            user, [text], key, 3,
            buttons=[[Button(label="Keep it", data=f"{KEEP}{obs.id}"),
                      Button(label="Drop it", data=f"{DROP}{obs.id}")]],
            tainted=True,  # the title is the user's task, but it may have come from a shared doc
        )
        await self._append(user.id, "asked", s.object_id)
        await repo.set_fields(obs.id, delivery=SENT)

    async def _speak_deferred(self, user_id: int, raw_id: str) -> None:
        try:
            obs = await repo.get(int(raw_id))
        except ValueError:
            return
        if obs is None or obs.user_id != user_id or obs.source not in WORKSPACE_SOURCES:
            return
        if obs.delivery != DEFERRED:
            return  # spoken already (a second firing) or held for good
        try:
            verdict = Verdict(obs.verdict)
            facts = obs.facts or {}
            s = Signal.model_validate({k: v for k, v in facts.items() if k in Signal.model_fields})
        except ValueError:
            return
        if verdict is Verdict.ASK and not await self._task_still_open(user_id, s.object_id):
            await repo.set_fields(obs.id, delivery=NONE)
            return
        user = await users.get(user_id)
        await self._speak(user, obs, s, verdict, obs.urgency, str((obs.facts or {}).get("loop_title") or ""))

    async def _task_still_open(self, user_id: int, task_id: str) -> bool:
        """A deferred keep-or-drop question is dropped if the task was finished or removed meanwhile."""
        data = await self._execute(user_id, "tasks.get", {"task_id": task_id})
        if data is None:
            return False
        status = pick(data, "status", "response_data.status", "data.status", default="")
        deleted = pick(data, "deleted", "response_data.deleted", "data.deleted", default=False)
        return status != "completed" and not deleted

    async def on_button(self, event: Event, data: str) -> None:
        try:
            obs_id = int(data.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            return
        # the lock attention's FeedbackHandler uses: a double tap reads the first tap's result
        async with lock(f"attention-feedback:{event.user_id}"):
            obs = await repo.get(obs_id)
            if obs is None or obs.user_id != event.user_id or obs.source not in WORKSPACE_SOURCES:
                return
            text = await self._feedback(obs, data)
        if text:
            await reply(event, [text])

    async def _feedback(self, obs: Any, data: str) -> str | None:
        facts = obs.facts or {}
        if data.startswith(MUTE):
            actor = str(facts.get("actor") or "").strip().lower()
            if not actor:
                return None  # never a catch-all "kind:" key
            await self._append(obs.user_id, "muted", f"{obs.kind}:{actor}")
            await repo.set_fields(obs.id, feedback="mute")
            return "Got it. Those go to your brief from now on."
        if obs.source != SOURCE_OF[SignalKind.TASK_OVERDUE]:
            return None  # Keep and Drop only exist on task questions
        if data.startswith(KEEP):
            await repo.set_fields(obs.id, feedback="keep")
            return "Okay, keeping it on your list."
        if not data.startswith(DROP) or obs.feedback == "drop":
            return None  # unknown, or a second tap on Drop
        task_id = str(facts.get("object_id") or "")
        try:
            res = await self.provider.execute(UserRef(user_id=obs.user_id), "tasks.delete",
                                              {"task_id": task_id})
            ok = res.ok
        except Exception as exc:  # noqa: BLE001 - audited as an error, the user is told
            log.warning("workspace.drop_failed", error=type(exc).__name__)
            ok = False
        await audit.record(obs.user_id, actor="user_button", action="tasks_delete",
                           detail={"task_id": task_id, "outcome": "ok" if ok else "error"})
        if not ok:
            return "I couldn't drop it just now. You can remove it in Google Tasks."
        await repo.set_fields(obs.id, feedback="drop")
        return "Dropped it from your list."

    # --- first sync (spec 5.4), registered through first_sync.register_first_sync_handler ---------------

    async def capture_email(self, user_id: int) -> str:
        """The user's Google address via workspace_guard.my_email (amendment A4); "" when unknown."""
        return await self.my_address(user_id)

    async def first_sync_tasks(self, user_id: int) -> list[str]:
        """Due today and overdue tasks become brief rows (never pings); the rest of the week is counted for
        the first brief. Then the baseline marker lets polls speak (amendment A3)."""
        await self.poll_tasks(user_id, quiet=True)
        user = await users.get(user_id)
        local = timeutil.to_local(self.clock(), user.timezone)
        week = (local + timedelta(days=7)).replace(hour=23, minute=59, second=59, microsecond=0)
        query = {"due_before": week.isoformat(), "max_results": 100}
        data = await self._execute(user_id, "tasks.list", query)
        today = local.date().isoformat()
        upcoming = sum(
            1 for t in extract_list(data, "tasks", "data.tasks")
            if t.get("status") != "completed" and str(t.get("due") or "")[:10] > today
        )
        await self.patch(user_id, upcoming=upcoming)
        await self.mark_baselined(user_id)
        return []

    async def first_sync_drive(self, user_id: int) -> list[str]:
        """Files shared in the last 7 days are logged silently as a baseline; the share cursor starts now."""
        await self.capture_email(user_id)
        now = self.clock()
        data = await self._execute(user_id, "drive.list_recent", {"shared_with_me": True, "max_results": 25})
        for f in extract_list(data, "files", "data.files"):
            when = to_datetime(f.get("sharedWithMeTime"))
            signal = shared_file_signal(f)
            if when is None or signal is None or when < now - timedelta(days=7):
                continue
            await repo.insert_signal(
                user_id, signal.message_id, source=signal.source, kind=signal.kind.value, verdict="log",
                urgency=0, summary=signal.object_title,
                facts={**signal.model_dump(mode="json"), "reason": "baseline"}, received_at=when,
            )
        await self._advance_cursor(user_id, now)
        return []

    async def first_sync_contacts(self, user_id: int) -> list[str]:
        """Contacts' email addresses seed actor_known (kept in users.state, at most MAX_CONTACTS)."""
        data = await self._execute(user_id, "contacts.list", {})
        people = extract_list(data, "response_data.connections", "connections",
                              "data.response_data.connections")
        emails = sorted({
            str(e["value"]).strip().lower()
            for p in people for e in p.get("emailAddresses") or [] if isinstance(e, dict) and e.get("value")
        })
        if emails:
            await self.patch(user_id, contacts=emails[:MAX_CONTACTS])
        return []

    # --- poll chains ------------------------------------------------------------------------------------

    async def on_wakeup(self, user_id: int, reason: str) -> None:
        if reason.startswith(SPEAK):
            await self._speak_deferred(user_id, reason.removeprefix(SPEAK))
            return
        capability = POLL_REASONS.get(reason)
        if capability is None:
            return
        if not ((await users.get_state(user_id)).get("synced") or {}).get(capability.value):
            return  # Google disconnected: the chain ends; activation re-arms it
        try:
            if reason == "tasks":
                await self.poll_tasks(user_id)
            else:
                await self.poll_shared(user_id)
        except Exception as exc:  # noqa: BLE001 - one failed poll must not end the chain
            log.warning("workspace.poll_failed", reason=reason, user_id=user_id, error=type(exc).__name__)
        finally:
            at = self.clock() + timedelta(minutes=get_settings().workspace_poll_minutes)
            await self.schedule(user_id, at, reason, WORKSPACE_POLL_KIND)

    async def ensure_chains(self, user_id: int, *, later: bool = False) -> int:
        """Arm the Tasks and Drive poll chains of a synced user (a pending chain absorbs this). `later`:
        the first poll one interval out (a fresh activation, amendment A3); else now (heal, morning)."""
        synced = (await users.get_state(user_id)).get("synced") or {}
        interval = timedelta(minutes=get_settings().workspace_poll_minutes)
        at = self.clock() + (interval if later else timedelta())
        armed = 0
        for reason, capability in POLL_REASONS.items():
            if synced.get(capability.value):
                await self.schedule(user_id, at, reason, WORKSPACE_POLL_KIND)
                armed += 1
        return armed

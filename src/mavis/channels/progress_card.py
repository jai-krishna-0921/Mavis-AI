"""One live, edited status card per user task (Phase 12, spec 8.2).

Updates are in-process calls from the task runner (no bus events). Each update changes the card state;
an edit goes out at most once per PROGRESS_EDIT_MIN_INTERVAL_S per card, the newest render wins, and an
unchanged render is skipped. Card edits bypass the outbox: they are idempotent last-write-wins UI.
State is persisted in task_cards so a resumed task (after an approval, on another worker) continues the
same card. Only the worker running a task touches its card (the task claim guarantees one runner).
An update that arrives inside the interval schedules one deferred flush, so the newest state goes out
when the interval ends even if nothing else happens (a long tool call never strands it).
Nothing here ever sleeps while holding the per-task lock: a flood wait, the pacer or the interval only
schedule a later flush, so the task runner (which awaits these hooks) is never stalled by a card.
A failed FINAL edit is retried, then replaced by a new final message, so the Cancel button always goes."""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from mavis.channels import get_channel
from mavis.channels.base import Channel, ChannelRateLimited, MessageGone
from mavis.channels.pacing import get_pacer
from mavis.config import get_settings
from mavis.domain.plans import PlanStep
from mavis.domain.progress import CardFinal, CardState, StepState, card_from_plan, render_card
from mavis.domain.tasks import TaskStatus
from mavis.store.repo import task_cards, tasks, users

log = structlog.get_logger(__name__)
_EARLY_MAX_TASKS = 500  # tasks whose pre-card updates are kept (each is dropped at start or finalize)
_EARLY_MAX_CHANGES = 100
_FINAL_MAX_FAILURES = 3  # failed (not rate-limited) final edits before a fresh final message is sent
_FINALIZED_MAX = 2000  # task ids remembered as finished, so late updates and a late start are dropped
_TERMINAL = (TaskStatus.DONE, TaskStatus.PARTIAL, TaskStatus.FAILED, TaskStatus.CANCELLED)


def _cosmetic(method: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    """A card problem (database, channel, bad state) is logged and never fails the task that fed it."""

    @functools.wraps(method)
    async def wrapper(self: Any, task_id: int, *args: Any, **kwargs: Any) -> None:
        try:
            await method(self, task_id, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("progress_card.failed", op=method.__name__, task_id=task_id, error=type(exc).__name__)

    return wrapper


class _Live:
    __slots__ = ("chat_id", "dirty", "failures", "last_edit", "last_text", "late", "message_id",
                 "retry_until", "state", "user_id")

    def __init__(self, state: CardState, user_id: int, chat_id: int, message_id: int | None,
                 last_text: str, last_edit: float) -> None:
        self.state, self.user_id, self.chat_id, self.message_id = state, user_id, chat_id, message_id
        self.last_text, self.last_edit, self.dirty = last_text, last_edit, False
        self.retry_until = 0.0  # no edit before this clock time (flood wait or pacer)
        self.failures = 0  # failed final edits
        self.late = False  # a finished card reopened only to update its footer


class ProgressCards:
    def __init__(self, channel: Channel | None = None, *, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._channel, self._clock, self._wall, self._sleep = channel, clock, wall, sleep
        self._live: dict[int, _Live] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._timers: dict[int, asyncio.Task[None]] = {}  # pending deferred flush per task
        self._finalized: dict[int, None] = {}  # insertion-ordered, bounded
        # Updates that arrive before the card exists (the card waits PROGRESS_CARD_AFTER_S): replayed at
        # start, so the first render already shows a running or finished step truthfully.
        self._early: dict[int, list[Callable[[CardState], None]]] = {}

    @property
    def channel(self) -> Channel:
        return self._channel or get_channel()

    def _lock(self, task_id: int) -> asyncio.Lock:
        return self._locks.setdefault(task_id, asyncio.Lock())

    def has_card(self, task_id: int) -> bool:
        return task_id in self._live

    def _mark_finalized(self, task_id: int) -> None:
        self._finalized[task_id] = None
        while len(self._finalized) > _FINALIZED_MAX:
            self._finalized.pop(next(iter(self._finalized)))

    async def _load(self, task_id: int, *, final_ok: bool = False) -> _Live | None:
        live = self._live.get(task_id)
        if live is not None:
            return live
        row = await task_cards.get(task_id)
        if row is None:
            return None
        if row.final:
            self._mark_finalized(task_id)
            if not final_ok:
                return None
        state = CardState.model_validate(row.state)
        live = _Live(state, row.user_id, row.chat_id, row.message_id, render_card(state, self._wall())[0],
                     self._clock())
        live.late = bool(row.final)
        self._live[task_id] = live
        return live

    @_cosmetic
    async def start(self, task_id: int, user_id: int, goal: str, steps: list[PlanStep], *,
                    tainted: bool) -> None:
        async with self._lock(task_id):
            if task_id in self._finalized or await self._load(task_id) is not None:
                return
            if task_id in self._finalized:  # _load found a finished card
                return
            task = await tasks.get(task_id)
            if task is not None and task.status in [s.value for s in _TERMINAL]:
                return  # the task ended while the delayed start was waiting: no card for a finished task
            user = await users.get(user_id)
            if user.telegram_chat_id is None:
                return
            state = card_from_plan(task_id, goal, steps, tainted=tainted, now=self._wall())
            for change in self._early.pop(task_id, []):
                change(state)
            text, buttons = render_card(state, self._wall())
            ids = await self.channel.send_text(user.telegram_chat_id, text, buttons)
            live = _Live(state, user_id, user.telegram_chat_id, ids[-1] if ids else None, text, self._clock())
            self._live[task_id] = live
            await task_cards.save(task_id, user_id, live.chat_id, live.message_id, state.model_dump(), False)

    @_cosmetic
    async def _update(self, task_id: int, change: Callable[[CardState], None]) -> None:
        if task_id in self._finalized:
            return  # a late update for a finished card changes nothing
        async with self._lock(task_id):
            live = await self._load(task_id)
            if live is None:
                if task_id not in self._finalized:
                    self._remember_early(task_id, change)
                return
            if live.state.final is not None:
                return
            change(live.state)
            live.dirty = True
            await self._maybe_edit(task_id, live)

    async def step_started(self, task_id: int, step_id: str) -> None:
        now = self._wall()

        def change(s: CardState) -> None:
            for st in s.steps:
                if st.id == step_id:
                    st.state, st.started_at = StepState.RUNNING, now
        await self._update(task_id, change)

    async def step_finished(self, task_id: int, step_id: str, state: StepState) -> None:
        now = self._wall()

        def change(s: CardState) -> None:
            for st in s.steps:
                if st.id == step_id:
                    st.state, st.finished_at = state, now
        await self._update(task_id, change)

    async def tool_called(self, task_id: int, label: str) -> None:
        await self._update(task_id, lambda s: setattr(s, "last", label[:80]))

    @_cosmetic
    async def file_sent(self, task_id: int, n: int = 1) -> None:
        """Files go out after the task ends too, so the footer count is also updated on a finished card."""
        async with self._lock(task_id):
            live = await self._load(task_id, final_ok=True)
            if live is None:
                if task_id not in self._finalized:
                    self._remember_early(task_id, lambda s: setattr(s, "files_sent", s.files_sent + n))
                return
            live.state.files_sent += n
            live.dirty = True
            await self._maybe_edit(task_id, live)

    async def set_live_url(self, task_id: int, url: str | None) -> None:
        await self._update(task_id, lambda s: setattr(s, "live_url", url))

    @_cosmetic
    async def flush(self, task_id: int) -> None:
        async with self._lock(task_id):
            live = await self._load(task_id)
            if live is not None and live.dirty:
                await self._maybe_edit(task_id, live)

    @_cosmetic
    async def finalize(self, task_id: int, final: CardFinal) -> None:
        async with self._lock(task_id):
            self._early.pop(task_id, None)
            live = await self._load(task_id)
            self._mark_finalized(task_id)
            if live is None or live.state.final is not None:
                return
            timer = self._timers.pop(task_id, None)
            if timer is not None and timer is not asyncio.current_task():
                timer.cancel()
            live.state.final, live.state.finished_at = final, self._wall()
            live.dirty = True
            await self._persist(task_id, live, final=True)  # the truth is stored before the edit is tried
            await self._maybe_edit(task_id, live)

    async def _persist(self, task_id: int, live: _Live, *, final: bool) -> None:
        await task_cards.save(task_id, live.user_id, live.chat_id, live.message_id,
                              live.state.model_dump(), final)

    async def _maybe_edit(self, task_id: int, live: _Live) -> None:
        """Send the newest render if allowed now, else schedule a flush for when it is. Never sleeps."""
        final = live.state.final is not None
        interval = get_settings().progress_edit_min_interval_s
        now = self._clock()
        wait = live.retry_until - now
        if not final:  # a final edit is never held back by the interval
            wait = max(wait, live.last_edit + interval - now)
        if wait > 0:
            self._defer(task_id, wait)  # the newest render goes out when the wait ends
            return
        text, buttons = render_card(live.state, self._wall())
        if text == live.last_text and not final:
            live.dirty = False
            return
        pace = await get_pacer().reserve(live.chat_id, kind="card")
        if pace > 0:
            live.retry_until = now + pace
            self._defer(task_id, pace)
            return
        try:
            await self._send(live, text, buttons)
        except ChannelRateLimited as exc:
            live.retry_until = self._clock() + exc.retry_after
            self._defer(task_id, exc.retry_after)  # retry later, outside the lock; the newest state wins
            return
        except Exception as exc:  # noqa: BLE001 - a card problem must never fail the task
            log.warning("progress_card.edit_failed", task_id=task_id, error=type(exc).__name__)
            await self._edit_failed(task_id, live, final, text)
            return
        # only a successful edit counts as sent: a failed one is retried by the next render
        live.last_text, live.last_edit, live.dirty = text, self._clock(), False
        await self._persist(task_id, live, final=final)
        if final:
            self._live.pop(task_id, None)

    async def _edit_failed(self, task_id: int, live: _Live, final: bool, text: str) -> None:
        live.last_edit = self._clock()  # do not hammer a failing edit
        if not final:
            live.dirty = False
            await self._persist(task_id, live, final=False)
            return
        live.failures += 1
        if live.late:  # only the footer count was at stake
            if live.failures >= _FINAL_MAX_FAILURES:
                self._live.pop(task_id, None)
            else:
                self._defer(task_id, max(1.0, get_settings().progress_edit_min_interval_s))
            return
        if live.failures < _FINAL_MAX_FAILURES:
            self._defer(task_id, max(1.0, get_settings().progress_edit_min_interval_s) * live.failures)
            return
        # the edit keeps failing: say the outcome in a new message so the user is not left with a live Cancel
        try:
            await self.channel.send_text(live.chat_id, text, None)
        except Exception as exc:  # noqa: BLE001
            log.warning("progress_card.final_resend_failed", task_id=task_id, error=type(exc).__name__)
        self._live.pop(task_id, None)

    def _remember_early(self, task_id: int, change: Callable[[CardState], None]) -> None:
        if task_id not in self._early and len(self._early) >= _EARLY_MAX_TASKS:
            self._early.pop(next(iter(self._early)))
        changes = self._early.setdefault(task_id, [])
        changes.append(change)
        del changes[:-_EARLY_MAX_CHANGES]

    def _defer(self, task_id: int, wait: float) -> None:
        if task_id in self._timers:
            return
        self._timers[task_id] = asyncio.get_running_loop().create_task(self._deferred(task_id, wait))

    async def _deferred(self, task_id: int, wait: float) -> None:
        try:
            await self._sleep(wait)
            self._timers.pop(task_id, None)
            await self.flush(task_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - cosmetic
            log.warning("progress_card.deferred_flush_failed", task_id=task_id, error=type(exc).__name__)

    async def _send(self, live: _Live, text: str, buttons) -> None:
        if live.message_id is None:
            ids = await self.channel.send_text(live.chat_id, text, buttons or None)
            live.message_id = ids[-1] if ids else None
            return
        try:
            await self.channel.edit_text(live.chat_id, live.message_id, text, buttons or None)
        except MessageGone:
            ids = await self.channel.send_text(live.chat_id, text, buttons or None)
            live.message_id = ids[-1] if ids else None


_cards: ProgressCards | None = None


def get_cards() -> ProgressCards:
    global _cards
    if _cards is None:
        _cards = ProgressCards()
    return _cards


def set_cards(c: ProgressCards | None) -> None:
    global _cards
    _cards = c


class _NoCards:
    """Hooks when PROGRESS_CARD_ENABLED is off: every call is a no-op (today's behaviour)."""

    def has_card(self, task_id: int) -> bool:
        return False

    async def _noop(self, *args: object, **kwargs: object) -> None:
        return None

    start = step_started = step_finished = tool_called = file_sent = set_live_url = finalize = flush = _noop


def hook_cards() -> ProgressCards | _NoCards:
    """The cards the task runner feeds: the real service when cards are on, a no-op otherwise."""
    return get_cards() if get_settings().progress_card_enabled else _NoCards()

"""Coalescing edits, Telegram edge cases, persistence across a resume, idempotent finalize."""

from __future__ import annotations

import pytest

from mavis.channels.base import ChannelRateLimited
from mavis.channels.fake import FakeChannel
from mavis.channels.progress_card import ProgressCards
from mavis.domain.plans import PlanStep
from mavis.domain.progress import CardFinal, StepState
from mavis.store.repo import task_cards, tasks, users


async def _settle(cards, tid):
    """Let the deferred flushes (interval, flood wait, pacer, final retry) run; the fake sleep is instant."""
    import asyncio

    for _ in range(50):
        timer = cards._timers.get(tid)
        if timer is None:
            await asyncio.sleep(0)
            if cards._timers.get(tid) is None:
                return
            continue
        await asyncio.wait([timer])


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


STEPS = [PlanStep(id="s1", agent="research", instruction="a", title="Look up train times"),
         PlanStep(id="s2", agent="research", instruction="b", title="Pick the fastest", depends_on=["s1"])]


@pytest.fixture
async def setup(db):
    u, _ = await users.get_or_create_by_chat(90_210, "Ira")
    tid = await tasks.create(u.id, goal="find the fastest train to Pune tomorrow")
    clock, ch = Clock(), FakeChannel()
    cards = ProgressCards(ch, clock=clock, wall=clock, sleep=clock.sleep)
    return cards, ch, clock, u, tid


async def test_start_sends_one_card_and_persists(setup):
    cards, ch, _clock, u, tid = setup
    await cards.start(tid, u.id, "find the fastest train to Pune tomorrow", STEPS, tainted=False)
    await cards.start(tid, u.id, "again", STEPS, tainted=False)  # idempotent
    assert len(ch.texts) == 1 and ch.texts[0].startswith("Working on: find the fastest train")
    row = await task_cards.get(tid)
    assert row.chat_id == 90_210 and row.message_id is not None and row.final is False


async def test_ten_updates_coalesce_into_one_edit(setup, settings):
    cards, ch, clock, u, tid = setup
    await cards.start(tid, u.id, "g", STEPS, tainted=False)
    for i in range(10):
        await cards.tool_called(tid, f"opened site{i}.example")
    clock.t += settings.progress_edit_min_interval_s
    await cards.flush(tid)
    assert len(ch.edits) == 1 and "site9.example" in ch.edits[0][2]


async def test_unchanged_render_is_not_edited(setup, settings):
    cards, ch, clock, u, tid = setup
    await cards.start(tid, u.id, "g", STEPS, tainted=False)
    clock.t += settings.progress_edit_min_interval_s
    await cards.flush(tid)
    assert ch.edits == []


async def test_deleted_card_is_resent_once(setup, settings):
    cards, ch, clock, u, tid = setup
    await cards.start(tid, u.id, "g", STEPS, tainted=False)
    first = (await task_cards.get(tid)).message_id
    ch.gone.add(first)
    await cards.step_started(tid, "s1")
    clock.t += settings.progress_edit_min_interval_s
    await cards.flush(tid)
    second = (await task_cards.get(tid)).message_id
    assert second != first and len(ch.texts) == 2
    await cards.step_finished(tid, "s1", StepState.DONE)
    clock.t += settings.progress_edit_min_interval_s
    await cards.flush(tid)
    assert len(ch.texts) == 2 and ch.edits[-1][1] == second


async def test_retry_after_sends_newest_state(setup, settings):
    cards, ch, clock, u, tid = setup
    await cards.start(tid, u.id, "g", STEPS, tainted=False)
    ch.fail_next.append(ChannelRateLimited(2.0))
    await cards.tool_called(tid, "ran Python (exit 1), fixing")
    clock.t += settings.progress_edit_min_interval_s
    await cards.flush(tid)
    assert ch.edits == [] and cards._live[tid].retry_until >= clock.t + 2.0 - 1e-9  # waiting, not sleeping
    assert cards._live[tid].dirty and cards.has_card(tid)
    await cards.tool_called(tid, "made chart.png")
    await _settle(cards, tid)
    assert "made chart.png" in ch.edits[-1][2] and len(ch.edits) == 1  # one edit, the newest state


@pytest.mark.parametrize("final,word", [(CardFinal.DONE, "Done"), (CardFinal.CANCELLED, "Cancelled"),
                                        (CardFinal.FAILED, "Couldn't finish")])
async def test_finalize_is_forced_idempotent_and_removes_buttons(setup, final, word):
    cards, ch, _clock, u, tid = setup
    await cards.start(tid, u.id, "g", STEPS, tainted=False)
    await cards.finalize(tid, final)
    await cards.finalize(tid, CardFinal.DONE)  # second call changes nothing
    assert len(ch.edits) == 1 and ch.edits[0][2].startswith(f"{word}: ") and ch.edits[0][3] == []
    assert (await task_cards.get(tid)).final is True


async def test_resume_on_a_fresh_service_continues_the_same_card(setup, settings):
    cards, ch, clock, u, tid = setup
    await cards.start(tid, u.id, "g", STEPS, tainted=False)
    await cards.step_started(tid, "s1")
    clock.t += settings.progress_edit_min_interval_s
    await cards.flush(tid)
    other = ProgressCards(ch, clock=clock, wall=clock, sleep=clock.sleep)  # another worker after approval
    await other.step_finished(tid, "s1", StepState.DONE)
    clock.t += settings.progress_edit_min_interval_s
    await other.flush(tid)
    assert len(ch.texts) == 1 and ch.edits[-1][2].count("✅") == 1


async def test_updates_for_a_task_without_a_card_are_ignored(setup):
    cards, ch, _clock, _u, tid = setup
    await cards.tool_called(tid, "x")
    await cards.finalize(tid, CardFinal.DONE)
    assert ch.sent == [] and ch.edits == []


async def test_paced_edit_waits_for_the_global_bucket(setup, settings):
    from mavis.channels import pacing

    calls = []

    class Slow(pacing.SendPacer):
        async def reserve(self, chat_id=None, *, kind="chat"):
            calls.append(kind)
            return 0.25 if len(calls) == 1 else 0.0

    pacing.set_pacer(Slow(rate=1))
    cards, ch, clock, u, tid = setup
    await cards.start(tid, u.id, "g", STEPS, tainted=False)
    await cards.tool_called(tid, "opened a.example")
    clock.t += settings.progress_edit_min_interval_s
    await cards.flush(tid)
    await _settle(cards, tid)
    assert len(calls) == 2 and len(ch.edits) == 1  # the busy bucket delayed the edit, then it went out


@pytest.mark.parametrize("label", ["opened maps.example", "ran Python (exit 0)", "made totals.xlsx"])
async def test_a_skipped_update_is_sent_later_without_another_update(setup, label):
    """Coalescing never strands the newest state: a deferred flush sends it once the interval passes."""
    import asyncio

    cards, ch, _clock, u, tid = setup
    await cards.start(tid, u.id, "g", STEPS, tainted=False)
    await cards.tool_called(tid, label)
    assert ch.edits == []
    for _ in range(20):
        await asyncio.sleep(0)
        if ch.edits:
            break
    assert len(ch.edits) == 1 and label in ch.edits[0][2]


async def test_start_failure_is_swallowed(setup):
    cards, ch, _clock, u, tid = setup
    ch.fail_next.append(RuntimeError("network down"))
    await cards.start(tid, u.id, "g", STEPS, tainted=False)
    assert not cards.has_card(tid) and await task_cards.get(tid) is None


@pytest.mark.parametrize("finished", [[], ["s1"]])
async def test_updates_before_the_card_show_in_its_first_render(setup, finished):
    cards, ch, _clock, u, tid = setup
    await cards.step_started(tid, "s1")
    for step_id in finished:
        await cards.step_finished(tid, step_id, StepState.DONE)
        await cards.step_started(tid, "s2")
    await cards.tool_called(tid, "searched the web")
    await cards.start(tid, u.id, "g", STEPS, tainted=False)
    first = ch.texts[0]
    if finished:
        assert "✅ 1. Look up train times" in first and "⏳ 2. Pick the fastest" in first
    else:
        assert "⏳ 1. Look up train times" in first and "▫️ 2. Pick the fastest" in first
    assert "Last: searched the web" in first


async def test_finalize_without_a_card_drops_early_updates(setup):
    cards, ch, _clock, u, tid = setup
    await cards.step_started(tid, "s1")
    await cards.finalize(tid, CardFinal.DONE)
    await cards.start(tid, u.id, "g", STEPS, tainted=False)
    assert ch.texts == []
    assert cards._early == {}


@pytest.mark.parametrize("op", ["step_started", "tool_called", "finalize"])
async def test_a_store_error_never_reaches_the_caller(setup, monkeypatch, op):
    cards, _ch, _clock, u, tid = setup
    await cards.start(tid, u.id, "g", STEPS, tainted=False)
    cards._live.clear()  # force a reload from the store

    async def broken(_task_id):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(task_cards, "get", broken)
    arg = {"step_started": "s1", "tool_called": "opened a.example", "finalize": CardFinal.DONE}[op]
    await getattr(cards, op)(tid, arg)


async def test_flood_wait_never_holds_the_task_lock(setup, settings):
    """A hook returns at once during a flood wait; the retry is scheduled, never slept under the lock."""
    import asyncio

    cards, ch, clock, u, tid = setup
    gate = asyncio.Event()

    async def blocking_sleep(_s):
        await gate.wait()

    cards._sleep = blocking_sleep
    await cards.start(tid, u.id, "g", STEPS, tainted=False)
    clock.t += settings.progress_edit_min_interval_s
    ch.fail_next.append(ChannelRateLimited(30.0))
    await asyncio.wait_for(cards.tool_called(tid, "a"), 1)
    assert not cards._lock(tid).locked()
    await asyncio.wait_for(cards.step_started(tid, "s1"), 1)
    await asyncio.wait_for(cards.finalize(tid, CardFinal.DONE), 1)
    gate.set()
    clock.t += 30
    await _settle(cards, tid)
    assert ch.edits[-1][2].startswith("Done: ") and ch.edits[-1][3] == []
    assert not cards.has_card(tid)


async def test_failed_final_edit_is_retried_then_succeeds(setup):
    cards, ch, clock, u, tid = setup
    await cards.start(tid, u.id, "g", STEPS, tainted=False)
    ch.fail_next.extend([RuntimeError("boom"), RuntimeError("boom")])
    await cards.finalize(tid, CardFinal.DONE)
    await _settle(cards, tid)
    assert len(ch.edits) == 1 and ch.edits[0][2].startswith("Done: ") and ch.edits[0][3] == []
    assert len(ch.texts) == 1  # no fallback message was needed


async def test_final_edit_that_keeps_failing_falls_back_to_a_final_message(setup):
    cards, ch, clock, u, tid = setup
    await cards.start(tid, u.id, "g", STEPS, tainted=False)
    ch.fail_next.extend([RuntimeError("boom")] * 3)
    await cards.finalize(tid, CardFinal.CANCELLED)
    await _settle(cards, tid)
    assert ch.edits == [] and len(ch.texts) == 2
    assert ch.texts[-1].startswith("Cancelled: ") and not cards.has_card(tid)
    assert ch.sent[-1].buttons in (None, [], ()) if hasattr(ch.sent[-1], "buttons") else True


async def test_failed_edit_is_not_recorded_as_sent(setup, settings):
    """The next identical render is retried, not skipped as 'unchanged'."""
    cards, ch, clock, u, tid = setup
    await cards.start(tid, u.id, "g", STEPS, tainted=False)
    ch.fail_next.append(RuntimeError("boom"))
    await cards.tool_called(tid, "opened a.example")
    clock.t += settings.progress_edit_min_interval_s
    await cards.flush(tid)
    assert ch.edits == []
    live = cards._live[tid]
    assert live.last_text != "" and "a.example" not in live.last_text
    await cards.tool_called(tid, "opened a.example")  # same state again
    await _settle(cards, tid)
    assert len(ch.edits) == 1 and "a.example" in ch.edits[0][2]


@pytest.mark.parametrize("how", ["finalized_here", "finalized_elsewhere", "status_done"])
async def test_start_makes_no_card_for_a_task_that_is_already_final(setup, how):
    from mavis.domain.tasks import TaskStatus

    cards, ch, _clock, u, tid = setup
    if how == "finalized_here":
        await cards.start(tid, u.id, "g", STEPS, tainted=False)
        await cards.finalize(tid, CardFinal.DONE)
    elif how == "finalized_elsewhere":
        await cards.start(tid, u.id, "g", STEPS, tainted=False)
        await cards.finalize(tid, CardFinal.DONE)
        cards = ProgressCards(ch, clock=_clock, wall=_clock, sleep=_clock.sleep)  # fresh process
    else:
        await tasks.set_status(tid, TaskStatus.DONE)
    before = len(ch.texts)
    await cards.start(tid, u.id, "g", STEPS, tainted=False)
    assert len(ch.texts) == before and not cards.has_card(tid)


async def test_late_updates_after_finalize_are_dropped_not_buffered(setup):
    cards, ch, _clock, u, tid = setup
    await cards.start(tid, u.id, "g", STEPS, tainted=False)
    await cards.finalize(tid, CardFinal.DONE)
    await cards.step_finished(tid, "s1", StepState.DONE)
    await cards.tool_called(tid, "late")
    fresh = ProgressCards(ch, clock=_clock_of(cards), wall=_clock_of(cards), sleep=cards._sleep)
    await fresh.tool_called(tid, "late again")  # another process: found final in the store
    assert cards._early == {} and fresh._early == {} and len(ch.edits) == 1


def _clock_of(cards):
    return cards._clock


@pytest.mark.parametrize("n", [1, 3])
async def test_files_sent_after_the_card_is_final_update_the_footer(setup, n):
    cards, ch, _clock, u, tid = setup
    await cards.start(tid, u.id, "g", STEPS, tainted=False)
    await cards.finalize(tid, CardFinal.DONE)
    await cards.file_sent(tid, n)
    await _settle(cards, tid)
    last = ch.edits[-1]
    assert last[2].startswith("Done: ") and f"{n} file" in last[2] and last[3] == []
    assert (await task_cards.get(tid)).state["files_sent"] == n
    assert (await task_cards.get(tid)).final is True


async def test_files_sent_before_the_card_exists_show_in_its_first_render(setup):
    cards, ch, _clock, u, tid = setup
    await cards.file_sent(tid)
    await cards.start(tid, u.id, "g", STEPS, tainted=False)
    assert "1 file sent" in ch.texts[0]


async def test_default_interval_is_eight_seconds(settings):
    assert settings.progress_edit_min_interval_s >= 8.0


async def test_a_long_noisy_task_edits_at_most_every_interval_and_the_final_always_goes(setup, settings):
    """E2E run 2: 51 edits in 8 minutes. Updates every second now give one edit per interval."""
    cards, ch, clock, u, tid = setup
    stamps: list[float] = []
    real_edit = ch.edit_text

    async def edit(chat_id, message_id, text, buttons=None):
        stamps.append(clock.t)
        return await real_edit(chat_id, message_id, text, buttons)

    ch.edit_text = edit
    await cards.start(tid, u.id, "g", STEPS, tainted=False)
    interval = settings.progress_edit_min_interval_s
    began = clock.t
    for i in range(120):  # two minutes, an update every second
        clock.t += 1
        await cards.tool_called(tid, f"opened page {i}")
    await _settle(cards, tid)
    live_edits = len(stamps)
    assert live_edits <= (clock.t - began) / interval + 2  # the fake sleep also advances this clock
    assert all(b - a >= interval - 1e-9 for a, b in zip(stamps, stamps[1:], strict=False))
    clock.t += 1  # the final edit is never held back by the interval
    await cards.finalize(tid, CardFinal.DONE)
    assert len(stamps) == live_edits + 1
    assert "page 119" in ch.edits[-1][2]

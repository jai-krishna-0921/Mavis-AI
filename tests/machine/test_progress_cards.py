"""Coalescing edits, Telegram edge cases, persistence across a resume, idempotent finalize."""

from __future__ import annotations

import pytest

from mavis.channels.base import ChannelRateLimited
from mavis.channels.fake import FakeChannel
from mavis.channels.progress_card import ProgressCards
from mavis.domain.plans import PlanStep
from mavis.domain.progress import CardFinal, StepState
from mavis.store.repo import task_cards, tasks, users


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
    await cards.tool_called(tid, "made chart.png")
    clock.t += settings.progress_edit_min_interval_s
    await cards.flush(tid)
    assert 2.0 in clock.slept and "made chart.png" in ch.edits[-1][2]


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
    assert 0.25 in clock.slept and len(ch.edits) == 1


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

"""T1: recalled past moments are replayed text too, so each carries a stamp of when it was written,
relative to the recall clock in the user's zone."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from mavis.domain.events import Trust
from mavis.domain.memory import Extraction
from mavis.memory.recall import recall
from mavis.memory.spotter import SpotterCache

IST, NYC, LON, AKL = "Asia/Kolkata", "America/New_York", "Europe/London", "Pacific/Auckland"


def local(tz: str, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(2026, 10, d, h, mi, tzinfo=ZoneInfo(tz)).astimezone(UTC)


class _NoGraph:
    async def entities(self, user_id):
        return []

    async def neighborhood(self, *a, **k):
        return []


async def _recall(vector, tz: str, text: str):
    return await recall(1, text, profile="", tz=tz, spotters=SpotterCache(_NoGraph()), graph=_NoGraph(),
                        vector=vector)


@pytest.mark.parametrize(("tz", "written", "now", "stamp"), [
    (IST, local(IST, 3, 20, 0), local(IST, 6, 10, 0), "[3 days ago, Sat 3 Oct 20:00]"),
    (NYC, local(NYC, 5, 22, 0), local(NYC, 6, 7, 0), "[yesterday, Mon 5 Oct 22:00]"),
    (LON, local(LON, 6, 8, 15), local(LON, 6, 9, 0), "[earlier today 08:15]"),
    (AKL, local(AKL, 5, 23, 50), local(AKL, 6, 0, 5), "[yesterday, Mon 5 Oct 23:50]"),
])
async def test_recalled_episodes_are_stamped_with_when_they_were_written(vector, clock, tz, written, now,
                                                                         stamp):
    await vector.add(1, ["dentist appointment tomorrow at 2pm"], kind="episode", at=written)
    clock.set(now)
    ctx = await _recall(vector, tz, "dentist appointment tomorrow")
    assert ctx.episodes == [f"{stamp} dentist appointment tomorrow at 2pm"]


async def test_untrusted_hit_keeps_its_wrap_and_taint_with_the_stamp_outside(vector, clock):
    await vector.add(1, ["invoice due tomorrow"], kind="signal", at=local(IST, 4, 9, 0))
    clock.set(local(IST, 6, 9, 0))
    ctx = await _recall(vector, IST, "invoice due tomorrow")
    [line] = ctx.episodes
    assert line.startswith('[2 days ago, Sun 4 Oct 09:00] <untrusted source="memory">')
    assert ctx.untrusted is True


async def test_learn_stores_episodes_at_the_texts_anchor_not_the_job_time(memory, user, fake_llm, clock):
    from mavis.store.repo import users

    await users.update(user.id, timezone=NYC)
    clock.set(local(NYC, 6, 12, 0))  # the job runs much later than the text was written
    fake_llm.push_structured(Extraction())
    await memory.learn(user.id, "I am meeting Jawahar for lunch tomorrow at the usual place", "tg:1",
                       Trust.USER, anchor_at=local(NYC, 4, 9, 30))
    ctx = await _recall(memory.vector, NYC, "meeting Jawahar lunch tomorrow")
    assert ctx.episodes[0].startswith("[2 days ago, Sun 4 Oct 09:30] I am meeting Jawahar")

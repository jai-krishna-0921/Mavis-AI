from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from mavis.domain.memory import ExtractedEvent, Extraction, LoopDraft
from mavis.memory.dates import apply_relative_day, resolve_relative_day

TZ = "Asia/Kolkata"
SAT_NOON = datetime(2026, 10, 3, 12, 0, tzinfo=ZoneInfo(TZ))  # Saturday


@pytest.mark.parametrize("text, expected", [
    ("Jawahar said they'd get back to me by Tuesday", date(2026, 10, 6)),
    ("dentist on Monday at 4pm", date(2026, 10, 5)),
    ("call mom tomorrow", date(2026, 10, 4)),
    ("submit the form today", date(2026, 10, 3)),
    ("by tue please", date(2026, 10, 6)),
])
def test_resolves_simple_phrases(text, expected):
    assert resolve_relative_day(text, SAT_NOON) == expected


@pytest.mark.parametrize("text", [
    "by next Tuesday",              # "next" is ambiguous
    "on Saturday",                  # same weekday as today
    "on Monday or by Tuesday",      # two different days
    "the report is due Tuesday",    # no anchor word
    "on Oct 9 or by Tuesday",       # explicit date present
    "it went really well",          # nothing at all
])
def test_ambiguous_or_absent_is_none(text):
    assert resolve_relative_day(text, SAT_NOON) is None


def test_tomorrow_just_past_midnight_is_none():
    assert resolve_relative_day("call mom tomorrow", datetime(2026, 10, 3, 1, 0, tzinfo=ZoneInfo(TZ))) is None


def test_overrides_wrong_llm_day_keeping_time():
    thursday = datetime(2026, 10, 8, 12, 30, tzinfo=UTC)  # 18:00 IST Thursday: the model's misreading
    x = Extraction(
        loops=[LoopDraft(kind="WAITING_ON", title="Hear back from Jawahar", due_at=thursday),
               LoopDraft(kind="COMMITMENT", title="Pay rent", due_at=thursday)],
        events=[ExtractedEvent(title="Call with Jawahar", starts_at=thursday)],
    )
    out = apply_relative_day(x, "Jawahar said they'd get back to me by Tuesday", SAT_NOON, TZ)
    tuesday = datetime(2026, 10, 6, 12, 30, tzinfo=UTC)
    assert out.loops[0].due_at == tuesday
    assert out.loops[1].due_at == thursday  # not about what the user just said
    assert out.events[0].starts_at == tuesday


async def test_learn_applies_the_override(memory, user, fake_llm, clock):
    clock.set(SAT_NOON.astimezone(UTC))
    await _set_tz(user.id)
    thursday = datetime(2026, 10, 8, 12, 30, tzinfo=UTC)
    fake_llm.push_structured(Extraction(loops=[LoopDraft(kind="WAITING_ON", title="Hear back from Jawahar",
                                                         due_at=thursday)]))
    out = await memory.learn(user.id, "Mavis: How did it go?\nUser: Jawahar will get back to me by Tuesday",
                             "tg:update:9")
    assert out.loops[0].due_at == datetime(2026, 10, 6, 12, 30, tzinfo=UTC)


async def _set_tz(user_id: int) -> None:
    from sqlalchemy import update

    from mavis.store.db import Session
    from mavis.store.models import User

    async with Session() as s:
        await s.execute(update(User).where(User.id == user_id).values(timezone=TZ))
        await s.commit()

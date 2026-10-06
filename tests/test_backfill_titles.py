"""I2: scripts/backfill_titles.py resolves relative words in existing live loop titles, anchored at each
loop's created_at in its user's zone, with the same resolver. Dry run by default."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from scripts.backfill_titles import backfill

from mavis.store.db import Session
from mavis.store.models import LoopRow
from mavis.store.repo import loops, users


def local(tz: str, d: int, h: int) -> datetime:
    return datetime(2026, 10, d, h, tzinfo=ZoneInfo(tz)).astimezone(UTC)


async def _row(user_id: int, title: str, created: datetime, status: str = "OPEN") -> int:
    async with Session() as s:
        row = LoopRow(user_id=user_id, kind="COMMITMENT", title=title, entities=[], status=status,
                      importance=3, source="", trust="user", origin="conversation", created_at=created,
                      updated_at=created)
        s.add(row)
        await s.commit()
        return row.id


async def test_dry_run_reports_only_changed_rows_and_writes_nothing(db, clock):
    a, _ = await users.get_or_create_by_chat(1, "A")
    b, _ = await users.get_or_create_by_chat(2, "B")
    await users.update(b.id, timezone="America/New_York")
    clock.set(local("Asia/Kolkata", 9, 12))
    r1 = await _row(a.id, "Block around 2 pm tomorrow", local("Asia/Kolkata", 3, 18))
    r2 = await _row(a.id, "Renew the lease", local("Asia/Kolkata", 3, 18))
    r3 = await _row(b.id, "Dinner with Priya tonight", local("America/New_York", 5, 9))
    r4 = await _row(a.id, "Call mom today", local("Asia/Kolkata", 2, 9), status="DONE")
    r5 = await _row(b.id, "Read the Sunday Times", local("America/New_York", 5, 9))
    lines: list[str] = []
    changes = await backfill(apply=False, out=lines.append)
    assert changes == [(r1, "Block around 2 pm tomorrow", "Block around 2 pm Sun 4 Oct"),
                       (r3, "Dinner with Priya tonight", "Dinner with Priya Mon 5 Oct evening")]
    assert lines == [f"{r1}: Block around 2 pm tomorrow -> Block around 2 pm Sun 4 Oct",
                     f"{r3}: Dinner with Priya tonight -> Dinner with Priya Mon 5 Oct evening"]
    assert (await loops.get(r1)).title == "Block around 2 pm tomorrow"
    assert {(await loops.get(r)).title for r in (r2, r4, r5)} == {"Renew the lease", "Call mom today",
                                                                  "Read the Sunday Times"}


async def test_apply_writes_and_a_second_run_finds_nothing(db, clock):
    a, _ = await users.get_or_create_by_chat(1, "A")
    clock.set(local("Asia/Kolkata", 9, 12))
    r1 = await _row(a.id, "Pay rent by Monday", local("Asia/Kolkata", 3, 18), status="AWAITING")
    await backfill(apply=True, out=lambda _: None)
    assert (await loops.get(r1)).title == "Pay rent by Mon 5 Oct"
    assert await backfill(apply=True, out=lambda _: None) == []

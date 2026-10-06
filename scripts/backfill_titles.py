"""One-off: make existing live loop titles time-neutral.

Titles written before the relative-date resolver existed can still say "tomorrow" or "this Friday". This
resolves them with the same resolver every writer now uses (mavis.domain.reldate), anchored at each loop's
created_at in its user's timezone. Only live loops (open, awaiting a reply) are touched, and only rows
whose title actually changes are listed.

Dry run by default: prints "id: old -> new" for every row that would change and writes nothing.

    uv run python scripts/backfill_titles.py           # dry run
    uv run python scripts/backfill_titles.py --apply   # write the new titles
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable

from sqlalchemy import select

from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.loops import LoopStatus
from mavis.domain.reldate import absolutize
from mavis.store.db import Session
from mavis.store.models import LoopRow, User

LIVE = (LoopStatus.OPEN.value, LoopStatus.AWAITING_REPLY.value)


async def backfill(apply: bool = False, out: Callable[[str], None] = print) -> list[tuple[int, str, str]]:
    """Returns (loop id, old title, new title) for every live loop whose title changes."""
    default_tz = get_settings().default_timezone
    changes: list[tuple[int, str, str]] = []
    async with Session() as s:
        zones = {u.id: (u.timezone or default_tz) for u in await s.scalars(select(User))}
        rows = list(await s.scalars(select(LoopRow).where(LoopRow.status.in_(LIVE)).order_by(LoopRow.id)))
        for row in rows:
            anchor = timeutil.ensure_utc(row.created_at)
            new = absolutize(row.title, anchor, zones.get(row.user_id, default_tz))
            if new == row.title:
                continue
            changes.append((row.id, row.title, new))
            out(f"{row.id}: {row.title} -> {new}")
            if apply:
                row.title = new
                row.version = (row.version or 1) + 1
                row.updated_at = timeutil.now()
        if apply and changes:
            await s.commit()
    return changes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the new titles (default: dry run)")
    args = parser.parse_args()
    changes = asyncio.run(backfill(apply=args.apply))
    verb = "updated" if args.apply else "would change (dry run, nothing written)"
    print(f"{len(changes)} loop title(s) {verb}.")


if __name__ == "__main__":
    main()

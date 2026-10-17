# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Fathom poll fallback: when a webhook was missed, list recent meetings and queue the ones with no note.

Runs on the 2 minute poll chain but only calls Fathom every 15 minutes (cursor `fathom_poll_at`). The
cursor `fathom_seen` is the newest meeting creation time seen; each list starts 10 minutes before it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from mavis.bus import get_bus
from mavis.domain import timeutil
from mavis.domain.events import Job, JobKind
from mavis.store.repo import meeting_notes
from mavis.tools.integrations.fathom_ingest import fathom_api
from mavis.tools.integrations.native.base import ReauthRequired
from mavis.tools.integrations.native.fathom_api import FathomError
from mavis.tools.integrations.native.providers import PollAuthError
from mavis.tools.integrations.normalize import to_datetime

log = structlog.get_logger(__name__)

EVERY = timedelta(minutes=15)
OVERLAP = timedelta(minutes=10)
FIRST_LOOKBACK = timedelta(days=1)
LIMIT = 50


def _when(value: object) -> datetime | None:
    dt = to_datetime(value) if isinstance(value, str) else None
    return timeutil.ensure_utc(dt) if dt else None


async def poll_fathom(poller, user_id: int, st: dict) -> int:
    cursors = st.get("cursors") or {}
    now = poller.clock()
    last = _when(cursors.get("fathom_poll_at"))
    if last is not None and now - last < EVERY:
        return 0
    api = fathom_api()
    if api is None:
        return 0
    # stamped before the call: a Fathom outage or a refused token is retried in 15 minutes, not every poll
    await poller.set_cursor(user_id, st, "fathom_poll_at", now.isoformat())
    seen = _when(cursors.get("fathom_seen"))
    after = (seen - OVERLAP) if seen else now - FIRST_LOOKBACK
    try:
        meetings = await api.meetings(user_id, created_after=after, limit=LIMIT)
    except ReauthRequired:
        raise PollAuthError from None
    except FathomError as exc:
        log.warning("fathom.poll_failed", user_id=user_id, kind=exc.kind.value, code=exc.code)
        return 0
    queued, created = 0, []
    for m in meetings:
        rid = m.get("recording_id")
        if isinstance(rid, bool) or not isinstance(rid, int) or rid < 1:
            continue
        if made := _when(m.get("created_at")):
            created.append(made)
        if await meeting_notes.by_recording(user_id, str(rid)) is not None:
            continue
        await get_bus().enqueue(Job(id=f"fathom:{user_id}:{rid}", user_id=user_id,
                                    kind=JobKind.FATHOM_RECORDING, payload={"meeting": m, "source": "poll"}))
        queued += 1
    if len(meetings) >= LIMIT and created:
        newest = min(created)  # the list was cut: the next one resumes from the oldest we reached
    else:
        newest = min(max(created, default=now), now)
    await poller.set_cursor(user_id, st, "fathom_seen", newest.isoformat())
    return queued

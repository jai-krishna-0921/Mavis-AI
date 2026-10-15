# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""OneDrive files shared with the user: the Microsoft poll publishes WORKSPACE_SIGNAL events of kind
`onedrive_share`; this handler maps one to the same FILE_SHARED signal a Drive share makes and hands it to
the workspace intake. It is separate from the Google workspace handler (which ignores unknown kinds) so a
Microsoft-only user is served too."""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.exc import NoResultFound

from mavis.attention.workspace_signals import shared_file_signal
from mavis.domain.events import Event, EventType
from mavis.store.repo import users

log = structlog.get_logger()
KIND = "onedrive_share"


def _workspace() -> Any:
    from mavis.attention.wiring import get_workspace  # lazy: wiring imports this module

    return get_workspace()


async def on_event(event: Event) -> None:
    if event.type is not EventType.WORKSPACE_SIGNAL or event.payload.get("kind") != KIND:
        return
    raw = event.payload.get("raw")
    signal = shared_file_signal(raw) if isinstance(raw, dict) else None
    if signal is None:
        return
    try:
        user = await users.get(event.user_id)
    except NoResultFound:
        return
    workspace = _workspace()
    await workspace.handle(user, signal, quiet=not await workspace.is_baselined(user.id))

# ruff: noqa: E501
from datetime import UTC, datetime

import pytest

from mavis.attention import onedrive_shares
from mavis.attention.workspace_signals import SignalKind
from mavis.domain.events import Event, EventType, Trust
from mavis.worker import runner


class FakeWorkspace:
    def __init__(self, baselined: bool) -> None:
        self.baselined, self.handled = baselined, []

    async def is_baselined(self, user_id: int) -> bool:
        return self.baselined

    async def handle(self, user, signal, *, quiet=False, backlog=False):
        self.handled.append((signal, quiet))
        return "ok"


def share(user_id: int, kind="onedrive_share"):
    raw = {"id": "ms:b", "name": "Plan.docx", "sharedWithMeTime": "2026-10-15T12:00:00Z",
           "sharingUser": {"emailAddress": "priya@corp.test", "displayName": "Priya"}}
    return Event(id=f"ods:{user_id}:b", user_id=user_id, type=EventType.WORKSPACE_SIGNAL, source="poller", occurred_at=datetime(2026, 10, 15, 12, tzinfo=UTC),
                 payload={"kind": kind, "raw": raw}, trust=Trust.UNTRUSTED)


@pytest.mark.parametrize("baselined", [True, False])
async def test_share_becomes_a_file_shared_signal(user, monkeypatch, baselined):
    ws = FakeWorkspace(baselined)
    monkeypatch.setattr(onedrive_shares, "_workspace", lambda: ws)
    await onedrive_shares.on_event(share(user.id))
    [(signal, quiet)] = ws.handled
    assert signal.kind is SignalKind.FILE_SHARED and signal.actor == "priya@corp.test"
    assert signal.object_title == "Plan.docx" and quiet is (not baselined)


@pytest.mark.parametrize("kind", ["share", "comment", "task"])
async def test_other_kinds_are_ignored(user, monkeypatch, kind):
    ws = FakeWorkspace(True)
    monkeypatch.setattr(onedrive_shares, "_workspace", lambda: ws)
    await onedrive_shares.on_event(share(user.id, kind))
    assert ws.handled == []


def test_registered_next_to_the_workspace_handler(monkeypatch):
    from mavis.attention import wiring
    saved = {k: list(v) for k, v in runner._event_handlers.items()}
    try:
        monkeypatch.setattr(wiring, "workspace_enabled", lambda: True)
        wiring.register_workspace()
        wiring.register_onedrive_shares()
        handlers = runner._event_handlers[EventType.WORKSPACE_SIGNAL]
        assert onedrive_shares.on_event in handlers and len(handlers) == 2
    finally:
        runner._event_handlers.clear()
        runner._event_handlers.update(saved)

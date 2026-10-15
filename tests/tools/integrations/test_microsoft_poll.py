# ruff: noqa: E501
from datetime import timedelta

from mavis.domain.errors import FailureKind as K
from mavis.domain.events import EventType
from mavis.domain.integrations import ToolResult
from mavis.domain.policy import Capability as C
from mavis.tools.integrations.native.providers import PollAuthError
from mavis.tools.integrations.poller import hook_for
from tests.tools.integrations.fakes import NOW

from .test_microsoft_sync import Native


class Bus:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        if any(e.id == event.id for e in self.events):
            return False
        self.events.append(event)
        return True


class P:
    def __init__(self, provider):
        self.provider, self.bus, self.cursors = provider, Bus(), {}
        self.now = NOW

    def clock(self):
        return self.now

    async def set_cursor(self, user_id, st, key, value):
        st.setdefault("cursors", {})[key] = value


def mail(mid, labels=("INBOX", "UNREAD"), ts="2026-10-15T08:00:00Z"):
    return {"messageId": f"ms:{mid}", "threadId": f"ms:t{mid}", "sender": "Priya <priya@corp.test>", "subject": "Plan",
            "messageText": "hi", "labelIds": list(labels), "messageTimestamp": ts}


async def test_every_microsoft_capability_has_a_hook():
    for cap in (C.OUTLOOK, C.OUTLOOK_CALENDAR, C.TEAMS, C.ONEDRIVE):
        assert hook_for(cap) is not None


async def test_mail_baseline_publishes_nothing_then_new_unread_mail_becomes_events():
    n, st = Native(), {}
    n.results["mail.inbox_delta"] = ToolResult(ok=True, data={"messages": [], "cursor": "L1", "baseline": True})
    poller = P(n)
    poll_mail = hook_for(C.OUTLOOK)
    assert await poll_mail(poller, 1, st) == 0 and st["cursors"]["outlook_delta"] == "L1"
    n.results["mail.inbox_delta"] = ToolResult(ok=True, data={"messages": [
        mail("1"), mail("2", labels=("INBOX",)), mail("3", ts="2026-09-01T00:00:00Z")], "cursor": "L2"})
    assert await poll_mail(poller, 1, st) == 1                  # read mail and old mail are not events
    [ev] = poller.bus.events
    assert ev.id == "outlook:1:msg:1" and ev.type is EventType.EMAIL_RECEIVED and st["cursors"]["outlook_delta"] == "L2"
    assert await poll_mail(poller, 1, st) == 0                  # the same message again: deduped by id


async def test_cursor_does_not_advance_when_publishing_fails():
    n, st = Native(), {"cursors": {"outlook_delta": "L1"}}
    n.results["mail.inbox_delta"] = ToolResult(ok=True, data={"messages": [mail("1")], "cursor": "L2"})
    poller = P(n)

    async def boom(event):
        raise RuntimeError("bus down")
    poller.bus.publish = boom
    try:
        await hook_for(C.OUTLOOK)(poller, 1, st)
    except RuntimeError:
        pass
    assert st["cursors"]["outlook_delta"] == "L1"


async def test_reset_rebaselines_and_auth_raises_and_400_falls_back_to_search():
    n, st = Native(), {"cursors": {"outlook_delta": "old"}}
    poller, poll_mail = P(n), hook_for(C.OUTLOOK)
    n.results["mail.inbox_delta"] = ToolResult(ok=True, data={"messages": [], "cursor": "", "reset": True})
    await poll_mail(poller, 1, st)
    assert st["cursors"]["outlook_delta"] == ""
    n.results["mail.inbox_delta"] = ToolResult(ok=False, error_kind=K.AUTH, error="x")
    try:
        await poll_mail(poller, 1, st)
        raise AssertionError("expected PollAuthError")
    except PollAuthError:
        pass
    st["cursors"]["outlook_delta"] = ""
    n.results["mail.inbox_delta"] = ToolResult(ok=False, error_kind=K.INVALID_ARGUMENT, error="x")
    n.results["mail.search"] = ToolResult(ok=True, data={"messages": [mail("9")]})
    assert await poll_mail(poller, 1, st) == 1
    assert n.native[-1][1] == "mail.search" and "in:inbox" in n.native[-1][2]["query"]


async def test_rate_limit_backs_off_exponentially_and_skips_ticks():
    n, st = Native(), {}
    n.results["mail.inbox_delta"] = ToolResult(ok=False, error_kind=K.RATE_LIMITED, error="x")
    poller, poll_mail = P(n), hook_for(C.OUTLOOK)
    await poll_mail(poller, 1, st)
    assert st["cursors"]["ms_backoff_n"] == 1
    calls = len(n.native)
    await poll_mail(poller, 1, st)                              # still inside the back-off: no call
    assert len(n.native) == calls
    poller.now = NOW + timedelta(minutes=3)
    n.results["mail.inbox_delta"] = ToolResult(ok=True, data={"messages": [], "cursor": "L", "baseline": True})
    await poll_mail(poller, 1, st)
    assert st["cursors"]["ms_backoff_n"] == 0


async def test_teams_first_tick_sets_the_watermark_then_publishes_new_messages_including_own():
    n, st = Native(), {}
    poller, poll_teams = P(n), hook_for(C.TEAMS)
    n.results["teams.chats"] = ToolResult(ok=True, data={"chats": [
        {"id": "ms:c1", "type": "oneOnOne", "topic": "", "members": [{"name": "Priya", "email": "priya@corp.test"}],
         "lastMessageAt": "2026-10-15T11:00:00Z"}]})
    assert await poll_teams(poller, 1, st) == 0 and st["cursors"]["teams_at"]
    st["cursors"]["teams_at"] = "2026-10-15T09:00:00+00:00"
    n.results["teams.history"] = ToolResult(ok=True, data={"messages": [
        {"id": "m2", "chatId": "ms:c1", "from": "Priya", "fromId": "u1", "fromMe": False, "text": "ping", "at": "2026-10-15T11:00:00Z"},
        {"id": "m1", "chatId": "ms:c1", "from": "Me", "fromId": "me", "fromMe": True, "text": "old", "at": "2026-10-15T08:00:00Z"}]})
    assert await poll_teams(poller, 1, st) == 1
    [ev] = poller.bus.events
    assert ev.type is EventType.TEAMS_MESSAGE and ev.id == "teams:1:c1:m2" and ev.payload["chat_type"] == "oneOnOne"
    assert st["cursors"]["teams_at"].startswith("2026-10-15T11:00")


async def test_teams_forbidden_is_swallowed():
    n, st = Native(), {"cursors": {"teams_at": "2026-10-15T09:00:00+00:00"}}
    n.results["teams.chats"] = ToolResult(ok=False, error_kind=K.PERMISSION_MISSING, error="x")
    assert await hook_for(C.TEAMS)(P(n), 1, st) == 0


async def test_onedrive_polls_every_thirty_minutes_baselines_then_signals_new_shares():
    n, st = Native(), {}
    poller, poll_od = P(n), hook_for(C.ONEDRIVE)
    n.results["onedrive.list_recent"] = ToolResult(ok=True, data={"files": [
        {"id": "ms:a", "name": "Old.docx", "sharedBy": "Priya", "sharedByEmail": "priya@corp.test", "modifiedTime": "2026-10-10T00:00:00Z"}]})
    assert await poll_od(poller, 1, st) == 0 and st["cursors"]["onedrive_seen"] == ["ms:a"]
    await poll_od(poller, 1, st)
    assert len([c for c in n.native if c[1] == "onedrive.list_recent"]) == 1      # inside 30 minutes: no call
    poller.now = NOW + timedelta(minutes=31)
    n.results["onedrive.list_recent"] = ToolResult(ok=True, data={"files": [
        {"id": "ms:a", "name": "Old.docx"}, {"id": "ms:b", "name": "Plan.docx", "sharedBy": "Priya", "sharedByEmail": "priya@corp.test", "modifiedTime": "2026-10-15T12:00:00Z"}]})
    assert await poll_od(poller, 1, st) == 1
    [ev] = poller.bus.events
    assert ev.type is EventType.WORKSPACE_SIGNAL and ev.payload["kind"] == "onedrive_share" and ev.payload["raw"]["name"] == "Plan.docx"

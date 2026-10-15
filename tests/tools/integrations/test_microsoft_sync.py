# ruff: noqa: E501
from mavis.domain.events import EventType
from mavis.domain.integrations import ToolResult
from mavis.domain.policy import Capability as C
from mavis.tools.integrations.first_sync import EXTRA_HANDLERS, FirstSync
from mavis.tools.integrations.microsoft_sync import MicrosoftSync
from tests.tools.integrations.fakes import NOW

from .test_first_sync import FakeLoops, FakeMemory, tz_of


class Native:
    """execute_native like NativeRouter's; records (provider, action, args); scripted per action."""

    def __init__(self):
        self.results, self.native, self.plain = {}, [], []
        self.tokens = self

    async def account(self, user_id, provider):
        return {"tid": "tenant-1", "oid": "o0"}

    async def execute(self, user, action, args):
        self.plain.append(action)
        return ToolResult(ok=False, error="merge path must not be used")

    async def execute_native(self, provider, user, action, args):
        self.native.append((provider.value, action, dict(args)))
        r = self.results.get(action)
        r = r(args) if callable(r) else r
        return r or ToolResult(ok=True, data={})


class Sink:
    def __init__(self):
        self.mail, self.teams_ = [], []

    async def email(self, uid, n, *, source="gmail"):
        self.mail.append((uid, n["message_id"], source))

    async def teams(self, uid, n):
        self.teams_.append((uid, n["chat_id"], n["message_id"], n))


def make(provider, fake_bus, sink=None, clock=lambda: NOW):
    mem, loops = FakeMemory(), FakeLoops()
    fs = FirstSync(provider=provider, memory=mem, loops=loops, bus=fake_bus, tz_of=tz_of, clock=clock,
                   connectors=sink)
    MicrosoftSync(fs).register()
    return fs, mem


def msg(mid, labels=("INBOX", "UNREAD"), sender="Priya <priya@corp.test>", subject="Plan", ts="2026-10-04T08:00:00Z"):
    return {"messageId": f"ms:{mid}", "threadId": f"ms:t{mid}", "sender": sender, "subject": subject,
            "messageText": "hello", "labelIds": list(labels), "messageTimestamp": ts}


async def test_outlook_sync_reads_microsoft_only_skips_other_and_reports(fake_bus):
    p, sink = Native(), Sink()
    p.results["mail.search"] = lambda a: ToolResult(ok=True, data={"messages": (
        [msg("1"), msg("2", labels=("INBOX", "CATEGORY_OTHER"))] if "inbox" in a["query"] else
        [msg("3", labels=("SENT",), sender="Me <me@corp.test>")])})
    fs, mem = make(p, fake_bus, sink)
    noticed = await fs.run(1, C.OUTLOOK)
    assert p.plain == [] and [c[0] for c in p.native] == ["microsoft", "microsoft"]
    assert [m[1] for m in sink.mail if m[2] == "outlook"] == ["ms:1", "ms:3"]       # the Other message is skipped
    assert "in:inbox" in p.native[0][2]["query"] and "newer_than:14d" in p.native[0][2]["query"]
    assert "in:sent" in p.native[1][2]["query"]
    assert any("waiting on you" in n for n in noticed)
    assert fake_bus.events[-1].payload["capability"] == "outlook" and fake_bus.events[-1].type is EventType.TASK_COMPLETED


async def test_outlook_without_connectors_learns_in_batches(fake_bus):
    p = Native()
    p.results["mail.search"] = ToolResult(ok=True, data={"messages": [msg("1")]})
    fs, mem = make(p, fake_bus)
    await fs.run(1, C.OUTLOOK)
    assert mem.calls and mem.calls[0][2].startswith("first_sync:1:outlook:")


async def test_calendar_sync_uses_the_ocal_prefix_and_native_calls(fake_bus):
    p = Native()
    p.results["calendar.list"] = ToolResult(ok=True, data={"items": [
        {"id": "ms:e1", "summary": "Standup", "start": {"dateTime": "2026-10-06T09:00:00+00:00"},
         "end": {"dateTime": "2026-10-06T09:30:00+00:00"}, "attendees": [{"email": "a@b.com"}]}]})
    fs, mem = make(p, fake_bus)
    noticed = await fs.run(1, C.OUTLOOK_CALENDAR)
    assert p.native[0][:2] == ("microsoft", "calendar.list") and p.plain == []
    assert mem.calls[0][2].startswith("ocal:1:first_sync:") and any("Next up: Standup" in n for n in noticed)


def teams_world(p, history):
    p.results["teams.chats"] = ToolResult(ok=True, data={"chats": [
        {"id": "ms:c1", "type": "oneOnOne", "topic": "", "members": [
            {"id": "u1", "name": "Priya", "email": "priya@corp.test", "me": False, "tenantId": "tenant-1"},
            {"id": "u0", "name": "Me", "email": "me@corp.test", "me": True, "tenantId": "tenant-1"}], "lastMessageAt": "2026-10-04T10:00:00Z"},
        {"id": "ms:c2", "type": "group", "topic": "Old", "members": [], "lastMessageAt": "2026-08-01T10:00:00Z"}]})
    p.results["teams.history"] = history


def tmsg(d, i, mid=None, **kw):
    return {"id": mid or f"m{d}{i}", "chatId": "ms:c1", "from": "Priya", "fromId": "u1", "fromMe": False,
            "text": "hi", "at": f"2026-10-{d:02d}T10:0{i}:00Z", "mentionsMe": False, **kw}


async def test_teams_sync_respects_window_and_caps_and_skips_other_messages(fake_bus):
    p, sink = Native(), Sink()
    teams_world(p, ToolResult(ok=True, data={"messages": [tmsg(4, 1), tmsg(3, 2), tmsg(1, 3, mid="old")], "skipped": 0}))
    p.results["teams.history"] = ToolResult(ok=True, data={"messages": [
        tmsg(4, 1), tmsg(3, 2), {**tmsg(1, 3), "id": "old", "at": "2026-09-20T10:00:00Z"}], "skipped": 0})
    fs, mem = make(p, fake_bus, sink)
    await fs.run(1, C.TEAMS)
    assert [m[2] for m in sink.teams_] == ["m41", "m32"]                              # 20 Sep is outside the 7 day window
    assert not any(c[2].get("chat_id") == "ms:c2" for c in p.native)                 # outside the 7 day window
    n = sink.teams_[0][3]
    assert n["from_address"] == "priya@corp.test" and n["chat_type"] == "oneOnOne" and n["kind"] == "message"
    assert n["from_me"] is False and n["external"] is False and n["members"][0]["email"] == "priya@corp.test"
    assert n["members"][1]["me"] is True


async def _external_for(fake_bus, tenant, **member):
    p, sink = Native(), Sink()
    p.results["teams.chats"] = ToolResult(ok=True, data={"chats": [
        {"id": "ms:c1", "type": "group", "topic": "", "lastMessageAt": "2026-10-04T10:00:00Z",
         "members": [{"id": "u1", "name": "G", "email": "g@other.test", "me": False, **member}]}]})
    p.results["teams.history"] = ToolResult(ok=True, data={"messages": [tmsg(4, 1)]})
    fs, _ = make(p, fake_bus, sink)
    await fs.run(1, C.TEAMS)
    return sink.teams_[0][3]["external"]


async def test_teams_sender_tenant_decides_external(fake_bus):
    assert await _external_for(fake_bus, "t", tenantId="tenant-1") is False        # colleague
    assert await _external_for(fake_bus, "t", tenantId="other-tenant") is True     # guest or federated
    assert await _external_for(fake_bus, "t") is True                              # unknown: fail safe


async def test_teams_empty_message_id_is_not_kept(fake_bus):
    p, sink = Native(), Sink()
    teams_world(p, ToolResult(ok=True, data={"messages": [{**tmsg(4, 1), "id": ""}, tmsg(4, 2)]}))
    fs, _ = make(p, fake_bus, sink)
    await fs.run(1, C.TEAMS)
    assert [m[2] for m in sink.teams_] == ["m42"]


async def test_teams_budget_is_checked_inside_a_chat(fake_bus):
    from datetime import timedelta

    from mavis.tools.integrations import microsoft_sync as ms

    t = [NOW]

    def clock():
        return t[0]

    p, sink = Native(), Sink()
    teams_world(p, lambda a: (t.__setitem__(0, t[0] + timedelta(seconds=ms.BACKFILL_BUDGET_S + 1)) or
                              ToolResult(ok=True, data={"messages": [tmsg(4, 1)], "next": "2026-10-04T10:00:00Z"})))
    fs, _ = make(p, fake_bus, sink, clock=clock)
    await fs.run(1, C.TEAMS)
    assert len(sink.teams_) == 1 and len([c for c in p.native if c[1] == "teams.history"]) == 1


async def test_teams_total_and_per_chat_caps(fake_bus, monkeypatch):
    from mavis.tools.integrations import microsoft_sync as ms

    monkeypatch.setattr(ms, "PER_CHAT_CAP", 3)
    monkeypatch.setattr(ms, "TOTAL_CAP", 4)
    p, sink = Native(), Sink()
    p.results["teams.chats"] = ToolResult(ok=True, data={"chats": [
        {"id": "ms:c1", "type": "group", "topic": "A", "members": [], "lastMessageAt": "2026-10-04T10:00:00Z"},
        {"id": "ms:c2", "type": "group", "topic": "B", "members": [], "lastMessageAt": "2026-10-03T10:00:00Z"}]})
    p.results["teams.history"] = ToolResult(ok=True, data={"messages": [tmsg(4, i, mid=f"x{i}") for i in range(6)]})
    fs, _ = make(p, fake_bus, sink)
    await fs.run(1, C.TEAMS)
    assert len(sink.teams_) == 4 and [m[1] for m in sink.teams_] == ["ms:c1"] * 3 + ["ms:c2"]


async def test_teams_rate_limited_page_ends_that_chat_only(fake_bus):
    from mavis.domain.errors import FailureKind as K

    p, sink = Native(), Sink()
    p.results["teams.chats"] = ToolResult(ok=True, data={"chats": [
        {"id": "ms:c1", "type": "group", "members": [], "lastMessageAt": "2026-10-04T10:00:00Z"},
        {"id": "ms:c2", "type": "group", "members": [], "lastMessageAt": "2026-10-03T10:00:00Z"}]})
    p.results["teams.history"] = lambda a: (ToolResult(ok=False, error_kind=K.RATE_LIMITED, error="x")
                                            if a["chat_id"] == "ms:c1" else ToolResult(ok=True, data={"messages": [tmsg(3, 1)]}))
    fs, _ = make(p, fake_bus, sink)
    await fs.run(1, C.TEAMS)
    assert [m[1] for m in sink.teams_] == ["ms:c2"]


async def test_teams_forbidden_is_silent(fake_bus):
    from mavis.domain.errors import FailureKind as K
    p, sink = Native(), Sink()
    p.results["teams.chats"] = ToolResult(ok=False, error_kind=K.PERMISSION_MISSING, error="x")
    fs, _ = make(p, fake_bus, sink)
    assert await fs.run(1, C.TEAMS) == [] and sink.teams_ == []


async def test_onedrive_sync_learns_titles_only(fake_bus):
    p = Native()
    p.results["onedrive.list_recent"] = lambda a: ToolResult(ok=True, data={"files": [
        {"id": "ms:1", "name": "Budget.xlsx" if not a.get("shared_with_me") else "Plan.docx"}]})
    fs, mem = make(p, fake_bus)
    noticed = await fs.run(1, C.ONEDRIVE)
    assert mem.calls[0][2] == "onedrive:1:titles" and "Budget.xlsx" in mem.calls[0][1] and "Plan.docx" in mem.calls[0][1]
    assert noticed == ["Found 2 OneDrive files"]
    assert not [c for c in p.native if c[1] in ("onedrive.read",)]


def test_handlers_are_registered_for_all_four(fake_bus):
    make(Native(), fake_bus)
    assert {C.OUTLOOK, C.OUTLOOK_CALENDAR, C.TEAMS, C.ONEDRIVE} <= set(EXTRA_HANDLERS)

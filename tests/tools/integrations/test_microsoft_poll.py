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


async def test_changes_spread_over_ticks_are_neither_lost_nor_repeated():
    n, st = Native(), {"cursors": {"outlook_delta": "L0"}}
    poller, poll_mail = P(n), hook_for(C.OUTLOOK)
    first = [mail(str(i)) for i in range(30)]
    n.results["mail.inbox_delta"] = lambda a: ToolResult(ok=True, data=(
        {"messages": first, "cursor": "NEXT", "more": True} if a["cursor"] == "L0"
        else {"messages": [mail(str(i)) for i in range(30, 60)], "cursor": "L2"}))
    assert await poll_mail(poller, 1, st) == 30 and st["cursors"]["outlook_delta"] == "NEXT"
    assert await poll_mail(poller, 1, st) == 30 and st["cursors"]["outlook_delta"] == "L2"
    assert len({e.id for e in poller.bus.events}) == 60


def chat(cid, last, members=()):
    return {"id": f"ms:{cid}", "type": "group", "topic": "T", "members": list(members), "lastMessageAt": last}


def tmsg(mid, at, cid, me=False, from_id="u1"):
    return {"id": mid, "chatId": f"ms:{cid}", "from": "P", "fromId": from_id, "fromMe": me, "text": "x", "at": at}


async def test_teams_failed_chat_keeps_the_floor_below_its_messages_and_is_retried():
    n, st = Native(), {"cursors": {"teams_at": "2026-10-15T09:00:00+00:00"}}
    poller, poll_teams = P(n), hook_for(C.TEAMS)
    n.results["teams.chats"] = ToolResult(ok=True, data={"chats": [
        chat("a", "2026-10-15T10:00:00Z"), chat("b", "2026-10-15T11:00:00Z")]})
    n.results["teams.history"] = lambda args: (
        ToolResult(ok=False, error_kind=K.UNKNOWN, error="x") if args["chat_id"] == "ms:a" else
        ToolResult(ok=True, data={"messages": [tmsg("b1", "2026-10-15T11:00:00Z", "b")]}))
    assert await poll_teams(poller, 1, st) == 1
    assert st["cursors"]["teams_at"].startswith("2026-10-15T09:00")      # chat a is still owed
    n.results["teams.history"] = lambda args: ToolResult(ok=True, data={"messages": [
        tmsg("a1", "2026-10-15T10:00:00Z", "a")] if args["chat_id"] == "ms:a" else [tmsg("b1", "2026-10-15T11:00:00Z", "b")]})
    assert await poll_teams(poller, 1, st) == 1                           # b1 is deduped, a1 arrives
    assert {e.id for e in poller.bus.events} == {"teams:1:b:b1", "teams:1:a:a1"}
    assert st["cursors"]["teams_at"].startswith("2026-10-15T11:00")


async def test_teams_throttled_history_sets_the_backoff_and_stops_reading():
    n, st = Native(), {"cursors": {"teams_at": "2026-10-15T09:00:00+00:00"}}
    poller = P(n)
    n.results["teams.chats"] = ToolResult(ok=True, data={"chats": [
        chat("a", "2026-10-15T10:00:00Z"), chat("b", "2026-10-15T11:00:00Z")]})
    n.results["teams.history"] = ToolResult(ok=False, error_kind=K.RATE_LIMITED, error="x")
    assert await hook_for(C.TEAMS)(poller, 1, st) == 0
    assert st["cursors"]["ms_backoff_n"] == 1 and [c[1] for c in n.native].count("teams.history") == 1
    assert st["cursors"]["teams_at"].startswith("2026-10-15T09:00")


async def test_teams_external_follows_the_sender_tenant():
    class Tokens:
        async def account(self, uid, provider):
            return {"tid": "TENANT-A"}

    n, st = Native(), {"cursors": {"teams_at": "2026-10-15T09:00:00+00:00"}}
    n.tokens = Tokens()
    poller = P(n)
    members = [{"id": "u1", "name": "P", "email": "p@x.com", "tenantId": "tenant-a"},
               {"id": "u2", "name": "Q", "email": "q@y.com", "tenantId": "tenant-b"},
               {"id": "u3", "name": "R", "email": "r@y.com", "tenantId": ""}]
    n.results["teams.chats"] = ToolResult(ok=True, data={"chats": [chat("a", "2026-10-15T11:00:00Z", members)]})
    n.results["teams.history"] = ToolResult(ok=True, data={"messages": [
        tmsg("1", "2026-10-15T10:00:00Z", "a", from_id="u1"), tmsg("2", "2026-10-15T10:01:00Z", "a", from_id="u2"),
        tmsg("3", "2026-10-15T10:02:00Z", "a", from_id="u3"), tmsg("4", "2026-10-15T10:03:00Z", "a", me=True, from_id="u2")]})
    await hook_for(C.TEAMS)(poller, 1, st)
    by = {e.payload["message_id"]: e.payload["external"] for e in poller.bus.events}
    assert by == {"1": False, "2": True, "3": True, "4": False}
    assert poller.bus.events[0].payload["members"][0]["tenantId"] == "tenant-a"


async def test_two_chains_writing_cursors_concurrently_keep_both(user):
    from mavis.tools.integrations.connect_flow import RepoUserState
    from mavis.tools.integrations.poller import Poller

    poller = Poller(provider=None, cache=None, bus=None, state=RepoUserState(), schedule=None)
    stale_a, stale_b = {"cursors": {}}, {"cursors": {}}              # both ticks read before either wrote
    await poller.set_cursor(user.id, stale_a, "outlook_delta", "L1")
    await poller.set_cursor(user.id, stale_b, "teams_at", "T1")      # old code rewrote the whole dict: L1 lost
    await poller.set_cursor(user.id, stale_a, "gmail_after", 5)
    cursors = (await RepoUserState().get(user.id))["cursors"]
    assert cursors == {"outlook_delta": "L1", "teams_at": "T1", "gmail_after": 5}


def history_of(count, *, start="2026-10-15T09:00:00", cid="a"):
    """A chat with `count` messages one minute apart after `start`, paged newest first like Graph."""
    from datetime import datetime, timedelta
    t0 = datetime.fromisoformat(start + "+00:00")
    msgs = [tmsg(f"{cid}{i}", (t0 + timedelta(minutes=i + 1)).isoformat(), cid) for i in range(count)]

    def fn(args):
        items = sorted(msgs, key=lambda m: m["at"], reverse=True)
        if args.get("before"):
            items = [m for m in items if m["at"] < args["before"]]
        page = items[:args["limit"]]
        return ToolResult(ok=True, data={"messages": page, "next": page[-1]["at"] if len(page) >= args["limit"] else None})
    return msgs, fn


async def test_teams_busy_chat_with_more_than_one_page_loses_nothing():
    n, st = Native(), {"cursors": {"teams_at": "2026-10-15T09:00:00+00:00"}}
    poller, poll_teams = P(n), hook_for(C.TEAMS)
    msgs, n.results["teams.history"] = history_of(45)
    n.results["teams.chats"] = ToolResult(ok=True, data={"chats": [chat("a", msgs[-1]["at"])]})
    assert await poll_teams(poller, 1, st) == 45
    assert {e.id for e in poller.bus.events} == {f"teams:1:a:a{i}" for i in range(45)}
    assert st["cursors"]["teams_at"].startswith(msgs[-1]["at"][:16]) and not st["cursors"]["teams_resume"]


async def test_teams_backlog_beyond_the_page_budget_continues_next_tick_and_only_then_moves_on():
    n, st = Native(), {"cursors": {"teams_at": "2026-10-15T09:00:00+00:00"}}
    poller, poll_teams = P(n), hook_for(C.TEAMS)
    msgs, n.results["teams.history"] = history_of(100)
    n.results["teams.chats"] = ToolResult(ok=True, data={"chats": [chat("a", msgs[-1]["at"])]})
    assert await poll_teams(poller, 1, st) == 60            # three pages of 20, newest first
    assert st["cursors"]["teams_at"].startswith("2026-10-15T09:00")   # the floor did not jump to the newest
    assert "ms:a" in st["cursors"]["teams_resume"]
    assert await poll_teams(poller, 1, st) == 40            # the older part
    assert {e.id for e in poller.bus.events} == {f"teams:1:a:a{i}" for i in range(100)}
    assert st["cursors"]["teams_at"].startswith(msgs[-1]["at"][:16]) and not st["cursors"]["teams_resume"]


async def test_teams_messages_arriving_while_a_backlog_resumes_are_not_lost():
    n, st = Native(), {"cursors": {"teams_at": "2026-10-15T09:00:00+00:00"}}
    poller, poll_teams = P(n), hook_for(C.TEAMS)
    msgs, n.results["teams.history"] = history_of(70)
    n.results["teams.chats"] = ToolResult(ok=True, data={"chats": [chat("a", msgs[-1]["at"])]})
    assert await poll_teams(poller, 1, st) == 60
    more = msgs + [tmsg("late", "2026-10-15T11:30:00+00:00", "a")]

    def fn(args):
        items = sorted(more, key=lambda m: m["at"], reverse=True)
        if args.get("before"):
            items = [m for m in items if m["at"] < args["before"]]
        page = items[:args["limit"]]
        return ToolResult(ok=True, data={"messages": page, "next": page[-1]["at"] if len(page) >= args["limit"] else None})
    n.results["teams.history"] = fn
    n.results["teams.chats"] = ToolResult(ok=True, data={"chats": [chat("a", "2026-10-15T11:30:00+00:00")]})
    await poll_teams(poller, 1, st)
    assert {e.id for e in poller.bus.events} == {f"teams:1:a:a{i}" for i in range(70)} | {"teams:1:a:late"}
    assert st["cursors"]["teams_at"].startswith("2026-10-15T11:30")


async def test_teams_unordered_chat_page_does_not_move_the_floor_past_unseen_chats():
    n, st = Native(), {"cursors": {"teams_at": "2026-10-15T09:00:00+00:00"}}
    poller, poll_teams = P(n), hook_for(C.TEAMS)
    n.results["teams.chats"] = ToolResult(ok=True, data={"unordered": True, "chats": [
        chat("a", "2026-10-15T12:00:00Z"), chat("b", "2026-10-15T10:00:00Z")]})
    n.results["teams.history"] = lambda args: ToolResult(ok=True, data={"messages": [
        tmsg("x" + args["chat_id"], "2026-10-15T12:00:00Z" if args["chat_id"] == "ms:a" else "2026-10-15T10:00:00Z", args["chat_id"][3:])]})
    assert await poll_teams(poller, 1, st) == 2
    assert st["cursors"]["teams_at"].startswith("2026-10-15T10:00")      # not 12:00: other chats may hold older news


async def test_a_long_baseline_is_finished_silently_before_changes_are_published():
    n, st = Native(), {}
    poller, poll_mail = P(n), hook_for(C.OUTLOOK)
    n.results["mail.inbox_delta"] = ToolResult(ok=True, data={"messages": [], "cursor": "NEXT1", "baseline": True, "more": True})
    assert await poll_mail(poller, 1, st) == 0 and st["cursors"]["outlook_delta"] == "NEXT1"
    # the rest of the baseline arrives as a plain page: still the baseline, so nothing is announced
    n.results["mail.inbox_delta"] = ToolResult(ok=True, data={"messages": [mail("1")], "cursor": "NEXT2", "baseline": False, "more": True})
    assert await poll_mail(poller, 1, st) == 0 and not poller.bus.events
    n.results["mail.inbox_delta"] = ToolResult(ok=True, data={"messages": [mail("2")], "cursor": "DELTA", "baseline": False, "more": False})
    assert await poll_mail(poller, 1, st) == 0 and st["cursors"]["outlook_delta"] == "DELTA"
    n.results["mail.inbox_delta"] = ToolResult(ok=True, data={"messages": [mail("3")], "cursor": "DELTA2", "baseline": False, "more": False})
    assert await poll_mail(poller, 1, st) == 1                  # real changes only after the baseline ended

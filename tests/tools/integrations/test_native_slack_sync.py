"""Backfill window and caps, and the poll safety net, through a provider serving slack.channels/history."""

from datetime import UTC, timedelta

from mavis.domain.errors import FailureKind
from mavis.domain.integrations import ConnectionState, ToolResult
from mavis.domain.policy import Capability
from mavis.tools.integrations.native import slack_events
from mavis.tools.integrations.normalize import normalize_slack, slack_event
from mavis.tools.integrations.poller import POLL_INTERVAL, POLL_KIND, Poller
from tests.tools.integrations.fakes import NOW, FakeBus, FakeProvider

NOW_TS = NOW.timestamp()


def ts(offset_s: float, n: int = 0) -> str:
    return f"{NOW_TS + offset_s:.6f}"[:-1] + str(n % 10)


def m(offset_s, text="hello there", n=0, **kw):
    return {"channel": "", "ts": ts(offset_s, n), "user": "U0ALICE001", "from": "Alice", "text": text,
            "thread_ts": "", "subtype": "", "bot": False, "from_me": False, **kw}


class SlackProvider(FakeProvider):
    """Serves channels and per-channel message lists, honoring oldest, limit and cursor like Slack."""

    def __init__(self, channels, messages, page=100):
        super().__init__()
        self.channels, self.messages, self.page = channels, messages, page
        self.fail: dict[str, ToolResult] = {}

    async def execute(self, user, action, args):
        self.executed.append((user.user_id, action, args))
        if action == "slack.channels":
            return self.fail.get(action) or ToolResult(ok=True, data={"channels": self.channels})
        cid = args["channel"]
        if cid in self.fail:
            return self.fail[cid]
        oldest = float(args.get("oldest") or 0)
        rows = sorted((x for x in self.messages.get(cid, []) if float(x["ts"]) > oldest),
                      key=lambda x: -float(x["ts"]))
        start = int(args.get("cursor") or 0)
        size = min(int(args["limit"]), self.page)
        chunk = rows[start:start + size]
        more = start + size < len(rows)
        return ToolResult(ok=True, data={"channel": cid, "messages": [{**x, "channel": cid} for x in chunk],
                                         "has_more": more, "next_cursor": str(start + size) if more else ""})


def chans():
    return [
        {"id": "C0000GEN01", "name": "general", "kind": "public", "updated": 1000},
        {"id": "C0000NEW01", "name": "launch", "kind": "private", "updated": 5000},
        {"id": "D0000DM001", "name": "Alice", "kind": "dm", "updated": 10},
        {"id": "C0000MPI01", "name": "a-b", "kind": "group_dm", "updated": 20},
    ]


async def test_backfill_window_order_and_event_shape():
    day = 86400
    data = {
        "D0000DM001": [m(-3600, "dm today"), m(-10 * day, "too old")],
        "C0000GEN01": [m(-2 * day, "in window"), m(-6.9 * day, "edge in"), m(-7.1 * day, "edge out"),
                       m(-100, "joined", subtype="channel_join"), m(-90, "bot says", bot=True)],
        "C0000NEW01": [m(-50, "recent", from_me=True)],
    }
    provider, bus = SlackProvider(chans(), data), FakeBus()
    n = await slack_events.backfill(provider, bus, 7, days=7, now=NOW)
    assert n == 4
    texts = [e.payload["text"] for e in bus.events]
    assert set(texts) == {"dm today", "in window", "edge in", "recent"}
    # DMs and group DMs are read before channels, busier channel (newer `updated`) first
    read_order = [a["channel"] for _, act, a in provider.executed if act == "slack.history"]
    assert read_order == ["C0000MPI01", "D0000DM001", "C0000NEW01", "C0000GEN01"]
    dm = next(e for e in bus.events if e.payload["text"] == "dm today")
    assert dm.payload["channel_type"] == "dm" and dm.payload["channel_name"] == "Alice"
    ev = next(e for e in bus.events if e.payload["text"] == "recent")
    assert ev.payload["from_me"] is True and ev.id.startswith("slack:7:C0000NEW01:")
    assert abs((ev.occurred_at - (NOW - timedelta(seconds=50))).total_seconds()) < 1
    for e in bus.events:
        assert e.occurred_at.tzinfo is UTC
        assert {k: e.payload[k] for k in normalize_slack(e.payload)} == normalize_slack(e.payload)
        assert slack_event(7, {**e.payload, "channel": e.payload["channel"]}, "x").id == e.id


async def test_backfill_per_channel_cap_spans_pages():
    rows = [m(-i - 1, f"msg {i}", n=i) for i in range(250)]
    provider, bus = SlackProvider([chans()[0]], {"C0000GEN01": rows}, page=100), FakeBus()
    n = await slack_events.backfill(provider, bus, 7, days=7, now=NOW, per_channel=120)
    assert n == 120 and len(provider.executed) == 1 + 2  # channels + two history pages
    assert {e.payload["text"] for e in bus.events} == {f"msg {i}" for i in range(120)}


async def test_backfill_total_cap_stops_across_channels_dms_first():
    data = {"D0000DM001": [m(-i - 1, f"dm {i}", n=i) for i in range(30)],
            "C0000NEW01": [m(-i - 1, f"ch {i}", n=i) for i in range(30)]}
    provider, bus = SlackProvider(chans(), data), FakeBus()
    n = await slack_events.backfill(provider, bus, 7, days=7, now=NOW, per_channel=25, total=40)
    assert n == 40
    kinds = [e.payload["text"].split()[0] for e in bus.events]
    assert kinds.count("dm") == 25 and kinds.count("ch") == 15


async def test_backfill_survives_a_failing_channel_and_is_idempotent():
    data = {"D0000DM001": [m(-5, "ok one")], "C0000NEW01": [m(-5, "ok two")]}
    provider, bus = SlackProvider(chans(), data), FakeBus()
    provider.fail["C0000MPI01"] = ToolResult(ok=False, error="Slack error channel_not_found",
                                             error_kind=FailureKind.NOT_FOUND)
    assert await slack_events.backfill(provider, bus, 7, days=7, now=NOW) == 2
    assert await slack_events.backfill(provider, bus, 7, days=7, now=NOW) == 0  # bus dedupes
    provider.fail["slack.channels"] = ToolResult(ok=False, error="x", error_kind=FailureKind.AUTH)
    assert await slack_events.backfill(provider, bus, 7, days=7, now=NOW) == 0


# --- poll -------------------------------------------------------------------------------------------


def make_poller(provider, bus, state, rec):
    from mavis.tools.integrations.connections import ConnectionCache

    return Poller(provider=provider, cache=ConnectionCache(provider, ttl_s=60), bus=bus, state=state,
                  schedule=rec.schedule, clock=lambda: NOW)


async def test_poll_first_sight_looks_10_minutes_back_then_advances_cursor(state, rec):
    data = {"C0000GEN01": [m(-300, "recent enough"), m(-1200, "older than lookback")]}
    provider, bus = SlackProvider(chans()[:1], data), FakeBus()
    provider.set_state(1, Capability.SLACK, ConnectionState.ACTIVE)
    await state.update(1, {"polling": {"slack": True}})
    poller = make_poller(provider, bus, state, rec)
    assert await poller.poll(1, Capability.SLACK) == 1
    cur = (await state.get(1))["cursors"]
    assert cur["slack_ts"]["C0000GEN01"] == ts(-300) and cur["slack_offset"] == 1
    assert rec.scheduled == [(1, NOW + POLL_INTERVAL, "slack", POLL_KIND)]
    # next tick asks only after the cursor and publishes nothing new
    assert await poller.poll(1, Capability.SLACK) == 0
    assert provider.executed[-1][2]["oldest"] == ts(-300)
    data["C0000GEN01"].append(m(-60, "fresh"))
    assert await poller.poll(1, Capability.SLACK) == 1
    assert (await state.get(1))["cursors"]["slack_ts"]["C0000GEN01"] == ts(-60)


async def test_poll_round_robin_batches_channels():
    many = [{"id": f"C{i:010d}", "name": f"c{i}", "kind": "public", "updated": 100 - i} for i in range(60)]
    data = {c["id"]: [m(-30, f"hi {c['id']}")] for c in many}
    provider, bus = SlackProvider(many, data), FakeBus()
    seen = set()
    cursors, offset = {}, 0
    for _ in range(3):
        n, cursors, offset = await slack_events.poll_messages(provider, bus, 1, cursors, offset, now=NOW)
        seen |= {a["channel"] for _, act, a in provider.executed if act == "slack.history"}
    assert len(seen) == 60 and len(bus.events) == 60 and offset == 75


async def test_poll_bots_and_system_messages_advance_cursor_without_events():
    data = {"C0000GEN01": [m(-100, "joined", subtype="channel_join"), m(-90, "ci", bot=True)]}
    provider, bus = SlackProvider(chans()[:1], data), FakeBus()
    n, cursors, _ = await slack_events.poll_messages(provider, bus, 1, {}, 0, now=NOW)
    assert n == 0 and bus.events == [] and cursors["C0000GEN01"] == ts(-90)


async def test_poll_rate_limit_keeps_cursors_and_auth_raises():
    provider, bus = SlackProvider(chans(), {"C0000GEN01": [m(-30, "x")]}), FakeBus()
    provider.fail["D0000DM001"] = ToolResult(ok=False, error="r", error_kind=FailureKind.RATE_LIMITED)
    n, cursors, _ = await slack_events.poll_messages(provider, bus, 1, {}, 0, now=NOW)
    assert n == 0 and set(cursors) <= {"C0000MPI01"}  # the limit stopped the batch at the DM, nothing after
    provider.fail["D0000DM001"] = ToolResult(ok=False, error="Slack unauthorized",
                                             error_kind=FailureKind.AUTH)
    try:
        await slack_events.poll_messages(provider, bus, 1, {}, 0, now=NOW)
    except slack_events.PollAuthError:
        pass
    else:
        raise AssertionError("auth failure must surface")


async def test_poll_dedupes_against_webhook_events(state, rec):
    data = {"C0000GEN01": [m(-30, "same message")]}
    provider, bus = SlackProvider(chans()[:1], data), FakeBus()
    pushed = slack_events.build_event(1, {"ts": ts(-30), "user": "U0ALICE001", "text": "same message"},
                                      "C0000GEN01", "slack", from_me=False)
    assert await bus.publish(pushed)
    provider.set_state(1, Capability.SLACK, ConnectionState.ACTIVE)
    await state.update(1, {"polling": {"slack": True}})
    assert await make_poller(provider, bus, state, rec).poll(1, Capability.SLACK) == 0
    assert len(bus.events) == 1


async def test_poll_auth_error_uses_reconnect_path(state, rec):
    provider, bus = SlackProvider(chans()[:1], {}), FakeBus()
    provider.fail["slack.channels"] = ToolResult(ok=False, error="Slack unauthorized (invalid_auth)",
                                                 error_kind=FailureKind.AUTH)
    provider.set_state(1, Capability.SLACK, ConnectionState.ACTIVE)
    await state.update(1, {"polling": {"slack": True}})
    assert await make_poller(provider, bus, state, rec).poll(1, Capability.SLACK) == 0
    assert rec.scheduled  # status still ACTIVE: the chain keeps going

"""SlackExecutor against httpx.MockTransport fixtures shaped like the Slack Web API."""

import httpx
import pytest

from mavis.domain.errors import FailureKind
from mavis.domain.integrations import UserRef
from mavis.tools.integrations.native.base import NativeProvider, ReauthRequired
from mavis.tools.integrations.native.slack import SlackExecutor
from mavis.tools.integrations.normalize import slack_event

USER = UserRef(user_id=7)
TEAM, ME = "T0TEAM001", "U0ME000001"


class Tokens:
    def __init__(self, account=None, fail=False):
        self.calls: list[bool] = []
        self.acct = {"team_id": TEAM, "slack_user_id": ME} if account is None else account
        self.fail = fail
        self.tokens = ["xoxp-old", "xoxp-new"]

    async def access_token(self, user_id, provider, *, force=False):
        assert provider is NativeProvider.SLACK
        if self.fail:
            raise ReauthRequired
        self.calls.append(force)
        return self.tokens[1] if force else self.tokens[0]

    async def account(self, user_id, provider):
        return self.acct


class Api:
    """Routes by Slack method name; each value is a dict, a list of responses, or a callable(request)."""

    def __init__(self, **routes):
        self.routes, self.seen, self.sleeps = routes, [], []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        form = dict(httpx.QueryParams(request.content.decode()))
        self.seen.append((method, form, request.headers["authorization"]))
        r = self.routes[method]
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if callable(r):
            r = r(request)
        if isinstance(r, httpx.Response):
            return r
        return httpx.Response(200, json=r)

    async def sleep(self, s):
        self.sleeps.append(s)

    def executor(self, tokens=None):
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return SlackExecutor(tokens or Tokens(), client, sleep=self.sleep)

    def calls(self, method):
        return [f for m, f, _ in self.seen if m == method]


def chan(cid, name, **kw):
    return {"id": cid, "name": name, "is_channel": True, "is_member": True, "is_archived": False, **kw}


def user_info(uid, display, email=""):
    return {"ok": True, "user": {"id": uid, "name": display.lower(), "real_name": display,
                                 "profile": {"display_name": display, "email": email}}}


def msg(ts, text, user="U0ALICE01", **kw):
    return {"type": "message", "user": user, "text": text, "ts": ts, **kw}


async def test_channels_paginate_filter_and_name_dms():
    pages = [
        {"ok": True, "channels": [chan("C0000GEN01", "general"), chan("C0000OFF01", "offtopic", is_member=False),
                                  chan("C0000OLD01", "old", is_archived=True)],
         "response_metadata": {"next_cursor": "dXNlcjpVMDYx"}},
        {"ok": True, "channels": [
            {"id": "G0000PRI01", "name": "leads", "is_private": True, "is_group": True},
            {"id": "D0000DM001", "is_im": True, "user": "U0ALICE01", "updated": 1760000000000},
            {"id": "C0000MPI01", "name": "mpdm-a--b", "is_mpim": True}],
         "response_metadata": {"next_cursor": ""}},
    ]
    api = Api(**{"conversations.list": pages, "users.info": user_info("U0ALICE01", "Alice", "a@x.com")})
    ex = api.executor()
    res = await ex.execute(USER, "slack.channels", {})
    assert res.ok
    by_id = {c["id"]: c for c in res.data["channels"]}
    assert set(by_id) == {"C0000GEN01", "G0000PRI01", "D0000DM001", "C0000MPI01"}
    assert by_id["D0000DM001"]["name"] == "Alice" and by_id["D0000DM001"]["kind"] == "dm"
    assert by_id["G0000PRI01"]["kind"] == "private" and by_id["C0000MPI01"]["kind"] == "group_dm"
    first, second = api.calls("conversations.list")
    assert first["types"] == "public_channel,private_channel,im,mpim" and first["exclude_archived"] == "true"
    assert "cursor" not in first and second["cursor"] == "dXNlcjpVMDYx"
    assert all(a == "Bearer xoxp-old" for _, _, a in api.seen)
    await ex.execute(USER, "slack.channels", {})  # channel list and user are cached
    assert len(api.calls("conversations.list")) == 2 and len(api.calls("users.info")) == 1


async def test_user_cache_expires_per_ttl():
    now = [0.0]
    api = Api(**{"users.info": user_info("U0ALICE01", "Alice")})
    ex = SlackExecutor(Tokens(), httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
                       sleep=api.sleep, clock=lambda: now[0])
    await ex.user_info(7, TEAM, "U0ALICE01")
    await ex.user_info(7, TEAM, "U0ALICE01")
    assert len(api.calls("users.info")) == 1
    now[0] = 4000
    await ex.user_info(7, TEAM, "U0ALICE01")
    assert len(api.calls("users.info")) == 2
    await ex.user_info(7, "T0OTHER001", "U0ALICE01")  # the cache is per team
    assert len(api.calls("users.info")) == 3


async def test_history_by_name_resolves_users_and_parses_through_normalize():
    history = {"ok": True, "has_more": True, "response_metadata": {"next_cursor": "bmV4dA=="},
               "messages": [msg("1760000200.000200", "ship it", user=ME),
                            msg("1760000100.000100", "see <https://x.dev|PR>", thread_ts="1760000000.000050"),
                            {"type": "message", "subtype": "channel_join", "user": "U0BOB00001",
                             "text": "<@U0BOB00001> has joined", "ts": "1760000050.000000"}]}
    api = Api(**{"conversations.list": {"ok": True, "channels": [chan("C0000GEN01", "general")]},
                 "conversations.history": history,
                 "users.info": lambda r: user_info("U0ALICE01", "Alice", "alice@x.com")})
    res = await api.executor().execute(USER, "slack.history", {"channel": "#General", "limit": 30})
    assert res.ok
    call = api.calls("conversations.history")[0]
    assert call["channel"] == "C0000GEN01" and call["limit"] == "30"
    d = res.data
    assert d["has_more"] and d["next_cursor"] == "bmV4dA=="
    mine, theirs, join = d["messages"]
    assert mine["from_me"] and not theirs["from_me"] and theirs["from"] == "Alice"
    assert theirs["from_email"] == "alice@x.com" and theirs["thread_ts"] == "1760000000.000050"
    assert join["subtype"] == "channel_join"
    event = slack_event(7, theirs, "test")
    assert event.id == "slack:7:C0000GEN01:1760000100.000100"
    assert event.payload["text"].startswith("see") and event.payload["thread_ts"] == "1760000000.000050"


async def test_history_with_thread_uses_replies_and_forwards_window():
    api = Api(**{"conversations.replies": {"ok": True, "messages": [msg("1760000000.000050", "parent"),
                                                                    msg("1760000010.000060", "reply")]},
                 "users.info": user_info("U0ALICE01", "Alice")})
    res = await api.executor().execute(USER, "slack.history", {
        "channel": "C0000GEN01", "thread_ts": "1760000000.000050", "oldest": "1759999000.000000"})
    assert res.ok and len(res.data["messages"]) == 2
    call = api.calls("conversations.replies")[0]
    assert call["ts"] == "1760000000.000050" and call["oldest"] == "1759999000.000000"
    assert api.calls("conversations.history") == []


async def test_history_unknown_channel_name_is_not_found():
    api = Api(**{"conversations.list": {"ok": True, "channels": [chan("C0000GEN01", "general")]}})
    res = await api.executor().execute(USER, "slack.history", {"channel": "nope"})
    assert not res.ok and res.error_kind is FailureKind.NOT_FOUND


async def test_send_posts_as_user_with_thread():
    api = Api(**{"chat.postMessage": {"ok": True, "channel": "C0000GEN01", "ts": "1760000300.000300"}})
    res = await api.executor().execute(USER, "slack.send", {
        "channel": "C0000GEN01", "text": "on it", "thread_ts": "1760000100.000100"})
    assert res.ok and res.data == {"ok": True, "channel": "C0000GEN01", "ts": "1760000300.000300"}
    method, form, auth = api.seen[0]
    assert method == "chat.postMessage" and auth == "Bearer xoxp-old"
    assert form == {"channel": "C0000GEN01", "text": "on it", "thread_ts": "1760000100.000100"}


async def test_send_blank_text_never_calls_slack():
    api = Api()
    res = await api.executor().execute(USER, "slack.send", {"channel": "C0000GEN01", "text": "  "})
    assert res.error_kind is FailureKind.INVALID_ARGUMENT and api.seen == []


@pytest.mark.parametrize("code,kind", [
    ("channel_not_found", FailureKind.NOT_FOUND),
    ("thread_not_found", FailureKind.NOT_FOUND),
    ("not_in_channel", FailureKind.NOT_FOUND),
    ("msg_too_long", FailureKind.INVALID_ARGUMENT),
    ("invalid_arguments", FailureKind.INVALID_ARGUMENT),
    ("is_archived", FailureKind.INVALID_ARGUMENT),
    ("account_inactive", FailureKind.AUTH),
    ("token_revoked", FailureKind.AUTH),
    ("missing_scope", FailureKind.AUTH),
    ("internal_error", FailureKind.UNAVAILABLE),
    ("some_new_unknown_code", FailureKind.UNKNOWN),
])
async def test_ok_false_codes_map_to_kinds(code, kind):
    api = Api(**{"chat.postMessage": {"ok": False, "error": code}})
    res = await api.executor().execute(USER, "slack.send", {"channel": "C0000GEN01", "text": "hi"})
    assert not res.ok and res.error_kind is kind
    if kind is FailureKind.AUTH:
        assert "unauthorized" in res.error  # the poller's is_auth_error markers fire


async def test_auth_retries_once_with_forced_token_then_succeeds():
    seq = [{"ok": False, "error": "invalid_auth"}, {"ok": True, "channel": "C0000GEN01", "ts": "1.1"}]
    api, tokens = Api(**{"chat.postMessage": seq}), Tokens()
    res = await api.executor(tokens).execute(USER, "slack.send", {"channel": "C0000GEN01", "text": "hi"})
    assert res.ok and tokens.calls == [False, True]
    assert [a for _, _, a in api.seen] == ["Bearer xoxp-old", "Bearer xoxp-new"]


async def test_auth_fails_after_one_retry():
    api, tokens = Api(**{"chat.postMessage": {"ok": False, "error": "invalid_auth"}}), Tokens()
    res = await api.executor(tokens).execute(USER, "slack.send", {"channel": "C0000GEN01", "text": "hi"})
    assert res.error_kind is FailureKind.AUTH and tokens.calls == [False, True] and len(api.seen) == 2


async def test_missing_grant_is_auth():
    res = await Api().executor(Tokens(fail=True)).execute(USER, "slack.channels", {})
    assert res.error_kind is FailureKind.AUTH
    res = await Api().executor(Tokens(account={})).execute(USER, "slack.channels", {})
    assert res.error_kind is FailureKind.AUTH


async def test_429_honors_retry_after_once():
    seq = [httpx.Response(429, headers={"Retry-After": "3"}, json={"ok": False, "error": "ratelimited"}),
           {"ok": True, "channel": "C0000GEN01", "ts": "1.1"}]
    api = Api(**{"chat.postMessage": seq})
    res = await api.executor().execute(USER, "slack.send", {"channel": "C0000GEN01", "text": "hi"})
    assert res.ok and api.sleeps == [3.0]


async def test_429_twice_or_long_wait_is_rate_limited():
    r429 = httpx.Response(429, headers={"Retry-After": "2"}, json={"ok": False, "error": "ratelimited"})
    api = Api(**{"chat.postMessage": [r429]})
    res = await api.executor().execute(USER, "slack.send", {"channel": "C0000GEN01", "text": "hi"})
    assert res.error_kind is FailureKind.RATE_LIMITED and api.sleeps == [2.0] and len(api.seen) == 2
    long = httpx.Response(429, headers={"Retry-After": "45"}, json={"ok": False, "error": "ratelimited"})
    api = Api(**{"chat.postMessage": [long]})
    res = await api.executor().execute(USER, "slack.send", {"channel": "C0000GEN01", "text": "hi"})
    assert res.error_kind is FailureKind.RATE_LIMITED and api.sleeps == [] and len(api.seen) == 1


async def test_ratelimited_inside_a_200_body_also_waits_once():
    seq = [{"ok": False, "error": "ratelimited"}, {"ok": True, "channel": "C0000GEN01", "ts": "1.1"}]
    api = Api(**{"chat.postMessage": seq})
    res = await api.executor().execute(USER, "slack.send", {"channel": "C0000GEN01", "text": "hi"})
    assert res.ok and len(api.sleeps) == 1


async def test_5xx_retries_once_then_unavailable():
    api = Api(**{"chat.postMessage": [httpx.Response(503), {"ok": True, "channel": "C0000GEN01", "ts": "1.1"}]})
    assert (await api.executor().execute(USER, "slack.send", {"channel": "C0000GEN01", "text": "x"})).ok
    api = Api(**{"chat.postMessage": [httpx.Response(502)]})
    res = await api.executor().execute(USER, "slack.send", {"channel": "C0000GEN01", "text": "x"})
    assert res.error_kind is FailureKind.UNAVAILABLE and len(api.seen) == 2


async def test_transport_error_retries_once():
    calls = []

    def boom(request):
        calls.append(1)
        raise httpx.ConnectError("down")

    api = Api(**{"chat.postMessage": boom})
    res = await api.executor().execute(USER, "slack.send", {"channel": "C0000GEN01", "text": "x"})
    assert res.error_kind is FailureKind.UNAVAILABLE and len(calls) == 2


async def test_unsupported_action_and_handles():
    ex = Api().executor()
    assert ex.handles("slack.history") and not ex.handles("mail.search") and not ex.handles("slack.users")
    assert ex.provider is NativeProvider.SLACK
    assert (await ex.execute(USER, "mail.search", {})).error_kind is FailureKind.INVALID_ARGUMENT

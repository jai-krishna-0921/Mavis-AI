import asyncio

import httpx
import pytest

from mavis.tools.integrations.native.base import NativeProvider, ReauthRequired

from .graphfake import (
    FakeGraph,
    GraphTokens,
    body_of,
    graph_chat,
    graph_chat_message,
    graph_error,
    graph_event,
    graph_item,
    graph_message,
    graph_removed,
    page,
    query_of,
)


async def test_routes_scripts_and_records():
    g = FakeGraph()
    g.on("GET", r"/me/messages$", [graph_error(429, "TooManyRequests", headers={"Retry-After": "2"}),
                                    httpx.Response(200, json=page([graph_message("m1")]))])
    async with g.client() as c:
        first = await c.get("https://graph.microsoft.com/v1.0/me/messages")
        second = await c.get("https://graph.microsoft.com/v1.0/me/messages")
    assert first.status_code == 429 and first.json()["error"]["code"] == "TooManyRequests"
    assert first.headers["Retry-After"] == "2"
    assert second.json()["value"][0]["id"] == "m1"
    assert len(g.calls("GET", r"/me/messages")) == 2


async def test_unrouted_request_is_a_graph_shaped_404_and_callables_see_the_request():
    g = FakeGraph()
    g.on("POST", r"/chats/c1/messages$", lambda r: httpx.Response(201, json={"echo": body_of(r)}))
    async with g.client() as c:
        miss = await c.get("https://graph.microsoft.com/v1.0/me/nothing")
        hit = await c.post("https://graph.microsoft.com/v1.0/chats/c1/messages", json={"a": 1})
    assert miss.status_code == 404 and set(miss.json()["error"]) >= {"code", "message"}
    assert miss.json()["error"]["code"] == "UnroutedInFakeGraph" and len(g.unrouted) == 1
    with pytest.raises(AssertionError, match="GET /v1.0/me/nothing"):
        g.assert_all_routed()
    assert hit.json() == {"echo": {"a": 1}}


def test_error_body_shape():
    r = graph_error(503, "serviceNotAvailable", "down", headers={"Retry-After": "7"})
    err = r.json()["error"]
    assert err["code"] == "serviceNotAvailable" and err["message"] == "down"
    assert err["innerError"]["request-id"]
    assert r.headers["Retry-After"] == "7"


def test_page_paging_links():
    assert page([1]) == {"value": [1]}
    p = page([1], next_link="https://graph.microsoft.com/v1.0/x?$skiptoken=a")
    assert p["@odata.nextLink"].endswith("a") and "@odata.deltaLink" not in p
    d = page([], delta_link="https://graph.microsoft.com/v1.0/x/delta?$deltatoken=b")
    assert d["@odata.deltaLink"].endswith("b") and "@odata.nextLink" not in d


def test_message_builder_matches_graph_shape():
    m = graph_message("m1", sender="Alice <alice@acme.com>", subject="Hi", text="Body", unread=True,
                      importance="high", classification="focused", folder="inbox")
    assert m["from"]["emailAddress"] == {"name": "Alice", "address": "alice@acme.com"}
    assert m["sender"] == m["from"]
    assert m["isRead"] is False and m["importance"] == "high" and m["inferenceClassification"] == "focused"
    assert m["body"]["contentType"] == "text" and m["conversationId"]
    assert m["internetMessageHeaders"][0]["name"]   # authentication results header present
    assert m["toRecipients"] == [{"emailAddress": {"name": "me@orbit.test", "address": "me@orbit.test"}}]


def test_message_builder_variants():
    bare = graph_message("m2", sender="bob@acme.com", html="<p>x</p>", unread=False, categories=("Red",))
    assert bare["from"]["emailAddress"] == {"name": "", "address": "bob@acme.com"}
    assert bare["body"] == {"contentType": "html", "content": "<p>x</p>"}
    assert bare["isRead"] is True and bare["categories"] == ["Red"]
    assert len(graph_message("m3", text="x" * 500)["bodyPreview"]) == 100


def test_event_builder_matches_graph_shape():
    e = graph_event("e1", subject="Sync", start="2026-10-06T09:00:00", end="2026-10-06T09:30:00",
                    attendees=("bob@acme.com",), is_online=True)
    assert graph_event("e0", start="2026-10-06", end="2026-10-07")["start"]["dateTime"] == (
        "2026-10-06T00:00:00.0000000"
    )
    assert e["start"] == {"dateTime": "2026-10-06T09:00:00.0000000", "timeZone": "UTC"}
    assert e["end"]["dateTime"].startswith("2026-10-06T09:30:00")
    assert e["attendees"][0] == {"emailAddress": {"name": "", "address": "bob@acme.com"},
                                 "status": {"response": "none"}, "type": "required"}
    assert e["isOnlineMeeting"] is True and e["onlineMeeting"]["joinUrl"]
    assert e["isCancelled"] is False and e["responseStatus"]["response"] == "accepted"
    assert {"showAs", "isAllDay", "location", "bodyPreview", "webLink", "organizer"} <= set(e)
    plain = graph_event("e2", subject="x", start="2026-10-06", end="2026-10-07", all_day=True, cancelled=True)
    assert plain["isAllDay"] is True and plain["isCancelled"] is True and plain["onlineMeeting"] is None


def test_chat_builders_match_graph_shape():
    c = graph_chat("c1", topic="Launch", chat_type="group")
    assert c["chatType"] == "group" and c["topic"] == "Launch"
    assert c["members"][0]["displayName"] == "Alice" and c["members"][0]["userId"] == "u-alice"
    assert c["members"][-1]["userId"] == "oid-me"
    assert c["members"][0]["email"] == "alice@acme.com"
    assert c["lastMessagePreview"]["from"]["user"]["id"] == "u-alice"
    assert c["lastMessagePreview"]["createdDateTime"] == "2026-10-05T10:00:00Z"
    m = graph_chat_message("cm1", "c1", html=True, text="<b>hi</b>")
    assert m["chatId"] == "c1" and m["messageType"] == "message"
    assert m["from"]["user"] == {"id": "u-alice", "displayName": "Alice", "userIdentityType": "aadUser"}
    assert m["body"] == {"contentType": "html", "content": "<b>hi</b>"}
    sys = graph_chat_message("cm2", "c1", kind="systemEventMessage")
    assert sys["messageType"] == "systemEventMessage" and sys["from"] is None


def test_drive_item_builder_matches_graph_shape():
    i = graph_item("i1", name="a.txt")
    assert i["file"] == {"mimeType": "text/plain"} and i["size"] == 1000 and "remoteItem" not in i
    assert i["lastModifiedBy"]["user"]["displayName"] and i["webUrl"]
    s = graph_item("i2", name="b.docx", shared_by="Carol")
    assert s["remoteItem"]["shared"]["owner"]["user"]["displayName"] == "Carol"


async def test_tokens_script_force_and_revocation():
    t = GraphTokens(account_data={"upn": "me@orbit.test"})
    assert await t.access_token(1, NativeProvider.MICROSOFT) == "tok"
    assert await t.access_token(1, NativeProvider.MICROSOFT, force=True) == "tok-forced"
    assert t.calls == [False, True]
    assert await t.account(1, NativeProvider.MICROSOFT) == {"upn": "me@orbit.test"}
    with pytest.raises(AssertionError):
        await t.access_token(1, NativeProvider.GOOGLE)
    with pytest.raises(ReauthRequired):
        await GraphTokens(revoked=True).access_token(1, NativeProvider.MICROSOFT)
    assert await GraphTokens(account_data=None).account(1, NativeProvider.MICROSOFT) is None


def test_assert_all_routed_passes_when_everything_matched():
    g = FakeGraph().on("GET", r"/me$", httpx.Response(200, json={}))

    async def run():
        async with g.client() as c:
            await c.get("https://graph.microsoft.com/v1.0/me")

    asyncio.run(run())
    g.assert_all_routed()


async def test_on_request_fields_follow_select_and_expand():
    g = FakeGraph()
    msg = graph_message("m1", html="<p>Hi <b>there</b></p>")
    g.on("GET", r"/me/messages$", httpx.Response(200, json=page([msg])))
    g.on("GET", r"/me/chats$", httpx.Response(200, json=page([graph_chat("c1")])))
    base = "https://graph.microsoft.com/v1.0"
    async with g.client() as c:
        plain = (await c.get(f"{base}/me/messages")).json()["value"][0]
        withh = (await c.get(f"{base}/me/messages?$select=id,internetMessageHeaders")).json()["value"][0]
        prefer = {"Prefer": 'outlook.body-content-type="text"'}
        text = (await c.get(f"{base}/me/messages", headers=prefer)).json()
        bare = (await c.get(f"{base}/me/chats")).json()["value"][0]
        members = (await c.get(f"{base}/me/chats?$expand=members")).json()["value"][0]
        preview = (await c.get(f"{base}/me/chats?$expand=lastMessagePreview")).json()["value"][0]
        both = (await c.get(f"{base}/me/chats?$expand=members,lastMessagePreview")).json()["value"][0]
    assert "internetMessageHeaders" not in plain and "internetMessageHeaders" in withh
    assert plain["body"]["contentType"] == "html"
    assert text["value"][0]["body"] == {"contentType": "text", "content": "Hi there"}
    assert "members" not in bare and "lastMessagePreview" not in bare
    assert "members" in members and "lastMessagePreview" not in members
    assert "lastMessagePreview" in preview and "members" not in preview
    assert "members" in both and "lastMessagePreview" in both
    assert msg["internetMessageHeaders"]  # the builder output itself is not mutated


def test_one_on_one_lists_both_participants_and_me_is_configurable():
    c = graph_chat("c1")
    assert [m["userId"] for m in c["members"]] == ["u-alice", "oid-me"]
    other = graph_chat("c2", me=("Zed", "zed@acme.com", "u-zed"))
    assert other["members"][-1]["email"] == "zed@acme.com"
    solo = graph_chat("c3", members=(("Me", "me@orbit.test", "oid-me"),))
    assert [m["userId"] for m in solo["members"]] == ["oid-me"]


def test_drive_item_folder_and_shared_variants():
    f = graph_item("f1", name="Docs", folder=True, child_count=5)
    assert f["folder"] == {"childCount": 5} and "file" not in f
    assert f["parentReference"]["driveId"] == "d-me"
    s = graph_item("i2", name="b.docx", shared_by="Carol", mime="application/vnd.ms-word")
    r = s["remoteItem"]
    assert r["parentReference"]["driveId"] == "d-remote" != s["parentReference"]["driveId"]
    assert r["name"] == "b.docx" and r["file"]["mimeType"] == "application/vnd.ms-word"
    assert "folder" in graph_item("f2", folder=True, shared_by="Carol")["remoteItem"]


def test_removed_tombstone():
    assert graph_removed("m9") == {"id": "m9", "@removed": {"reason": "deleted"}}


async def test_query_matcher_and_query_of():
    g = FakeGraph()
    g.on("GET", r"/me/messages$", httpx.Response(200, json=page([])), query={"$search": '"from:alice"'})
    unread = httpx.Response(200, json=page([graph_message("m1")]))
    g.on("GET", r"/me/messages$", unread, query={"$filter": "isRead eq false"})
    base = "https://graph.microsoft.com/v1.0/me/messages"
    async with g.client() as c:
        a = await c.get(base, params={"$filter": "isRead eq false", "$top": "50"})
        b = await c.get(base, params={"$search": '"from:alice"'})
        miss = await c.get(base, params={"$filter": "isRead eq true"})
    assert a.json()["value"][0]["id"] == "m1" and b.json()["value"] == []
    assert miss.json()["error"]["code"] == "UnroutedInFakeGraph"
    first = g.calls("GET", r"/me/messages", query={"$filter": "isRead eq false"})
    assert len(first) == 1 and query_of(first[0]) == {"$filter": "isRead eq false", "$top": "50"}
    # a real executor must never send $search together with $filter
    assert not [r for r in g.requests if {"$search", "$filter"} <= set(query_of(r))]

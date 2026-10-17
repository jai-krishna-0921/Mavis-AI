from datetime import UTC, datetime

import httpx
import pytest

from .notionfake import BOT_ID, HEADERS, FakeNotion

BASE = "https://api.notion.com/v1"


def para(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def title(text: str) -> dict:
    return {"title": [{"text": {"content": text}}]}


async def make_page(c, name: str = "p") -> str:
    r = await c.post(f"{BASE}/pages", json={"parent": {"type": "page_id", "page_id": "root"},
                                             "properties": {"title": title(name)}})
    return r.json()["id"]


async def make_ds(c):
    db = (await c.post(f"{BASE}/databases", json={
        "parent": {"type": "page_id", "page_id": "root"},
        "title": [{"type": "text", "text": {"content": "Todos"}}],
        "initial_data_source": {"properties": {"Name": {"type": "title", "title": {}},
                                               "Done": {"type": "checkbox", "checkbox": {}}}},
    })).json()
    return db, db["data_sources"][0]["id"]


async def test_query_filters_by_minute_truncated_last_edited_time():
    n = FakeNotion()
    n.clock = lambda: datetime(2026, 10, 17, 12, 5, 59, tzinfo=UTC)
    async with n.client(HEADERS) as c:
        _, ds = await make_ds(c)
        a = (await c.post(f"{BASE}/pages", json={"parent": {"type": "data_source_id", "data_source_id": ds},
                                                  "properties": {"Name": title("a")}})).json()
        n.clock = lambda: datetime(2026, 10, 17, 12, 7, 1, tzinfo=UTC)
        b = (await c.post(f"{BASE}/pages", json={"parent": {"type": "data_source_id", "data_source_id": ds},
                                                  "properties": {"Name": title("b")}})).json()
        assert a["last_edited_time"] == "2026-10-17T12:05:00.000Z"
        q = {"filter": {"timestamp": "last_edited_time",
                        "last_edited_time": {"on_or_after": "2026-10-17T12:06:00.000Z"}},
             "sorts": [{"timestamp": "last_edited_time", "direction": "ascending"}]}
        r = (await c.post(f"{BASE}/data_sources/{ds}/query", json=q)).json()
        everything = (await c.post(f"{BASE}/data_sources/{ds}/query", json={
            "sorts": [{"timestamp": "last_edited_time", "direction": "ascending"}]})).json()
    assert [p["id"] for p in r["results"]] == [b["id"]]
    assert r["results"][0]["last_edited_by"] == {"object": "user", "id": BOT_ID}
    assert [p["id"] for p in everything["results"]] == [a["id"], b["id"]]
    assert r["has_more"] is False and r["next_cursor"] is None


async def test_query_paginates():
    n = FakeNotion()
    async with n.client(HEADERS) as c:
        _, ds = await make_ds(c)
        for i in range(3):
            await c.post(f"{BASE}/pages", json={"parent": {"type": "data_source_id", "data_source_id": ds},
                                                 "properties": {"Name": title(str(i))}})
        first = (await c.post(f"{BASE}/data_sources/{ds}/query", json={"page_size": 2})).json()
        rest = (await c.post(f"{BASE}/data_sources/{ds}/query",
                             json={"page_size": 2, "start_cursor": first["next_cursor"]})).json()
    assert len(first["results"]) == 2 and first["has_more"] and len(rest["results"]) == 1


async def test_append_children_position_start_inserts_first():
    n = FakeNotion()
    async with n.client(HEADERS) as c:
        p1 = await make_page(c)
        await c.patch(f"{BASE}/blocks/{p1}/children", json={"children": [para("one"), para("two")]})
        r = (await c.patch(f"{BASE}/blocks/{p1}/children",
                           json={"children": [para("zero")], "position": {"type": "start"}})).json()
        first_id = n.blocks[p1][1]["id"]
        await c.patch(f"{BASE}/blocks/{p1}/children", json={
            "children": [para("one-and-half")],
            "position": {"type": "after_block", "after_block": {"id": first_id}}})
        listed = (await c.get(f"{BASE}/blocks/{p1}/children")).json()
        await c.delete(f"{BASE}/blocks/{first_id}")
        after = (await c.get(f"{BASE}/blocks/{p1}/children")).json()

    def texts(res):
        return [b["paragraph"]["rich_text"][0]["text"]["content"] for b in res["results"]]

    assert texts(r) == ["zero"]
    assert texts(listed) == ["zero", "one", "one-and-half", "two"]
    assert texts(after) == ["zero", "one-and-half", "two"]


async def test_children_over_100_is_validation_error():
    n = FakeNotion()
    async with n.client(HEADERS) as c:
        p1 = await make_page(c)
        r = await c.patch(f"{BASE}/blocks/{p1}/children", json={"children": [para("x")] * 101})
        long = await c.patch(f"{BASE}/blocks/{p1}/children", json={"children": [para("x" * 2001)]})
        ok = await c.patch(f"{BASE}/blocks/{p1}/children", json={"children": [para("x" * 2000)] * 100})
    assert r.status_code == 400 and r.json()["code"] == "validation_error"
    assert long.status_code == 400 and long.json()["code"] == "validation_error"
    assert ok.status_code == 200 and len(n.blocks[p1]) == 100


async def test_user_edit_sets_editor():
    n = FakeNotion()
    async with n.client(HEADERS) as c:
        _, ds = await make_ds(c)
        p = (await c.post(f"{BASE}/pages", json={"parent": {"type": "data_source_id", "data_source_id": ds},
                                                  "properties": {"Name": title("t")}})).json()
        assert p["last_edited_by"]["id"] == BOT_ID
        n.clock = lambda: datetime(2030, 1, 1, 0, 0, 5, tzinfo=UTC)
        n.user_edit(p["id"], {"Done": {"checkbox": True}}, "user-9")
        got = (await c.get(f"{BASE}/pages/{p['id']}")).json()
    assert got["last_edited_by"] == {"object": "user", "id": "user-9"}
    assert got["properties"]["Done"]["checkbox"] is True
    assert got["last_edited_time"] == "2030-01-01T00:00:00.000Z"


async def test_database_data_source_properties_and_relation():
    n = FakeNotion()
    async with n.client(HEADERS) as c:
        db, ds = await make_ds(c)
        assert db["data_sources"][0]["name"] == "Todos"
        got = (await c.get(f"{BASE}/data_sources/{ds}")).json()
        assert all(p["id"] for p in got["properties"].values())
        name_id = got["properties"]["Name"]["id"]
        up = await c.patch(f"{BASE}/data_sources/{ds}", json={"properties": {
            "Parent": {"relation": {"data_source_id": ds, "type": "single_property", "single_property": {}}},
            "Done": {"name": "Finished"}}})
        again = (await c.get(f"{BASE}/data_sources/{ds}")).json()
    assert up.status_code == 200
    assert again["properties"]["Parent"]["relation"]["data_source_id"] == ds
    assert again["properties"]["Parent"]["relation"]["type"] == "single_property"
    assert again["properties"]["Name"]["id"] == name_id and "Finished" in again["properties"]
    assert "Done" not in again["properties"]


async def test_search_users_me_and_trash():
    n = FakeNotion()
    async with n.client(HEADERS) as c:
        _, ds = await make_ds(c)
        p = (await c.post(f"{BASE}/pages", json={"parent": {"type": "data_source_id", "data_source_id": ds},
                                                  "properties": {"Name": title("t")}})).json()
        page_filter = {"filter": {"property": "object", "value": "page"}}
        pages = (await c.post(f"{BASE}/search", json=page_filter)).json()
        sources = (await c.post(f"{BASE}/search",
                                json={"filter": {"property": "object", "value": "data_source"}})).json()
        me = (await c.get(f"{BASE}/users/me")).json()
        await c.patch(f"{BASE}/pages/{p['id']}", json={"in_trash": True})
        after = (await c.post(f"{BASE}/search", json=page_filter)).json()
    assert [x["id"] for x in pages["results"]] == [p["id"]]
    assert [x["object"] for x in sources["results"]] == ["data_source"]
    assert me["id"] == BOT_ID and me["type"] == "bot"
    assert after["results"] == [] and n.pages[p["id"]]["in_trash"] is True


async def test_scripted_failures_and_routing_log():
    n = FakeNotion()
    n.script_429(1)
    n.script_error(503, "service_unavailable")
    async with n.client(HEADERS) as c:
        a = await c.get(f"{BASE}/users/me")
        b = await c.get(f"{BASE}/users/me")
        ok = await c.get(f"{BASE}/users/me")
        miss = await c.get(f"{BASE}/pages/nope")
        odd = await c.get(f"{BASE}/whatever")
    assert a.status_code == 429 and a.headers["Retry-After"] == "1" and a.json()["code"] == "rate_limited"
    assert b.status_code == 503 and b.json()["code"] == "service_unavailable"
    assert ok.status_code == 200 and miss.json()["code"] == "object_not_found"
    assert odd.status_code == 404 and len(n.requests) == 5
    with pytest.raises(AssertionError, match="/v1/whatever"):
        n.assert_all_routed()


async def test_envelope_version_auth_host_and_prefix_are_enforced():
    n = FakeNotion()
    url = f"{BASE}/users/me"
    async with httpx.AsyncClient(transport=httpx.MockTransport(n)) as raw:
        none = await raw.get(url, headers={"Authorization": "Bearer t"})
        old = await raw.get(url, headers={"Authorization": "Bearer t", "Notion-Version": "2022-06-28"})
        anon = await raw.get(url, headers={"Notion-Version": "2026-03-11"})
        ok = await raw.get(url, headers=HEADERS)
        wrong_host = await raw.get("https://api.notion.example/v1/users/me", headers=HEADERS)
        wrong_prefix = await raw.get("https://api.notion.com/users/me", headers=HEADERS)
    assert none.status_code == 400 and none.json()["code"] == "missing_version"
    assert old.status_code == 400 and old.json()["code"] == "invalid_request"
    assert anon.status_code == 401 and anon.json()["code"] == "unauthorized"
    assert ok.status_code == 200
    assert wrong_host.status_code == 404 and wrong_prefix.status_code == 404 and len(n.unrouted) == 2


async def test_block_writes_stamp_the_owning_page():
    n = FakeNotion()
    async with n.client(HEADERS) as c:
        p = await make_page(c)
        nested = (await c.patch(f"{BASE}/blocks/{p}/children", json={"children": [
            {"object": "block", "type": "toggle", "toggle": {"rich_text": [], "children": [para("in")]}}
        ]})).json()["results"][0]
        inner = n.blocks[nested["id"]][0]["id"]
        n.clock = lambda: datetime(2031, 1, 1, 1, 1, 1, tzinfo=UTC)
        n.user_edit(p, {"title": title("x")}, "user-9")
        n.clock = lambda: datetime(2031, 1, 1, 1, 5, 0, tzinfo=UTC)
        await c.patch(f"{BASE}/blocks/{nested['id']}/children", json={"children": [para("more")]})
        assert n.pages[p]["last_edited_time"] == "2031-01-01T01:05:00.000Z"
        assert n.pages[p]["last_edited_by"]["id"] == BOT_ID
        n.user_edit(p, {"title": title("y")}, "user-9")
        n.clock = lambda: datetime(2031, 1, 1, 1, 9, 0, tzinfo=UTC)
        await c.delete(f"{BASE}/blocks/{inner}")
    assert n.pages[p]["last_edited_time"] == "2031-01-01T01:09:00.000Z"
    assert n.pages[p]["last_edited_by"]["id"] == BOT_ID


async def test_page_properties_are_checked_against_the_schema():
    n = FakeNotion()
    async with n.client(HEADERS) as c:
        _, ds = await make_ds(c)
        parent = {"type": "data_source_id", "data_source_id": ds}
        unknown = await c.post(f"{BASE}/pages", json={"parent": parent, "properties": {"Nope": title("x")}})
        wrong = await c.post(f"{BASE}/pages", json={"parent": parent, "properties": {"Done": title("x")}})
        good = {"parent": parent, "properties": {"Name": title("t")}}
        p = (await c.post(f"{BASE}/pages", json=good)).json()
        assert set(p["properties"]) == {"Name", "Done"}
        assert p["properties"]["Done"]["checkbox"] is False and p["properties"]["Done"]["id"]
        bad_patch = await c.patch(f"{BASE}/pages/{p['id']}", json={"properties": {"Done": {"select": None}}})
        await c.patch(f"{BASE}/pages/{p['id']}", json={"in_trash": True})
        edit_trashed = await c.patch(f"{BASE}/pages/{p['id']}",
                                     json={"properties": {"Done": {"checkbox": True}}})
        restore = await c.patch(f"{BASE}/pages/{p['id']}", json={"in_trash": False})
        await c.patch(f"{BASE}/data_sources/{ds}", json={"properties": {"Notes": {"rich_text": {}}}})
        later = (await c.get(f"{BASE}/pages/{p['id']}")).json()
    for r in (unknown, wrong, bad_patch, edit_trashed):
        assert r.status_code == 400 and r.json()["code"] == "validation_error"
    assert restore.status_code == 200 and restore.json()["in_trash"] is False
    assert later["properties"]["Notes"]["rich_text"] == []


async def test_unknown_block_parents_are_not_found():
    n = FakeNotion()
    async with n.client(HEADERS) as c:
        append = await c.patch(f"{BASE}/blocks/ghost/children", json={"children": [para("x")]})
        listed = await c.get(f"{BASE}/blocks/ghost/children")
    assert append.status_code == 404 and append.json()["code"] == "object_not_found"
    assert listed.status_code == 404 and "ghost" not in n.blocks

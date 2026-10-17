import httpx
import pytest

from mavis.domain.errors import FailureKind as K
from mavis.tools.integrations.native.base import NativeProvider, ReauthRequired
from mavis.tools.integrations.native.notion_api import (
    NOTION_VERSION,
    NotionApi,
    NotionError,
    notion_id,
    page_text,
)

from .notionfake import FakeNotion

RAW = "0123456789abcdef0123456789abcdef"
DASHED = "01234567-89ab-cdef-0123-456789abcdef"
ROOT = {"type": "workspace", "workspace": True}


class Tokens:
    def __init__(self, revoked: bool = False) -> None:
        self.calls: list[bool] = []
        self.revoked = revoked

    async def access_token(self, user_id, provider, *, force: bool = False) -> str:
        assert provider is NativeProvider.NOTION
        if self.revoked:
            raise ReauthRequired("invalid_grant")
        self.calls.append(force)
        return "tok-forced" if force else "tok"

    async def account(self, user_id, provider):
        return None


def api_for(fake: FakeNotion, tokens=None, clock=None):
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    api = NotionApi(tokens or Tokens(), fake.client(), sleep=sleep, clock=clock or (lambda: 100.0))
    return api, sleeps


def test_notion_id_accepts_dashed_and_plain_rejects_paths():
    assert notion_id(RAW) == DASHED
    assert notion_id(DASHED) == DASHED
    assert notion_id(RAW.upper()) == DASHED
    assert notion_id("../x") is None
    assert notion_id("abc") is None
    assert notion_id(RAW + "?q") is None
    assert notion_id(RAW + "/children") is None
    assert notion_id("") is None


async def test_version_header_on_every_call():
    fake = FakeNotion()
    api, _ = api_for(fake)
    page = await api.create_page(1, ROOT, {})
    await api.page(1, page["id"])
    await api.search(1, "", None, 5)
    assert len(fake.requests) == 3
    for r in fake.requests:
        assert r.headers["Notion-Version"] == NOTION_VERSION == "2026-03-11"
        assert r.headers["Authorization"] == "Bearer tok"


async def test_pacing_never_exceeds_rate():
    fake = FakeNotion()
    api, sleeps = api_for(fake)  # a frozen clock: only sleeping refills the bucket
    for _ in range(20):
        await api.search(1, "", None, 1)
    assert len(sleeps) == 15  # a burst of 5, then every call waits
    assert all(s == pytest.approx(0.4) for s in sleeps)  # 2.5 per second
    assert sum(sleeps) >= (20 - 5) / 2.5 - 1e-9


async def test_pacing_refills_with_the_clock_and_is_per_user():
    fake = FakeNotion()
    now = [0.0]
    api, sleeps = api_for(fake, clock=lambda: now[0])
    for _ in range(5):
        await api.search(1, "", None, 1)
    for _ in range(5):
        await api.search(2, "", None, 1)  # another user has a full bucket
    assert sleeps == []
    now[0] += 2.0  # 5 tokens back for user 1
    for _ in range(5):
        await api.search(1, "", None, 1)
    assert sleeps == []


async def test_429_waits_retry_after_capped_at_60_then_retries_once():
    fake = FakeNotion()
    fake.script_429(1, retry_after=7)
    api, sleeps = api_for(fake)
    await api.search(1, "", None, 1)
    assert sleeps == [7.0]
    fake2 = FakeNotion()
    fake2.script_429(2, retry_after=500)
    api2, sleeps2 = api_for(fake2)
    with pytest.raises(NotionError) as exc:
        await api2.search(1, "", None, 1)
    assert exc.value.kind is K.RATE_LIMITED and sleeps2 == [60.0]
    assert len(fake2.requests) == 2


async def test_401_forces_refresh_once_then_auth():
    fake = FakeNotion()
    fake.script_error(401, "unauthorized")
    tokens = Tokens()
    api, _ = api_for(fake, tokens)
    await api.search(1, "", None, 1)
    assert tokens.calls == [False, True]
    assert fake.requests[-1].headers["Authorization"] == "Bearer tok-forced"
    fake2 = FakeNotion()
    fake2.script_error(401, "unauthorized", 2)
    tokens2 = Tokens()
    api2, _ = api_for(fake2, tokens2)
    with pytest.raises(ReauthRequired):
        await api2.search(1, "", None, 1)
    assert tokens2.calls == [False, True]


@pytest.mark.parametrize(("status", "code", "kind"), [
    (403, "restricted_resource", K.PERMISSION_MISSING),
    (404, "object_not_found", K.NOT_FOUND),
    (400, "validation_error", K.INVALID_ARGUMENT),
])
async def test_error_mapping(status, code, kind):
    fake = FakeNotion()
    fake.script_error(status, code)
    api, _ = api_for(fake)
    with pytest.raises(NotionError) as exc:
        await api.page(1, "page-1")
    assert exc.value.kind is kind and exc.value.code == code
    assert "SECRET-DETAIL" not in str(exc.value)


async def test_5xx_retries_once_for_get_only():
    fake = FakeNotion()
    api, sleeps = api_for(fake)
    page = await api.create_page(1, ROOT, {})
    fake.script_error(503, "service_unavailable")
    got = await api.page(1, page["id"])
    assert got["id"] == page["id"] and sleeps == [1.0]
    fake.script_error(503, "service_unavailable", 2)
    with pytest.raises(NotionError) as exc:
        await api.page(1, page["id"])
    assert exc.value.kind is K.UNAVAILABLE


async def test_5xx_on_a_write_is_unconfirmed_and_not_repeated():
    fake = FakeNotion()
    fake.script_error(500, "internal_server_error")
    api, _ = api_for(fake)
    with pytest.raises(NotionError) as exc:
        await api.create_page(1, ROOT, {})
    assert exc.value.kind is K.UNCONFIRMED
    assert len(fake.requests) == 1 and not fake.pages


async def test_409_conflict_retries_once():
    fake = FakeNotion()
    fake.script_error(409, "conflict_error")
    api, _ = api_for(fake)
    page = await api.create_page(1, ROOT, {})
    assert page["id"] and len(fake.requests) == 2
    fake.script_error(409, "conflict_error", 2)
    with pytest.raises(NotionError):
        await api.create_page(1, ROOT, {})


async def test_error_body_never_in_error_text():
    fake = FakeNotion()
    fake.pages["p"] = {}
    fake._scripted.append(httpx.Response(400, json={"code": "validation_error", "message": "SECRET-DETAIL"}))
    api, _ = api_for(fake)
    with pytest.raises(NotionError) as exc:
        await api.page(1, "p")
    assert "SECRET-DETAIL" not in str(exc.value) and str(exc.value) == "notion 400 validation_error"


async def test_hostile_ids_are_refused_before_any_request():
    fake = FakeNotion()
    api, _ = api_for(fake)
    with pytest.raises(NotionError) as exc:
        await api.page(1, "../users/me")
    assert exc.value.kind is K.INVALID_ARGUMENT and fake.requests == []


def blocks(n: int) -> list[dict]:
    return [{"type": "paragraph", "paragraph": {"rich_text": [
        {"type": "text", "text": {"content": f"b{i}"}}]}} for i in range(n)]


async def test_append_chunks_150_blocks_into_two_requests():
    fake = FakeNotion()
    api, _ = api_for(fake)
    page = await api.create_page(1, ROOT, {})
    ids = await api.append(1, page["id"], blocks(150))
    patches = [r for r in fake.requests if r.method == "PATCH"]
    assert len(patches) == 2 and len(ids) == 150
    assert [b["paragraph"]["rich_text"][0]["text"]["content"] for b in fake.blocks[page["id"]]] == [
        f"b{i}" for i in range(150)]


async def test_append_with_position_keeps_order_across_chunks():
    fake = FakeNotion()
    api, _ = api_for(fake)
    page = await api.create_page(1, ROOT, {}, blocks(2))
    await api.append(1, page["id"], blocks(150)[:101], position={"type": "start"})
    texts = [b["paragraph"]["rich_text"][0]["text"]["content"] for b in fake.blocks[page["id"]]]
    assert texts == [f"b{i}" for i in range(101)] + ["b0", "b1"]


async def test_create_page_with_150_children_appends_the_rest():
    fake = FakeNotion()
    api, _ = api_for(fake)
    page = await api.create_page(1, ROOT, {}, blocks(150))
    assert len(fake.blocks[page["id"]]) == 150


async def test_children_and_query_paginate_to_the_cap():
    fake = FakeNotion()
    api, _ = api_for(fake)
    page = await api.create_page(1, ROOT, {}, blocks(100))
    await api.append(1, page["id"], blocks(100))
    assert len(await api.children(1, page["id"], limit=150)) == 150
    assert len(await api.children(1, page["id"])) == 200


async def test_database_roundtrip_and_query():
    fake = FakeNotion()
    api, _ = api_for(fake)
    home = await api.create_page(1, ROOT, {})
    db_id, ds_id = await api.create_database(1, home["id"], "Todos", {"Done": {"checkbox": {}}})
    assert db_id in fake.databases and ds_id in fake.data_sources
    ds = await api.data_source(1, ds_id)
    assert "Done" in ds["properties"]
    await api.update_data_source(1, ds_id, {"Note": {"rich_text": {}}})
    assert "Note" in fake.data_sources[ds_id]["properties"]
    row = await api.create_page(1, {"type": "data_source_id", "data_source_id": ds_id}, {})
    rows = await api.query(1, ds_id, filter=None, sorts=None, limit=10)
    assert [r["id"] for r in rows] == [row["id"]]
    await api.update_page(1, row["id"], in_trash=True)
    assert await api.query(1, ds_id, filter=None, sorts=None, limit=10) == []
    blk = (await api.append(1, home["id"], blocks(1)))[0]
    await api.delete_block(1, blk)
    assert fake.blocks[home["id"]] == []


def test_page_text_plain_text_of_known_blocks():
    def rt(t):
        return [{"type": "text", "plain_text": t}]
    got = page_text([
        {"type": "heading_1", "heading_1": {"rich_text": rt("Title")}},
        {"type": "paragraph", "paragraph": {"rich_text": rt("Body")}},
        {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rt("one")}},
        {"type": "to_do", "to_do": {"rich_text": rt("task"), "checked": True}},
        {"type": "quote", "quote": {"rich_text": rt("said")}},
        {"type": "callout", "callout": {"rich_text": rt("note")}},
        {"type": "code", "code": {"rich_text": rt("x = 1")}},
        {"type": "child_page", "child_page": {"title": "Sub"}},
        {"type": "image", "image": {}},
    ])
    assert got.splitlines() == ["# Title", "Body", "- one", "[x] task", "> said", "note", "```", "x = 1",
                                "```", "[page: Sub]"]
    assert len(page_text([
        {"type": "paragraph", "paragraph": {"rich_text": rt("y" * 500)}}], limit=100)) == 100

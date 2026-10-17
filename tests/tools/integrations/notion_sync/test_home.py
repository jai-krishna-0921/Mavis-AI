from __future__ import annotations

import pytest

from mavis.store.repo import notion_links
from mavis.tools.integrations.notion_sync import blocks
from mavis.tools.integrations.notion_sync.home import DBS, SCHEMAS, Home, ensure_home

from .conftest import UID


def titles(fake):
    return [(d["title"][0]["text"]["content"], d) for d in fake.databases.values()]


async def test_builds_home_under_template_page_with_five_linked_databases(fake, api):
    tpl = fake.add_page("Template")
    home = await ensure_home(api, UID, {"template_page_id": tpl["id"]})
    assert isinstance(home, Home)
    assert fake.pages[home.page_id]["parent"]["page_id"] == tpl["id"]
    assert list(home.ds) == list(DBS) == ["people", "projects", "meetings", "briefings", "todos"]
    assert [t for t, _ in titles(fake)] == ["People", "Projects", "Meetings", "Briefings", "Todos"]
    for name in DBS:
        ds = fake.data_sources[home.ds[name]]
        assert ds["properties"]["Mavis key"]["type"] == "rich_text"
        assert home.props[name]["Mavis key"] == ds["properties"]["Mavis key"]["id"]
        assert set(SCHEMAS[name]) == set(home.props[name])

    def target(db, prop):
        return fake.data_sources[home.ds[db]]["properties"][prop]["relation"]["data_source_id"]

    assert target("todos", "People") == home.ds["people"]
    assert target("todos", "Project") == home.ds["projects"]
    assert target("todos", "Meeting") == home.ds["meetings"]
    assert target("meetings", "People") == home.ds["people"]
    assert target("projects", "People") == home.ds["people"]
    rel = fake.data_sources[home.ds["todos"]]["properties"]["People"]["relation"]
    assert rel["type"] == "single_property"
    assert (await notion_links.get(UID, "home", "home")).page_id == home.page_id
    assert (await notion_links.get(UID, "db", "todos")).meta["ds_id"] == home.ds["todos"]
    fake.assert_all_routed()


async def test_builds_under_top_level_shared_page_when_no_template(fake, api):
    nested = fake.add_page("Nested", parent={"type": "page_id", "page_id": "x"})
    top = fake.add_page("Top")
    nested["last_edited_time"] = "2026-10-17T13:00:00.000Z"  # newest, but not top level
    home = await ensure_home(api, UID, {})
    assert isinstance(home, Home)
    assert fake.pages[home.page_id]["parent"]["page_id"] == top["id"]


async def test_no_shared_page_is_a_problem(fake, api):
    assert await ensure_home(api, UID, {}) == "no_page"
    assert not fake.databases


async def test_reconnect_adopts_existing_home_without_new_databases(fake, api):
    first = await ensure_home(api, UID, {"template_page_id": fake.add_page("T")["id"]})
    again = await ensure_home(api, UID, {})
    assert again == first
    assert len(fake.databases) == 5 and len(fake.pages) == 2


async def test_renamed_column_still_matched_and_deleted_column_readded(fake, api):
    home = await ensure_home(api, UID, {"template_page_id": fake.add_page("T")["id"]})
    ds = fake.data_sources[home.ds["todos"]]
    due_id = ds["properties"]["Due"]["id"]
    await api.update_data_source(UID, home.ds["todos"], {"Due": {"name": "Deadline"}})
    await api.update_data_source(UID, home.ds["todos"], {"Source": None})
    again = await ensure_home(api, UID, {})
    assert again.props["todos"]["Due"] == due_id
    assert "Deadline" in ds["properties"] and "Due" not in ds["properties"]
    assert "Source" in ds["properties"]
    assert again.props["todos"]["Source"] == ds["properties"]["Source"]["id"]
    link = await notion_links.get(UID, "db", "todos")
    assert link.meta["props"]["Source"] == again.props["todos"]["Source"]


async def test_deleted_database_reports_gone_and_does_not_rebuild(fake, api):
    home = await ensure_home(api, UID, {"template_page_id": fake.add_page("T")["id"]})
    fake.data_sources[home.ds["meetings"]]["in_trash"] = True
    assert await ensure_home(api, UID, {}) == "gone"
    assert len(fake.databases) == 5
    del fake.data_sources[home.ds["people"]]
    assert await ensure_home(api, UID, {}) == "gone"


async def test_replace_owned_keeps_user_blocks(fake, api):
    page = fake.add_page("P")["id"]
    owned = await blocks.init_page_body(api, UID, page, [blocks.heading("A"), *blocks.para("one")])
    assert len(owned) == 2
    kids = await api.children(UID, page)
    assert [b["type"] for b in kids] == ["heading_2", "paragraph", "divider", "heading_2", "paragraph"]
    assert kids[3]["heading_2"]["rich_text"][0]["text"]["content"] == blocks.YOUR_NOTES
    mine = await api.append(UID, page, [*blocks.para("my note")], position={"type": "after_block",
                            "after_block": {"id": owned[0]}})
    new = await blocks.replace_owned(api, UID, page, owned, [blocks.heading("B"), *blocks.para("two")])
    assert len(new) == 2 and not set(new) & set(owned)
    kids = await api.children(UID, page)
    ids = [b["id"] for b in kids]
    assert ids[:2] == new and mine[0] in ids and len(kids) == 6
    assert kids[-2]["heading_2"]["rich_text"][0]["text"]["content"] == blocks.YOUR_NOTES
    # an already deleted owned block is ignored
    await blocks.replace_owned(api, UID, page, [*new, "00000000-0000-0000-0000-00000000ffff"], [])


def test_block_helpers():
    assert len(blocks.para("x" * 4500)) == 3
    assert blocks.content_hash({"a": 1, "b": 2}) == blocks.content_hash({"b": 2, "a": 1})
    assert len(blocks.content_hash([1])) == 32
    assert [b["type"] for b in blocks.bullets(["a", "b"])] == ["bulleted_list_item"] * 2
    assert blocks.callout("hi")["type"] == "callout" and blocks.divider()["type"] == "divider"


async def test_trashed_or_missing_home_page_is_gone_not_rebuilt(fake, api):
    home = await ensure_home(api, UID, {"template_page_id": fake.add_page("T")["id"]})
    fake.pages[home.page_id]["in_trash"] = True
    assert await ensure_home(api, UID, {}) == "gone"
    del fake.pages[home.page_id]
    assert await ensure_home(api, UID, {}) == "gone"
    assert len(fake.databases) == 5 and len(fake.pages) == 1  # nothing new built


async def test_build_cut_short_resumes_into_one_home(fake, api, monkeypatch):
    from mavis.domain.errors import FailureKind
    from mavis.tools.integrations.native.notion_api import NotionError

    tpl = fake.add_page("T")["id"]
    real, calls = api.create_database, []

    async def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 3:
            raise NotionError(FailureKind.UNAVAILABLE, "transport")
        return await real(*a, **kw)

    monkeypatch.setattr(api, "create_database", flaky)
    with pytest.raises(NotionError):
        await ensure_home(api, UID, {"template_page_id": tpl})
    assert len(fake.databases) == 2
    home = await ensure_home(api, UID, {"template_page_id": tpl})
    assert isinstance(home, Home)
    assert len(fake.databases) == 5
    assert len([p for p in fake.pages.values() if p["parent"].get("page_id") == tpl]) == 1
    assert fake.data_sources[home.ds["todos"]]["properties"]["People"]["type"] == "relation"
    assert (await notion_links.get(UID, "home", "home")).meta["built"] is True

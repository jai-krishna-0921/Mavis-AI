import pytest
from sqlalchemy.exc import IntegrityError

from mavis.domain import timeutil
from mavis.store.repo import notion_links


async def test_link_upsert_and_unique_page(user):
    await notion_links.upsert(user.id, "todo", "loop:1", "page-a", pushed_hash="h1", meta={"x": 1})
    row = await notion_links.get(user.id, "todo", "loop:1")
    assert row is not None and row.page_id == "page-a" and row.pushed_hash == "h1" and row.meta == {"x": 1}
    assert (await notion_links.by_page(user.id, "page-a")).local_key == "loop:1"

    # second upsert updates in place and keeps fields it was not given
    now = timeutil.now()
    await notion_links.upsert(user.id, "todo", "loop:1", "page-b", edited_at=now)
    row = await notion_links.get(user.id, "todo", "loop:1")
    assert row.page_id == "page-b" and row.pushed_hash == "h1" and row.edited_at is not None
    assert await notion_links.by_page(user.id, "page-a") is None
    assert len(await notion_links.all_of(user.id, "todo")) == 1

    # one page cannot back two local keys
    with pytest.raises(IntegrityError):
        await notion_links.upsert(user.id, "todo", "loop:2", "page-b")


async def test_delete_user_kinds(user):
    await notion_links.upsert(user.id, "todo", "a", "p1")
    await notion_links.upsert(user.id, "meeting", "b", "p2")
    await notion_links.upsert(user.id, "home", "c", "p3")
    assert await notion_links.delete_user_kinds(user.id, ["todo", "meeting"]) == 2
    assert await notion_links.get(user.id, "home", "c") is not None
    await notion_links.delete(user.id, "home", "c")
    assert await notion_links.all_of(user.id, "home") == []

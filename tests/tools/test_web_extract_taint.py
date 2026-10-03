"""C1 part 2: inside a tainted task, web_extract opens only hosts the user named in the task goal."""

from __future__ import annotations

import pytest

from mavis.store.repo import tasks
from mavis.tools import web
from mavis.tools.registry import ToolRun, current_run, current_task_id


@pytest.fixture
def fetched(monkeypatch) -> list[str]:
    seen: list[str] = []

    async def fake_extract(url: str, max_chars: int = 20000) -> str:
        seen.append(url)
        return "page text"

    monkeypatch.setattr(web, "extract", fake_extract)
    return seen


async def _in_task(task_id: int, url: str, run_tainted: bool = False) -> str:
    token = current_task_id.set(task_id)
    run_token = current_run.set(ToolRun(tainted=run_tainted))
    try:
        return await web.web_extract(1, web.ExtractArgs(url=url))
    finally:
        current_run.reset(run_token)
        current_task_id.reset(token)


async def test_tainted_task_refuses_hosts_the_user_did_not_name(user, fetched):
    tid = await tasks.create(user.id, goal="compare laptops on https://www.example.com and notebookcheck.net",
                             tainted=True)
    out = await _in_task(tid, "https://evil.example/c?d=meetings")
    assert out.startswith("Refused") and fetched == []
    assert await _in_task(tid, "https://example.com/laptops") == "page text"
    assert await _in_task(tid, "https://www.notebookcheck.net/review") == "page text"
    assert fetched == ["https://example.com/laptops", "https://www.notebookcheck.net/review"]


async def test_task_tainted_mid_run_is_restricted_too(user, fetched):
    tid = await tasks.create(user.id, goal="summarize my inbox")  # no hosts named
    out = await _in_task(tid, "https://evil.example/x", run_tainted=True)
    assert out.startswith("Refused") and fetched == []


async def test_subtask_uses_the_root_goal(user, fetched):
    root = await tasks.create(user.id, goal="read docs.python.org on asyncio", tainted=True)
    child = await tasks.create(user.id, goal="open evil.example", parent_id=root, tainted=True)
    assert (await _in_task(child, "https://evil.example/")).startswith("Refused")
    assert await _in_task(child, "https://docs.python.org/3/library/asyncio.html") == "page text"


async def test_untainted_task_and_no_task_are_unchanged(user, fetched):
    tid = await tasks.create(user.id, goal="research laptops")
    assert await _in_task(tid, "https://anything.example/") == "page text"
    assert await web.web_extract(1, web.ExtractArgs(url="https://other.example/")) == "page text"


def test_named_hosts() -> None:
    assert web.named_hosts("see https://www.Example.com/x, docs.python.org and e.g. this") == {
        "example.com", "docs.python.org"}

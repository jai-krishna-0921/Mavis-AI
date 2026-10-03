"""Inside a tainted task, web_extract opens only exact URLs the user gave (verbatim in the root goal) or
that the task's own web_search returned. Untainted tasks are unchanged."""

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
    web._search_urls.clear()
    yield seen
    web._search_urls.clear()


async def _in_task(task_id: int, coro_fn, run_tainted: bool = False):
    token = current_task_id.set(task_id)
    run_token = current_run.set(ToolRun(tainted=run_tainted))
    try:
        return await coro_fn()
    finally:
        current_run.reset(run_token)
        current_task_id.reset(token)


async def _extract(task_id: int, url: str, run_tainted: bool = False) -> str:
    return await _in_task(task_id, lambda: web.web_extract(1, web.ExtractArgs(url=url)), run_tainted)


async def test_query_on_a_named_host_is_refused(user, fetched):
    tid = await tasks.create(user.id, goal="research the speaker list on evilconf.com", tainted=True)
    assert (await _extract(tid, "https://evilconf.com/?d=secret")).startswith("Refused")
    assert (await _extract(tid, "https://evilconf.com/speakers")).startswith("Refused")
    assert fetched == []
    assert await _extract(tid, "https://EvilConf.com/") == "page text"  # the bare root page only


async def test_form_on_a_subdomain_of_a_named_host_is_refused(user, fetched):
    tid = await tasks.create(user.id, goal="look up my question on google.com", tainted=True)
    form = "https://docs.google.com/forms/d/e/abc/formResponse?entry.1=secret"
    assert (await _extract(tid, form)).startswith("Refused") and fetched == []


async def test_verbatim_goal_url_is_allowed_exactly(user, fetched):
    tid = await tasks.create(user.id, goal="summarize https://example.com/laptops/review?id=7.", tainted=True)
    assert await _extract(tid, "https://EXAMPLE.COM/laptops/review?id=7") == "page text"
    assert (await _extract(tid, "https://example.com/laptops/review?id=7&d=secret")).startswith("Refused")
    assert (await _extract(tid, "https://example.com/Laptops/review?id=7")).startswith("Refused")


async def test_url_from_the_tasks_own_search_is_allowed(user, fetched, monkeypatch):
    async def fake_search(query, max_results=5):
        return [web.SearchHit(title="Review", url="https://www.notebookcheck.net/review-x/", snippet="s")]

    monkeypatch.setattr(web, "search", fake_search)
    tid = await tasks.create(user.id, goal="compare laptops", tainted=True)
    await _in_task(tid, lambda: web.web_search(1, web.SearchArgs(query="laptop review")))
    assert await _extract(tid, "https://www.notebookcheck.net/review-x") == "page text"
    assert (await _extract(tid, "https://www.notebookcheck.net/review-x?d=1")).startswith("Refused")
    other = await tasks.create(user.id, goal="compare laptops", tainted=True)  # not this task's search
    assert (await _extract(other, "https://www.notebookcheck.net/review-x")).startswith("Refused")


async def test_task_tainted_mid_run_is_restricted_too(user, fetched):
    tid = await tasks.create(user.id, goal="summarize my inbox")
    assert (await _extract(tid, "https://evil.example/x", run_tainted=True)).startswith("Refused")
    assert fetched == []


async def test_subtask_uses_the_root_goal(user, fetched):
    doc = "https://docs.python.org/3/library/asyncio.html"
    root = await tasks.create(user.id, goal=f"read {doc}", tainted=True)
    child = await tasks.create(user.id, goal="open https://evil.example/", parent_id=root, tainted=True)
    assert (await _extract(child, "https://evil.example/")).startswith("Refused")
    assert await _extract(child, "https://docs.python.org/3/library/asyncio.html") == "page text"


async def test_untainted_task_and_no_task_are_unchanged(user, fetched):
    tid = await tasks.create(user.id, goal="research laptops")
    assert await _extract(tid, "https://anything.example/?q=1") == "page text"
    assert await web.web_extract(1, web.ExtractArgs(url="https://other.example/")) == "page text"


def test_urls_in_goal() -> None:
    got = web.urls_in_goal("see https://Example.com/x/, mail jai@gmail.com, and docs.python.org.")
    assert got == {"https://example.com/x", "https://docs.python.org", "http://docs.python.org"}

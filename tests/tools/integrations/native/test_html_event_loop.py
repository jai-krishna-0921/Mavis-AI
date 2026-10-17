"""The HTML parse blocks on its worker process, so no async handler may run it on the event loop: with a
parse that takes 0.5 s the loop must keep ticking through Outlook, Teams and Gmail reads."""

import asyncio
import time

import httpx
import pytest

from mavis.domain.integrations import UserRef
from mavis.tools.integrations.native import graph_html

from .gfake import FakeGoogle, gmail_message
from .graphfake import FakeGraph, graph_chat_message, graph_message, page
from .test_gmail_mime import part
from .test_microsoft_mail import with_folders
from .test_microsoft_transport import U

GET = r"/gmail/v1/users/me/messages/[^/]+$"
SLOW_S = 0.5


@pytest.fixture
def slow_parse(monkeypatch):
    real = graph_html._pool.run

    def slow(markup, limit, styles):
        time.sleep(SLOW_S)
        return real(markup, limit, styles)

    monkeypatch.setattr(graph_html._pool, "run", slow)


async def _max_gap_during(coro) -> tuple[float, object]:
    gaps: list[float] = []
    done = asyncio.Event()

    async def ticker() -> None:
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.02)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    started = time.perf_counter()
    result = await coro
    elapsed = time.perf_counter() - started
    done.set()
    await task
    assert elapsed >= SLOW_S * 0.9, "the slow parse did not run"
    return max(gaps), result


async def test_outlook_read_does_not_block_the_loop(slow_parse):
    g = with_folders(FakeGraph())
    g.on("GET", r"/v1.0/me/messages/m1$", httpx.Response(200, json=graph_message("m1", html="<p>Hi</p>")))
    ex, _ = g.executor()
    gap, r = await _max_gap_during(ex.execute(U, "mail.read", {"message_id": "m1"}))
    assert r.ok and r.data["messageText"] == "Hi"
    assert gap < 0.25, gap


async def test_teams_history_does_not_block_the_loop(slow_parse):
    msgs = [graph_chat_message("1", "c", text="<p>ok</p>", html=True)]
    g = FakeGraph().on("GET", r"/v1.0/chats/c/messages$", httpx.Response(200, json=page(msgs)))
    ex, _ = g.executor()
    gap, r = await _max_gap_during(ex.execute(U, "teams.history", {"chat_id": "c"}))
    assert r.data["messages"][0]["text"] == "ok"
    assert gap < 0.25, gap


async def test_gmail_read_does_not_block_the_loop(slow_parse):
    fake = FakeGoogle().on("GET", GET, httpx.Response(
        200, json=gmail_message("m9", payload=part("text/html", "<div>Hi <b>Sam</b></div>"))))
    ex, _ = fake.executor()
    gap, res = await _max_gap_during(ex.execute(UserRef(user_id=7), "mail.read", {"message_id": "m9"}))
    assert res.ok and res.data["messageText"] == "Hi Sam"
    assert gap < 0.25, gap

# ruff: noqa: E501
"""The /mcp Telegram command: a short list of the user's servers and a link to the dashboard."""

from __future__ import annotations

import pytest

from mavis.agents import commands
from mavis.agents.commands import canonical_commands, parse_command, run_command
from mavis.config import get_settings
from mavis.domain.events import Event, EventType, Trust
from mavis.store.repo import mcp_servers as repo
from tests.mcp.test_service import open_server
from tests.tools.integrations.fakes import NOW


def msg(text: str, user_id: int = 1) -> Event:
    return Event(id=f"tg:{text}:{user_id}", user_id=user_id, type=EventType.USER_MESSAGE, occurred_at=NOW,
                 source="telegram", payload={"text": text}, trust=Trust.USER)


@pytest.fixture
def flow(provider, cache, fake_bus, rec, state):
    from tests.agents.test_commands import flow_for

    return flow_for(provider, cache, fake_bus, rec, state)


def test_mcp_is_a_known_command_and_survives_reply_cleanup() -> None:
    assert "mcp" in commands.COMMANDS
    assert parse_command("/mcp") == ("mcp", [])
    assert canonical_commands("Use /mcp to see them") == "Use /mcp to see them"


async def test_it_says_it_is_not_available_while_the_flag_is_off(db, flow, rec) -> None:
    assert await run_command(msg("/mcp"), flow) is True
    assert rec.sent[-1].text == "Tools from other apps (MCP) are not available on this Mavis."


async def test_it_points_at_the_dashboard_when_nothing_is_connected(mcp_env, db, user, flow, rec) -> None:
    assert await run_command(msg("/mcp", user.id), flow) is True
    text = rec.sent[-1].text
    assert "have not connected any apps" in text and "https://mavis.test/workspace" in text
    assert rec.sent[-1].buttons[0][0].url == "https://mavis.test/workspace"


async def test_it_lists_servers_with_status_and_tool_count(mcp_env, db, user, flow, rec) -> None:
    async with open_server() as (fake, url):
        from mavis.mcp.service import McpService, set_service

        service = McpService()
        set_service(service)
        await service.create(user.id, name="Notes", url=url)
        bad = await repo.create(user.id, slug="old", display_name="Old Wiki", url="https://old.example.com/mcp",
                                auth_kind="oauth", status=repo.NEEDS_AUTH)
        await repo.create(user.id, slug="dead", display_name="Dead", url="https://dead.example.com/mcp",
                          auth_kind="none", status=repo.ERROR)
        await repo.set_status(user.id, (await repo.by_slug(user.id, "dead")).id, repo.ERROR, "not_found")
        assert bad is not None
        await run_command(msg("/mcp", user.id), flow)
    lines = rec.sent[-1].text.splitlines()
    assert lines[0] == "Your apps over MCP:"
    assert "Notes: ready, 2 tools" in lines
    assert "Old Wiki: needs sign-in" in lines
    assert any(line.startswith("Dead: not working") and "did not answer" in line for line in lines)
    assert "Add or fix one on the Workspace page: https://mavis.test/workspace" in lines


async def test_it_lists_only_the_askers_servers(mcp_env, db, two_users, flow, rec) -> None:
    a, b = two_users
    await repo.create(a.id, slug="mine", display_name="Mine", url="https://a.example.com/mcp", auth_kind="none")
    await run_command(msg("/mcp", b.id), flow)
    assert "Mine" not in rec.sent[-1].text and "have not connected" in rec.sent[-1].text


async def test_a_local_dev_base_url_gets_no_button_because_telegram_rejects_it(mcp_env, db, user, flow, rec,
                                                                              monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:8000")
    get_settings.cache_clear()
    await run_command(msg("/mcp", user.id), flow)
    assert rec.sent[-1].buttons == [] and "http://localhost:8000/workspace" in rec.sent[-1].text


async def test_the_copy_has_no_em_or_en_dash(mcp_env, db, user, flow, rec) -> None:
    await run_command(msg("/mcp", user.id), flow)
    assert "—" not in rec.sent[-1].text and "–" not in rec.sent[-1].text

"""Offline checks for the verify and smoke scripts and the production compose flag."""

from __future__ import annotations

from pathlib import Path

from scripts.smoke_workspace import READ_ONLY
from scripts.verify_composio import sample_args, slugs_to_check

from mavis.domain.policy import RiskClass
from mavis.tools.integrations.actions import ACTIONS
from mavis.tools.integrations.composio_map import COMPOSIO_ACTIONS

ROOT = Path(__file__).resolve().parents[1]


def test_every_mapped_action_has_a_valid_sample():
    for action, mapping in COMPOSIO_ACTIONS.items():
        assert isinstance(mapping.translate(sample_args(action)), dict), action


def test_gmail_and_calendar_are_checked_on_both_toolkits():
    assert slugs_to_check("mail.search") == ["GMAIL_FETCH_EMAILS", "GOOGLESUPER_FETCH_EMAILS"]
    assert slugs_to_check("calendar.update_event") == [
        "GOOGLECALENDAR_PATCH_EVENT", "GOOGLESUPER_PATCH_EVENT",
    ]
    assert slugs_to_check("drive.search") == ["GOOGLESUPER_FIND_FILE"]
    assert slugs_to_check("slack.send") == ["SLACK_SEND_MESSAGE"]


def test_smoke_script_only_reads():
    assert READ_ONLY and all(ACTIONS[a].risk is RiskClass.READ for a in READ_ONLY)
    assert all(ACTIONS[a].risk_fn is None for a in READ_ONLY)


def test_workspace_flag_and_poll_interval_are_in_prod_compose():
    compose = (ROOT / "docker-compose.prod.yml").read_text()
    assert "GOOGLE_WORKSPACE_ENABLED: ${GOOGLE_WORKSPACE_ENABLED:-true}" in compose
    assert "WORKSPACE_POLL_MINUTES: ${WORKSPACE_POLL_MINUTES:-30}" in compose


def test_verify_execute_runs_only_plain_read_actions():
    from scripts.verify_composio import executable

    for name, spec in ACTIONS.items():
        assert executable(name) is (spec.risk is RiskClass.READ and spec.risk_fn is None), name
    assert executable("drive.create_folder") is False  # WRITE_SELF never needs approval, still refused
    assert executable("tasks.add") is False
    assert executable("no.such_action") is False


async def test_verify_execute_refuses_a_write_before_any_call(monkeypatch, capsys):
    from scripts import verify_composio

    monkeypatch.setenv("COMPOSIO_API_KEY", "ck_test")

    async def boom(*a, **k):
        raise AssertionError("must not reach the provider")

    monkeypatch.setattr(verify_composio.ComposioProvider, "execute", boom)
    monkeypatch.setattr(verify_composio.ComposioProvider, "status", boom)
    args = verify_composio._Args(None, "drive.create_folder", 1, False)
    assert await verify_composio.main(args) == 2
    assert "read" in capsys.readouterr().err


def _asserts(path: Path, function: str | None = None) -> list[int]:
    import ast

    tree = ast.parse(path.read_text())
    if function is not None:
        tree = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)
                    and n.name == function)
    return [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assert)]


def test_workspace_production_code_has_no_assert_statements():
    # python -O strips asserts: a guard written as one silently disappears in an optimized run
    src = ROOT / "src" / "mavis"
    assert _asserts(src / "tools" / "integrations" / "composio.py") == []
    assert _asserts(src / "tools" / "integrations" / "workspace_tools.py") == []
    assert _asserts(src / "store" / "repo" / "attention.py", "insert_signal") == []
    assert _asserts(ROOT / "scripts" / "smoke_workspace.py") == []


async def test_workspace_tool_wrappers_reject_the_wrong_args_model():
    import pytest

    from mavis.tools.integrations import workspace_tools
    from mavis.tools.registry import ToolContext

    with pytest.raises(TypeError):
        await workspace_tools._drive_read(ToolContext(user_id=1), ACTIONS["tasks.list"].args_model())

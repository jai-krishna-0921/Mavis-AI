"""The MCP flag is off by default, localhost is never allowed in prod, and Hermes code is attributed."""

from __future__ import annotations

import re
from pathlib import Path

from mavis.config import Settings, get_settings

ROOT = Path(__file__).resolve().parents[2]
ATTRIBUTION = ("Portions adapted from Hermes Agent (https://github.com/NousResearch/hermes-agent), "
               "Copyright (c) 2025 Nous Research, MIT License.")


def test_mcp_is_off_by_default(settings) -> None:
    assert settings.mcp_enabled is False
    assert settings.mcp_allow_localhost is False
    assert settings.mcp_localhost_ok is False


def test_the_flag_reads_the_env_var(settings, monkeypatch) -> None:
    monkeypatch.setenv("MCP_ENABLED", "true")
    get_settings.cache_clear()
    assert get_settings().mcp_enabled is True


def test_localhost_is_allowed_in_dev_only_when_asked(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert Settings(env="dev", mcp_allow_localhost=True).mcp_localhost_ok is True
    assert Settings(env="dev", mcp_allow_localhost=False).mcp_localhost_ok is False


def test_localhost_is_never_allowed_in_prod(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert Settings(env="prod", mcp_allow_localhost=True).mcp_localhost_ok is False


def test_notice_carries_the_full_hermes_mit_notice() -> None:
    notice = (ROOT / "NOTICE").read_text()
    assert "Hermes Agent" in notice and "Copyright (c) 2025 Nous Research" in notice
    assert "Permission is hereby granted, free of charge" in notice
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in notice


def _comment_block(path: Path) -> str:
    lines = []
    for line in path.read_text().splitlines():
        if not line.startswith("#"):
            break
        lines.append(line.lstrip("# ").rstrip())
    return " ".join(lines)


def test_every_mcp_module_that_mentions_hermes_carries_the_attribution_header() -> None:
    bad = []
    for path in (ROOT / "src" / "mavis" / "mcp").rglob("*.py"):
        text = path.read_text()
        if re.search(r"hermes", text, re.I) and ATTRIBUTION not in _comment_block(path):
            bad.append(path.name)
    assert bad == []

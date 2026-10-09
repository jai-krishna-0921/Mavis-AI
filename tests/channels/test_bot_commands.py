"""The "/" menu lists only commands the bot actually handles."""

import re
from pathlib import Path

from mavis.agents import commands
from mavis.channels import telegram_webhook

SRC = Path(__file__).resolve().parents[2] / "src" / "mavis"


def _registered_user_commands() -> set[str]:
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        found.update(re.findall(r'register_user_command\(\s*"([a-z_]+)"', path.read_text(encoding="utf-8")))
    return found


def test_every_menu_command_has_a_handler():
    handled = set(commands.COMMANDS) | _registered_user_commands() | {"start"}
    listed = [c for c, _ in telegram_webhook.BOT_COMMANDS]
    assert listed and not set(listed) - handled
    assert len(listed) == len(set(listed))


def test_menu_entries_meet_telegram_limits():
    for command, description in telegram_webhook.BOT_COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", command)
        assert 1 <= len(description) <= 256
        assert "—" not in description and "–" not in description


async def test_set_commands_publishes_the_menu(monkeypatch):
    sent: list[tuple[str, dict]] = []

    async def fake_call(method, payload=None):
        sent.append((method, payload))
        return True

    monkeypatch.setattr(telegram_webhook, "_call", fake_call)
    await telegram_webhook.set_commands()
    method, payload = sent[0]
    assert method == "setMyCommands"
    assert [c["command"] for c in payload["commands"]] == [c for c, _ in telegram_webhook.BOT_COMMANDS]

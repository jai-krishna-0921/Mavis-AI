"""Shared contract C: every env var prod needs must be whitelisted in docker-compose.prod.yml x-app-env.

Plans add their keys to PASSTHROUGH. The compose file whitelists variables, so a key missing here never
reaches the containers (339e301)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PASSTHROUGH: set[str] = {
    # Phase 12 slice A
    "PROGRESS_CARD_ENABLED", "PROGRESS_CARD_AFTER_S", "PROGRESS_EDIT_MIN_INTERVAL_S",
    "PROGRESS_MAX_SCREENSHOTS", "TELEGRAM_GLOBAL_SEND_RATE", "TEST_MIRROR_CHAT_ID",
}


def app_env_keys() -> set[str]:
    text = (ROOT / "docker-compose.prod.yml").read_text()
    block = text.split("x-app-env: &app-env", 1)[1].split("\n\n", 1)[0]
    return set(re.findall(r"^\s{2}([A-Z][A-Z0-9_]+):", block, flags=re.M))


def test_every_passthrough_key_is_in_app_env():
    missing = sorted(PASSTHROUGH - app_env_keys())
    assert missing == [], f"add to x-app-env in docker-compose.prod.yml: {missing}"


def test_passthrough_keys_are_settings_fields():
    from mavis.config import Settings

    fields = {name.upper() for name in Settings.model_fields}
    assert sorted(PASSTHROUGH - fields) == []

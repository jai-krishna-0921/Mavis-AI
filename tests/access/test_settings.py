"""Phase 11 settings: safe defaults, owner ids accept the old env name, flags are typed."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_defaults_keep_todays_behaviour(settings):
    assert settings.access_mode == "allowlist"
    assert settings.worker_scheduler == "legacy"
    assert settings.llm_limiter == "local"
    assert settings.invite_max_active == 20 and settings.invite_max_uses == 25
    assert settings.owner_telegram_chat_ids == []


@pytest.mark.parametrize("env_name", ["OWNER_TELEGRAM_CHAT_IDS", "ALLOWED_TELEGRAM_CHAT_IDS"])
@pytest.mark.parametrize("ids", [[5001], [7302, 9944], [123456789]])
def test_owner_ids_read_new_and_old_names(settings, monkeypatch, env_name, ids):
    from mavis.config import get_settings

    monkeypatch.delenv("OWNER_TELEGRAM_CHAT_IDS", raising=False)
    monkeypatch.delenv("ALLOWED_TELEGRAM_CHAT_IDS", raising=False)
    monkeypatch.setenv(env_name, str(ids))
    get_settings.cache_clear()
    s = get_settings()
    assert s.owner_telegram_chat_ids == ids
    assert s.allowed_telegram_chat_ids == ids  # contract D: old readers keep working


def test_access_mode_rejects_unknown_values(settings, monkeypatch):
    from mavis.config import Settings

    monkeypatch.setenv("ACCESS_MODE", "everyone")
    with pytest.raises(ValidationError):
        Settings()


async def test_fake_redis_runs_lua(fake_redis):
    assert await fake_redis.eval("return redis.call('incr', KEYS[1])", 1, "k") == 1

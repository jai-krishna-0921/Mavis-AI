"""Progress card settings: defaults from the spec, blank optional ids are unset."""

from __future__ import annotations

import pytest


def test_progress_defaults(settings):
    assert settings.progress_card_enabled is True
    assert settings.progress_card_after_s == 4.0
    assert settings.progress_edit_min_interval_s == 3.0
    assert settings.progress_max_screenshots == 4
    assert settings.telegram_global_send_rate == 25.0
    assert settings.test_mirror_chat_id is None


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_mirror_chat_is_unset(settings, monkeypatch, raw):
    from mavis.config import get_settings

    monkeypatch.setenv("TEST_MIRROR_CHAT_ID", raw)
    get_settings.cache_clear()
    assert get_settings().test_mirror_chat_id is None


@pytest.mark.parametrize("raw,value", [("424242", 424242), ("7", 7), ("-55", -55)])
def test_mirror_chat_parses(settings, monkeypatch, raw, value):
    from mavis.config import get_settings

    monkeypatch.setenv("TEST_MIRROR_CHAT_ID", raw)
    get_settings.cache_clear()
    assert get_settings().test_mirror_chat_id == value

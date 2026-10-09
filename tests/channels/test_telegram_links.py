"""All Telegram links come from one helper and use the reachable telegram.me alias."""

import pytest

from mavis.channels.telegram_links import telegram_link


def test_links_use_the_alias_and_the_start_parameter(settings):
    assert telegram_link("Mavis247_bot") == "https://telegram.me/Mavis247_bot"
    assert telegram_link("@Mavis247_bot", "login_ab12") == "https://telegram.me/Mavis247_bot?start=login_ab12"


def test_no_bot_means_no_link(settings):
    assert telegram_link("") is None and telegram_link("  ") is None


def test_the_base_can_be_overridden(settings, monkeypatch):
    monkeypatch.setattr(settings, "telegram_link_base", "https://telegram.me/")
    assert telegram_link("bot") == "https://telegram.me/bot"


@pytest.mark.parametrize("bad", ["", "a" * 65, "has space", "semi;colon", "q?x=1"])
def test_start_parameters_outside_telegram_rules_are_refused(settings, bad):
    with pytest.raises(ValueError):
        telegram_link("bot", bad)

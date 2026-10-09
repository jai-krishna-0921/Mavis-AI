"""Every Telegram link Mavis hands out is built here.

telegram.me is Telegram's official alias of t.me with the same deep-link behaviour (?start=...). Some
networks block t.me by DNS (seen on an Indian ISP: t.me resolved to a block page) while telegram.me still
works, so links use the alias by default. TELEGRAM_LINK_BASE overrides it.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from mavis.config import get_settings

_START = re.compile(r"[A-Za-z0-9_-]{1,64}")  # Telegram's limits for the start parameter


def telegram_link(bot: str, start: str | None = None) -> str | None:
    """https://telegram.me/<bot>[?start=<param>], or None when no bot username is configured."""
    bot = (bot or "").strip().lstrip("@")
    if not bot:
        return None
    base = get_settings().telegram_link_base.rstrip("/")
    url = f"{base}/{quote(bot, safe='')}"
    if start is not None:
        if not _START.fullmatch(start):
            raise ValueError("Telegram start parameters are 1 to 64 letters, digits, _ or -")
        url += f"?start={start}"
    return url

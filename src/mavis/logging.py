"""structlog setup with secret redaction. Call configure_logging() once per process entrypoint."""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

from mavis.config import get_settings

_SECRET_KEY = re.compile(r"(?i)(key|token|secret|password)")
REDACTED = "***"


_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
# Telegram bot tokens: `bot<id>:<token>` inside API URLs, and bare `<id>:<35 chars>` tokens
_BOT_URL = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_BOT_TOKEN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}")


def _scrub_str(value: str) -> str:
    value = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", value)
    value = _BOT_URL.sub(f"bot{REDACTED}", value)
    return _BOT_TOKEN.sub(REDACTED, value)


def _scrub(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return _scrub_str(value)
    if isinstance(value, BaseException):  # exception text often embeds the request URL
        return _scrub_str(f"{type(value).__name__}: {value}")
    if depth >= 4:
        return value
    if isinstance(value, dict):
        return {k: REDACTED if isinstance(k, str) and _SECRET_KEY.search(k) else _scrub(v, depth + 1)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub(v, depth + 1) for v in value) if type(value) in (list, tuple) else value
    return value


def redact_secrets(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Mask credential-named fields and token-looking values (nested dicts, lists, exceptions included)."""
    for key in list(event_dict):
        if key != "event" and _SECRET_KEY.search(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _scrub(event_dict[key])
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    renderer: Any = (
        structlog.processors.JSONRenderer() if get_settings().log_json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redact_secrets,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )

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


def redact_secrets(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Mask any log field whose name looks like a credential."""
    for key in list(event_dict):
        if key != "event" and _SECRET_KEY.search(key):
            event_dict[key] = REDACTED
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

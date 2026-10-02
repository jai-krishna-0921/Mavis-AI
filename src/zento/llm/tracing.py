"""Optional Langfuse tracing: no-op unless both keys are configured."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import structlog

from zento.config import get_settings

log = structlog.get_logger(__name__)


@lru_cache
def _handler() -> Any | None:
    s = get_settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        return None
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", s.langfuse_public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", s.langfuse_secret_key)
    os.environ.setdefault("LANGFUSE_HOST", s.langfuse_host)
    try:
        from langfuse.langchain import CallbackHandler

        return CallbackHandler()
    except Exception as exc:  # noqa: BLE001 - tracing must never break the agent
        log.warning("langfuse.disabled", error=str(exc))
        return None


def callbacks() -> list[Any]:
    handler = _handler()
    return [handler] if handler else []

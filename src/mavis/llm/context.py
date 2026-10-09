"""Who and what an LLM call is for, set by the executor and job runner (spec 8.3): no signature changes."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar

llm_user_id: ContextVar[int | None] = ContextVar("llm_user_id", default=None)
llm_purpose: ContextVar[str] = ContextVar("llm_purpose", default="other")


@contextlib.contextmanager
def bind_user(user_id: int | None, purpose: str = "other") -> Iterator[None]:
    t1, t2 = llm_user_id.set(user_id), llm_purpose.set(purpose)
    try:
        yield
    finally:
        llm_user_id.reset(t1)
        llm_purpose.reset(t2)

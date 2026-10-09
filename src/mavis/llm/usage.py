"""Token metering (spec 9.2): one LangChain callback books every model call to the current user."""

from __future__ import annotations

from typing import Any

import structlog
from langchain_core.callbacks import AsyncCallbackHandler

from mavis import bus
from mavis.config import get_settings
from mavis.domain import redis_keys, timeutil
from mavis.llm.context import llm_purpose, llm_user_id
from mavis.store.repo import usage as repo

log = structlog.get_logger(__name__)


def cost_micros(model: str, prompt_tokens: int, completion_tokens: int) -> int:
    s = get_settings()
    pin, pout = s.llm_prices.get(model, s.llm_price_default)
    return round(prompt_tokens * pin + completion_tokens * pout)  # USD per 1M tokens == micros per token


async def _local_day(user_id: int):
    from mavis.store.repo import users

    tz = get_settings().default_timezone
    if user_id:
        try:
            tz = (await users.get(user_id)).timezone or tz
        except Exception:  # noqa: BLE001
            pass
    return timeutil.to_local(timeutil.now(), tz).date()


async def record(user_id: int, model: str, provider: str, purpose: str, prompt: int, completion: int) -> None:
    cost = cost_micros(model, prompt, completion)
    day = await _local_day(user_id)
    await repo.add(user_id, day, provider, model, purpose, prompt, completion, cost)
    client = bus.get_redis()
    if client is not None:
        key = redis_keys.user_key("spend", user_id, f"{day:%Y%m%d}")
        await client.incrby(key, cost)
        await client.expire(key, 48 * 3600)


async def spend_micros_today(user_id: int) -> int:
    day = await _local_day(user_id)
    client = bus.get_redis()
    if client is not None:
        v = await client.get(redis_keys.user_key("spend", user_id, f"{day:%Y%m%d}"))
        if v is not None:
            return int(v)
    return sum(r.cost_micros for r in await repo.daily(user_id, day))


class UsageCallback(AsyncCallbackHandler):
    async def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        try:
            for gens in response.generations:
                for g in gens:
                    msg = getattr(g, "message", None)
                    meta = getattr(msg, "usage_metadata", None) or {}
                    model = (getattr(msg, "response_metadata", {}) or {}).get("model_name", "unknown")
                    provider = "secondary" if model in _secondary_models() else "ollama"
                    await record(llm_user_id.get() or 0, model, provider, llm_purpose.get(),
                                 int(meta.get("input_tokens", 0)), int(meta.get("output_tokens", 0)))
        except Exception as exc:  # noqa: BLE001 - metering must never break a call
            log.warning("llm.usage_record_failed", error=type(exc).__name__)


def _secondary_models() -> set[str]:
    s = get_settings()
    return {m for m in (s.llm_secondary_model_fast, s.llm_secondary_model_smart) if m}

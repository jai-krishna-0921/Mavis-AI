"""Model routing (FAST / SMART) and validated structured output.

Callers MUST use `from zento.llm import models as llm` and call `llm.structured(...)` etc. so tests can
monkeypatch this module. Internal calls go through the module-global `chat_model`, so patching it
also fakes `complete()` and the JSON fallback in `structured()`.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from functools import lru_cache
from typing import Any

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from zento.config import get_settings
from zento.domain.errors import LLMError
from zento.llm.tracing import callbacks

log = structlog.get_logger(__name__)

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class Tier(StrEnum):
    FAST = "fast"
    SMART = "smart"


@lru_cache(maxsize=16)
def _build(model: str, base_url: str, api_key: str, temperature: float, timeout: float) -> ChatOpenAI:
    return ChatOpenAI(
        model=model, base_url=base_url, api_key=api_key or "missing",
        temperature=temperature, timeout=timeout, max_retries=2,
    )


def chat_model(tier: Tier = Tier.FAST, temperature: float = 0.6) -> BaseChatModel:
    s = get_settings()
    fast = tier is Tier.FAST
    return _build(
        s.model_fast if fast else s.model_smart,
        s.ollama_base_url,
        s.ollama_api_key,
        temperature,
        s.llm_timeout_fast_s if fast else s.llm_timeout_smart_s,
    )


def run_config(name: str) -> dict[str, Any]:
    return {"callbacks": callbacks(), "run_name": name}


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)


async def complete(
    messages: list[BaseMessage], tier: Tier = Tier.FAST, temperature: float = 0.6, name: str = "complete"
) -> str:
    """Plain-text completion. Raises LLMError on failure or empty output."""
    try:
        out = await chat_model(tier, temperature).ainvoke(messages, config=run_config(name))
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"{tier.value} model call failed: {type(exc).__name__}") from exc
    text = _text_of(out.content).strip()
    if not text:
        raise LLMError(f"{tier.value} model returned empty content")
    return text


def _messages(system: str, user: str | list[BaseMessage]) -> list[BaseMessage]:
    head: list[BaseMessage] = [SystemMessage(system)]
    return head + ([HumanMessage(user)] if isinstance(user, str) else list(user))


async def structured[T: BaseModel](
    schema: type[T], system: str, user: str | list[BaseMessage], tier: Tier = Tier.FAST
) -> T:
    """Return a validated `schema` instance or raise LLMError.

    1) native tool-calling structured output; 2) up to two JSON-mode attempts validated locally.
    """
    messages = _messages(system, user)
    cfg = run_config(f"structured:{schema.__name__}")

    try:
        runnable = chat_model(tier, 0.1).with_structured_output(schema, method="function_calling")
        result = await runnable.ainvoke(messages, config=cfg)
        if isinstance(result, schema):
            return result
        if isinstance(result, dict):
            return schema.model_validate(result)
    except Exception as exc:  # noqa: BLE001 - fall through to JSON mode
        log.debug("llm.structured.tool_mode_failed", schema=schema.__name__, error=type(exc).__name__)

    hint = HumanMessage(
        "Respond with ONLY a JSON object matching this JSON schema, no prose:\n"
        + json.dumps(schema.model_json_schema())
    )
    last: Exception | None = None
    for _ in range(2):
        try:
            raw = await chat_model(tier, 0.1).ainvoke(messages + [hint], config=cfg)
        except Exception as exc:  # noqa: BLE001
            last = exc
            continue
        content = _text_of(raw.content)
        match = _JSON_OBJECT.search(content)
        try:
            return schema.model_validate_json(match.group(0) if match else content)
        except ValidationError as exc:
            last = exc
    raise LLMError(f"could not get a valid {schema.__name__}: {type(last).__name__ if last else 'unknown'}")

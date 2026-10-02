import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from mavis.domain.errors import LLMError
from mavis.llm import models
from mavis.llm.models import Tier
from tests.fakes.llm import FakeLLM


class Sample(BaseModel):
    name: str
    n: int


@pytest.fixture
def scripted(monkeypatch) -> FakeLLM:
    """Patch only chat_model so the REAL structured()/complete() run against scripted outputs."""
    fake = FakeLLM()
    monkeypatch.setattr(models, "chat_model", fake.chat_model)
    return fake


async def test_structured_tool_mode_returns_object(settings, scripted) -> None:
    scripted.push_structured(Sample(name="a", n=1))
    assert await models.structured(Sample, "sys", "user") == Sample(name="a", n=1)


async def test_structured_falls_back_to_json_mode(settings, scripted) -> None:
    scripted.push_error(ValueError("tool calling unsupported"), structured=True)
    scripted.push_text('Sure! {"name": "x", "n": 2} hope that helps')
    assert await models.structured(Sample, "sys", "user") == Sample(name="x", n=2)
    # the JSON-mode prompt carried the schema hint as the last message
    assert "JSON schema" in scripted.calls[-1][-1].content


async def test_structured_falls_back_then_raises_llm_error(settings, scripted) -> None:
    scripted.push_error(ValueError("tool calling unsupported"), structured=True)
    scripted.push_text("not json at all")
    scripted.push_text('{"name": "x"}')  # missing field -> still invalid
    with pytest.raises(LLMError, match="Sample"):
        await models.structured(Sample, "sys", "user")


async def test_structured_accepts_message_list(settings, scripted) -> None:
    scripted.push_structured(Sample(name="a", n=1))
    await models.structured(Sample, "sys", [HumanMessage("hi")])


async def test_complete_returns_text_and_wraps_errors(settings, scripted) -> None:
    scripted.push_text("  hello  ")
    assert await models.complete([SystemMessage("s"), HumanMessage("h")]) == "hello"
    scripted.push_error(TimeoutError())
    with pytest.raises(LLMError):
        await models.complete([HumanMessage("h")])
    scripted.push_text("   ")
    with pytest.raises(LLMError, match="empty"):
        await models.complete([HumanMessage("h")])


def test_chat_model_uses_tier_models(settings) -> None:
    assert models.chat_model(Tier.FAST).model_name == "gpt-oss:20b"
    assert models.chat_model(Tier.SMART).model_name == "gpt-oss:120b"


async def test_fake_llm_fixture_patches_module(fake_llm) -> None:
    fake_llm.push_structured(Sample(name="z", n=9))
    assert await models.structured(Sample, "s", "u") == Sample(name="z", n=9)
    assert fake_llm.structured_calls[0]["schema"] is Sample


# --- fallback chain, concurrency limiter, 429 backoff -------------------------------------------
import asyncio  # noqa: E402

import httpx  # noqa: E402
import openai  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402


class _Chat:
    def __init__(self, name: str, log: list[str], script: list, delay: float = 0.0) -> None:
        self.name, self.log, self.script, self.delay = name, log, script, delay

    async def ainvoke(self, messages, config=None):
        self.log.append(self.name)
        if self.delay:
            await asyncio.sleep(self.delay)
        item = self.script.pop(0) if self.script else AIMessage(content=f"from {self.name}")
        if isinstance(item, Exception):
            raise item
        return item


def _timeout() -> Exception:
    return openai.APITimeoutError(request=httpx.Request("POST", "http://x"))


def _http_429() -> Exception:
    req = httpx.Request("POST", "http://x")
    return openai.RateLimitError("too many concurrent requests",
                                 response=httpx.Response(429, request=req), body=None)


@pytest.fixture
def chain(settings, monkeypatch):
    """Patch chat_model(tier, temperature, model=None) with scripted per-model chats."""
    log: list[str] = []
    scripts: dict[str, list] = {}

    def fake(tier=Tier.FAST, temperature=0.6, model=None):
        name = model or settings.model_fast
        return _Chat(name, log, scripts.setdefault(name, []))

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(models, "chat_model", fake)
    monkeypatch.setattr(models, "_sleep", no_sleep)
    models._limiters.clear()
    return log, scripts, settings


async def test_complete_falls_back_on_timeout(chain) -> None:
    log, scripts, s = chain
    scripts[s.model_fast] = [_timeout()]
    assert await models.complete([HumanMessage("hi")]) == "from gemma4:31b"
    assert log == [s.model_fast, "gemma4:31b"]


async def test_complete_all_models_fail_raises_llm_error(chain) -> None:
    log, scripts, s = chain
    for name in (s.model_fast, *s.model_fast_fallbacks):
        scripts[name] = [_timeout()]
    with pytest.raises(LLMError):
        await models.complete([HumanMessage("hi")])
    assert log == [s.model_fast, *s.model_fast_fallbacks]


async def test_complete_does_not_fall_back_on_non_retriable(chain) -> None:
    log, scripts, s = chain
    scripts[s.model_fast] = [ValueError("bad request")]
    with pytest.raises(LLMError):
        await models.complete([HumanMessage("hi")])
    assert log == [s.model_fast]


async def test_structured_falls_back_on_timeout(chain) -> None:
    log, scripts, s = chain

    class _Struct(_Chat):
        def with_structured_output(self, schema, method=None):
            outer = self

            class R:
                async def ainvoke(self, messages, config=None):
                    outer.log.append(outer.name)
                    if outer.script:
                        raise outer.script.pop(0)
                    return schema(name="ok", n=1)

            return R()

    def fake(tier=Tier.FAST, temperature=0.6, model=None):
        name = model or s.model_fast
        return _Struct(name, log, scripts.setdefault(name, []))

    models.chat_model = fake  # restored by monkeypatch teardown in `chain`
    scripts[s.model_fast] = [_timeout()]
    assert await models.structured(Sample, "sys", "u") == Sample(name="ok", n=1)
    assert log == [s.model_fast, "gemma4:31b"]


async def test_429_retried_on_same_model_then_succeeds(chain) -> None:
    log, scripts, s = chain
    scripts[s.model_fast] = [_http_429(), _http_429()]
    assert await models.complete([HumanMessage("hi")]) == f"from {s.model_fast}"
    assert log == [s.model_fast] * 3


async def test_429_exhausted_moves_to_next_model(chain) -> None:
    log, scripts, s = chain
    scripts[s.model_fast] = [_http_429() for _ in range(4)]
    assert await models.complete([HumanMessage("hi")]) == "from gemma4:31b"
    assert log == [s.model_fast] * 4 + ["gemma4:31b"]


async def test_limiter_caps_concurrency_at_one(chain, monkeypatch) -> None:
    log, scripts, s = chain
    active = peak = 0

    class Slow(_Chat):
        async def ainvoke(self, messages, config=None):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return AIMessage(content="x")

    monkeypatch.setattr(models, "chat_model", lambda *a, **k: Slow("m", log, []))
    await asyncio.gather(models.complete([HumanMessage("a")]), models.complete([HumanMessage("b")]))
    assert peak == 1


async def test_interactive_jumps_ahead_of_queued_background(chain, monkeypatch) -> None:
    log, scripts, s = chain
    order: list[str] = []

    class Tag(_Chat):
        async def ainvoke(self, messages, config=None):
            order.append(messages[0].content)
            await asyncio.sleep(0.02)
            return AIMessage(content="x")

    monkeypatch.setattr(models, "chat_model", lambda *a, **k: Tag("m", log, []))
    first = asyncio.create_task(models.complete([HumanMessage("first")]))
    await asyncio.sleep(0.005)  # `first` holds the only slot
    bg = asyncio.create_task(models.complete([HumanMessage("bg")], priority="background"))
    await asyncio.sleep(0.005)
    fg = asyncio.create_task(models.complete([HumanMessage("fg")]))
    await asyncio.gather(first, bg, fg)
    assert order == ["first", "fg", "bg"]

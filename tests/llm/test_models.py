import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from mavis.domain.errors import LLMError
from mavis.llm import models, share
from mavis.llm.models import Tier
from tests.fakes.llm import FakeLLM


@pytest.fixture(autouse=True)
def _pinned_models(monkeypatch) -> None:
    """These tests exercise the routing, fallback and limiter mechanics, not the production model choice:
    pin a known model set (gpt-oss primaries take reasoning_effort; one LLM slot) so they do not move when
    the defaults do."""
    monkeypatch.setenv("MODEL_FAST", "gpt-oss:20b")
    monkeypatch.setenv("MODEL_SMART", "gpt-oss:120b")
    monkeypatch.setenv("MODEL_FAST_FALLBACKS", '["gemma4:31b", "gpt-oss:120b"]')
    monkeypatch.setenv("MODEL_SMART_FALLBACKS", '["gpt-oss:20b"]')
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "1")


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
    scripted.push_error(ValueError("boom"))
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


def _http_500() -> Exception:
    req = httpx.Request("POST", "http://x")
    return openai.InternalServerError("boom", response=httpx.Response(500, request=req), body=None)


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

    settings.llm_timeout_cooldown_s = 0.05
    monkeypatch.setattr(models, "chat_model", fake)
    monkeypatch.setattr(models, "_sleep", no_sleep)
    models._limiters.clear()
    return log, scripts, settings


async def test_complete_falls_back_on_5xx(chain) -> None:
    log, scripts, s = chain
    scripts[s.model_fast] = [_http_500()]
    assert await models.complete([HumanMessage("hi")]) == "from gemma4:31b"
    assert log == [s.model_fast, "gemma4:31b"]


async def test_complete_all_models_fail_raises_llm_error(chain) -> None:
    log, scripts, s = chain
    for name in (s.model_fast, *s.model_fast_fallbacks):
        scripts[name] = [_http_500()]
    with pytest.raises(LLMError):
        await models.complete([HumanMessage("hi")])
    assert log == [s.model_fast, *s.model_fast_fallbacks]


async def test_complete_does_not_fall_back_on_non_retriable(chain) -> None:
    log, scripts, s = chain
    scripts[s.model_fast] = [ValueError("bad request")]
    with pytest.raises(LLMError):
        await models.complete([HumanMessage("hi")])
    assert log == [s.model_fast]


async def test_structured_falls_back_on_5xx(chain) -> None:
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
    scripts[s.model_fast] = [_http_500()]
    assert await models.structured(Sample, "sys", "u") == Sample(name="ok", n=1)
    assert log == [s.model_fast, "gemma4:31b"]


async def test_429_retried_on_same_model_then_succeeds(chain) -> None:
    log, scripts, s = chain
    scripts[s.model_fast] = [_http_429(), _http_429()]
    assert await models.complete([HumanMessage("hi")]) == f"from {s.model_fast}"
    assert log == [s.model_fast] * 3


async def test_429_never_falls_back_to_another_ollama_model(chain) -> None:
    log, scripts, s = chain
    scripts[s.model_fast] = [_http_429() for _ in range(20)]
    with pytest.raises(LLMError):
        await models.complete([HumanMessage("hi")])
    assert set(log) == {s.model_fast}


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


async def test_background_waiter_ages_into_interactive(chain, monkeypatch) -> None:
    log, scripts, s = chain
    order: list[str] = []

    class Tag(_Chat):
        async def ainvoke(self, messages, config=None):
            order.append(messages[0].content)
            await asyncio.sleep(0.05)
            return AIMessage(content="x")

    monkeypatch.setattr(models, "chat_model", lambda *a, **k: Tag("m", log, []))
    monkeypatch.setattr(models, "BACKGROUND_AGING_S", 0.02)
    first = asyncio.create_task(models.complete([HumanMessage("first")]))
    await asyncio.sleep(0.005)
    bg = asyncio.create_task(models.complete([HumanMessage("bg")], priority="background"))
    await asyncio.sleep(0.03)  # bg is now older than the aging threshold
    fg = asyncio.create_task(models.complete([HumanMessage("fg")]))
    await asyncio.gather(first, bg, fg)
    assert order == ["first", "bg", "fg"]


async def test_background_acquire_times_out_with_llm_error(chain, monkeypatch) -> None:
    log, scripts, s = chain

    class Hold(_Chat):
        async def ainvoke(self, messages, config=None):
            await asyncio.sleep(0.2)
            return AIMessage(content="x")

    monkeypatch.setattr(models, "chat_model", lambda *a, **k: Hold("m", log, []))
    monkeypatch.setattr(models, "BACKGROUND_ACQUIRE_TIMEOUT_S", 0.03)
    holder = asyncio.create_task(models.complete([HumanMessage("a")]))
    await asyncio.sleep(0.005)
    with pytest.raises(LLMError):
        await models.complete([HumanMessage("b")], priority="background")
    await holder


async def test_interactive_deadline_covers_queue_and_attempts(chain, monkeypatch) -> None:
    log, scripts, s = chain

    class Slow(_Chat):
        async def ainvoke(self, messages, config=None):
            await asyncio.sleep(5)
            return AIMessage(content="x")

    monkeypatch.setattr(models, "chat_model", lambda *a, **k: Slow("m", log, []))
    monkeypatch.setattr(models, "INTERACTIVE_DEADLINE_S", 0.05)
    t0 = asyncio.get_running_loop().time()
    with pytest.raises(LLMError):
        await models.complete([HumanMessage("a")])
    assert asyncio.get_running_loop().time() - t0 < 1


async def test_deadline_applies_while_queued(chain, monkeypatch) -> None:
    log, scripts, s = chain

    class Hold(_Chat):
        async def ainvoke(self, messages, config=None):
            await asyncio.sleep(0.3)
            return AIMessage(content="x")

    monkeypatch.setattr(models, "chat_model", lambda *a, **k: Hold("m", log, []))
    monkeypatch.setattr(models, "INTERACTIVE_DEADLINE_S", 0.05)
    holder = asyncio.create_task(models.complete([HumanMessage("a")]))  # times out itself at 0.05
    await asyncio.sleep(0.001)
    with pytest.raises(LLMError):
        await models.complete([HumanMessage("b")])
    with pytest.raises(LLMError):
        await holder


async def test_cancelled_waiter_does_not_leak_slot(chain, monkeypatch) -> None:
    log, scripts, s = chain

    class Hold(_Chat):
        async def ainvoke(self, messages, config=None):
            await asyncio.sleep(0.03)
            return AIMessage(content="x")

    monkeypatch.setattr(models, "chat_model", lambda *a, **k: Hold("m", log, []))
    holder = asyncio.create_task(models.complete([HumanMessage("a")]))
    await asyncio.sleep(0.005)
    waiter = asyncio.create_task(models.complete([HumanMessage("b")]))
    await asyncio.sleep(0.005)
    waiter.cancel()
    holder2 = asyncio.create_task(models.complete([HumanMessage("c")]))
    await holder
    assert await asyncio.wait_for(holder2, 1) == "x"
    # cancelling a call that HOLDS the slot also frees it
    running = asyncio.create_task(models.complete([HumanMessage("d")]))
    await asyncio.sleep(0.005)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert await asyncio.wait_for(models.complete([HumanMessage("e")]), 1) == "x"


async def test_retry_after_header_is_capped(chain, monkeypatch) -> None:
    log, scripts, s = chain
    slept: list[float] = []

    async def rec(sec):
        slept.append(sec)

    monkeypatch.setattr(models, "_sleep", rec)
    req = httpx.Request("POST", "http://x")
    err = openai.RateLimitError(
        "too many concurrent requests",
        response=httpx.Response(429, request=req, headers={"retry-after": "99"}), body=None,
    )
    scripts[s.model_fast] = [err]
    assert await models.complete([HumanMessage("hi")]) == f"from {s.model_fast}"
    assert len(slept) == 1 and 29 < slept[0] <= models.RETRY_AFTER_CAP_S


async def test_structured_moves_to_next_model_when_tool_and_json_modes_both_fail(chain) -> None:
    log, scripts, s = chain

    class M(_Chat):
        def with_structured_output(self, schema, method=None):
            class R:
                async def ainvoke(self, messages, config=None):
                    raise ValueError("tool calling unsupported")

            return R()

        async def ainvoke(self, messages, config=None):
            self.log.append(self.name)
            good = self.name == "gemma4:31b"
            return AIMessage(content='{"name": "ok", "n": 3}' if good else "garbage")

    models.chat_model = lambda tier=Tier.FAST, temperature=0.6, model=None: M(
        model or s.model_fast, log, []
    )
    assert await models.structured(Sample, "sys", "u") == Sample(name="ok", n=3)
    assert log == [s.model_fast, s.model_fast, "gemma4:31b"]


async def test_release_between_waiter_cancel_and_cleanup_does_not_leak_slot() -> None:
    lim = models._Limiter(1)
    await lim.acquire("interactive", 1)  # holds the only slot
    waiter = asyncio.create_task(lim.acquire("interactive", 5))
    await asyncio.sleep(0)  # waiter is queued
    waiter.cancel()  # its future is cancelled now, but the task has not run its cleanup yet
    lim.release()  # must skip the dead waiter and must not raise
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.wait_for(lim.acquire("interactive", 1), 1)  # slot still usable
    lim.release()
    await asyncio.wait_for(lim.acquire("background", 1), 1)


async def test_timed_out_waiter_is_dropped_before_release() -> None:
    lim = models._Limiter(1)
    await lim.acquire("interactive", 1)
    with pytest.raises(LLMError):
        await lim.acquire("interactive", 0.01)
    lim.release()
    await asyncio.wait_for(lim.acquire("interactive", 1), 1)


async def test_slot_granted_then_cancel_is_passed_on() -> None:
    lim = models._Limiter(1)
    await lim.acquire("interactive", 1)
    waiter = asyncio.create_task(lim.acquire("interactive", 5))
    await asyncio.sleep(0)
    lim.release()  # grants the slot to the waiter (future result set)
    waiter.cancel()  # cancelled in the same tick, before it resumes
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.wait_for(lim.acquire("interactive", 1), 1)


async def test_background_complete_makes_a_single_model_attempt(chain) -> None:
    log, scripts, s = chain
    scripts[s.model_fast] = [_http_500()]
    with pytest.raises(LLMError):
        await models.complete([HumanMessage("learn")], priority="background")
    assert log == [s.model_fast]  # no walk down the fallback chain


async def test_background_structured_makes_a_single_model_attempt(chain, monkeypatch) -> None:
    log, scripts, s = chain

    class _Struct(_Chat):
        def with_structured_output(self, schema, method=None):
            outer = self

            class R:
                async def ainvoke(self, messages, config=None):
                    outer.log.append(outer.name)
                    raise _http_500()

            return R()

    def fake(tier=Tier.FAST, temperature=0.6, model=None):
        return _Struct(model or s.model_fast, log, [])

    monkeypatch.setattr(models, "chat_model", fake)
    with pytest.raises(LLMError):
        await models.structured(Sample, "sys", "u", priority="background")
    assert log == [s.model_fast]


async def test_background_can_opt_back_into_fallback(chain) -> None:
    log, scripts, s = chain
    scripts[s.model_fast] = [_http_500()]
    out = await models.complete([HumanMessage("x")], priority="background", fallback=True)
    assert out == "from gemma4:31b" and log == [s.model_fast, "gemma4:31b"]


async def test_interactive_can_disable_fallback(chain) -> None:
    log, scripts, s = chain
    scripts[s.model_fast] = [_http_500()]
    with pytest.raises(LLMError):
        await models.complete([HumanMessage("x")], fallback=False)
    assert log == [s.model_fast]


# --- hotfix: server occupancy, global backoff, no same-provider fallback, secondary provider -------


async def test_timeout_holds_slot_for_cooldown_then_retries_same_model(chain) -> None:
    log, scripts, s = chain
    scripts[s.model_fast] = [_timeout()]
    t0 = asyncio.get_running_loop().time()
    assert await models.complete([HumanMessage("hi")]) == f"from {s.model_fast}"
    # the abandoned request still occupies the account: the retry waited out the cooldown
    assert asyncio.get_running_loop().time() - t0 >= 0.04
    assert log == [s.model_fast, s.model_fast]  # no fallback to another Ollama model


async def test_slot_stays_occupied_during_cooldown(chain) -> None:
    log, scripts, s = chain
    s.llm_timeout_cooldown_s = 0.2
    scripts[s.model_fast] = [_timeout()]
    task = asyncio.create_task(models.complete([HumanMessage("a")]))
    await asyncio.sleep(0.05)
    assert models._limiter()._free == 0  # held although the call itself already timed out
    assert models._ollama.unavailable_s() > 0
    assert await task == f"from {s.model_fast}"


async def test_429_sets_global_backoff_increasing_and_reset_on_success(chain, monkeypatch) -> None:
    log, scripts, s = chain
    slept: list[float] = []

    async def rec(sec):
        slept.append(sec)

    monkeypatch.setattr(models, "_sleep", rec)
    scripts[s.model_fast] = [_http_429(), _http_429(), _http_429()]
    assert await models.complete([HumanMessage("hi")], priority="background") == f"from {s.model_fast}"
    # 5s, 10s, 20s steps (minus elapsed microseconds); first attempt has no wait
    assert [round(x) for x in slept] == [5, 10, 20]
    assert models._ollama.level == 0 and models._ollama.backoff_remaining() == 0  # reset on success


def test_backoff_steps_cap_at_30() -> None:
    st = models._OllamaState()
    assert [st.note_rate_limit(None) for _ in range(6)] == [5, 10, 20, 30, 30, 30]
    st.note_success()
    assert st.note_rate_limit(None) == 5


async def test_backoff_is_global_across_callers(chain, monkeypatch) -> None:
    log, scripts, s = chain
    slept: list[float] = []

    async def rec(sec):
        slept.append(sec)

    monkeypatch.setattr(models, "_sleep", rec)
    models._ollama.note_rate_limit(None)  # another caller just got a 429
    await models.complete([HumanMessage("bg")], priority="background")
    assert len(slept) == 1 and 4 < slept[0] <= 5  # a different call waits too


async def test_interactive_fails_with_llm_error_when_backoff_outlasts_deadline(chain, monkeypatch) -> None:
    log, scripts, s = chain
    monkeypatch.setattr(models, "INTERACTIVE_DEADLINE_S", 1.0)
    models._ollama.note_rate_limit(None)  # 5s backoff > 1s deadline
    with pytest.raises(LLMError):
        await models.complete([HumanMessage("hi")])
    assert log == []  # never even called Ollama


async def test_timeout_does_not_fall_back_to_other_models(chain) -> None:
    log, scripts, s = chain
    scripts[s.model_fast] = [_timeout() for _ in range(10)]
    with pytest.raises(LLMError):
        await models.complete([HumanMessage("hi")])
    assert set(log) == {s.model_fast}


class _Sec(_Chat):
    def with_structured_output(self, schema, method=None):
        outer = self

        class R:
            async def ainvoke(self, messages, config=None):
                outer.log.append(outer.name)
                if outer.script:
                    raise outer.script.pop(0)
                return schema(name="sec", n=7)

        return R()


@pytest.fixture
def secondary(chain, monkeypatch):
    log, scripts, s = chain
    s.llm_secondary_base_url = "https://sec.example/v1"
    s.llm_secondary_model_fast = "sec-fast"
    s.llm_secondary_model_smart = "sec-smart"
    sec_scripts: list = []
    monkeypatch.setattr(
        models, "secondary_chat_model",
        lambda tier=Tier.FAST, temperature=0.6: _Sec("secondary", log, sec_scripts),
    )
    return log, scripts, s


async def test_secondary_not_used_when_unconfigured(chain) -> None:
    log, scripts, s = chain
    assert not models._secondary_ready(Tier.FAST)


@pytest.mark.parametrize("err", [_timeout, _http_429])
async def test_complete_falls_back_once_to_secondary(secondary, err) -> None:
    log, scripts, s = secondary
    scripts[s.model_fast] = [err()]
    assert await models.complete([HumanMessage("hi")]) == "from secondary"
    assert log == [s.model_fast, "secondary"]


async def test_connection_error_falls_back_to_secondary(secondary) -> None:
    log, scripts, s = secondary
    scripts[s.model_fast] = [openai.APIConnectionError(request=httpx.Request("POST", "http://x"))]
    assert await models.complete([HumanMessage("hi")]) == "from secondary"


async def test_secondary_failure_raises_llm_error(secondary, monkeypatch) -> None:
    log, scripts, s = secondary
    scripts[s.model_fast] = [_http_429()]
    monkeypatch.setattr(models, "secondary_chat_model",
                        lambda tier=Tier.FAST, temperature=0.6: _Chat("secondary", log, [_http_429()]))
    with pytest.raises(LLMError):
        await models.complete([HumanMessage("hi")])
    assert log == [s.model_fast, "secondary"]


async def test_interactive_goes_straight_to_secondary_during_cooldown(secondary) -> None:
    log, scripts, s = secondary
    models._ollama.note_rate_limit(None)
    assert await models.complete([HumanMessage("hi")]) == "from secondary"
    assert log == ["secondary"]


async def test_background_waits_for_ollama_not_secondary_during_backoff(secondary, monkeypatch) -> None:
    log, scripts, s = secondary
    models._ollama.note_rate_limit(None)
    assert await models.complete([HumanMessage("bg")], priority="background") == f"from {s.model_fast}"
    assert log == [s.model_fast]


async def test_structured_falls_back_to_secondary(secondary, monkeypatch) -> None:
    log, scripts, s = secondary

    class _Primary(_Sec):
        pass

    prim_script = [_http_429()]
    monkeypatch.setattr(
        models, "chat_model",
        lambda tier=Tier.FAST, temperature=0.6, model=None: _Primary(model or s.model_fast, log, prim_script),
    )
    assert await models.structured(Sample, "sys", "u") == Sample(name="sec", n=7)
    assert log == [s.model_fast, "secondary"]


async def test_secondary_has_its_own_limiter(secondary) -> None:
    models._limiter()._free = 0  # Ollama slot held (cooldown)
    assert models._limiter(secondary=True)._free == 2


# --- invoke_tools: the tool-calling path shares complete()'s chain ------------------------------


class _Bindable(_Chat):
    def bind_tools(self, tools, **kwargs):
        self.bound = list(tools)
        self.log.append(f"bind:{self.name}:{len(self.bound)}")
        return self


def _tool_ai() -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "echo", "args": {"text": "x"}, "id": "c1"}])


@pytest.fixture
def bindable(chain, monkeypatch):
    log, scripts, s = chain

    def fake(tier=Tier.FAST, temperature=0.6, model=None):
        name = model or s.model_fast
        return _Bindable(name, log, scripts.setdefault(name, []))

    monkeypatch.setattr(models, "chat_model", fake)
    return log, scripts, s


async def test_invoke_tools_binds_tools_and_allows_empty_content_with_calls(bindable) -> None:
    log, scripts, s = bindable
    scripts[s.model_fast] = [_tool_ai()]
    out = await models.invoke_tools([HumanMessage("hi")], ["t1", "t2"])
    assert out.tool_calls[0]["name"] == "echo"
    assert log == [f"bind:{s.model_fast}:2", s.model_fast]


async def test_invoke_tools_empty_content_without_calls_raises(bindable) -> None:
    log, scripts, s = bindable
    scripts[s.model_fast] = [AIMessage(content="  ")]
    with pytest.raises(LLMError):
        await models.invoke_tools([HumanMessage("hi")], ["t"])


async def test_invoke_tools_falls_back_on_5xx(bindable) -> None:
    log, scripts, s = bindable
    scripts[s.model_fast] = [_http_500()]
    out = await models.invoke_tools([HumanMessage("hi")], ["t"])
    assert out.content.startswith("from ")
    assert len([x for x in log if not x.startswith("bind:")]) == 2


async def test_invoke_tools_never_falls_back_to_another_ollama_model_on_429(bindable) -> None:
    log, scripts, s = bindable
    scripts[s.model_fast] = [_http_429()]
    await models.invoke_tools([HumanMessage("hi")], ["t"])
    assert [x for x in log if not x.startswith("bind:")] == [s.model_fast, s.model_fast]


async def test_invoke_tools_uses_secondary_provider_with_tools_bound(bindable, monkeypatch) -> None:
    log, scripts, s = bindable
    s.llm_secondary_base_url = "https://sec.example/v1"
    s.llm_secondary_model_fast = "sec-fast"
    s.llm_secondary_model_smart = "sec-smart"
    monkeypatch.setattr(models, "secondary_chat_model",
                        lambda tier=Tier.FAST, temperature=0.6: _Bindable("secondary", log, [_tool_ai()]))
    scripts[s.model_fast] = [_timeout()]
    out = await models.invoke_tools([HumanMessage("hi")], ["t"])
    assert out.tool_calls[0]["id"] == "c1"
    assert log == [f"bind:{s.model_fast}:1", s.model_fast, "bind:secondary:1", "secondary"]


# --- hotfix3 RC1: best_effort yields to interactive/background; FAST reasoning effort -----------


async def test_best_effort_fails_fast_while_interactive_waits(monkeypatch) -> None:
    monkeypatch.setattr(models, "INTERACTIVE_GRACE_S", 0.0)
    lim = models._Limiter(1)
    await lim.acquire("best_effort", 1)  # a LEARN call holds the slot
    fg = asyncio.create_task(lim.acquire("interactive", 5))
    await asyncio.sleep(0)
    t0 = asyncio.get_running_loop().time()
    with pytest.raises(LLMError, match="interactive"):
        await lim.acquire("best_effort", 5)
    assert asyncio.get_running_loop().time() - t0 < 0.5
    lim.release()
    await asyncio.wait_for(fg, 1)


async def test_best_effort_fails_fast_right_after_interactive_use(monkeypatch) -> None:
    monkeypatch.setattr(models, "INTERACTIVE_GRACE_S", 20.0)
    lim = models._Limiter(1)
    await lim.acquire("interactive", 1)
    lim.release()
    with pytest.raises(LLMError, match="interactive"):
        await lim.acquire("best_effort", 5)  # slot is free, but a chat just used it


async def test_best_effort_runs_once_grace_passed(monkeypatch) -> None:
    monkeypatch.setattr(models, "INTERACTIVE_GRACE_S", 0.01)
    lim = models._Limiter(1)
    await lim.acquire("interactive", 1)
    lim.release()
    await asyncio.sleep(0.02)
    await asyncio.wait_for(lim.acquire("best_effort", 1), 1)


async def test_background_task_work_still_queues_after_chat(monkeypatch) -> None:
    monkeypatch.setattr(models, "INTERACTIVE_GRACE_S", 20.0)
    lim = models._Limiter(1)
    await lim.acquire("interactive", 1)
    lim.release()
    await asyncio.wait_for(lim.acquire("background", 1), 1)  # task runs are not failed fast


async def test_queued_best_effort_waiter_is_failed_when_interactive_arrives(monkeypatch) -> None:
    monkeypatch.setattr(models, "INTERACTIVE_GRACE_S", 20.0)
    monkeypatch.setattr(models, "BACKGROUND_AGING_S", 0.0)  # best_effort never ages ahead of anyone
    lim = models._Limiter(1)
    await lim.acquire("best_effort", 1)  # holder, no recent chat
    bg = asyncio.create_task(lim.acquire("best_effort", 5))
    await asyncio.sleep(0.01)  # bg queued for a while
    fg = asyncio.create_task(lim.acquire("interactive", 5))
    await asyncio.sleep(0)
    with pytest.raises(LLMError):
        await asyncio.wait_for(bg, 1)  # failed as soon as the chat queued, not after the holder
    lim.release()
    await asyncio.wait_for(fg, 1)


async def test_best_effort_timeout_is_not_retried(chain) -> None:
    log, scripts, s = chain
    scripts[s.model_fast] = [_timeout(), _timeout()]
    with pytest.raises(LLMError):
        await models.complete([HumanMessage("learn")], priority="best_effort")
    assert log == [s.model_fast]  # one attempt: no same-model retry after the cooldown


async def test_best_effort_does_not_wait_out_a_429_backoff(chain) -> None:
    log, scripts, s = chain
    models._ollama.note_rate_limit(None)
    with pytest.raises(LLMError):
        await models.complete([HumanMessage("learn")], priority="best_effort")
    assert log == []


async def test_memory_calls_are_best_effort() -> None:
    import inspect

    from mavis.memory import consolidate, extractor, summaries

    for mod in (extractor, summaries, consolidate):
        assert 'priority="best_effort"' in inspect.getsource(mod), mod.__name__


def test_fast_tier_gets_reasoning_effort(settings) -> None:
    models._build.cache_clear()
    assert settings.llm_reasoning_effort_fast == "low"
    assert models.chat_model(Tier.FAST).reasoning_effort == "low"
    assert models.chat_model(Tier.SMART).reasoning_effort is None


def test_empty_reasoning_effort_is_not_sent(settings) -> None:
    models._build.cache_clear()
    settings.llm_reasoning_effort_fast = ""
    assert models.chat_model(Tier.FAST).reasoning_effort is None
    settings.llm_reasoning_effort_fast = "medium"
    assert models.chat_model(Tier.FAST).reasoning_effort == "medium"  # cache key includes it


def test_reasoning_effort_only_for_gpt_oss_models(settings) -> None:
    models._build.cache_clear()
    assert models.chat_model(Tier.FAST, model="gemma4:31b").reasoning_effort is None
    assert models.chat_model(Tier.FAST, model="gpt-oss:120b").reasoning_effort == "low"  # FAST fallback
    settings.model_fast = "llama3.3:70b"
    assert models.chat_model(Tier.FAST).reasoning_effort is None


# --- several slots: the policy is mavis.llm.share (tested there); here the limiter applies it ----------


@pytest.fixture
def fast_share(monkeypatch):
    monkeypatch.setattr(share, "MIN_START_GAP_S", 0.0)
    monkeypatch.setattr(share, "LULL_QUIET_S", 0.03)
    monkeypatch.setattr(share, "ESCAPE_AFTER_S", 0.05)
    monkeypatch.setattr(share, "ESCAPE_CHAT_GAP_S", 0.03)
    monkeypatch.setattr(models, "BEST_EFFORT_TICK_S", 0.01)


@pytest.mark.parametrize("size", [3, 5])
async def test_best_effort_runs_next_to_a_busy_chat_it_does_not_wait_for_a_lull(fast_share, size) -> None:
    """Live E2E: LEARN refused while chat was merely active. A guaranteed share runs beside a chat call."""
    lim = models._Limiter(size)
    await lim.acquire("interactive", 1)  # a chat call is in flight right now
    assert await asyncio.wait_for(lim.acquire("best_effort", 1), 1) == share.TIMEOUT_S


async def test_best_effort_waits_for_two_free_slots_and_keeps_the_last_one(fast_share) -> None:
    lim = models._Limiter(3)
    await lim.acquire("interactive", 1)
    await lim.acquire("interactive", 1)  # one free: the chat's next call needs it
    be = asyncio.create_task(lim.acquire("best_effort", 5))
    await asyncio.sleep(0.02)
    assert not be.done()
    await asyncio.wait_for(lim.acquire("interactive", 1), 1)  # the chat still gets it at once
    be.cancel()


async def test_best_effort_share_is_one_of_three_slots(fast_share) -> None:
    lim = models._Limiter(3)
    await lim.acquire("interactive", 1)
    await lim.acquire("best_effort", 1)
    second = asyncio.create_task(lim.acquire("best_effort", 5))
    await asyncio.sleep(0.02)
    assert not second.done()  # cap 1 while a chat call runs
    second.cancel()


async def test_best_effort_never_delays_a_waiting_chat_call(fast_share) -> None:
    lim = models._Limiter(3)
    for _ in range(3):
        await lim.acquire("interactive", 1)
    be = asyncio.create_task(lim.acquire("best_effort", 5))
    chat = asyncio.create_task(lim.acquire("interactive", 5))
    await asyncio.sleep(0.01)
    lim.release()
    await asyncio.wait_for(chat, 1)  # the freed slot goes to the chat
    assert not be.done()
    be.cancel()


async def test_old_waiter_takes_the_last_slot_by_timer_with_a_clamped_timeout(fast_share) -> None:
    """Starvation escape: no acquire/release happens once the waiter is old enough; the timer must notice."""
    lim = models._Limiter(3)
    await lim.acquire("interactive", 1)
    await lim.acquire("interactive", 1)  # exactly one free slot
    be = asyncio.create_task(lim.acquire("best_effort", 5))
    await asyncio.sleep(0.02)
    assert not be.done()
    assert await asyncio.wait_for(be, 1) == share.ESCAPE_TIMEOUT_S


async def test_old_waiter_does_not_take_the_last_slot_while_a_chat_call_is_queued(fast_share) -> None:
    lim = models._Limiter(3)
    await lim.acquire("interactive", 1)
    await lim.acquire("interactive", 1)
    await lim.acquire("interactive", 1)
    be = asyncio.create_task(lim.acquire("best_effort", 5))
    chat = asyncio.create_task(lim.acquire("interactive", 5))
    await asyncio.sleep(0.12)  # old enough, but a chat call is queued
    assert not be.done() and not chat.done()
    lim.release()
    await asyncio.wait_for(chat, 1)
    be.cancel()


async def test_best_effort_timer_stops_when_nobody_waits(fast_share) -> None:
    lim = models._Limiter(3)
    for _ in range(3):
        await lim.acquire("interactive", 1)
    be = asyncio.create_task(lim.acquire("best_effort", 5))
    await asyncio.sleep(0.03)
    be.cancel()
    await asyncio.sleep(0.05)
    assert lim._tick is None


async def test_best_effort_call_timeout_is_the_short_one_and_holds_no_cooldown(chain, monkeypatch) -> None:
    """A timed-out LEARN call frees its slot at once and does not start the 45 s cooldown."""
    log, scripts, settings = chain
    monkeypatch.setattr(share, "TIMEOUT_S", 0.05)
    models._limiters.clear()
    settings.llm_max_concurrency = 3

    class Slow(_Chat):
        async def ainvoke(self, *a, **k):
            await asyncio.sleep(5)

    monkeypatch.setattr(models, "chat_model", lambda *a, **k: Slow("m", [], []))
    with pytest.raises(LLMError):
        await models.complete([HumanMessage("learn")], priority="best_effort")
    lim = next(iter(models._limiters.values()))
    assert lim._free == 3 and lim._be_inflight == 0
    assert models._ollama.cooldown_until == 0.0


# --- E2E run 2 (R4): a background timeout never blocks interactive work -------------------------------


async def test_background_timeout_marks_only_the_background_lane(chain) -> None:
    log, scripts, s = chain
    s.llm_timeout_cooldown_s = 0.5
    scripts[s.model_fast] = [_timeout()]
    task = asyncio.create_task(models.complete([HumanMessage("bg")], priority="background"))
    await asyncio.sleep(0.05)
    assert models._ollama.unavailable_s("interactive") <= 0  # chat sees a healthy account
    assert models._ollama.unavailable_s("background") > 0
    assert models.unavailable_s() > 0  # background drains still wait it out
    await task


async def test_interactive_timeout_still_marks_the_interactive_lane(chain) -> None:
    log, scripts, s = chain
    s.llm_timeout_cooldown_s = 0.5
    scripts[s.model_fast] = [_timeout()]
    task = asyncio.create_task(models.complete([HumanMessage("chat")]))
    await asyncio.sleep(0.05)
    assert models._ollama.unavailable_s("interactive") > 0
    await task


async def test_chat_takes_back_the_slot_a_timed_out_background_call_holds(chain) -> None:
    """One slot, held 45 s after a background timeout: a chat reply must not wait for it."""
    log, scripts, s = chain
    s.llm_timeout_cooldown_s = 30
    scripts[s.model_fast] = [_timeout()]  # the background attempt; its retry then queues for a slot
    bg = asyncio.create_task(models.complete([HumanMessage("bg")], priority="background"))
    await asyncio.sleep(0.05)
    assert models._limiter()._free == 0  # the abandoned request still occupies the slot
    t0 = asyncio.get_running_loop().time()
    assert await asyncio.wait_for(models.complete([HumanMessage("chat")]), 2) == f"from {s.model_fast}"
    assert asyncio.get_running_loop().time() - t0 < 1
    bg.cancel()


async def test_background_timeout_does_not_send_chat_to_the_secondary(secondary) -> None:
    log, scripts, s = secondary
    s.llm_timeout_cooldown_s = 0.5
    models._ollama.note_timeout("background")
    assert not models._prefer_secondary(Tier.FAST, "interactive")
    models._ollama.note_timeout("interactive")
    assert models._prefer_secondary(Tier.FAST, "interactive")


async def test_rate_limit_backoff_stays_global(chain) -> None:
    models._ollama.note_rate_limit(None)
    assert models._ollama.unavailable_s("interactive") > 0 and models._ollama.unavailable_s("background") > 0

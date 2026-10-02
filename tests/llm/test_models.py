import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from tests.fakes.llm import FakeLLM
from zento.domain.errors import LLMError
from zento.llm import models
from zento.llm.models import Tier


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

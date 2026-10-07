"""H1: a background loop that runs out of tool rounds answers from what it gathered (no discarded work)."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from mavis.agents import spawn as spawn_mod
from mavis.agents.react import WRAP_UP_NOTE
from mavis.agents.specialists import get_specialist
from mavis.agents.specialists.base import Specialist, run_specialist
from mavis.domain.policy import RiskClass
from mavis.tools.registry import MavisTool
from tests.conftest import SendNoteArgs


def _register_lookup(registry, name: str, agents: set[str]) -> list[str]:
    seen: list[str] = []

    async def _look(user_id: int, args: SendNoteArgs) -> str:
        seen.append(args.text)
        return f"{name} found: {args.text}"

    registry.register(MavisTool(name=name, description=f"{name} lookup", args_model=SendNoteArgs,
                                risk=RiskClass.READ, fn=_look, agents=frozenset(agents)))
    return seen


def _keep_calling(fake_llm, tool: str, rounds: int, final: str) -> None:
    for i in range(rounds):
        fake_llm.push_ai(AIMessage(content="", tool_calls=[
            {"name": tool, "args": {"text": f"q{i}"}, "id": f"c{i}"}]))
    # the wrap-up call: even if the model asks for another tool, it is ignored and its text is used
    fake_llm.push_ai(AIMessage(content=final,
                               tool_calls=[{"name": tool, "args": {"text": "more"}, "id": "x"}]))


@pytest.mark.parametrize("agent,tool,steps", [
    ("research", "web_search", 3), ("calendar", "calendar_list", 2), ("knowledge", "memory_lookup", 4),
])
async def test_specialist_out_of_rounds_wraps_up_with_what_it_gathered(user, fresh_registry, fake_llm,
                                                                      agent, tool, steps):
    seen = _register_lookup(fresh_registry, tool, {agent})
    spec = Specialist(name=agent, description="d", prompt="p", tool_names=(tool,), max_steps=steps)
    _keep_calling(fake_llm, tool, steps, f"Here is what I found so far about {agent}.")
    out = await run_specialist(spec, user.id, "dig")
    assert out.ok and out.partial
    assert out.text == f"Here is what I found so far about {agent}."
    assert seen == [f"q{i}" for i in range(steps)]  # the wrap-up's extra call never ran
    assert WRAP_UP_NOTE in fake_llm.calls[-1][-1].content


async def test_specialist_finishing_in_budget_is_not_partial(user, fresh_registry, fake_llm):
    _register_lookup(fresh_registry, "web_search", {"research"})
    spec = Specialist(name="research", description="d", prompt="p", tool_names=("web_search",), max_steps=3)
    fake_llm.push_ai(AIMessage(content="",
                               tool_calls=[{"name": "web_search", "args": {"text": "a"}, "id": "1"}]))
    fake_llm.push_text("Complete answer [1]")
    out = await run_specialist(spec, user.id, "dig")
    assert out.ok and not out.partial


async def test_wrap_up_with_no_answer_is_a_failed_step(user, fresh_registry, fake_llm):
    _register_lookup(fresh_registry, "web_search", {"research"})
    spec = Specialist(name="research", description="d", prompt="p", tool_names=("web_search",), max_steps=1)
    _keep_calling(fake_llm, "web_search", 1, "")
    out = await run_specialist(spec, user.id, "dig")
    assert not out.ok and out.partial


async def test_spawned_worker_wraps_up_too(user, fresh_registry, fake_llm):
    _register_lookup(fresh_registry, "price_check", {"spawn"})
    _keep_calling(fake_llm, "price_check", 2, "Two prices found: 300 and 420.")
    out = await spawn_mod.spawn_agent(user.id, "pricer", "find prices", ["price_check"],
                                      budget=spawn_mod.Budget(max_steps=2))
    assert out.ok and out.partial and "420" in out.text


def test_research_round_budget_is_configurable(settings, monkeypatch):
    from mavis.agents.specialists.base import step_budget
    from mavis.config import get_settings

    research = get_specialist("research")
    assert step_budget(research) == get_settings().research_max_steps >= 8
    monkeypatch.setenv("RESEARCH_MAX_STEPS", "13")
    get_settings.cache_clear()
    assert step_budget(research) == 13
    assert step_budget(get_specialist("knowledge")) == get_specialist("knowledge").max_steps

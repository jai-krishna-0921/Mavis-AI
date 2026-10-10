"""The studio: skills catalogue (Agent Skills format), per-user brand, the sub-agent and its tools."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mavis.domain.errors import LLMError
from mavis.store.repo import outbox, users
from mavis.studio import agent, brand, skills, tools
from mavis.studio.spec import DeckSpec, DocSpec, Section, Slide


def test_every_skill_follows_the_format_and_carries_no_dashes():
    found = skills.all_skills()
    assert {"deck-design", "doc-design", "sheet-design", "anti-slop", "brand-personalisation"} <= set(found)
    for s in found.values():
        assert s.description and len(s.description) <= 1024 and s.body
        assert "—" not in s.body and "–" not in s.body, s.name
    assert {s.renderer for s in found.values() if s.renderer} == {"deck", "doc", "sheet"}
    assert [s.name for s in skills.for_kind("deck")] == ["deck-design", "anti-slop", "brand-personalisation"]
    for name in ("deck-design", "doc-design", "sheet-design"):
        assert (skills.ROOT / name / "scripts" / "render.py").exists()


async def test_load_skill_tool_loads_on_demand_and_reads_only_named_sources():
    loaded: dict[str, str] = {}
    load, read = agent._skill_tools(loaded, [], 1, ["f1"])
    assert "Loaded pitch-deck" in await load.ainvoke({"name": "pitch-deck"})
    assert "pitch-deck" in loaded and "No skill named" in await load.ainvoke({"name": "nope"})
    assert "Only the source files" in await read.ainvoke({"file_id": "someone-elses"})


async def test_brand_is_per_user_and_drives_the_theme(db):
    a, _ = await users.get_or_create_by_chat(11, "A")
    b, _ = await users.get_or_create_by_chat(12, "B")
    await brand.set_brand(a.id, style="corporate", primary="#123456", company="Orbit", bogus="x")
    assert await brand.get_brand(a.id) == {"style": "corporate", "primary": "#123456", "company": "Orbit"}
    assert await brand.get_brand(b.id) == {}
    theme = brand.theme_of(await brand.get_brand(a.id))
    assert (theme.name, theme.primary, theme.company) == ("corporate", "#123456", "Orbit")
    await brand.set_brand(a.id, company="")
    assert "company" not in await brand.get_brand(a.id)


def _deck() -> DeckSpec:
    return DeckSpec(title="Falcon kickoff", slides=[
        Slide(layout="title", title="Falcon kickoff", subtitle="Q4 plan"),
        Slide(layout="stats", title="Where we stand", stats=[{"value": "18 lakh", "label": "Budget"}]),
        Slide(layout="closing", title="Next: pilot by 20 Nov")])


def _doc() -> DocSpec:
    return DocSpec(title="Q3 analysis", sections=[Section(heading="Revenue grew 22%")])


async def test_run_renders_in_the_chosen_style_and_does_not_lock_it(db, settings, monkeypatch):
    user, _ = await users.get_or_create_by_chat(21, "JK")

    async def fake_plan(uid, req):
        return _deck(), "bold"

    delivered: list = []

    async def fake_deliver(uid, job_id, req, path, style):
        delivered.append((path, style))
        return "https://docs.google.com/presentation/d/abc/edit"

    sent: list = []

    async def capture(msg):
        sent.append(msg)
        return 1

    monkeypatch.setattr(agent, "plan", fake_plan)
    monkeypatch.setattr(agent, "_deliver", fake_deliver)
    monkeypatch.setattr(outbox, "enqueue_now", capture)
    req = agent.Request(kind="deck", title="Falcon kickoff", brief="kickoff deck")
    await agent.run(user.id, "studio:1", req)
    [(path, style)] = delivered
    assert path.exists() and path.suffix == ".pptx" and style == "bold"
    # only a style the user picks is kept: each later artifact gets the style that fits it
    assert "style" not in await brand.get_brand(user.id)
    assert "Here it is: Falcon kickoff" in sent[-1].text and "docs.google.com" in sent[-1].text


async def test_a_failed_plan_is_told_in_plain_words(db, settings, monkeypatch):
    user, _ = await users.get_or_create_by_chat(22, "JK")

    async def broken(uid, req):
        raise LLMError("model down")

    sent: list = []

    async def capture(msg):
        sent.append(msg)
        return 1

    monkeypatch.setattr(agent, "plan", broken)
    monkeypatch.setattr(outbox, "enqueue_now", capture)
    await agent.run(user.id, "studio:2", agent.Request(kind="doc", title="Q3 report", brief="a report"))
    assert sent[-1].text.startswith("I couldn't finish Q3 report")


async def test_create_document_starts_the_sub_agent_job_and_says_so(monkeypatch):
    jobs: list = []
    monkeypatch.setattr(tools, "get_bus", lambda: SimpleNamespace(enqueue=_record(jobs)))
    out = await tools.create_document(5, tools.CreateDocumentArgs(
        kind="deck", title="Hiring plan", brief="Three slides on hiring two AI engineers", format="pdf"))
    [job] = jobs
    assert job.kind.value == "studio" and job.user_id == 5 and job.payload["format"] == "pdf"
    assert out.startswith("STARTED")


def _record(jobs):
    async def enqueue(job):
        jobs.append(job)
    return enqueue


@pytest.mark.parametrize("kind,ext", [("deck", "pptx"), ("doc", "docx"), ("sheet", "xlsx")])
def test_default_formats(kind, ext):
    assert agent.default_format(kind) == ext


async def test_a_data_report_without_its_data_is_not_drawn(db, settings, monkeypatch):
    """The spec writer says what is missing instead of filling a spending report with invented figures:
    nothing is rendered or sent, and the failure names the gap."""
    user, _ = await users.get_or_create_by_chat(23, "JK")

    async def no_research(*a, **k):
        return None

    async def no_context(*a, **k):
        return "", None

    async def writer(schema, system, human, **kw):
        assert "do not write a spec" in system
        return schema(style="ocean", missing="the October transactions")

    def no_render(*a, **k):
        raise AssertionError("a report without its data must not be drawn")

    sent: list = []

    async def capture(msg):
        sent.append(msg)
        return 1

    monkeypatch.setattr("mavis.agents.react.react_loop", no_research)
    monkeypatch.setattr("mavis.agents.turn_support.build_context_ex", no_context)
    monkeypatch.setattr(agent.llm, "structured", writer)
    monkeypatch.setattr("mavis.studio.render.render", no_render)
    monkeypatch.setattr(outbox, "enqueue_now", capture)
    req = agent.Request(kind="doc", title="October spending", brief="analyse my October spending")
    await agent.run(user.id, "studio:3", req)
    assert [m.text for m in sent] == [
        "I couldn't finish October spending: I don't have the October transactions. Want me to try again?"]


async def test_an_analysis_report_gets_a_gradient_style_unless_the_user_chose_one(db, settings, monkeypatch):
    user, _ = await users.get_or_create_by_chat(24, "JK")

    async def loads_analysis(tools, *a, **k):
        load = next(t for t in tools if t.name == "load_skill")
        await load.ainvoke({"name": "analysis-report"})

    async def no_context(*a, **k):
        return "", None

    async def writer(schema, system, human, **kw):
        return schema(style="minimal", spec=_doc())

    monkeypatch.setattr("mavis.agents.react.react_loop", loads_analysis)
    monkeypatch.setattr("mavis.agents.turn_support.build_context_ex", no_context)
    monkeypatch.setattr(agent.llm, "structured", writer)
    req = agent.Request(kind="doc", title="Q3 analysis", brief="analyse Q3")
    assert (await agent.plan(user.id, req))[1] == "ocean"
    await brand.set_brand(user.id, style="minimal")
    assert (await agent.plan(user.id, req))[1] == "minimal"


def test_a_dash_the_model_wrote_never_reaches_a_file():
    spec = DocSpec(title="Q3 \u2014 review", summary="Revenue grew 10\u201312%; see https://a.example/x\u2014y",
                   sections=[Section(heading="Costs \u2013 flat")])
    out = agent.undashed(spec)
    text = out.model_dump_json()
    assert "\u2014" not in out.title and "\u2013" not in out.sections[0].heading
    assert "10 to 12%" in out.summary or "10-12%" in out.summary
    assert isinstance(out, DocSpec) and "Q3" in text

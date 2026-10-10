"""The studio: skills catalogue (Agent Skills format), per-user brand, the sub-agent and its tools."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mavis.domain.errors import LLMError
from mavis.store.repo import outbox, users
from mavis.studio import agent, brand, skills, tools
from mavis.studio.spec import DeckSpec, Slide


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


async def test_run_renders_in_the_users_theme_delivers_and_fixes_their_style(db, settings, monkeypatch):
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
    assert (await brand.get_brand(user.id))["style"] == "bold"  # later artifacts match the first
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

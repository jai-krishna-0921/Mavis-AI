"""Spec 5.3-5.4: Workspace lines in the morning brief and evening wrap; quiet first sync."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from mavis.attention.rhythm import EveningWrap, register_evening_source
from mavis.attention.workspace import BASELINE_KEY, WorkspaceIntake
from mavis.attention.workspace_rhythm import WorkspaceBrief, workspace_evening
from mavis.domain.integrations import ConnectionState, ToolResult
from mavis.domain.policy import Capability
from mavis.initiative import routines
from mavis.initiative.routines import BriefItem, Routines
from mavis.store.repo import attention as repo
from mavis.timers.service import WakeupService
from mavis.tools.integrations.first_sync import FirstSync, register_first_sync_handler

MORNING = datetime(2026, 10, 3, 2, 30, tzinfo=UTC)  # 08:00 IST
EVENING = datetime(2026, 10, 3, 15, 0, tzinfo=UTC)  # 20:30 IST


class Exec:
    def __init__(self, sends: bool = True) -> None:
        self.notified: list = []
        self.delivered: list = []
        self.sends = sends

    async def notify(self, user, intent, context="", quiet_streak=0, untrusted=False, original_due=None,
                     origin=None, buttons=None) -> bool:
        self.notified.append(SimpleNamespace(intent=intent, untrusted=untrusted))
        return self.sends

    async def deliver(self, user, bubbles, dedupe_key=None, urgency=3, quiet_streak=0, extra_keys=None,
                      buttons=None, tainted=False) -> None:
        self.delivered.append(bubbles)


class NoLoops:
    async def active(self, user_id, entities=None, due_within=None):
        return []

    async def close(self, loop_id, status=None):
        return None


def intake(provider, ex, rec, at) -> WorkspaceIntake:
    return WorkspaceIntake(provider=provider, executor_of=lambda: ex, loops=NoLoops(), schedule=rec.schedule,
                           clock=lambda: at)


async def row(user_id, mid, *, source, kind, verdict="brief", at=MORNING, **facts):
    await repo.insert_signal(user_id, mid, source=source, kind=kind, verdict=verdict, urgency=0,
                             summary=facts.pop("title", "x"), facts=facts, received_at=at)


async def test_morning_brief_lists_due_tasks_and_shared_files_once(user, clock, provider, rec):
    clock.set(MORNING)
    ws = intake(provider, Exec(), rec, MORNING)
    await row(user.id, "tasks:t1:due", source="tasks", kind="task_due", title="Pay rent", due="2026-10-03")
    await row(user.id, "tasks:t2:due", source="tasks", kind="task_due", title="Old", due="2026-10-02")
    await row(user.id, "drive:f1:s", source="drive", kind="file_shared", title="Q3 deck",
              actor="priya@example.com", at=MORNING - timedelta(hours=2))
    await row(user.id, "drive:f2:s", source="drive", kind="file_shared", title="Verify your account.html",
              actor="x@evil.example", security=True, at=MORNING - timedelta(hours=1))
    await row(user.id, "drive:f3:s", source="drive", kind="file_shared", verdict="log", title="Noise")
    brief = WorkspaceBrief(ws)
    items = await brief.items(user.id, MORNING, MORNING)
    texts = [i.text for i in items]
    assert texts[0] == "Today: 1 task due: Pay rent"
    assert "Shared with you: Q3 deck (from priya)" in texts
    assert any(t.startswith("Heads up: someone you haven't emailed shared") for t in texts)
    assert not any("Noise" in t for t in texts)
    assert not any(i.trusted for i in items)  # titles are third-party text
    # amendment A10: building the items stamps nothing; an undelivered brief repeats its files
    assert [i.text for i in await brief.items(user.id, MORNING, MORNING)] == texts
    await brief.delivered(user.id, MORNING)
    again = [i.text for i in await brief.items(user.id, MORNING, MORNING)]
    assert again == ["Today: 1 task due: Pay rent"]  # files appear once; due tasks every morning


class Source:
    name = "spy"

    def __init__(self) -> None:
        self.stamped: list = []

    async def items(self, user_id, start, end):
        return [BriefItem("Shared with you: Q3 deck (from priya)", False)]

    async def delivered(self, user_id, at):
        self.stamped.append((user_id, at))


class LoopsService:
    async def active(self, user_id, entities=None, due_within=None):
        return []


async def test_brief_sources_hear_about_delivery_only_when_the_brief_went_out(user, clock):
    clock.set(MORNING)
    spy = Source()
    routines.register_brief_source(spy)
    await Routines(LoopsService(), WakeupService(), Exec(sends=False))._send_morning(user)
    assert spy.stamped == []  # deferred or blocked: the lines come again next time
    await Routines(LoopsService(), WakeupService(), Exec(sends=True))._send_morning(user)
    assert spy.stamped == [(user.id, MORNING)]


async def test_evening_wrap_adds_overdue_tasks_and_waiting_comments(user, clock):
    clock.set(EVENING)
    await row(user.id, "tasks:t2:overdue", source="tasks", kind="task_overdue", title="Renew passport",
              at=EVENING - timedelta(hours=3))
    await row(user.id, "docs:d1:c1", source="docs", kind="comment", verdict="notify", title="Launch plan",
              owned_by_me=True, at=EVENING - timedelta(hours=5))
    await row(user.id, "docs:d2:c1", source="docs", kind="comment", verdict="brief", title="Old plan",
              owned_by_me=True, at=EVENING - timedelta(days=4))
    await row(user.id, "docs:d3:c1", source="docs", kind="comment", verdict="log", title="Muted plan",
              owned_by_me=True, at=EVENING - timedelta(hours=2))
    register_evening_source(workspace_evening)
    ex = Exec()
    assert await EveningWrap(lambda: ex, WakeupService())._send(user.id) is True
    [sent] = ex.notified
    assert "From their Google account" in sent.intent.intent and sent.untrusted
    assert "Overdue task: Renew passport" in sent.intent.intent
    assert "Comment waiting on your doc: Launch plan" in sent.intent.intent
    assert "Old plan" not in sent.intent.intent  # comments older than 3 days are left out
    assert "Muted plan" not in sent.intent.intent  # a muted (log) comment never comes back


async def test_evening_wrap_without_sources_still_skips_a_quiet_day(user, clock):
    clock.set(EVENING)
    await row(user.id, "tasks:t2:overdue", source="tasks", kind="task_overdue", title="Renew passport",
              at=EVENING - timedelta(hours=3))
    ex = Exec()
    assert await EveningWrap(lambda: ex, WakeupService())._send(user.id) is False
    assert ex.notified == []


async def test_first_sync_drive_logs_a_silent_baseline(user, provider, rec):
    now = datetime(2026, 10, 3, 4, 0, tzinfo=UTC)
    provider.set_state(user.id, Capability.GMAIL, ConnectionState.ACTIVE)
    profile = {"response_data": {"emailAddress": "J@x.com"}}
    provider.results["mail.profile"] = ToolResult(ok=True, data=profile)
    provider.results["drive.list_recent"] = ToolResult(ok=True, data={"files": [
        {"id": "f1", "name": "Q3 deck", "sharedWithMeTime": "2026-10-02T04:00:00Z",
         "owners": [{"emailAddress": "priya@example.com"}]},
        {"id": "f2", "name": "Ancient", "sharedWithMeTime": "2026-09-01T04:00:00Z",
         "owners": [{"emailAddress": "priya@example.com"}]},
    ]})
    ex = Exec()
    ws = intake(provider, ex, rec, now)
    assert await ws.first_sync_drive(user.id) == []
    rows = await repo.signals(user.id, now - timedelta(days=60), sources=("drive",))
    assert [(r.summary, r.verdict) for r in rows] == [("Q3 deck", "log")]
    st = await ws.state(user.id)
    assert st["email"] == "j@x.com" and st["shared_after"] == now.isoformat()
    assert ex.notified == [] and ex.delivered == []


async def test_capture_email_reuses_the_cached_address(user, provider, rec):
    ws = intake(provider, Exec(), rec, MORNING)
    await ws.patch(user.id, email="me@x.com")
    assert await ws.capture_email(user.id) == "me@x.com"
    assert not any(action == "mail.profile" for _, action, _ in provider.executed)


async def test_first_sync_contacts_seeds_actor_known(user, provider, rec):
    provider.results["contacts.list"] = ToolResult(ok=True, data={"response_data": {"connections": [
        {"emailAddresses": [{"value": "Priya@Example.com"}]}, {"names": [{"displayName": "No email"}]}]}})
    ws = intake(provider, Exec(), rec, MORNING)
    await ws.first_sync_contacts(user.id)
    assert await ws.known(user.id, "priya@example.com")
    assert not await ws.known(user.id, "stranger@example.com")


async def test_first_sync_tasks_is_quiet_counts_the_week_and_sets_the_baseline(user, provider, rec):
    now = datetime(2026, 10, 3, 4, 0, tzinfo=UTC)
    provider.results["tasks.list"] = ToolResult(ok=True, data={"tasks": [
        {"id": "t1", "title": "Very old", "due": "2026-09-01T00:00:00.000Z"},
        {"id": "t2", "title": "Friday thing", "due": "2026-10-06T00:00:00.000Z"},
    ]})
    ex = Exec()
    ws = intake(provider, ex, rec, now)
    assert await ws.first_sync_tasks(user.id) == []
    assert ex.delivered == []  # no keep-or-drop flood on connect
    st = await ws.state(user.id)
    assert st["asked"] == ["t1"] and st["upcoming"] == 1
    assert st[BASELINE_KEY] == now.isoformat()  # amendment A3: polls may speak from now on
    [overdue] = await repo.signals(user.id, now - timedelta(days=1), sources=("tasks",))
    assert overdue.verdict == "brief"


async def test_first_sync_runs_registered_workspace_handlers(user, provider, fake_bus):
    seen: list[int] = []

    async def tasks_handler(user_id: int) -> list[str]:
        seen.append(user_id)
        return []

    register_first_sync_handler(Capability.TASKS, tasks_handler)

    class Learner:
        async def learn(self, user_id, text, source_ref):
            return None

    async def tz(user_id):
        return "Asia/Kolkata"

    sync = FirstSync(provider=provider, memory=Learner(), loops=None, bus=fake_bus, tz_of=tz)
    assert await sync.run(user.id, Capability.TASKS) == []
    assert seen == [user.id]


async def test_register_attention_wires_workspace_brief_evening_and_first_sync(
    workspace_on, recording_bus, fake_memory, embedder
):
    from mavis.attention import rhythm
    from mavis.attention.wiring import get_workspace, register_attention
    from mavis.initiative import wiring as initiative_wiring
    from mavis.initiative.wiring import build_initiative
    from mavis.tools.integrations.first_sync import EXTRA_HANDLERS

    async def no_embed(texts):
        return [[1.0, 0.0] for _ in texts]

    initiative_wiring.set_current(build_initiative(recording_bus, fake_memory, embed=no_embed))
    register_attention()
    register_attention()  # idempotent
    assert [s.name for s in routines.brief_sources()].count("workspace") == 1
    assert rhythm._evening_sources == [workspace_evening]
    ws = get_workspace()
    assert EXTRA_HANDLERS == {Capability.TASKS: ws.first_sync_tasks, Capability.DRIVE: ws.first_sync_drive,
                              Capability.CONTACTS: ws.first_sync_contacts}

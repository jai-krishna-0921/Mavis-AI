"""Workspace intake (spec 5.1-5.2): webhook or poll -> one row -> notify, ask or brief. Fake provider,
fake executor, real ping policy, no LLM."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from mavis.attention.workspace import BASELINE_KEY, KEEP, SPEAK, WorkspaceIntake
from mavis.domain.errors import LLMError
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.integrations import ConnectionState, ToolResult
from mavis.domain.loops import Loop, LoopKind, LoopStatus
from mavis.domain.policy import Capability
from mavis.store.db import Session
from mavis.store.models import AttentionSender
from mavis.store.repo import attention as repo
from mavis.store.repo import audit, users
from mavis.tools.integrations.composio_webhooks import parse_composio_webhook
from mavis.tools.integrations.poller import WORKSPACE_POLL_KIND

NOW = datetime(2026, 10, 3, 4, 0, tzinfo=UTC)  # 09:30 IST


class Exec:
    def __init__(self, fail_compose: Exception | None = None) -> None:
        self.notified: list = []
        self.delivered: list = []
        self.fail_compose = fail_compose

    async def notify(self, user, intent, context="", quiet_streak=0, untrusted=False, original_due=None,
                     origin=None, buttons=None) -> bool:
        if self.fail_compose is not None:
            raise self.fail_compose
        self.notified.append(SimpleNamespace(intent=intent, untrusted=untrusted, buttons=buttons))
        return True

    async def deliver(self, user, bubbles, dedupe_key=None, urgency=3, quiet_streak=0, extra_keys=None,
                      buttons=None, tainted=False) -> None:
        self.delivered.append(SimpleNamespace(bubbles=bubbles, buttons=buttons, tainted=tainted,
                                              key=dedupe_key))


class Loops:
    def __init__(self, loops=()) -> None:
        self.loops, self.closed = list(loops), []

    async def active(self, user_id, entities=None, due_within=None):
        return self.loops

    async def close(self, loop_id, status=LoopStatus.DONE):
        self.closed.append(loop_id)


@pytest.fixture
def ex() -> Exec:
    return Exec()


def intake(provider, ex, rec, loops=None, at=NOW) -> WorkspaceIntake:
    return WorkspaceIntake(provider=provider, executor_of=lambda: ex, loops=loops or Loops(),
                           schedule=rec.schedule, clock=lambda: at)


async def baselined(user_id: int, **extra) -> None:
    """first_sync_tasks has run (amendment A3): polls may speak."""
    await users.update_state(user_id, {"workspace": {BASELINE_KEY: "2026-10-02T00:00:00+00:00", **extra}})


def connected(provider, user_id: int) -> None:
    for capability in (Capability.GMAIL, Capability.DRIVE, Capability.DOCS, Capability.TASKS):
        provider.set_state(user_id, capability, ConnectionState.ACTIVE)


def shared_file(fid="f1", name="Q3 deck", by="priya@example.com", at="2026-10-03T03:50:00Z") -> dict:
    return {"id": fid, "name": name, "mimeType": "application/vnd.google-apps.presentation",
            "sharedWithMeTime": at, "owners": [{"emailAddress": by}], "sharingUser": {"emailAddress": by}}


def event(kind: str, raw: dict, user_id: int) -> Event:
    return Event(id=f"gws:{kind}:{time.time_ns()}", user_id=user_id, type=EventType.WORKSPACE_SIGNAL,
                 occurred_at=NOW, source="composio", trust=Trust.UNTRUSTED,
                 payload={"kind": kind, "raw": raw})


def button(data: str, user_id: int) -> Event:
    return Event(id=f"tg:btn:{data}", user_id=user_id, type=EventType.BUTTON_PRESSED, occurred_at=NOW,
                 source="telegram", trust=Trust.USER, payload={"data": data})


def waiting(loop_id: int, title: str) -> Loop:
    return Loop(id=loop_id, user_id=1, kind=LoopKind.WAITING_ON, title=title)


OVERDUE = {"id": "t2", "title": "Renew passport", "due": "2026-09-30T00:00:00.000Z", "status": "needsAction"}


# --- Drive shares ----------------------------------------------------------------------------------------


async def test_share_seen_by_webhook_and_poll_is_one_row(user, provider, ex, rec):
    await baselined(user.id, contacts=["priya@example.com"])
    provider.results["drive.list_recent"] = ToolResult(ok=True, data={"files": [shared_file()]})
    ws = intake(provider, ex, rec)
    await ws.on_event(event("share", {"event_type": "permissions_added", "new_permissions": []}, user.id))
    assert await ws.poll_shared(user.id) == 0  # the cursor moved past it
    await ws.patch(user.id, shared_after=None)  # even a reset cursor cannot double it
    assert await ws.poll_shared(user.id) == 0
    rows = await repo.signals(user.id, NOW - timedelta(days=1), sources=("drive",))
    assert len(rows) == 1 and rows[0].verdict == "brief" and rows[0].summary == "Q3 deck"
    assert ex.notified == [] and ex.delivered == []


async def test_known_share_matching_a_loop_notifies_with_fixed_text_and_closes_it(user, provider, ex, rec):
    await baselined(user.id, contacts=["priya@example.com"])
    provider.results["drive.list_recent"] = ToolResult(ok=True, data={"files": [
        shared_file(name="Q3 Sales Deck")]})
    waiting = Loop(id=5, user_id=user.id, kind=LoopKind.WAITING_ON, title="Priya to send the Q3 sales deck")
    loops = Loops([waiting])
    await intake(provider, ex, rec, loops).poll_shared(user.id)
    assert loops.closed == [5]
    assert ex.notified == []  # amendment A8: no LLM for this one
    [sent] = ex.delivered
    assert sent.bubbles == ['priya@example.com shared "Q3 Sales Deck". It looks like what you were waiting '
                            'for ("Priya to send the Q3 sales deck"), so I marked that done.']
    assert sent.tainted and sent.buttons[0][0].label == "Not useful"
    [row] = await repo.signals(user.id, NOW - timedelta(days=1), sources=("drive",))
    assert row.verdict == "notify" and row.delivery == "sent"


async def test_not_useful_on_a_share_sends_the_next_one_from_that_sharer_to_the_brief(
    user, provider, ex, rec, sent
):
    await baselined(user.id, contacts=["priya@example.com"])
    provider.results["drive.list_recent"] = ToolResult(ok=True, data={"files": [
        shared_file(fid="f1", name="Q3 Sales Deck")]})
    loops = Loops([waiting(5, "Priya to send the Q3 sales deck"), waiting(6, "Priya budget review notes")])
    ws = intake(provider, ex, rec, loops)
    await ws.poll_shared(user.id)
    data = ex.delivered[0].buttons[0][0].data
    await ws.on_button(button(data, user.id), data)
    assert (await ws.state(user.id))["muted"] == ["file_shared:priya@example.com"]
    provider.results["drive.list_recent"] = ToolResult(ok=True, data={"files": [
        shared_file(fid="f2", name="Priya budget review notes", at="2026-10-03T03:55:00Z")]})
    await ws.poll_shared(user.id)
    assert len(ex.delivered) == 1 and loops.closed == [5, 6]  # quieter, but the loop is still done
    rows = await repo.signals(user.id, NOW - timedelta(days=1), sources=("drive",))
    assert sorted(r.verdict for r in rows) == ["brief", "notify"]


async def test_loop_match_share_seen_by_webhook_and_poll_is_spoken_once(user, provider, ex, rec):
    await baselined(user.id, contacts=["priya@example.com"])
    provider.results["drive.list_recent"] = ToolResult(ok=True, data={"files": [
        shared_file(name="Q3 Sales Deck")]})
    loops = Loops([waiting(5, "Priya to send the Q3 sales deck")])
    ws = intake(provider, ex, rec, loops)
    grant = {"new_permissions": [{"file_id": "f1", "permission_id": "p9"}]}
    await ws.on_event(event("share", grant, user.id))
    await ws.patch(user.id, shared_after=None)
    await ws.poll_shared(user.id)
    assert len(ex.delivered) == 1 and loops.closed == [5]


async def test_sharer_known_from_mail_history(user, provider, ex, rec):
    await baselined(user.id)
    async with Session() as s:
        s.add(AttentionSender(user_id=user.id, address="ravi@example.org", domain="example.org", count=2,
                              first_seen=NOW, last_seen=NOW))
        await s.commit()
    provider.results["drive.list_recent"] = ToolResult(ok=True, data={"files": [
        shared_file(name="Trip plan", by="Ravi@Example.org")]})
    await intake(provider, ex, rec).poll_shared(user.id)
    [row] = await repo.signals(user.id, NOW - timedelta(days=1), sources=("drive",))
    assert row.verdict == "brief" and row.facts["actor_known"] is True


async def test_unknown_sharer_is_logged_silently(user, provider, ex, rec):
    await baselined(user.id)
    provider.results["drive.list_recent"] = ToolResult(ok=True, data={"files": [
        shared_file(name="Holiday photos", by="stranger@example.net")]})
    await intake(provider, ex, rec).poll_shared(user.id)
    [row] = await repo.signals(user.id, NOW - timedelta(days=1), sources=("drive",))
    assert row.verdict == "log" and ex.notified == [] and ex.delivered == []


async def test_polls_stay_quiet_until_first_sync_sets_the_baseline(user, provider, ex, rec):
    await users.update_state(user.id, {"workspace": {"contacts": ["priya@example.com"]}})
    provider.results["drive.list_recent"] = ToolResult(ok=True, data={"files": [
        shared_file(name="Q3 Sales Deck")]})
    provider.results["tasks.list"] = ToolResult(ok=True, data={"tasks": [OVERDUE]})
    loops = Loops([Loop(id=5, user_id=user.id, kind=LoopKind.WAITING_ON, title="Q3 sales deck from Priya")])
    ws = intake(provider, ex, rec, loops)
    await ws.poll_shared(user.id)
    await ws.poll_tasks(user.id)
    assert ex.delivered == [] and ex.notified == [] and loops.closed == []
    rows = await repo.signals(user.id, NOW - timedelta(days=1), sources=("drive", "tasks"))
    assert sorted(r.verdict for r in rows) == ["brief", "brief"]
    assert "asked" not in await ws.state(user.id)


# --- Docs comments ---------------------------------------------------------------------------------------


def comment(cid: str, text: str = "Can you update the numbers?") -> dict:
    return {"comment_id": cid, "file_id": "d1", "comment_text": text,
            "commenter": {"displayName": "Priya", "me": False}, "created_time": "2026-10-03T03:55:00Z"}


async def test_comment_on_my_doc_notifies_and_not_useful_demotes_the_next(user, provider, ex, rec, sent):
    connected(provider, user.id)
    provider.results["drive.meta"] = ToolResult(ok=True, data={"name": "Launch plan"})
    provider.results["drive.permissions"] = ToolResult(ok=True, data={"permissions": [
        {"id": "p1", "type": "user", "role": "owner"}]})
    ws = intake(provider, ex, rec)
    await ws.on_event(event("comment", comment("c1"), user.id))
    [note] = ex.notified
    assert note.untrusted and '<untrusted source="file_title">' in note.intent.intent
    assert "their doc" in note.intent.intent
    data = note.buttons[0][0].data
    assert data.startswith("ws:m:")
    await ws.on_button(button(data, user.id), data)
    assert (await ws.state(user.id))["muted"] == ["comment:priya"]
    assert sent[-1].text == "Got it. Those go to your brief from now on."
    await ws.on_event(event("comment", comment("c2"), user.id))
    assert len(ex.notified) == 1  # the second comment went to the brief
    rows = await repo.signals(user.id, NOW - timedelta(days=1), sources=("docs",))
    assert sorted(r.verdict for r in rows) == ["brief", "notify"]


async def test_comment_on_someone_elses_doc_goes_to_the_brief_unless_it_mentions_me(user, provider, ex, rec):
    connected(provider, user.id)
    provider.results["mail.profile"] = ToolResult(ok=True, data={"emailAddress": "Jai@Example.com"})
    provider.results["drive.meta"] = ToolResult(ok=True, data={"name": "Team notes"})
    provider.results["drive.permissions"] = ToolResult(ok=True, data={"permissions": [
        {"id": "p1", "type": "user", "role": "owner", "emailAddress": "priya@example.com"},
        {"id": "p2", "type": "user", "role": "writer", "emailAddress": "jai@example.com"}]})
    ws = intake(provider, ex, rec)
    await ws.on_event(event("comment", comment("c1"), user.id))
    assert ex.notified == []
    assert (await ws.state(user.id))["email"] == "jai@example.com"  # looked up once, then cached
    await ws.on_event(event("comment", comment("c2", "@jaiswal please check"), user.id))
    assert ex.notified == []
    await ws.on_event(event("comment", comment("c3", "@jai please check"), user.id))
    [note] = ex.notified
    assert "a doc they follow" in note.intent.intent
    assert sum(1 for _, action, _ in provider.executed if action == "mail.profile") == 1


@pytest.mark.parametrize("error", [LLMError("slot busy"), RuntimeError("composer bug")])
async def test_comment_notify_falls_back_to_fixed_text_when_composing_fails(user, provider, rec, error):
    connected(provider, user.id)
    provider.results["drive.meta"] = ToolResult(ok=True, data={"name": "Launch plan"})
    provider.results["drive.permissions"] = ToolResult(ok=True, data={"permissions": [
        {"id": "p1", "type": "user", "role": "owner"}]})
    busy = Exec(fail_compose=error)
    await intake(provider, busy, rec).on_event(event("comment", comment("c1"), user.id))
    [sent] = busy.delivered
    assert sent.bubbles == ['Priya commented on your doc "Launch plan". Open it in Google Docs to reply.']
    assert sent.tainted and sent.buttons[0][0].label == "Not useful"
    [row] = await repo.signals(user.id, NOW - timedelta(days=1), sources=("docs",))
    assert row.delivery == "sent"


async def test_a_comment_with_no_author_name_offers_no_catch_all_mute(user, provider, ex, rec, sent):
    connected(provider, user.id)
    provider.results["drive.meta"] = ToolResult(ok=True, data={"name": "Launch plan"})
    provider.results["drive.permissions"] = ToolResult(ok=True, data={"permissions": [
        {"id": "p1", "type": "user", "role": "owner"}]})
    ws = intake(provider, ex, rec)
    nameless = {**comment("c1"), "commenter": {"me": False}}
    await ws.on_event(event("comment", nameless, user.id))
    [note] = ex.notified
    assert note.buttons is None
    [row] = await repo.signals(user.id, NOW - timedelta(days=1), sources=("docs",))
    await ws.on_button(button(f"ws:m:{row.id}", user.id), f"ws:m:{row.id}")  # a forged tap
    assert "muted" not in await ws.state(user.id) and sent == []


async def test_unreadable_file_is_not_treated_as_mine(user, provider, ex, rec):
    connected(provider, user.id)
    provider.results["drive.permissions"] = ToolResult(ok=False, error="403")
    await intake(provider, ex, rec).on_event(event("comment", comment("c1"), user.id))
    [row] = await repo.signals(user.id, NOW - timedelta(days=1), sources=("docs",))
    assert row.verdict == "brief" and row.facts["owned_by_me"] is False and ex.notified == []


# --- Google Tasks ----------------------------------------------------------------------------------------


async def test_due_and_overdue_tasks_from_the_poll(user, provider, ex, rec):
    await baselined(user.id)
    provider.results["tasks.list"] = ToolResult(ok=True, data={"tasks": [
        {"id": "t1", "title": "Pay rent", "due": "2026-10-03T00:00:00.000Z", "status": "needsAction"},
        OVERDUE,
    ]})
    ws = intake(provider, ex, rec)
    assert await ws.poll_tasks(user.id) == 2
    assert await ws.poll_tasks(user.id) == 0  # same day: deduped
    [ask] = ex.delivered
    assert ask.bubbles == ['"Renew passport" is 3 days past its due date. Keep it or drop it?']
    assert [b.label for b in ask.buttons[0]] == ["Keep it", "Drop it"] and ask.tainted
    assert (await ws.state(user.id))["asked"] == ["t2"]
    due = await repo.signals(user.id, NOW - timedelta(days=1), sources=("tasks",), kinds=("task_due",))
    assert [r.verdict for r in due] == ["brief"]


async def test_drop_button_deletes_the_task_and_audits(user, provider, ex, rec, sent):
    await baselined(user.id)
    provider.results["tasks.list"] = ToolResult(ok=True, data={"tasks": [OVERDUE]})
    ws = intake(provider, ex, rec)
    await ws.poll_tasks(user.id)
    drop = ex.delivered[0].buttons[0][1].data
    await ws.on_button(button(drop, user.id), drop)
    assert provider.executed[-1][1:] == ("tasks.delete", {"task_id": "t2"})
    assert (await audit.recent(user.id))[0].action == "tasks_delete"
    assert sent[-1].text == "Dropped it from your list."
    keep = ex.delivered[0].buttons[0][0].data
    assert keep.startswith(KEEP)
    await ws.on_button(button(drop, user.id + 1), drop)  # someone else's tap does nothing
    assert sum(1 for _, action, _ in provider.executed if action == "tasks.delete") == 1


async def test_double_tapped_drop_deletes_once(user, provider, ex, rec, sent):
    import asyncio

    await baselined(user.id)
    provider.results["tasks.list"] = ToolResult(ok=True, data={"tasks": [OVERDUE]})
    ws = intake(provider, ex, rec)
    await ws.poll_tasks(user.id)
    drop = ex.delivered[0].buttons[0][1].data
    real = provider.execute

    async def slow(user_ref, action, args):
        await asyncio.sleep(0.01)  # the first tap is mid-delete when the second arrives
        return await real(user_ref, action, args)

    provider.execute = slow
    await asyncio.gather(ws.on_button(button(drop, user.id), drop), ws.on_button(button(drop, user.id), drop))
    assert sum(1 for _, action, _ in provider.executed if action == "tasks.delete") == 1
    assert [m.text for m in sent] == ["Dropped it from your list."]


async def test_drop_survives_a_provider_exception_and_audits_it(user, provider, ex, rec, sent):
    await baselined(user.id)
    provider.results["tasks.list"] = ToolResult(ok=True, data={"tasks": [OVERDUE]})
    ws = intake(provider, ex, rec)
    await ws.poll_tasks(user.id)
    drop = ex.delivered[0].buttons[0][1].data

    async def broken(user_ref, action, args):
        raise RuntimeError("network down")

    provider.execute = broken
    await ws.on_button(button(drop, user.id), drop)
    [entry] = await audit.recent(user.id)
    assert entry.action == "tasks_delete" and entry.detail["outcome"] == "error"
    assert sent[-1].text == "I couldn't drop it just now. You can remove it in Google Tasks."


async def test_drop_only_acts_on_task_rows(user, provider, ex, rec):
    row, _ = await repo.insert_signal(user.id, "docs:d1:c1", source="docs", kind="comment", verdict="notify",
                                      urgency=3, summary="Launch plan", facts={"object_id": "d1"},
                                      received_at=NOW)
    ws = intake(provider, ex, rec)
    await ws.on_button(button(f"ws:d:{row.id}", user.id), f"ws:d:{row.id}")
    assert not any(action == "tasks.delete" for _, action, _ in provider.executed)


async def test_quiet_hours_defer_the_ask_and_the_chain_goes_on(user, provider, ex, rec):
    await baselined(user.id)
    await users.update_state(user.id, {"synced": {"tasks": "x"}})
    provider.results["tasks.list"] = ToolResult(ok=True, data={"tasks": [OVERDUE]})
    night = datetime(2026, 10, 3, 18, 0, tzinfo=UTC)  # 23:30 IST
    await intake(provider, ex, rec, at=night).on_wakeup(user.id, "tasks")
    assert ex.delivered == []
    [speak] = [r for r in rec.scheduled if r[2].startswith(SPEAK)]
    assert speak[1:] == (datetime(2026, 10, 4, 1, 30, tzinfo=UTC), speak[2], WORKSPACE_POLL_KIND)
    assert (user.id, night + timedelta(minutes=30), "tasks", WORKSPACE_POLL_KIND) in rec.scheduled
    [row] = await repo.signals(user.id, night - timedelta(days=1), sources=("tasks",))
    assert row.verdict == "ask" and row.delivery == "deferred"
    morning = datetime(2026, 10, 4, 1, 30, tzinfo=UTC)  # 07:00 IST
    ws = intake(provider, ex, rec, at=morning)
    await ws.on_wakeup(user.id, speak[2])
    [ask] = ex.delivered
    assert ask.bubbles == ['"Renew passport" is 4 days past its due date. Keep it or drop it?']
    await ws.on_wakeup(user.id, speak[2])  # a second firing sends nothing new
    assert len(ex.delivered) == 1


async def test_deferred_ask_is_skipped_when_the_task_is_already_done(user, provider, ex, rec):
    await baselined(user.id)
    provider.results["tasks.list"] = ToolResult(ok=True, data={"tasks": [OVERDUE]})
    night = datetime(2026, 10, 3, 18, 0, tzinfo=UTC)
    await intake(provider, ex, rec, at=night).poll_tasks(user.id)
    [speak] = [r for r in rec.scheduled if r[2].startswith(SPEAK)]
    provider.results["tasks.get"] = ToolResult(ok=True, data={"id": "t2", "status": "completed"})
    await intake(provider, ex, rec, at=datetime(2026, 10, 4, 1, 30, tzinfo=UTC)).on_wakeup(user.id, speak[2])
    assert ex.delivered == []


# --- poll chains -----------------------------------------------------------------------------------------


async def test_poll_chain_reschedules_and_stops_when_disconnected(user, provider, ex, rec):
    provider.results["tasks.list"] = ToolResult(ok=True, data={"tasks": []})
    ws = intake(provider, ex, rec)
    await ws.on_wakeup(user.id, "tasks")
    assert rec.scheduled == []  # not synced: the chain is over
    await users.update_state(user.id, {"synced": {"tasks": "x", "drive": "x"}})
    await ws.on_wakeup(user.id, "tasks")
    assert rec.scheduled == [(user.id, NOW + timedelta(minutes=30), "tasks", WORKSPACE_POLL_KIND)]
    rec.scheduled.clear()
    assert await ws.ensure_chains(user.id) == 2
    assert {(r[1], r[2]) for r in rec.scheduled} == {(NOW, "tasks"), (NOW, "drive")}
    rec.scheduled.clear()
    assert await ws.ensure_chains(user.id, later=True) == 2  # amendment A3: first poll one interval out
    assert {r[1] for r in rec.scheduled} == {NOW + timedelta(minutes=30)}


async def test_google_activated_retires_legacy_and_arms_polls_one_interval_out(
    user, provider, ex, rec, workspace_on, monkeypatch
):
    from mavis.attention import wiring as attention_wiring
    from mavis.tools.integrations import wiring as integrations_wiring

    retired: list[int] = []

    class Activator:
        async def retire_legacy(self, user_id: int) -> int:
            retired.append(user_id)
            return 0

    await users.update_state(user.id, {"synced": {"tasks": "x", "drive": "x"}})
    monkeypatch.setattr(integrations_wiring, "get_activator", lambda: Activator())
    monkeypatch.setattr(attention_wiring, "get_workspace", lambda: intake(provider, ex, rec))
    await integrations_wiring.google_activated(user.id)
    assert retired == [user.id]
    assert {(r[1], r[2]) for r in rec.scheduled} == {(NOW + timedelta(minutes=30), "tasks"),
                                                     (NOW + timedelta(minutes=30), "drive")}


# --- webhooks and wiring ---------------------------------------------------------------------------------


def _signed(slug: str, data: dict, secret: str = "whsec_test") -> tuple[dict, bytes]:
    body = json.dumps({"metadata": {"trigger_slug": slug, "user_id": "mavis-1"}, "data": data}).encode()
    wid, ts = "msg_1", str(int(time.time()))
    digest = hmac.new(secret.encode(), f"{wid}.{ts}.{body.decode()}".encode(), hashlib.sha256).digest()
    sig = base64.b64encode(digest).decode()
    return {"webhook-id": wid, "webhook-timestamp": ts, "webhook-signature": f"v1,{sig}"}, body


def test_googlesuper_webhooks_become_workspace_and_mail_events():
    headers, body = _signed("GOOGLESUPER_COMMENT_ADDED_TRIGGER", comment("c9"))
    [ev] = parse_composio_webhook(headers, body, "whsec_test")
    assert ev.type is EventType.WORKSPACE_SIGNAL and ev.payload["kind"] == "comment"
    assert ev.id == "gws:1:comment:c9" and ev.trust is Trust.UNTRUSTED
    mail_data = {"message_id": "m1", "sender": "a@b.com", "subject": "Hi"}
    headers, body = _signed("GOOGLESUPER_NEW_MESSAGE", mail_data)
    [mail] = parse_composio_webhook(headers, body, "whsec_test")
    assert mail.type is EventType.EMAIL_RECEIVED and mail.id == "gmail:1:msg:m1"
    headers, body = _signed("GOOGLESUPER_SLIDE_ADDED_TRIGGER", {"x": 1})
    assert parse_composio_webhook(headers, body, "whsec_test") == []


def test_initiative_agent_never_sees_workspace_signals():
    from mavis.initiative.handler import HANDLED_TYPES

    assert EventType.WORKSPACE_SIGNAL not in HANDLED_TYPES  # amendment A1


async def test_register_attention_wires_workspace_when_enabled(
    workspace_on, recording_bus, fake_memory, embedder
):
    from mavis.agents import buttons
    from mavis.attention.wiring import get_workspace, register_attention
    from mavis.initiative import wiring as initiative_wiring
    from mavis.initiative.wiring import build_initiative
    from mavis.timers import system
    from mavis.worker import runner

    async def no_embed(texts):
        return [[1.0, 0.0] for _ in texts]

    initiative_wiring.set_current(build_initiative(recording_bus, fake_memory, embed=no_embed))
    register_attention()
    assert runner._event_handlers[EventType.WORKSPACE_SIGNAL] == [get_workspace().on_event]
    assert "ws:" in buttons.BUTTON_HANDLERS
    assert WORKSPACE_POLL_KIND in system.SYSTEM_WAKEUP_HANDLERS


async def test_register_attention_leaves_workspace_out_when_the_flag_is_off(
    recording_bus, fake_memory, embedder
):
    from mavis.agents import buttons
    from mavis.attention.wiring import register_attention
    from mavis.initiative import wiring as initiative_wiring
    from mavis.initiative.wiring import build_initiative
    from mavis.timers import system
    from mavis.worker import runner

    async def no_embed(texts):
        return [[1.0, 0.0] for _ in texts]

    initiative_wiring.set_current(build_initiative(recording_bus, fake_memory, embed=no_embed))
    register_attention()
    assert EventType.WORKSPACE_SIGNAL not in runner._event_handlers
    assert "ws:" not in buttons.BUTTON_HANDLERS
    assert WORKSPACE_POLL_KIND not in system.SYSTEM_WAKEUP_HANDLERS


async def test_poll_auth_error_sends_the_reconnect_prompt(user, provider, ex, rec):
    # legacy Gmail still ACTIVE, googlesuper EXPIRED: the poll fails with an auth error and the user
    # gets the (daily-deduped) Google reconnect prompt instead of a silent log line
    await baselined(user.id)
    prompted: list = []

    async def on_auth_failed(user_id, capability):
        prompted.append((user_id, capability))
        return True

    provider.set_state(user.id, Capability.GMAIL, ConnectionState.ACTIVE)
    provider.results["tasks.list"] = ToolResult(ok=False, error="Composio answered 401 for POST /tools")
    ws = WorkspaceIntake(provider=provider, executor_of=lambda: ex, loops=Loops(), schedule=rec.schedule,
                         clock=lambda: NOW, on_auth_failed=on_auth_failed)
    assert await ws.poll_tasks(user.id) == 0
    assert prompted == [(user.id, Capability.TASKS)]
    provider.results["tasks.list"] = ToolResult(ok=False, error="Composio answered 500 for POST /tools")
    await ws.poll_tasks(user.id)
    assert len(prompted) == 1  # other errors only log


def test_wired_intake_prompts_reconnect(monkeypatch):
    from mavis.attention import wiring as attention_wiring
    from mavis.attention.wiring import get_workspace
    from mavis.tools.integrations.wiring import reconnect_prompt

    monkeypatch.setattr(attention_wiring.initiative_wiring, "current", lambda: SimpleNamespace(loops=Loops()))
    get_workspace.cache_clear()
    try:
        assert get_workspace().on_auth_failed is reconnect_prompt
    finally:
        get_workspace.cache_clear()

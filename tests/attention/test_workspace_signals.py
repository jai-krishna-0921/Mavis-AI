"""Spec 5.1-5.2: Workspace inputs normalize to Signal; the scoring table is deterministic."""

from __future__ import annotations

from datetime import date

from mavis.attention.schema import Verdict
from mavis.attention.workspace_signals import (
    Signal,
    SignalKind,
    comment_signal,
    decide,
    is_lure,
    match_loop,
    safe_title,
    shared_file_signal,
    task_signal,
)
from mavis.domain.loops import Loop, LoopKind

TODAY = date(2026, 10, 3)


def test_shared_file_signal_from_a_drive_file():
    s = shared_file_signal({
        "id": "f1", "name": "Q3 deck.pptx", "sharedWithMeTime": "2026-10-03T03:00:00.000Z",
        "owners": [{"emailAddress": "Priya@Example.com"}],
        "sharingUser": {"emailAddress": "Priya@Example.com"},
    })
    assert s is not None and s.kind is SignalKind.FILE_SHARED and s.actor == "priya@example.com"
    assert s.object_title == "Q3 deck.pptx"
    assert s.message_id == "drive:f1:shared-2026-10-03T03:00:00"
    assert shared_file_signal({"id": "f2", "name": "Mine", "sharedWithMeTime": "x",
                               "owners": [{"me": True}]}) is None


def test_comment_signal_skips_my_own_and_detects_mentions():
    raw = {"comment_id": "c1", "file_id": "d1",
           "comment_text": "+jai@example.com can you check https://x.example",
           "commenter": {"displayName": "Priya", "me": False}}
    s = comment_signal(raw, me="jai@example.com")
    assert s is not None and s.mentions_you and s.actor == "Priya" and "x.example" not in s.preview
    assert s.message_id == "docs:d1:c1"
    assert comment_signal({**raw, "commenter": {"me": True}}) is None


def test_task_signal_due_overdue_and_future():
    due = task_signal({"id": "t1", "title": "Pay rent", "due": "2026-10-03T00:00:00.000Z",
                       "status": "needsAction"}, TODAY)
    assert due is not None and due.kind is SignalKind.TASK_DUE
    assert due.message_id == "tasks:t1:task_due-2026-10-03"
    late = task_signal({"id": "t2", "title": "Renew", "due": "2026-10-01T00:00:00.000Z"}, TODAY)
    assert late is not None and late.kind is SignalKind.TASK_OVERDUE and late.overdue_days == 2
    assert task_signal({"id": "t3", "title": "Later", "due": "2026-10-09T00:00:00.000Z"}, TODAY) is None
    done = {"id": "t4", "title": "Done", "due": "2026-10-01", "status": "completed"}
    assert task_signal(done, TODAY) is None


def test_titles_keep_file_names_but_drop_links_and_markup():
    assert safe_title("Budget 2026.xlsx <b>https://evil.example/x</b>") == "Budget 2026.xlsx b[link]/b"
    assert is_lure("Reset your password.html") and is_lure("invoice.exe") and not is_lure("Q3 deck")


def sig(kind: SignalKind, **kw) -> Signal:
    return Signal(kind=kind, object_id="o1", event_id="e1", **kw)


def test_scoring_table():
    assert decide(sig(SignalKind.FILE_SHARED, actor_known=True)).verdict is Verdict.BRIEF
    loop = decide(sig(SignalKind.FILE_SHARED, actor_known=True), loop_id=7)
    assert loop.verdict is Verdict.NOTIFY and loop.close_loop == 7 and loop.urgency == 3
    assert decide(sig(SignalKind.FILE_SHARED, object_title="Q3 deck")).verdict is Verdict.LOG
    lure = decide(sig(SignalKind.FILE_SHARED, object_title="Verify your account"))
    assert lure.verdict is Verdict.BRIEF and lure.security
    assert decide(sig(SignalKind.COMMENT, owned_by_me=True)).verdict is Verdict.NOTIFY
    assert decide(sig(SignalKind.COMMENT, mentions_you=True)).verdict is Verdict.NOTIFY
    assert decide(sig(SignalKind.COMMENT)).verdict is Verdict.BRIEF
    assert decide(sig(SignalKind.TASK_DUE)).verdict is Verdict.BRIEF
    assert decide(sig(SignalKind.TASK_OVERDUE, overdue_days=1)).verdict is Verdict.BRIEF
    assert decide(sig(SignalKind.TASK_OVERDUE, overdue_days=2)).verdict is Verdict.ASK
    assert decide(sig(SignalKind.TASK_OVERDUE, overdue_days=3), asked=True).verdict is Verdict.BRIEF


def test_not_useful_demotes_but_never_security():
    assert decide(sig(SignalKind.COMMENT, owned_by_me=True), muted=True).verdict is Verdict.BRIEF
    assert decide(sig(SignalKind.FILE_SHARED, actor_known=True), muted=True).verdict is Verdict.LOG
    lure = decide(sig(SignalKind.FILE_SHARED, object_title="password reset"), muted=True)
    assert lure.verdict is Verdict.BRIEF and lure.security
    matched = decide(sig(SignalKind.FILE_SHARED, actor_known=True), loop_id=7, muted=True)
    assert matched.verdict is Verdict.BRIEF and matched.close_loop == 7  # quieter, still marked done


def test_match_loop_needs_a_waiting_or_commitment_loop_and_real_overlap():
    loops = [
        Loop(id=1, user_id=1, kind=LoopKind.GOAL, title="Q3 sales deck"),
        Loop(id=2, user_id=1, kind=LoopKind.WAITING_ON, title="Priya to send the Q3 sales deck"),
        Loop(id=3, user_id=1, kind=LoopKind.WAITING_ON, title="Landlord lease renewal"),
    ]
    assert match_loop("Q3 Sales Deck v2", loops) == 2
    assert match_loop("Holiday photos", loops) is None


def test_mentions_match_whole_handles_only():
    from mavis.attention.workspace_signals import mentions

    assert mentions("@jai can you look?", "jai@example.com")
    assert mentions("Thanks @Jai.", "jai@example.com")
    assert mentions("cc jai@example.com", "jai@example.com")
    assert not mentions("@jaiswal can you look?", "jai@example.com")
    assert not mentions("mail xjai@example.com", "jai@example.com")
    assert not mentions("mail jai@example.com.au", "jai@example.com")
    assert mentions("+jai@example.com see this.", "jai@example.com")
    assert not mentions("@jai can you look?", "")


def test_titles_turn_control_characters_into_spaces():
    assert safe_title("Line one\nLine two\tend") == "Line one Line two end"
    assert safe_title("a\x00b") == "a b"


def test_match_loop_prefers_the_best_overlap_and_ignores_function_words():
    loops = [
        Loop(id=1, user_id=1, kind=LoopKind.WAITING_ON, title="Priya Q3 notes"),
        Loop(id=2, user_id=1, kind=LoopKind.WAITING_ON, title="Q3 budget review"),
    ]
    assert match_loop("Priya budget review Q3", loops) == 2
    chat = [Loop(id=3, user_id=1, kind=LoopKind.WAITING_ON, title="Talk to Ravi to be sure it is in")]
    assert match_loop("Notes to be filed in it", chat) is None

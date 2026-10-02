from datetime import timedelta

from mavis.domain import timeutil
from mavis.domain.messages import Role
from mavis.domain.wakeups import WakeupKind
from mavis.initiative.quiet import QuietTracker, ends_with_question
from mavis.store.repo import messages
from mavis.timers.service import WakeupService


def test_ends_with_question():
    assert ends_with_question("How'd it go?")
    assert ends_with_question("How'd it go? 🙂")
    assert not ends_with_question("Nice. Talk later.")


async def test_question_schedules_user_quiet(user, clock, settings, monkeypatch):
    monkeypatch.setattr(settings, "onboarding_quiet_hours", 4.0)
    wakeups = WakeupService()
    wid = await QuietTracker(wakeups).after_assistant_message(user.id, "What's on your plate?")
    [w] = await wakeups.pending(user.id, WakeupKind.USER_QUIET)
    assert w.id == wid and w.due_at == timeutil.now() + timedelta(hours=4)
    assert w.payload["streak"] == 0 and w.payload["question"] == "What's on your plate?"


async def test_statement_does_not_schedule_and_clears_previous(user, clock):
    wakeups = WakeupService()
    tracker = QuietTracker(wakeups)
    await tracker.after_assistant_message(user.id, "Anything else?")
    assert await tracker.after_assistant_message(user.id, "Done, it's on your calendar.") is None
    assert await wakeups.pending(user.id, WakeupKind.USER_QUIET) == []


async def test_user_message_cancels_and_streak_caps(user, clock):
    wakeups = WakeupService()
    tracker = QuietTracker(wakeups)
    await tracker.after_assistant_message(user.id, "You there?")
    assert await tracker.on_user_message(user.id) == 1
    assert await tracker.after_assistant_message(user.id, "Still there?", streak=2) is None


async def test_still_quiet_checks_for_later_user_message(user, clock):
    asked_at = timeutil.now() - timedelta(hours=1)
    tracker = QuietTracker(WakeupService())
    assert await tracker.still_quiet(user.id, asked_at)
    await messages.log(user.id, Role.USER, "here!")
    assert not await tracker.still_quiet(user.id, asked_at)

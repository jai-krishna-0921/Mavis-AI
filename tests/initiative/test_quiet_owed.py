"""H5: USER_QUIET is only the onboarding nudge; offers are never chased; the onboarding window ends."""

from datetime import timedelta

import pytest

from mavis.domain import timeutil
from mavis.domain.decisions import InitiativeDecision
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.loops import LoopKind, LoopOrigin, LoopUpsert
from mavis.domain.messages import Role
from mavis.domain.wakeups import WakeupKind
from mavis.initiative.quiet import QuietTracker
from mavis.initiative.reasoner import Reasoner
from mavis.initiative.wiring import build_initiative
from mavis.loops.service import LoopService
from mavis.store.repo import messages, users
from mavis.timers.service import WakeupService

OFFERS = ["Want me to draft a reply?", "Should I look up flights for you?", "Need anything else?",
          "Shall I set a reminder for that?"]


async def no_embed(texts):
    return [[1.0, 0.0] for _ in texts]


async def _veteran(user, clock):
    """A user whose first message was well past the onboarding window."""
    await messages.log(user.id, Role.USER, "hello")
    clock.advance(days=5)


@pytest.mark.parametrize("question", OFFERS)
async def test_offers_are_never_chased_after_onboarding(user, clock, question):
    await _veteran(user, clock)
    wakeups = WakeupService()
    assert await QuietTracker(wakeups).after_assistant_message(user.id, question) is None
    assert await wakeups.pending(user.id, WakeupKind.USER_QUIET) == []


async def test_onboarding_flag_is_set_when_the_window_ends(user, clock):
    tracker = QuietTracker(WakeupService())
    await messages.log(user.id, Role.USER, "hi")
    assert await tracker.in_onboarding(user.id)
    assert not (await users.get(user.id)).onboarded
    clock.advance(days=4)
    assert not await tracker.in_onboarding(user.id)
    assert (await users.get(user.id)).onboarded  # nothing else set it: the gate was dead in prod


@pytest.mark.parametrize("question", OFFERS)
async def test_new_user_with_items_is_not_chased_about_offers(user, clock, recording_bus, question):
    await messages.log(user.id, Role.USER, "remind me to pay rent")
    await LoopService(recording_bus).upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Pay rent",
                                                                origin=LoopOrigin.CONVERSATION))
    assert await QuietTracker(WakeupService()).after_assistant_message(user.id, question) is None


async def test_new_user_onboarding_question_is_still_nudged(user, clock):
    """spec 4.5: the onboarding nudge after the first exchange, while nothing is known yet."""
    await messages.log(user.id, Role.USER, "hi")
    wakeups = WakeupService()
    assert await QuietTracker(wakeups).after_assistant_message(user.id, "What's on your plate?") is not None


async def test_armed_quiet_wakeup_is_revalidated_when_it_fires(user, clock, recording_bus, fake_memory,
                                                               monkeypatch):
    """A USER_QUIET armed before the gate was fixed (prod) does not reach the reasoner."""
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    called = {"n": 0}

    async def counting(*a, **k):
        called["n"] += 1
        return InitiativeDecision()

    monkeypatch.setattr(Reasoner, "decide", counting)
    await _veteran(user, clock)
    asked = timeutil.now() - timedelta(hours=4)
    await init.handler.handle(Event(id="wakeup:3219", user_id=user.id, type=EventType.USER_QUIET,
                                    occurred_at=timeutil.now(), source="timer", trust=Trust.SYSTEM,
                                    payload={"kind": "user_quiet", "asked_at": asked.isoformat(),
                                             "question": "Want me to prep anything?", "wakeup_id": 3219}))
    assert called["n"] == 0

# ruff: noqa: F811
"""Live E2E 2026-10-08 (D2): a card never breaks the conversation.

A pending card is decided by a plain yes / no (any short run of yes-words), edited only by the message
after tapping Edit or a reply to the card itself, and never swallows an unrelated message. A card is
additive to the reply: substantive prose is kept next to it. A repeated request is one card."""

from __future__ import annotations

import pytest

from mavis.agents.conversation import run_turn
from mavis.domain.events import JobKind
from mavis.domain.messages import Role
from mavis.domain.tasks import ApprovalStatus
from mavis.policy.approvals import EDIT_QUESTION, ApprovalReplyInterpretation, quick_decision
from mavis.store.repo import approvals, messages
from tests.agents.test_conversation import _call, _event, _prompted, _texts, jobs, tools  # noqa: F401


@pytest.mark.parametrize("text", [
    "yes go ahead", "Yes, go ahead!", "yep do it", "sounds good, go for it", "ok sure", "yes please",
    "go ahead and send it", "absolutely, do it", "👍",
])
def test_plain_affirmations_approve(text):
    assert quick_decision(text).decision == "approve"


@pytest.mark.parametrize("text", ["no", "no thanks", "don't do it", "nope, cancel that", "nah never mind",
                                  "please don't"])
def test_plain_refusals_cancel(text):
    assert quick_decision(text).decision == "cancel"


@pytest.mark.parametrize("text", [
    "yes but make it shorter", "ok what time is it", "yes and remind me at 5", "go ahead and email Bob",
    "sure?", "yes no", "do it tomorrow", "remind me in 3 minutes to drink water", "not yes",
    "ok I will think about it",
])
def test_anything_else_is_not_a_decision(text):
    assert quick_decision(text) is None


async def test_yes_go_ahead_approves_the_waiting_card_without_a_second_one(user, channel, fake_llm,
                                                                           fake_memory, jobs, tools):
    aid = await _prompted(user.id)
    await run_turn(_event(user.id, "yes go ahead"))
    [resume] = jobs(JobKind.RESUME_TASK)
    assert resume.payload["approval_id"] == aid and resume.payload["decision"] == "ok"
    assert fake_llm.calls == [] and len(await approvals.open_for_user(user.id)) == 1


@pytest.mark.parametrize("text", [
    "remind me in 3 minutes to drink water", "what's the weather like", "make it shorter and warmer",
])
async def test_an_unrelated_message_is_never_taken_for_an_edit_of_a_pending_card(
    user, channel, fake_llm, fake_memory, jobs, tools, text
):
    aid = await _prompted(user.id)
    # a model that would call it an edit, as it did live: it must not even be asked
    fake_llm.push_structured(ApprovalReplyInterpretation(decision="edit", instructions=text))
    fake_llm.push_text("Sure thing.")
    await run_turn(_event(user.id, text))
    assert jobs(JobKind.RESUME_TASK) == [] and await _texts() == ["Sure thing."]
    assert (await approvals.get(aid)).status == ApprovalStatus.PENDING


async def test_a_change_sent_as_a_reply_to_the_card_is_an_edit(user, channel, fake_llm, fake_memory, jobs,
                                                               tools):
    aid = await _prompted(user.id, "hi")
    fake_llm.push_structured(ApprovalReplyInterpretation(decision="edit", instructions="say hello"))
    event = _event(user.id, "say hello")
    event.payload["reply_to_text"] = "Ready when you are. Want me to go ahead?\n\nSend note: hi"
    await run_turn(event)
    [resume] = jobs(JobKind.RESUME_TASK)
    assert resume.payload["approval_id"] == aid and resume.payload["decision"] == "edit"


async def test_a_reply_to_some_other_message_is_not_an_edit(user, channel, fake_llm, fake_memory, jobs,
                                                            tools):
    await _prompted(user.id, "hi")
    fake_llm.push_text("Noted.")
    event = _event(user.id, "say hello")
    event.payload["reply_to_text"] = "Here is your summary of the week."
    await run_turn(event)
    assert jobs(JobKind.RESUME_TASK) == []


async def test_after_tapping_edit_only_the_next_message_is_the_change(user, channel, fake_llm, fake_memory,
                                                                      jobs, tools):
    aid = await _prompted(user.id)
    await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.AWAITING_EDIT)
    await messages.log(user.id, Role.ASSISTANT, EDIT_QUESTION)
    await messages.log(user.id, Role.USER, "wait")
    await messages.log(user.id, Role.ASSISTANT, "Your 3pm moved to 4pm.", proactive=True)  # time moved on
    fake_llm.push_text("Ok.")
    await run_turn(_event(user.id, "what's on tomorrow"))
    assert jobs(JobKind.RESUME_TASK) == []


ANALYSIS = ("Here is what the numbers say.\nYour average is 47,000 a month. The first three months average "
            "42.2k and the last three 51.8k, so spending is up 23%.\nJune to July is the big jump: +12,000, "
            "which is 31%.\nWant me to keep an eye on it?")


async def test_a_card_never_replaces_the_substantive_answer(user, channel, fake_llm, fake_memory, jobs,
                                                            tools):
    from mavis.domain.messages import TAINT_SUFFIX

    await messages.log(user.id, Role.ASSISTANT, "Found some desks online.",
                       event_id=f"reply:tg:update:0{TAINT_SUFFIX}")
    fake_llm.push_ai(_call("track_loop", {"kind": "ROUTINE", "title": "Weekly money check-in"}))
    fake_llm.push_text(ANALYSIS)
    await run_turn(_event(user.id, "analyse these monthly figures: 40k 41k 46k 50k 52k 53k"))
    [card] = await approvals.open_for_user(user.id)
    assert card.tool == "track_loop"
    assert await _texts() == [ANALYSIS]


async def test_a_card_still_drops_prose_that_only_points_at_it(user, channel, fake_llm, fake_memory, jobs,
                                                               tools):
    fake_llm.push_ai(_call("send_note", {"text": "running late"}))
    fake_llm.push_text("Ready when you are. Just tap Approve to send it!")
    await run_turn(_event(user.id, "tell Priya I'm running late"))
    assert await _texts() == []
    assert len(await approvals.open_for_user(user.id)) == 1


async def test_the_same_request_in_other_words_is_one_card(user, channel, fake_llm, fake_memory, jobs, tools):
    from tests.agents.test_self_only_persist import PHISH  # noqa: F401 - the untrusted read taints the turn

    for n, goal in enumerate([
        "Research GATE CS coaching institutes in Chennai and compare fees and reviews",
        "Compare GATE coaching institutes in Chennai: fees, reviews",
    ], 1):
        fake_llm.push_ai(_call("read_page", {}, f"r{n}"))
        fake_llm.push_ai(_call("start_task", {"goal": goal}, f"s{n}"))
        fake_llm.push_text("Waiting on your OK.")
        await run_turn(_event(user.id, "yes do that" if n == 1 else "go on then, the research", n))
    assert [a.tool for a in await approvals.open_for_user(user.id)] == ["start_task"]


async def test_a_different_request_is_its_own_card(user, channel, fake_llm, fake_memory, jobs, tools):
    for n, goal in enumerate([
        "Research GATE CS coaching institutes in Chennai and compare fees and reviews",
        "Plan a three day Ladakh trip with a packing list",
    ], 1):
        fake_llm.push_ai(_call("read_page", {}, f"r{n}"))
        fake_llm.push_ai(_call("start_task", {"goal": goal}, f"s{n}"))
        fake_llm.push_text("Waiting on your OK.")
        await run_turn(_event(user.id, "yes do that", n))
    assert [a.tool for a in await approvals.open_for_user(user.id)] == ["start_task", "start_task"]


async def test_the_same_reminder_words_for_another_time_is_its_own_card(user, channel, fake_llm, fake_memory,
                                                                        jobs, tools):
    for n, at in enumerate(["2030-01-01T09:00:00", "2030-01-01T18:00:00"], 1):
        fake_llm.push_ai(_call("read_page", {}, f"r{n}"))
        fake_llm.push_ai(_call("wake_me", {"at": at, "reason": "stretch and drink some water"}, f"w{n}"))
        fake_llm.push_text("Waiting on your OK.")
        await run_turn(_event(user.id, "yes do that", n))
    assert [a.tool for a in await approvals.open_for_user(user.id)] == ["wake_me", "wake_me"]

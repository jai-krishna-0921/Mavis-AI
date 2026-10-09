"""Where a message goes: replies back to the channel that asked, proactive messages by preference."""

import pytest

from mavis.agents import commands
from mavis.agents.simple_turn import run_turn
from mavis.channels import routing
from mavis.channels.base import ChannelRateLimited
from mavis.channels.outbox_sender import OutboxSender
from mavis.channels.progress_card import ProgressCards
from mavis.channels.slack import chat_id
from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.messages import Button, Outbound
from mavis.domain.plans import PlanStep
from mavis.store.repo import messages, outbox, users
from mavis.tools.integrations.connect_flow import ConnectFlow
from mavis.worker import runner
from tests.channels.slack_fakes import MY_DM, TEAM, SlackFake
from tests.tools.integrations.fakes import NOW

TG = 7001
DM_CHAT = chat_id(TEAM, MY_DM)
THREAD = chat_id(TEAM, "C0CHAN001", "1760000200.000100")
FIRE = "\U0001f525"
CARD = [[Button(label="Send", data="ap:1:ok"), Button(label="Cancel", data="ap:1:no")]]


@pytest.fixture
async def world(db, channel, monkeypatch):
    """A Mavis user with Telegram chat TG; Slack reachable through DM_CHAT unless a test breaks it."""
    slack = SlackFake()
    routing.set_slack_channel(slack)
    reach = {"ok": True}

    async def slack_chat_for(user_id):
        return DM_CHAT if reach["ok"] else None

    monkeypatch.setattr(routing, "slack_chat_for", slack_chat_for)
    user, _ = await users.get_or_create_by_chat(TG, "Jai")
    yield user, slack, channel, reach
    routing.set_slack_channel(None)


async def deliver(**kw):
    return await OutboxSender().run_once(**kw)


async def queue(user, text="hello", **kw):
    return await outbox.enqueue_now(Outbound(user_id=user.id, text=text, **kw))


def slack_event(user_id, text="hi", *, chat=DM_CHAT, n=1, **payload):
    return Event(id=f"slack:chat:{TEAM}:{MY_DM}:{n}", user_id=user_id, type=EventType.USER_MESSAGE,
                 occurred_at=timeutil.now(), source="slack_chat", trust=Trust.USER,
                 payload={"text": text, "message_id": 1760000100000100, "reply_chat": chat, **payload})


# --- destination rules ---------------------------------------------------------------------------------


async def test_default_goes_to_telegram_only(world):
    user, slack, tg, _ = world
    await queue(user)
    await queue(user, "proactive hello", proactive=True)
    await deliver()
    assert tg.texts == ["hello", "proactive hello"] and slack.sent == []


@pytest.mark.parametrize("pref,telegram,slackbox", [("telegram", 1, 0), ("slack", 0, 1), ("both", 1, 1)])
async def test_proactive_messages_follow_the_channel_preference(world, pref, telegram, slackbox):
    user, slack, tg, _ = world
    await routing.set_pref(user.id, pref)
    await queue(user, "morning brief", proactive=True)
    await deliver()
    assert len(tg.texts) == telegram and len(slack.texts) == slackbox
    if slackbox:
        assert slack.sent[0].chat_id == DM_CHAT


async def test_a_bad_or_missing_preference_means_telegram(world):
    user, slack, tg, _ = world
    await users.update_state(user.id, {"channel_pref": "carrier-pigeon"})
    await queue(user, "x", proactive=True)
    await deliver()
    assert tg.texts == ["x"] and slack.sent == []


async def test_non_proactive_follows_where_the_user_last_wrote_from(world):
    user, slack, tg, _ = world
    await users.update_state(user.id, {"last_channel": "slack"})
    await queue(user, "task result")
    await queue(user, "proactive", proactive=True)  # preference still telegram
    await deliver()
    assert slack.texts == ["task result"] and tg.texts == ["proactive"]


async def test_unreachable_slack_falls_back_to_telegram(world):
    user, slack, tg, reach = world
    reach["ok"] = False
    await routing.set_pref(user.id, "slack")
    await users.update_state(user.id, {"last_channel": "slack"})
    await queue(user, "a", proactive=True)
    await queue(user, "b")
    await deliver()
    assert tg.texts == ["a", "b"] and slack.sent == []


async def test_a_routed_row_goes_only_where_it_says(world):
    user, slack, tg, _ = world
    await queue(user, "in thread", route=THREAD)
    await deliver()
    assert slack.sent[0].chat_id == THREAD and tg.texts == []


async def test_cards_with_buttons_never_go_to_a_shared_thread(world):
    user, slack, tg, _ = world
    await queue(user, "Send this email?", route=THREAD, buttons=CARD)
    await queue(user, "plain follow up", route=THREAD)
    await deliver()
    assert [(s.chat_id, s.text) for s in slack.sent] == [(DM_CHAT, "Send this email?"),
                                                         (THREAD, "plain follow up")]
    assert slack.sent[0].buttons[0][0].data == "ap:1:ok"


async def test_both_channels_telegram_failure_retries_but_slack_failure_does_not(world):
    user, slack, tg, _ = world
    await routing.set_pref(user.id, "both")
    oid = await queue(user, "brief", proactive=True)
    slack.fail_next.append(RuntimeError("slack down"))
    assert await deliver() == 1  # Telegram copy delivered, the Slack copy is best effort
    assert tg.texts == ["brief"] and (await outbox_row(oid)).status == "sent"
    oid2 = await queue(user, "second", proactive=True)
    tg.fail_next.append(RuntimeError("telegram down"))
    assert await deliver() == 0
    assert (await outbox_row(oid2)).status == "pending" and slack.texts == []  # nothing duplicated


@pytest.mark.parametrize("failure", [ChannelRateLimited(3), RuntimeError("slack down")])
async def test_both_channels_a_later_failure_never_redelivers_the_first(world, failure):
    user, slack, tg, _ = world
    await routing.set_pref(user.id, "both")
    oid = await queue(user, "brief", proactive=True)
    slack.fail_next.append(failure)
    assert await deliver() == 1
    assert tg.texts == ["brief"] and (await outbox_row(oid)).status == "sent"
    assert await deliver() == 0 and tg.texts == ["brief"]  # no retry, no duplicate


async def test_a_slack_rate_limit_is_a_backoff_not_a_failure(world):
    user, slack, _, _ = world
    oid = await queue(user, "x", route=DM_CHAT)
    slack.fail_next.append(ChannelRateLimited(3))
    await deliver()
    row = await outbox_row(oid)
    assert row.status == "pending" and row.attempts == 0


async def outbox_row(oid):
    from mavis.store.db import Session
    from mavis.store.models import OutboxMessage

    async with Session() as s:
        return await s.get_one(OutboxMessage, oid)


# --- a whole turn ----------------------------------------------------------------------------------------


async def test_slack_turn_replies_on_slack_reacts_on_slack_and_telegram_stays_quiet(
    world, fake_llm, memory, bus
):
    user, slack, tg, _ = world
    runner.register_event_handler(EventType.USER_MESSAGE, run_turn)
    fake_llm.push_text(f"You have two meetings.\n[react: {FIRE}]")
    await runner.handle_event(slack_event(user.id, "what is on today?"))
    await deliver()
    assert slack.texts == ["You have two meetings."] and slack.sent[0].chat_id == DM_CHAT
    assert tg.texts == [] and tg.reactions == [] and not [s for s in tg.sent if s.kind == "typing"]
    assert (DM_CHAT, 1760000100000100, FIRE) in slack.reactions
    assert (await messages.recent(user.id))[-1].content == "You have two meetings."
    assert (await users.get(user.id)).state["last_channel"] == "slack"


async def test_mention_turn_answers_in_the_thread(world, fake_llm, memory, bus):
    user, slack, tg, _ = world
    runner.register_event_handler(EventType.USER_MESSAGE, run_turn)
    fake_llm.push_text("Here is the summary.")
    await runner.handle_event(slack_event(user.id, "summarise this", chat=THREAD))
    await deliver()
    assert [s.chat_id for s in slack.sent if s.kind == "text"] == [THREAD] and tg.texts == []


async def test_telegram_turns_are_unchanged_and_move_the_last_channel_back(world, fake_llm, memory, bus):
    user, slack, tg, _ = world
    await users.update_state(user.id, {"last_channel": "slack"})
    runner.register_event_handler(EventType.USER_MESSAGE, run_turn)
    fake_llm.push_text("Hey.")
    event = Event(id="tg:update:5", user_id=user.id, type=EventType.USER_MESSAGE, occurred_at=timeutil.now(),
                  source="telegram", payload={"text": "hi", "message_id": 10}, trust=Trust.USER)
    await runner.handle_event(event)
    await deliver()
    assert tg.texts == ["Hey."] and slack.sent == []
    assert (await users.get(user.id)).state["last_channel"] == "telegram"


async def test_the_route_does_not_leak_into_the_next_event(world, fake_llm, memory, bus):
    user, slack, tg, _ = world
    runner.register_event_handler(EventType.USER_MESSAGE, run_turn)
    fake_llm.push_text("slack answer")
    await runner.handle_event(slack_event(user.id, "one"))
    from mavis.channels.turn_route import reply_chat

    assert reply_chat.get() is None
    row = await queue(user, "later")
    assert (await outbox_row(row)).route is None


# --- progress cards ----------------------------------------------------------------------------------------


async def test_progress_card_for_a_slack_user_lives_in_the_dm_and_is_edited_there(world):
    user, slack, tg, _ = world
    await users.update_state(user.id, {"last_channel": "slack"})
    cards = ProgressCards(clock=lambda: 0.0, wall=lambda: 0.0)
    steps = [PlanStep(id="s1", agent="researcher", instruction="look it up", title="Look it up")]
    # the cards use the process channels, resolved per chat
    await cards.start(11, user.id, "Find the report", steps, tainted=False)
    [sent] = slack.sent
    assert sent.chat_id == DM_CHAT and sent.buttons[0][0].data == "tk:11:x" and tg.sent == []
    from mavis.domain.progress import CardFinal

    await cards.finalize(11, CardFinal.DONE)
    assert slack.edits and slack.edits[-1][0] == DM_CHAT and slack.edits[-1][3] == []


# --- /channel -------------------------------------------------------------------------------------------


def cmd(user_id, text):
    return Event(id=f"cmd:{text}", user_id=user_id, type=EventType.USER_MESSAGE, occurred_at=NOW,
                 source="telegram", payload={"text": text}, trust=Trust.USER)


@pytest.fixture
def flow(provider, cache, fake_bus, rec, state):
    return ConnectFlow(provider=provider, cache=cache, bus=fake_bus, notify=rec.notify, schedule=rec.schedule,
                       state=state, base_url="https://mavis.test", clock=lambda: NOW)


async def test_channel_command_shows_and_sets_the_preference(world, flow, rec):
    user, _, _, _ = world
    assert await commands.run_command(cmd(user.id, "/channel"), flow)
    assert "Telegram" in rec.sent[-1].text and "/channel slack" in rec.sent[-1].text
    await commands.run_command(cmd(user.id, "/channel slack"), flow)
    assert rec.sent[-1].text == "Done. My own messages now go to Slack."
    assert routing.pref_of(await users.get_state(user.id)) == "slack"
    await commands.run_command(cmd(user.id, "/channel BOTH"), flow)
    assert routing.pref_of(await users.get_state(user.id)) == "both"
    await commands.run_command(cmd(user.id, "/channel telegram"), flow)
    assert routing.pref_of(await users.get_state(user.id)) == "telegram"


async def test_channel_command_refuses_slack_when_it_is_not_set_up(world, flow, rec):
    user, _, _, reach = world
    reach["ok"] = False
    await commands.run_command(cmd(user.id, "/channel both"), flow)
    assert "/connect slack" in rec.sent[-1].text
    assert routing.pref_of(await users.get_state(user.id)) == "telegram"
    await commands.run_command(cmd(user.id, "/channel pigeon"), flow)
    assert routing.pref_of(await users.get_state(user.id)) == "telegram"
    await commands.run_command(cmd(user.id, "/channel telegram"), flow)  # always allowed
    assert rec.sent[-1].text.startswith("Done")


def test_no_long_dashes_in_user_facing_copy():
    from mavis.agents.commands import CHANNEL_NAMES
    from mavis.channels import slack_inbound, slack_interactive

    texts = [slack_inbound.UNKNOWN_REPLY, slack_interactive.NOT_YOURS, slack_interactive.UNKNOWN,
             slack_interactive.NO_DM, *CHANNEL_NAMES.values()]
    assert not any("—" in t or "–" in t for t in texts)

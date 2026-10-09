"""DMs and @mentions to the Mavis bot become chat turns; nothing else does, and nothing loops."""

import pytest

from mavis.channels import routing, slack_inbound
from mavis.channels.slack import chat_id, id_to_ts, ts_to_id
from mavis.domain.events import EventType, Trust
from mavis.tools.integrations.native import slack_events
from tests.channels.slack_fakes import (
    ALICE,
    BOT_UID,
    MY_DM,
    STRANGER,
    TEAM,
    Lookup,
    SlackFake,
    callback,
    dm_message,
    mention,
)
from tests.tools.integrations.fakes import FakeBus


@pytest.fixture
def env(monkeypatch):
    slack_inbound._told.clear()
    monkeypatch.setattr(slack_events, "_dedupe", slack_events.EventDedupe())
    slack = SlackFake()
    routing.set_slack_channel(slack)
    yield FakeBus(), Lookup(), slack
    routing.set_slack_channel(None)


async def run(env, event, **kw):
    bus, lookup, _ = env
    return await slack_events.handle_callback(callback(event, **kw), lookup, bus)


async def test_dm_becomes_a_user_message_routed_back_to_the_dm(env):
    bus, _, slack = env
    counts = await run(env, dm_message("What is on my calendar today?"))
    assert counts["published"] == 1
    [ev] = bus.events
    assert ev.type is EventType.USER_MESSAGE and ev.user_id == 7 and ev.trust is Trust.USER
    assert ev.source == "slack_chat" and ev.id == f"slack:chat:{TEAM}:{MY_DM}:1760000100.000100"
    assert ev.payload["text"] == "What is on my calendar today?"
    assert ev.payload["reply_chat"] == chat_id(TEAM, MY_DM)
    assert id_to_ts(ev.payload["message_id"]) == "1760000100.000100"
    assert slack.sent == []


async def test_mention_is_cleaned_and_answered_in_its_thread(env):
    bus, _, _ = env
    await run(env, mention(
        f"<@{BOT_UID}>  please look at <https://x.com/a?b=1|this doc> &amp; <#C0CHAN001|ops>"))
    [ev] = bus.events
    assert ev.payload["text"] == "please look at this doc (https://x.com/a?b=1) & #ops"
    assert ev.payload["reply_chat"] == chat_id(TEAM, "C0CHAN001", "1760000200.000100")


async def test_mention_inside_a_thread_replies_in_that_thread(env):
    bus, _, _ = env
    await run(env, mention(thread_ts="1760000000.000900"))
    assert bus.events[0].payload["reply_chat"] == chat_id(TEAM, "C0CHAN001", "1760000000.000900")


async def test_slash_text_in_a_dm_is_a_command(env):
    bus, _, _ = env
    await run(env, dm_message("/channel slack"))
    assert bus.events[0].payload["command"] == "channel"


@pytest.mark.parametrize("event", [
    dm_message(bot_id="B0OTHERBOT"),                                   # another app
    dm_message(user=BOT_UID),                                          # Mavis's own post
    dm_message(subtype="bot_message"),
    dm_message(subtype="channel_join"),
    dm_message(subtype="message_changed"),
    dm_message(subtype="message_deleted"),
    dm_message(text="   "),
    dm_message(text=f"<@{BOT_UID}>"),                                  # only a mention, nothing said
    {**dm_message(), "user": ""},
    {**dm_message(), "bot_profile": {"name": "x"}},
    mention(bot_id="B0OTHERBOT"),
    mention(user=BOT_UID, text="hello <@U0X>"),
])
async def test_own_bot_and_system_messages_are_never_processed(env, event):
    bus, _, slack = env
    await run(env, event)
    assert bus.events == [] and slack.sent == []


async def test_unknown_user_gets_one_plain_reply_and_nothing_runs(env):
    bus, _, slack = env
    counts = await run(env, dm_message("hi", user=STRANGER, channel="D0STRANGER"))
    assert counts["unmapped"] == 1 and counts["published"] == 0 and bus.events == []
    [sent] = slack.sent
    assert sent.chat_id == chat_id(TEAM, "D0STRANGER") and "/connect slack" in sent.text
    assert "—" not in sent.text and "–" not in sent.text
    await run(env, dm_message("hello??", user=STRANGER, channel="D0STRANGER", ts="1760000300.000100"),
              event_id="Ev0002")
    assert len(slack.sent) == 1 and bus.events == []  # told once, not nagged


async def test_unknown_user_mention_is_refused_in_thread(env):
    bus, _, slack = env
    await run(env, mention(user=STRANGER))
    assert bus.events == []
    assert slack.sent[0].chat_id == chat_id(TEAM, "C0CHAN001", "1760000200.000100")


async def test_redelivery_and_retry_publish_once(env):
    bus, _, _ = env
    first = await run(env, dm_message(), event_id="EvA")
    again = await run(env, dm_message(), event_id="EvA")  # same event id (Slack retry)
    other = await run(env, dm_message(), event_id="EvB")  # same message under a new delivery id
    assert first["published"] == 1 and again["duplicate"] == 1 and other["duplicate"] == 1
    assert len(bus.events) == 1


async def test_user_token_copy_of_a_bot_dm_is_not_also_a_record(env):
    """The same DM message arriving as a user-token event (no bot authorization) must not become a record."""
    bus, _, _ = env
    counts = await run(env, dm_message(), bot_auth=False)
    assert counts["published"] == 1
    assert [e.type for e in bus.events] == [EventType.USER_MESSAGE]


async def test_bot_delivery_alone_is_chat_even_without_a_noted_dm(env):
    bus, lookup, _ = env
    lookup.dms.clear()
    await run(env, dm_message(), user_auth=None)
    assert [e.type for e in bus.events] == [EventType.USER_MESSAGE]


async def test_other_dms_and_channels_still_become_records(env):
    bus, _, _ = env
    await run(env, dm_message("lunch?", user=ALICE, channel="D0ALICEME1"), bot_auth=False)  # a human DM
    await run(env, {"type": "message", "channel": "C0CHAN001", "channel_type": "channel", "user": ALICE,
                    "text": "deploy at 5", "ts": "1760000400.000100"}, bot_auth=False, event_id="Ev3")
    assert [e.type for e in bus.events] == [EventType.SLACK_MESSAGE, EventType.SLACK_MESSAGE]


async def test_without_an_installed_bot_nothing_changes(env):
    bus, lookup, _ = env
    lookup.bot, lookup.dms = None, {}
    await run(env, dm_message(), bot_auth=False)
    assert [e.type for e in bus.events] == [EventType.SLACK_MESSAGE]


def test_clean_text_handles_entities_and_keeps_other_mentions():
    assert slack_inbound.clean_text("&lt;b&gt; &amp; <@U0OTHER> hi", BOT_UID) == "<b> & <@U0OTHER> hi"
    assert slack_inbound.clean_text("a\n\n  b   c", "") == "a\n\nb c"
    assert ts_to_id("1760000100.000100") == 1760000100000100

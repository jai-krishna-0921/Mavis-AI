"""The Slack adapter against a scripted Web API: threads, Block Kit buttons, edits, reactions, errors."""

import json
from urllib.parse import parse_qsl

import httpx
import pytest

from mavis.agents import reactions
from mavis.channels.base import ChannelRateLimited, MessageGone
from mavis.channels.slack import (
    EMOJI_NAMES,
    SlackChannel,
    SlackError,
    chat_id,
    dm_of,
    emoji_name,
    id_to_ts,
    in_dm,
    is_slack_chat,
    parse_chat,
    ts_to_id,
)
from mavis.channels.slack_format import to_mrkdwn
from mavis.domain.messages import Button

TEAM, DM = "T1", "D0DM00001"


class Api:
    def __init__(self):
        self.calls: list[tuple[str, dict, str]] = []
        self.answers: dict[str, httpx.Response | dict] = {}
        self._n = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("application/json"):
            body = json.loads(request.content)
        elif ctype.startswith("application/x-www-form-urlencoded"):
            body = {k: json.loads(v) if k == "files" else v
                    for k, v in parse_qsl(request.content.decode())}
            if "length" in body:
                body["length"] = int(body["length"])
        else:
            body = {}
        self.calls.append((method, body, request.headers.get("authorization", "")))
        ans = self.answers.get(method)
        if isinstance(ans, httpx.Response):
            return ans
        if ans is not None:
            return httpx.Response(200, json=ans)
        self._n += 1
        return httpx.Response(200, json={"ok": True, "ts": f"1760000{self._n:03d}.000100",
                                         "channel": {"id": DM}})

    def of(self, method):
        return [(b, a) for m, b, a in self.calls if m == method]


@pytest.fixture
def api():
    return Api()


@pytest.fixture
def ch(api):
    async def token(team):
        return "xoxb-test" if team == TEAM else None

    return SlackChannel(token, httpx.AsyncClient(transport=httpx.MockTransport(api)))


def test_chat_handles_round_trip():
    h = chat_id(TEAM, DM)
    assert is_slack_chat(h) and not is_slack_chat(12345) and not is_slack_chat("tg:1")
    assert parse_chat(h).channel == DM and parse_chat(h).thread_ts is None and in_dm(h)
    t = chat_id(TEAM, "C0CHAN001", "1760000100.000200")
    assert parse_chat(t).thread_ts == "1760000100.000200" and not in_dm(t)
    assert dm_of(t) == "slack:T1:C0CHAN001"
    for bad in ("slack:", "slack:T1", "slack::D1", 5):
        with pytest.raises(ValueError):
            parse_chat(bad)


@pytest.mark.parametrize("ts", ["1760000100.000100", "1760000100.123456", "1.000001", "1760000100.000000"])
def test_ts_id_round_trip(ts):
    assert id_to_ts(ts_to_id(ts)) == ts and isinstance(ts_to_id(ts), int)


async def test_send_uses_the_bot_token_and_returns_encoded_ts(ch, api):
    ids = await ch.send_text(chat_id(TEAM, DM), "hello **there**")
    [(body, auth)] = api.of("chat.postMessage")
    assert auth == "Bearer xoxb-test" and body["channel"] == DM and body["text"] == "hello *there*"
    assert "blocks" not in body and "thread_ts" not in body
    assert id_to_ts(ids[0]).startswith("1760000")


async def test_mention_reply_goes_in_the_thread(ch, api):
    await ch.send_text(chat_id(TEAM, "C0CHAN001", "1760000100.000200"), "on it")
    [(body, _)] = api.of("chat.postMessage")
    assert body["channel"] == "C0CHAN001" and body["thread_ts"] == "1760000100.000200"


async def test_buttons_become_block_kit_actions_on_the_last_chunk(ch, api):
    buttons = [[Button(label="Send", data="ap:7:ok"), Button(label="Cancel", data="ap:7:no"),
                Button(label="Open", url="https://example.com/x")]]
    text = ("para one is here\n\n" * 400)  # splits into several messages
    ids = await ch.send_text(chat_id(TEAM, DM), text, buttons)
    posts = api.of("chat.postMessage")
    assert len(posts) == len(ids) > 1
    assert all("blocks" not in b for b, _ in posts[:-1])
    last = posts[-1][0]["blocks"]
    actions = [b for b in last if b["type"] == "actions"][0]["elements"]
    assert [(e["action_id"], e.get("value")) for e in actions[:2]] == [("ap:7:ok", "ap:7:ok"),
                                                                       ("ap:7:no", "ap:7:no")]
    assert actions[2]["url"] == "https://example.com/x" and "value" not in actions[2]
    assert actions[0]["text"] == {"type": "plain_text", "text": "Send", "emoji": True}
    assert all(len(b["text"]["text"]) <= 3000 for b in last if b["type"] == "section")


async def test_edit_replaces_text_and_clears_old_buttons(ch, api):
    await ch.edit_text(chat_id(TEAM, DM), ts_to_id("1760000100.000100"), "Done: **ok**")
    [(body, _)] = api.of("chat.update")
    assert body["ts"] == "1760000100.000100" and body["blocks"] == [] and body["text"] == "Done: *ok*"
    await ch.edit_text(chat_id(TEAM, DM), ts_to_id("1760000100.000100"), "Working",
                       [[Button(label="Cancel", data="tk:1:x")]])
    body = api.of("chat.update")[1][0]
    assert body["blocks"][-1]["elements"][0]["value"] == "tk:1:x"


@pytest.mark.parametrize("code", ["message_not_found", "cant_update_message", "edit_window_closed"])
async def test_edit_of_a_vanished_message_is_message_gone(ch, api, code):
    api.answers["chat.update"] = {"ok": False, "error": code}
    with pytest.raises(MessageGone):
        await ch.edit_text(chat_id(TEAM, DM), 5, "x")


async def test_rate_limits_surface_as_retry_after(ch, api):
    api.answers["chat.postMessage"] = httpx.Response(429, headers={"retry-after": "7"}, json={})
    with pytest.raises(ChannelRateLimited) as exc:
        await ch.send_text(chat_id(TEAM, DM), "x")
    assert exc.value.retry_after == 7
    api.answers["chat.postMessage"] = {"ok": False, "error": "ratelimited"}
    with pytest.raises(ChannelRateLimited):
        await ch.send_text(chat_id(TEAM, DM), "x")


async def test_errors_carry_the_code_only_and_missing_bot_is_an_error(ch, api):
    api.answers["chat.postMessage"] = {"ok": False, "error": "channel_not_found", "detail": "xoxb-secret"}
    with pytest.raises(SlackError) as exc:
        await ch.send_text(chat_id(TEAM, DM), "x")
    assert str(exc.value) == "slack: channel_not_found" and "xoxb" not in str(exc.value)
    with pytest.raises(SlackError, match="bot_not_installed"):
        await ch.send_text(chat_id("T9", DM), "x")


async def test_every_mood_reaction_has_a_slack_name():
    for _, group in reactions.MOODS:
        for emoji in group:
            assert emoji_name(emoji), emoji
    assert emoji_name(reactions.normalize("\N{EYES}") or "\N{EYES}") == "eyes"
    assert all(n == n.lower() and " " not in n for n in EMOJI_NAMES.values())


async def test_react_replaces_the_previous_reaction_and_clears(ch, api):
    chat, mid = chat_id(TEAM, DM), ts_to_id("1760000100.000100")
    await ch.react(chat, mid, "\N{EYES}")
    await ch.react(chat, mid, "\N{FIRE}")
    await ch.react(chat, mid, None)
    seq = [(m, b["name"]) for m, b, _ in api.calls]
    assert seq == [("reactions.add", "eyes"), ("reactions.remove", "eyes"), ("reactions.add", "fire"),
                   ("reactions.remove", "fire")]


async def test_react_tolerates_known_errors_and_unknown_emoji(ch, api):
    chat, mid = chat_id(TEAM, DM), 1760000100000100
    api.answers["reactions.add"] = {"ok": False, "error": "already_reacted"}
    await ch.react(chat, mid, "\N{FIRE}")
    api.answers["reactions.remove"] = {"ok": False, "error": "no_reaction"}
    await ch.react(chat, mid, None)
    n = len(api.calls)
    await ch.react(chat, mid, "\N{ALIEN MONSTER}")  # no mapping: skipped, no call
    assert len(api.calls) == n


async def test_typing_is_a_noop_and_open_dm_is_cached(ch, api):
    await ch.send_typing(chat_id(TEAM, DM))
    assert api.calls == []
    assert await ch.open_dm(TEAM, "U1") == DM and await ch.open_dm(TEAM, "U1") == DM
    assert len(api.of("conversations.open")) == 1


async def test_document_upload_uses_the_external_upload_flow(ch, api, tmp_path):
    f = tmp_path / "plan.txt"
    f.write_text("hello")
    api.answers["files.getUploadURLExternal"] = {"ok": True, "upload_url": "https://files.slack.test/up",
                                                 "file_id": "F1"}
    await ch.send_document(chat_id(TEAM, "C0CHAN001", "1760000100.000200"), str(f), "the plan")
    assert api.of("files.getUploadURLExternal")[0][0] == {"filename": "plan.txt", "length": 5}
    done = api.of("files.completeUploadExternal")[0][0]
    assert done["channel_id"] == "C0CHAN001" and done["thread_ts"] == "1760000100.000200"
    assert done["files"] == [{"id": "F1", "title": "plan.txt"}] and done["initial_comment"] == "the plan"


def test_mrkdwn_escapes_and_converts():
    out = to_mrkdwn("# Plan\n**Bold** and _it_ <script> & [site](https://a.com/?a=1&b=2)\n- one\n- two")
    assert out.splitlines()[0] == "*Plan*"
    assert "*Bold*" in out and "&lt;script&gt; &amp;" in out
    assert "<https://a.com/?a=1&amp;b=2|site>" in out and "• one" in out


def test_mrkdwn_keeps_code_and_verbatim_exactly_and_has_no_long_dashes():
    out = to_mrkdwn("run `a<b` now — ok\n```\nx = 1 & 2\n```\n\x0eTo: a@b.com — hi\x0f")
    assert "`a&lt;b`" in out and "x = 1 &amp; 2" in out and "To: a@b.com — hi" in out
    assert "now — ok" not in out

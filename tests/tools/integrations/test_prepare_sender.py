# ruff: noqa: E501, F811
import pytest

from mavis.tools import integrations
from mavis.tools.integrations.actions import CalendarCreateArgs, MailComposeArgs, MailReplyArgs
from mavis.tools.integrations.mail_render import render_search
from mavis.tools.integrations.native.router import NativeRouter
from mavis.tools.integrations.workspace_tools import PREPARES, prepare_sender
from mavis.tools.registry import ToolContext
from tests.tools.integrations.fakes import FakeProvider

from .native.conftest import client, native_env, oauth, tokens, vendor  # noqa: F401  (fixtures)
from .native.test_router import ALL_GOOGLE, G, grant
from .native.test_router_merge import Exec, M, ms_grant

CTX = ToolContext(user_id=1)
MAIL = MailComposeArgs(to=["a@b.com"], subject="s", body="b")


@pytest.fixture
def router(tokens, oauth, client, monkeypatch):  # noqa: F811
    r = NativeRouter(FakeProvider(), tokens, oauth, [Exec(G, {"ok": 1}), Exec(M, {"ok": 1})], client)
    def get_provider():
        return r

    get_provider.cache_clear = lambda: None  # the suite's autouse reset calls it
    monkeypatch.setattr(integrations, "get_provider", get_provider)
    return r


def test_registered_for_the_four_outgoing_actions():
    for name in ("mail.send", "mail.draft", "mail.reply", "calendar.create_event"):
        assert PREPARES[name] is prepare_sender


async def test_two_accounts_and_no_default_refuses_naming_both(router, tokens):
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    out = await prepare_sender(CTX, MAIL)
    assert out.refusal and "work: me@corp.test" in out.refusal and "personal: u1@x.com" in out.refusal


async def test_explicit_account_names_the_sender(router, tokens):
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    out = await prepare_sender(CTX, MailComposeArgs(to=["a@b.com"], subject="s", body="b", account="me@corp.test"))
    assert out.note == "From: me@corp.test" and out.refusal is None


async def test_calendar_create_uses_the_calendar_kind(router, tokens):
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    args = CalendarCreateArgs(summary="x", start="2026-10-20T10:00:00", account="google")
    assert (await prepare_sender(CTX, args)).note == "From: u1@x.com"


async def test_reply_follows_the_thread_prefix(router, tokens):
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    out = await prepare_sender(CTX, MailReplyArgs(thread_id="ms:T1", to="a@b.com", body="hi"))
    assert out.note == "From: me@corp.test"
    out = await prepare_sender(CTX, MailReplyArgs(thread_id="18c0ffee", to="a@b.com", body="hi"))
    assert out.note == "From: u1@x.com"


async def test_microsoft_only_user_sees_the_sender(router, tokens):
    await ms_grant(tokens)
    assert (await prepare_sender(CTX, MAIL)).note == "From: me@corp.test"


async def test_google_only_user_keeps_todays_card(router, tokens):
    await grant(tokens, G, ALL_GOOGLE)
    out = await prepare_sender(CTX, MAIL)
    assert out.note is None and out.refusal is None and out.risk is None


def test_render_search_shows_account_and_notes():
    data = {"messages": [{"messageId": "m", "threadId": "t", "sender": "A <a@x.com>", "subject": "S",
                          "labelIds": [], "messageTimestamp": "1790000000000", "messageText": "hi",
                          "account": "work: me@corp.test"}],
            "notes": ["Could not reach personal: u1@x.com (temporarily unavailable)"]}
    text = render_search(data)
    assert "Account: work: me@corp.test" in text
    assert "Note: Could not reach personal: u1@x.com (temporarily unavailable)" in text


def test_render_search_without_the_keys_is_unchanged():
    data = {"messages": [{"messageId": "m", "threadId": "t", "sender": "A <a@x.com>", "subject": "S",
                          "labelIds": [], "messageTimestamp": "1790000000000", "messageText": "hi"}]}
    text = render_search(data)
    assert "Account:" not in text and "Note:" not in text
    assert text.splitlines()[0] == "1 email(s). Call mail_read with a message_id to read one in full."

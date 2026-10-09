"""Which Google actions a grant allows: a table keyed by action, any-of scope sets, all groups needed."""

from __future__ import annotations

import pytest

from mavis.domain.errors import FailureKind, failure_text
from mavis.domain.integrations import ConnectionState, UserRef
from mavis.domain.policy import Capability
from mavis.tools.integrations.actions import ACTIONS
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.router import ACTION_SCOPES, NativeRouter, allows
from mavis.tools.integrations.native.tokens import Grant
from tests.tools.integrations.fakes import FakeProvider

from .conftest import *  # noqa: F403 - fixtures
from .gfake import FakeGoogle
from .test_router import FakeExecutor, grant

A = "https://www.googleapis.com/auth/"
G = NativeProvider.GOOGLE
U = UserRef(user_id=1)


def g(*scopes: str) -> Grant:
    return Grant(1, G, "ACTIVE", {"scopes": [s if s.startswith("http") else A + s for s in scopes]})


def test_every_action_the_google_executor_handles_has_a_scope_entry():
    handled = set(FakeGoogle().executor()[0]._handlers)
    assert handled <= set(ACTION_SCOPES), sorted(handled - set(ACTION_SCOPES))
    assert set(ACTION_SCOPES) <= set(ACTIONS)  # no entry for an action that does not exist


@pytest.mark.parametrize(
    ("scopes", "allowed", "denied"),
    [
        (["gmail.readonly"], ["mail.search", "mail.read", "mail.thread", "mail.profile"],
         ["mail.send", "mail.draft", "mail.reply"]),
        (["gmail.send"], ["mail.send"], ["mail.search", "mail.draft", "mail.reply"]),
        (["gmail.compose"], ["mail.draft", "mail.send"], ["mail.search", "mail.reply"]),
        (["gmail.readonly", "gmail.send"], ["mail.send", "mail.reply", "mail.search"], ["mail.draft"]),
        (["gmail.readonly", "gmail.compose"], ["mail.send", "mail.draft", "mail.reply"], []),
        (["gmail.modify"], ["mail.search", "mail.send", "mail.draft", "mail.reply"], []),
        (["https://mail.google.com/"], ["mail.search", "mail.send", "mail.draft", "mail.reply"], []),
        (["calendar.events"], ["calendar.list", "calendar.create_event", "calendar.update_event"], []),
        (["calendar.readonly"], ["calendar.list", "calendar.find"],
         ["calendar.create_event", "calendar.update_event"]),
        (["calendar"], ["calendar.create_event", "calendar.list"], []),
        (["drive.readonly"], ["drive.search", "drive.read", "docs.read", "sheets.read", "sheets.find"], []),
        (["drive.file"], [], ["drive.search", "drive.read", "docs.read", "sheets.read"]),
        (["drive.appdata"], [], ["drive.search", "drive.read"]),
        (["contacts.readonly"], ["contacts.search", "contacts.list"], ["mail.search", "drive.search"]),
        (["openid", "email", "profile"], [], ["mail.search", "calendar.list", "drive.search",
                                              "contacts.list"]),
    ],
)
def test_scope_matrix(scopes, allowed, denied):
    grant_ = g(*scopes)
    for action in allowed:
        assert allows(grant_, action), (scopes, action)
    for action in denied:
        assert not allows(grant_, action), (scopes, action)


def test_an_action_without_an_entry_is_never_allowed():
    assert not allows(g("gmail.modify", "calendar", "drive"), "mail.something_new")


@pytest.fixture
def parts(tokens, oauth, client):
    fallback = FakeProvider()
    ex = FakeExecutor(G, {"mail.search", "mail.send", "mail.draft", "mail.reply", "calendar.create_event"})
    return fallback, ex, NativeRouter(fallback, tokens, oauth, [ex], client)


READ_ONLY = [A + "gmail.readonly", "openid"]


@pytest.mark.parametrize("action", ["mail.send", "mail.draft", "mail.reply"])
async def test_a_read_only_grant_never_sends_natively_and_says_what_is_missing(parts, tokens, action):
    fallback, ex, router = parts
    await grant(tokens, G, READ_ONLY)
    res = await router.execute(U, action, {})
    assert not res.ok and res.error_kind is FailureKind.PERMISSION_MISSING
    assert ex.calls == [] and fallback.executed == []
    text = failure_text(res.error_kind, "Gmail")
    assert "permission" in text and "Reconnect" in text and "—" not in text and "–" not in text


async def test_a_read_only_grant_still_reads_natively(parts, tokens):
    fallback, ex, router = parts
    await grant(tokens, G, READ_ONLY)
    assert (await router.execute(U, "mail.search", {})).ok
    assert [c[1] for c in ex.calls] == ["mail.search"] and fallback.executed == []


async def test_a_missing_permission_falls_back_to_an_active_composio_account_only(parts, tokens):
    fallback, ex, router = parts
    await grant(tokens, G, READ_ONLY)
    fallback.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    await router.execute(U, "mail.send", {})
    assert [e[1] for e in fallback.executed] == ["mail.send"] and ex.calls == []
    fallback.set_state(1, Capability.GMAIL, ConnectionState.INITIATED)
    res = await router.execute(U, "mail.draft", {})
    assert res.error_kind is FailureKind.PERMISSION_MISSING and len(fallback.executed) == 1


async def test_a_grant_with_the_permission_runs_natively(parts, tokens):
    fallback, ex, router = parts
    await grant(tokens, G, [A + "gmail.readonly", A + "gmail.send", A + "calendar.events"])
    await router.execute(U, "mail.send", {})
    await router.execute(U, "mail.reply", {})
    await router.execute(U, "calendar.create_event", {})
    assert len(ex.calls) == 3 and fallback.executed == []
    res = await router.execute(U, "mail.draft", {})
    assert res.error_kind is FailureKind.PERMISSION_MISSING


async def test_status_follows_what_the_grant_can_do(parts, tokens):
    _, _, router = parts
    await grant(tokens, G, [A + "gmail.send"])
    assert (await router.status(U))["gmail"] is ConnectionState.ACTIVE
    assert (await router.status(U))["googlecalendar"] is not ConnectionState.ACTIVE

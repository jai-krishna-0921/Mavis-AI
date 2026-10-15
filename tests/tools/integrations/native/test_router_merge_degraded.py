# ruff: noqa: E501, F811
"""A Microsoft account that stopped working never silently turns into Google (review of task 6)."""
import pytest

from mavis.domain.errors import ActionFailed, ConnectionRequired
from mavis.domain.errors import FailureKind as K
from mavis.domain.integrations import ToolResult
from mavis.domain.policy import Capability as C
from mavis.store.repo import users
from mavis.tools.integrations.actions import MailComposeArgs, MailReadArgs
from mavis.tools.integrations.connections import ConnectionCache
from mavis.tools.integrations.tools import gated
from mavis.tools.integrations.workspace_guard import my_email
from mavis.tools.integrations.workspace_tools import prepare_sender
from mavis.tools.registry import ToolContext

from .test_router import G, grant
from .test_router_merge import ALL_GOOGLE, U, both, gmail, ms_grant  # noqa: F401  (the `both` fixture)

CTX = ToolContext(user_id=1)
SEND = {"to": ["a@b.com"], "subject": "s", "body": "b"}


async def test_revoked_outlook_reads_keep_google_and_say_so(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens, status="REVOKED")
    r = await router.execute(U, "mail.search", {"query": "x"})
    assert r.ok and [x["messageId"] for x in r.data["messages"]] == ["g1", "g2"]
    assert any("me@corp.test" in n and "needs reconnecting" in n for n in r.data["notes"])
    assert not m.calls


async def test_default_microsoft_but_revoked_never_sends_from_gmail(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens, status="REVOKED")
    await users.update_state(1, {"defaults": {"mail": "microsoft"}})
    with pytest.raises(ConnectionRequired) as e:
        await router.execute(U, "mail.send", SEND)
    assert e.value.capability is C.OUTLOOK and e.value.revoked and not g.calls


async def test_named_outlook_address_but_revoked_never_sends_from_gmail(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens, status="REVOKED")
    with pytest.raises(ConnectionRequired) as e:
        await router.execute(U, "mail.send", {**SEND, "account": "me@corp.test"})
    assert e.value.capability is C.OUTLOOK and not g.calls
    with pytest.raises(ConnectionRequired):
        await prepare_sender_via(router, MailComposeArgs(to=["a@b.com"], subject="s", body="b", account="me@corp.test"))


async def prepare_sender_via(router, args):
    from mavis.tools import integrations

    def get_provider():
        return router

    get_provider.cache_clear = lambda: None
    old = integrations.get_provider
    integrations.get_provider = get_provider
    try:
        return await prepare_sender(CTX, args)
    finally:
        integrations.get_provider = old


async def test_ms_id_failure_with_google_active_prompts_outlook(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens, status="REVOKED")
    with pytest.raises(ConnectionRequired) as e:
        await gated(CTX, "mail.read", MailReadArgs(message_id="ms:AAA="), provider=router, cache=ConnectionCache(router))
    assert e.value.capability is C.OUTLOOK and e.value.revoked


async def test_microsoft_permission_failure_names_outlook_not_google(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    m.reply = ToolResult(ok=False, error_kind=K.PERMISSION_MISSING, error="scope")
    with pytest.raises(ActionFailed) as e:
        await gated(CTX, "mail.send", MailComposeArgs(to=["a@b.com"], subject="s", body="b", account="me@corp.test"),
                    provider=router, cache=ConnectionCache(router))
    assert e.value.capability is C.OUTLOOK and "Outlook" in e.value.reason and "Gmail" not in e.value.reason


async def test_calendar_id_goes_only_to_the_provider_that_owns_it(both, tokens):
    fb, g, m, router = both
    g.reply = m.reply = {"items": []}
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    a = await router.execute(U, "calendar.list", {"calendar_id": "ms:CAL=", "max_results": 5})
    assert [c[0] for c in m.calls] == ["calendar.list"] and not g.calls and "notes" not in a.data
    b = await router.execute(U, "calendar.list", {"calendar_id": "team@group.calendar.google.com", "max_results": 5})
    assert len(g.calls) == 1 and len(m.calls) == 1 and "notes" not in b.data
    await router.execute(U, "calendar.list", {"calendar_id": "primary", "max_results": 5})
    assert len(g.calls) == 2 and len(m.calls) == 2


async def test_my_email_stays_on_googles_own_profile(both, tokens):
    fb, g, m, router = both
    g.reply = {"emailAddress": "u1@x.com"}
    m.reply = {"emailAddress": "me@corp.test"}
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    await users.update_state(1, {"defaults": {"mail": "microsoft"}})
    assert await my_email(CTX, provider=router, cache=ConnectionCache(router)) == "u1@x.com"
    assert not m.calls

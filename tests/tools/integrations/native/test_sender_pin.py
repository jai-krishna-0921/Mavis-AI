# ruff: noqa: E501, F811
"""The card's From: is the account the message goes out from, whatever changes after the card."""
import pytest

from mavis.domain.errors import ConnectionRequired, FailureKind
from mavis.domain.integrations import ToolResult
from mavis.domain.policy import Capability as C
from mavis.store.repo import users
from mavis.tools.integrations.actions import ACTIONS, MailComposeArgs
from mavis.tools.registry import ToolContext

from .test_router import G, grant
from .test_router_merge import ALL_GOOGLE, U, both, ms_grant  # noqa: F401
from .test_router_merge_degraded import prepare_sender_via

CTX = ToolContext(user_id=1)


def mail(**kw):
    return MailComposeArgs(to=["a@b.com"], subject="s", body="b", **kw)


async def card(router, **kw):
    args = mail(**kw)
    out = await prepare_sender_via(router, args)
    return args, out


async def test_card_pins_the_resolved_account_and_shows_it(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    args, out = await card(router, account="me@corp.test")
    assert out.note == "From: me@corp.test" and args.sender_pin == "microsoft:me@corp.test"
    assert args.model_dump(mode="json")["sender_pin"] == "microsoft:me@corp.test"


async def test_default_changed_after_the_card_still_sends_from_the_cards_account(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    await users.update_state(1, {"defaults": {"mail": "microsoft"}})
    args, _ = await card(router)
    await users.update_state(1, {"defaults": {"mail": "google"}})
    r = await router.execute(U, "mail.send", args.model_dump(mode="json"))
    assert r.ok and len(m.calls) == 1 and not g.calls
    assert "sender_pin" not in m.calls[0][1] and "account" not in m.calls[0][1]


async def test_cards_account_disconnected_is_refused_not_sent_from_the_other(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    args, _ = await card(router, account="me@corp.test")
    await tokens.mark(1, m.provider, "REVOKED")  # Microsoft goes away, Google stays
    with pytest.raises(ConnectionRequired) as e:
        await router.execute(U, "mail.send", args.model_dump(mode="json"))
    assert e.value.capability is C.OUTLOOK and not g.calls and not m.calls


async def test_pinned_google_gone_while_microsoft_remains_is_refused(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    args, _ = await card(router, account="google")
    await tokens.mark(1, G, "REVOKED")
    with pytest.raises(ConnectionRequired) as e:
        await router.execute(U, "mail.send", args.model_dump(mode="json"))
    assert e.value.capability is C.GMAIL and not m.calls and not g.calls


async def test_a_pin_the_model_supplied_is_ignored(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    args, _ = await card(router, account="google", sender_pin="microsoft:me@corp.test")
    assert args.sender_pin == "google:u1@x.com"
    only = MailComposeArgs(to=["a@b.com"], subject="s", body="b", sender_pin="microsoft:me@corp.test")
    assert "sender_pin" in only.model_dump()  # set by hand ...
    out = await prepare_sender_via(router, only)  # ... but the pre-step clears it (and asks which)
    assert only.sender_pin is None and out.refusal


async def test_google_only_payload_and_card_are_unchanged(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    args, out = await card(router)
    assert out.note is None and args.sender_pin is None
    assert "sender_pin" not in args.model_dump(mode="json")
    assert "sender_pin" not in MailComposeArgs.model_json_schema()["properties"]


def test_different_senders_are_different_actions():
    from mavis.store.repo.approvals import equivalence_key

    ident = ACTIONS["mail.send"].identity
    base = {"to": ["a@b.com"], "cc": ["c@d.com"], "subject": "s", "body": "b"}
    assert equivalence_key({**base, "sender_pin": "google:u1@x.com"}, ident) != equivalence_key(
        {**base, "sender_pin": "microsoft:me@corp.test"}, ident)
    assert equivalence_key(base, ident) == equivalence_key({**base, "account": "work"}, ident)


async def test_pinned_send_with_an_explicit_account_remembers_it_as_the_default(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    args, _ = await card(router, account="me@corp.test")  # the real path: pre-step pins, approval runs it
    assert (await router.execute(U, "mail.send", args.model_dump(mode="json"))).ok
    assert (await users.get_state(1))["defaults"]["mail"] == "microsoft"
    again, out = await card(router)  # no account this time: no question, same account
    assert out.refusal is None and again.sender_pin == "microsoft:me@corp.test"


async def test_pinned_send_without_an_account_argument_does_not_set_a_default(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    await users.update_state(1, {"defaults": {"mail": "microsoft"}})
    args, _ = await card(router)
    await router.execute(U, "mail.send", args.model_dump(mode="json"))
    await users.update_state(1, {"defaults": {"mail": "google"}})
    args, _ = await card(router)
    await router.execute(U, "mail.send", args.model_dump(mode="json"))
    assert (await users.get_state(1))["defaults"]["mail"] == "google"


async def test_a_failed_pinned_send_does_not_remember_the_account(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    args, _ = await card(router, account="me@corp.test")
    m.reply = ToolResult(ok=False, error_kind=FailureKind.UNKNOWN, error="boom")
    await router.execute(U, "mail.send", args.model_dump(mode="json"))
    assert "mail" not in ((await users.get_state(1)).get("defaults") or {})

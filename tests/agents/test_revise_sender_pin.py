"""Editing a card keeps the account it showed (the pin), and cannot switch it."""
from datetime import timedelta

import pytest

from mavis.agents.orchestrator_graph import SenderChange, revise_approval
from mavis.store.db import utcnow
from mavis.store.repo import approvals
from mavis.tools.integrations.tools import register_integration_tools

ORIGINAL = {"to": ["a@b.com"], "cc": [], "subject": "Hi", "body": "old", "account": "",
            "sender_pin": "microsoft:me@corp.test"}


async def card(user, registry, arguments, preview):
    register_integration_tools(registry)
    aid = await approvals.create(user.id, None, "mail_send", arguments, preview,
                                 utcnow() + timedelta(hours=48))
    return registry.get("mail_send"), aid


async def test_edited_card_keeps_the_original_account_and_from_line(
        user, fake_llm, rec_bus, sent, fresh_registry):
    tool, aid = await card(user, fresh_registry, ORIGINAL, "Send mail\nFrom: me@corp.test")
    fake_llm.push_structured(tool.args_model.model_validate(
        {"to": ["a@b.com"], "subject": "Hi", "body": "new", "sender_pin": "google:evil@x.com"}))
    await revise_approval(await approvals.get(aid), "change the body")
    row = await approvals.get(aid)
    assert row.arguments["sender_pin"] == "microsoft:me@corp.test" and row.arguments["body"] == "new"
    assert row.preview.endswith("\nFrom: me@corp.test") and "evil" not in row.preview


async def test_edit_that_switches_account_is_refused(user, fake_llm, rec_bus, sent, fresh_registry):
    tool, aid = await card(user, fresh_registry, ORIGINAL, "Send mail\nFrom: me@corp.test")
    fake_llm.push_structured(tool.args_model.model_validate(
        {"to": ["a@b.com"], "subject": "Hi", "body": "old", "account": "google"}))
    with pytest.raises(SenderChange):
        await revise_approval(await approvals.get(aid), "send it from my gmail")
    row = await approvals.get(aid)
    assert row.arguments == ORIGINAL and row.preview == "Send mail\nFrom: me@corp.test"


async def test_google_only_edit_is_unchanged(user, fake_llm, rec_bus, sent, fresh_registry):
    plain = {"to": ["a@b.com"], "cc": [], "subject": "Hi", "body": "old", "account": ""}
    tool, aid = await card(user, fresh_registry, plain, "Send mail")
    fake_llm.push_structured(tool.args_model.model_validate(
        {"to": ["a@b.com"], "subject": "Hi", "body": "new", "sender_pin": "microsoft:me@corp.test"}))
    await revise_approval(await approvals.get(aid), "change the body")
    row = await approvals.get(aid)
    assert "sender_pin" not in row.arguments and "From:" not in row.preview

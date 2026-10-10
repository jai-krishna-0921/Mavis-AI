"""scripts/live_native.py: prints counts and masked accounts only, and sends to the user alone."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from scripts import live_native

from mavis.domain.errors import FailureKind
from mavis.domain.integrations import ToolResult
from mavis.tools.integrations.native.base import NativeProvider


class Provider:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    async def execute(self, user, action, args):
        self.calls.append((action, args))
        if action in self.fail:
            return ToolResult(ok=False, error="boom secret-token", error_kind=FailureKind.AUTH)
        data = {
            "mail.profile": {"emailAddress": "jai@orbit.test", "messagesTotal": 120},
            "mail.search": {"messages": [{"messageId": "a"}, {"messageId": "b"}]},
            "calendar.list": {"items": [{"id": "e1"}]},
            "slack.channels": {"channels": [{"id": "D0SELF0001", "kind": "dm", "user": "U1"},
                                            {"id": "D0OTHER001", "kind": "dm", "user": "U2"},
                                            {"id": "C0000GEN01", "kind": "public"}]},
            "mail.send": {"id": "m"}, "slack.send": {"ok": True},
        }[action]
        return ToolResult(ok=True, data=data)


async def test_read_checks_print_counts_and_never_the_address_or_error_text():
    lines = []
    failures, email = await live_native.read_checks(Provider(), 1, lines.append)
    text = "\n".join(lines)
    assert failures == 0 and email == "jai@orbit.test"
    assert "2 messages" in text and "1 events" in text and "3 conversations (2 direct)" in text
    assert "jai@orbit.test" not in text and "j***@orbit.test" in text


async def test_a_failed_read_counts_and_hides_the_vendor_text():
    lines = []
    failures, _ = await live_native.read_checks(Provider(fail={"mail.search"}), 1, lines.append)
    assert failures == 1 and "secret-token" not in "\n".join(lines)


async def test_send_test_goes_only_to_the_own_address_and_the_own_dm():
    p, lines = Provider(), []
    assert await live_native.send_tests(p, 1, "jai@orbit.test", "U1", lines.append) == 0
    sends = {a: args for a, args in p.calls if a.endswith(".send")}
    assert sends["mail.send"]["to"] == ["jai@orbit.test"] and "cc" not in sends["mail.send"]
    assert sends["slack.send"]["channel"] == "D0SELF0001"


async def test_send_test_skips_rather_than_guess_a_recipient():
    p, lines = Provider(), []
    assert await live_native.send_tests(p, 1, "", "U9", lines.append) == 2
    assert not [a for a, _ in p.calls if a.endswith(".send")]


async def test_grants_are_described_without_tokens():
    g = SimpleNamespace(provider=NativeProvider.GOOGLE, status="ACTIVE", account={"email": "jai@orbit.test"},
                        scopes=frozenset({"a", "b"}), expires_at=datetime(2026, 10, 9, 12, 0, tzinfo=UTC))

    async def grants(_uid):
        return [g]

    lines = []
    found = await live_native.grant_report(SimpleNamespace(grants=grants), 1, lines.append)
    assert list(found) == [NativeProvider.GOOGLE]
    assert lines[0].startswith("grant google: ACTIVE account=j***@orbit.test scopes=2")
    assert lines[1] == "grant slack: none"

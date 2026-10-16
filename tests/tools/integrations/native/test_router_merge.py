# ruff: noqa: E501, F811
# tests/tools/integrations/native/test_router_merge.py
import json

import pytest

from mavis.domain.errors import FailureKind as K
from mavis.domain.integrations import ToolResult, UserRef
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.microsoft_spec import MS_SCOPES, PERSONAL_TENANT
from mavis.tools.integrations.native.router import NativeRouter
from tests.tools.integrations.fakes import FakeProvider

from .test_router import ALL_GOOGLE as READ_ONLY_GOOGLE
from .test_router import G, grant

# The router refuses an action the grant's scopes do not cover, so writes need the modify scope.
ALL_GOOGLE = [*READ_ONLY_GOOGLE, "https://www.googleapis.com/auth/gmail.modify"]

M = NativeProvider.MICROSOFT
U = UserRef(user_id=1)
MAIL = {"mail.search", "mail.read", "mail.thread", "mail.send", "mail.draft", "mail.reply", "mail.archive",
        "mail.profile", "calendar.list", "calendar.find", "calendar.free_slots", "calendar.calendars",
        "calendar.get", "calendar.create_event", "calendar.update_event", "calendar.delete_event",
        "calendar.respond"}


class Exec:
    def __init__(self, provider, reply):
        self.provider, self.reply, self.calls = provider, reply, []

    def handles(self, action):
        return action in MAIL

    async def execute(self, user, action, args):
        self.calls.append((action, dict(args)))
        out = self.reply(action, args) if callable(self.reply) else self.reply
        return out if isinstance(out, ToolResult) else ToolResult(ok=True, data=out)


def gmail(mid, ts_ms, subject="g"):
    return {"messageId": mid, "threadId": "t" + mid, "sender": "A <a@x.com>", "subject": subject,
            "labelIds": ["INBOX"], "messageTimestamp": str(ts_ms), "messageText": "x"}


def outlook(mid, iso, subject="o"):
    return {"messageId": "ms:" + mid, "threadId": "ms:t" + mid, "sender": "B <b@corp.test>", "subject": subject,
            "labelIds": ["INBOX"], "messageTimestamp": iso, "messageText": "y"}


async def ms_grant(tokens, user=1, tid="11111111-2222-3333-4444-555555555555", mail="me@corp.test", status="ACTIVE"):
    await tokens.save(user, M, account={"tid": tid, "oid": f"o{user}", "mail": mail, "scopes": list(MS_SCOPES)},
                      access_token="a", refresh_token="r", expires_at=None)
    if status != "ACTIVE":
        await tokens.mark(user, M, status)


@pytest.fixture
def both(tokens, oauth, client):
    fb = FakeProvider()
    g = Exec(G, lambda a, args: {"messages": [gmail("g1", 1_790_000_000_000), gmail("g2", 1_780_000_000_000)]}
             if a == "mail.search" else {"ok": True})
    m = Exec(M, lambda a, args: {"messages": [outlook("m1", "2026-10-01T00:00:00Z"), outlook("m2", "2030-01-01T00:00:00Z")]}
             if a == "mail.search" else {"ok": True})
    return fb, g, m, NativeRouter(fb, tokens, oauth, [g, m], client)


async def test_google_only_user_is_untouched(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    r = await router.execute(U, "mail.search", {"query": "x"})
    assert [c[0] for c in g.calls] == ["mail.search"] and not m.calls
    assert "account" not in r.data["messages"][0] and "notes" not in r.data


async def test_reads_merge_newest_first_and_tag_accounts(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    r = await router.execute(U, "mail.search", {"query": "x", "max_results": 3})
    ids = [x["messageId"] for x in r.data["messages"]]
    assert ids == ["ms:m2", "ms:m1", "g1"]      # newest first (2030, 2026-10-01, 2026-09), cut to 3
    tags = {x["account"] for x in r.data["messages"]}
    assert tags == {"work: me@corp.test", "personal: u1@x.com"}


async def test_one_account_down_still_answers_and_says_which(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    m.reply = ToolResult(ok=False, error_kind=K.UNAVAILABLE, error="microsoft 503 secret-corp-detail")
    r = await router.execute(U, "mail.search", {"query": "x"})
    assert r.ok and len(r.data["messages"]) == 2
    assert any("work: me@corp.test" in n and "temporarily unavailable" in n for n in r.data["notes"])
    assert "secret-corp-detail" not in json.dumps(r.data)


async def test_all_sides_down_returns_the_first_failure(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    bad = ToolResult(ok=False, error_kind=K.AUTH, error="x")
    g.reply = m.reply = bad
    r = await router.execute(U, "mail.search", {"query": "x"})
    assert not r.ok and r.error_kind is K.AUTH


async def test_ms_ids_route_to_microsoft_and_others_to_google(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    await router.execute(U, "mail.read", {"message_id": "ms:AAA="})
    await router.execute(U, "mail.read", {"message_id": "18c0ffee"})
    assert [c[1]["message_id"] for c in m.calls] == ["ms:AAA="]
    assert [c[1]["message_id"] for c in g.calls] == ["18c0ffee"]


async def test_mixed_id_lists_are_split(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    await router.execute(U, "mail.archive", {"message_ids": ["ms:a", "g1", "ms:b"]})
    assert m.calls[0][1]["message_ids"] == ["ms:a", "ms:b"] and g.calls[0][1]["message_ids"] == ["g1"]


async def test_ms_id_with_revoked_microsoft_never_reaches_google(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens, status="REVOKED")
    r = await router.execute(U, "mail.reply", {"thread_id": "ms:T1", "to": "a@b.com", "body": "hi"})
    assert r.error_kind is K.AUTH and not g.calls and not m.calls and fb.executed == []


async def test_new_mail_with_two_accounts_and_no_default_asks_once_then_remembers(both, tokens):
    fb, g, m, router = both
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    r = await router.execute(U, "mail.send", {"to": ["a@b.com"], "subject": "s", "body": "b"})
    assert r.error_kind is K.INVALID_ARGUMENT and r.error_field == "account"
    assert "work: me@corp.test" in r.error and "personal: u1@x.com" in r.error and not g.calls and not m.calls
    ok = await router.execute(U, "mail.send", {"to": ["a@b.com"], "subject": "s", "body": "b", "account": "me@corp.test"})
    assert ok.ok and len(m.calls) == 1 and "account" not in m.calls[0][1]
    again = await router.execute(U, "mail.send", {"to": ["a@b.com"], "subject": "s2", "body": "b"})
    assert again.ok and len(m.calls) == 2      # remembered default: no second question
    other = await router.execute(U, "mail.send", {"to": ["a@b.com"], "subject": "s3", "body": "b", "account": "google"})
    assert other.ok and len(g.calls) == 1 and len(m.calls) == 2   # an explicit choice still wins


async def test_one_account_needs_no_question(tokens, oauth, client):
    m = Exec(M, {"ok": True})
    router = NativeRouter(FakeProvider(), tokens, oauth, [m], client)
    await ms_grant(tokens)
    assert (await router.execute(U, "mail.send", {"to": ["a@b.com"], "subject": "s", "body": "b"})).ok


async def test_personal_microsoft_account_is_tagged_personal(both, tokens):
    fb, g, m, router = both
    await ms_grant(tokens, tid=PERSONAL_TENANT, mail="me@outlook.com")
    accounts = await router.merge.accounts(1, "mail")
    assert [a.tag for a in accounts] == ["personal: me@outlook.com"]


async def test_free_slots_are_intersected(both, tokens):
    fb, g, m, router = both
    g.reply = {"busy": [], "free_slots": [{"start": "2026-10-15T09:00:00+00:00", "end": "2026-10-15T12:00:00+00:00", "minutes": 180}]}
    m.reply = {"busy": [], "free_slots": [{"start": "2026-10-15T10:00:00+00:00", "end": "2026-10-15T14:00:00+00:00", "minutes": 240}]}
    await grant(tokens, G, ALL_GOOGLE)
    await ms_grant(tokens)
    r = await router.execute(U, "calendar.free_slots", {"time_min": "2026-10-15T08:00:00Z", "time_max": "2026-10-15T18:00:00Z"})
    assert r.data["free_slots"] == [{"start": "2026-10-15T10:00:00+00:00", "end": "2026-10-15T12:00:00+00:00", "minutes": 120}]


async def test_provider_grant_never_returns_another_users_grant(both, tokens):
    fb, g, m, router = both
    await ms_grant(tokens, user=2, mail="other@corp.test")
    assert await router.provider_grant(1, M) is None             # user 1 has none, user 2's is not theirs
    mine = await router.provider_grant(2, M)
    assert mine is not None and mine.user_id == 2 and mine.account["mail"] == "other@corp.test"
    await ms_grant(tokens, user=1)
    assert (await router.provider_grant(1, M)).account["mail"] == "me@corp.test"
    assert (await router.provider_grant(2, M)).account["mail"] == "other@corp.test"
    await tokens.mark(2, M, "REVOKED")
    assert await router.provider_grant(2, M) is None and await router.provider_grant(1, M) is not None

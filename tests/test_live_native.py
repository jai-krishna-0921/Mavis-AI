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


class MsProvider:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    async def execute(self, user, action, args):
        raise AssertionError("the Microsoft section must never use the merging execute")

    async def execute_native(self, provider, user, action, args):
        assert provider is NativeProvider.MICROSOFT
        self.calls.append((action, args))
        if action in self.fail:
            return ToolResult(ok=False, error="boom secret-token", error_kind=FailureKind.UNAVAILABLE)
        data = {
            "mail.profile": {"emailAddress": "jai@corp.test"},
            "mail.search": {"messages": [{"messageId": "ms:a"}, {"messageId": "ms:b"}]},
            "mail.inbox_delta": {"messages": [], "cursor": "x", "baseline": True},
            "mail.send": {"id": "ms:sent"},
            "calendar.list": {"items": [{"id": "ms:e1"}]},
            "calendar.delta": {"items": [], "cursor": "y", "baseline": True},
            "teams.chats": {"chats": [{"id": "ms:c1", "type": "oneOnOne"}, {"id": "ms:c2", "type": "group"}]},
            "onedrive.list_recent": {"files": [{"id": "ms:f1", "name": "secret-name.txt",
                                                "mimeType": "text/plain"}]},
            "onedrive.read": {"text": "secret body"},
        }[action]
        return ToolResult(ok=True, data=data)


async def test_microsoft_checks_print_counts_only():
    lines = []
    failures, email = await live_native.microsoft_checks(MsProvider(), 1, lines.append)
    text = "\n".join(lines)
    assert failures == 0 and email == "jai@corp.test"
    assert "2 messages" in text and "1 events" in text and "teams chats: 2 (1 direct)" in text
    assert "inbox delta baseline: ok" in text and "calendar delta baseline: ok" in text
    assert "phrase search: ok" in text and "onedrive read first text file: ok" in text
    for secret in ("jai@corp.test", "secret-name", "secret body", "ms:"):
        assert secret not in text
    assert "j***@corp.test" in text
    assert text.count("live-check ") == 7 and "7 sharedWithMe: PASS" in text


async def test_personal_account_without_teams_is_not_a_failure():
    class Personal(MsProvider):
        async def execute_native(self, provider, user, action, args):
            if action == "teams.chats":
                return ToolResult(ok=False, error="x", error_kind=FailureKind.PERMISSION_MISSING)
            return await super().execute_native(provider, user, action, args)
    lines = []
    failures, _ = await live_native.microsoft_checks(Personal(), 1, lines.append)
    assert failures == 0 and any("teams: not available" in line for line in lines)


async def test_a_failed_microsoft_read_counts_and_hides_the_vendor_text():
    lines = []
    failures, _ = await live_native.microsoft_checks(MsProvider(fail={"mail.search"}), 1, lines.append)
    assert failures >= 1 and "secret-token" not in "\n".join(lines)


async def test_shared_with_me_unavailable_is_informational():
    lines = []
    p = MsProvider()
    real = p.execute_native

    async def shared_gone(provider, user, action, args):
        if action == "onedrive.list_recent" and args.get("shared_with_me"):
            return ToolResult(ok=False, error="x", error_kind=FailureKind.UNAVAILABLE)
        return await real(provider, user, action, args)
    p.execute_native = shared_gone
    failures, _ = await live_native.microsoft_checks(p, 1, lines.append)
    assert failures == 0 and any("sharedWithMe: unavailable" in line for line in lines)
    assert any("7 sharedWithMe: FAIL" in line for line in lines)


def test_describe_microsoft_accounts_masks_and_names_the_kind():
    from mavis.tools.integrations.native.microsoft_spec import PERSONAL_TENANT
    work = live_native.describe_account(NativeProvider.MICROSOFT, {"mail": "jai@corp.test", "tid": "t1"})
    personal = live_native.describe_account(
        NativeProvider.MICROSOFT, {"mail": "jai@outlook.com", "tid": PERSONAL_TENANT})
    assert work == "j***@corp.test (work tenant)" and personal == "j***@outlook.com (personal account)"


async def verdict_of(check, *routes):
    return (await check(probe_of(list(routes))))[0]


def probe_of(routes):
    """routes: list of (path substring, predicate on params or None, (data, kind)); first match wins."""
    seen = []

    async def probe(path, params=None, headers=None):
        seen.append((path, params or {}, headers or {}))
        for sub, test, answer in routes:
            if sub in path and (test is None or test(params or {})):
                return answer
        return None, "error"
    probe.seen = seen
    return probe


async def test_item1_search_phrase_pass_fail_unknown():
    assert (await live_native.check_search_phrase(probe_of([("/me/messages", None, ({}, ""))])))[0] == "PASS"
    p = probe_of([("/me/messages", None, (None, "invalid_argument"))])
    assert (await live_native.check_search_phrase(p))[0] == "FAIL"
    assert '\\"' in p.seen[0][1]["$search"]
    assert (await live_native.check_search_phrase(probe_of([("/me", None, (None, "rate_limited"))])))[0] \
        == "UNKNOWN"


async def test_item2_reply_lookup_uses_a_real_conversation_id():
    msgs = ({"value": [{"conversationId": "AAQ=c'd"}]}, "")
    ok = probe_of([("/me/messages", lambda q: "$filter" in q, ({}, "")), ("/me/messages", None, msgs)])
    assert (await live_native.check_reply_lookup(ok))[0] == "PASS"
    flt = next(q for _, q, _ in ok.seen if "$filter" in q)
    assert "conversationId eq 'AAQ=c''d'" in flt["$filter"] and flt["$orderby"] == "receivedDateTime desc"
    bad = probe_of([("/me/messages", lambda q: "$filter" in q, (None, "invalid_argument")),
                    ("/me/messages", None, msgs)])
    assert (await live_native.check_reply_lookup(bad))[0] == "FAIL"
    empty = probe_of([("/me/messages", None, ({"value": []}, ""))])
    assert (await live_native.check_reply_lookup(empty))[0] == "UNKNOWN"


def test_item3_header_judgement():
    good = [{"name": "Authentication-Results", "value": "spf=pass smtp.mailfrom=x"},
            {"name": "X-MS-Exchange-Organization-AuthAs", "value": "Anonymous"}]
    assert live_native.judge_auth_headers(good)[0] == "PASS"
    server = [{"name": "Authentication-Results", "value": "mx.eur.protection.outlook.com; dkim=pass"},
              {"name": "x-ms-exchange-organization-authas", "value": "Internal"}]
    assert live_native.judge_auth_headers(server)[0] == "PASS"
    foreign = [{"name": "Authentication-Results", "value": "mx.google.com; spf=pass"},
               {"name": "X-MS-Exchange-Organization-AuthAs", "value": "Anonymous"}]
    assert live_native.judge_auth_headers(foreign)[0] == "FAIL"
    assert live_native.judge_auth_headers(good[:1])[0] == "FAIL"
    assert live_native.judge_auth_headers(good[1:])[0] == "FAIL"
    assert live_native.judge_auth_headers([])[0] == "UNKNOWN"
    assert live_native.judge_auth_headers(None)[0] == "UNKNOWN"


async def test_item3_fetches_the_headers_of_one_recent_message():
    p = probe_of([("/internetMessageHeaders", None, (None, "error")),
                  ("/me/messages/AAMk-1_x=", None, ({"internetMessageHeaders": [
                      {"name": "Authentication-Results", "value": "spf=pass"},
                      {"name": "X-MS-Exchange-Organization-AuthAs", "value": "Anonymous"}]}, "")),
                  ("/inbox/messages", None, ({"value": [{"id": "AAMk-1_x="}]}, ""))])
    assert (await live_native.check_auth_headers(p))[0] == "PASS"
    none = probe_of([("/inbox/messages", None, ({"value": []}, ""))])
    assert (await live_native.check_auth_headers(none))[0] == "UNKNOWN"


def test_item4_all_day_judgement():
    day = {"dateTime": "2026-10-20T00:00:00.0000000", "timeZone": "UTC"}
    nxt = {"dateTime": "2026-10-21T00:00:00.0000000", "timeZone": "UTC"}
    assert live_native.judge_all_day(day, nxt)[0] == "PASS"
    assert live_native.judge_all_day(day, {"dateTime": "2026-10-21T05:30:00", "timeZone": "UTC"})[0] == "FAIL"
    assert live_native.judge_all_day({**day, "timeZone": "Pacific Standard Time"}, nxt)[0] == "FAIL"


async def test_item4_asks_for_utc_and_reports_unknown_without_an_all_day_event():
    day = {"dateTime": "2026-10-20T00:00:00.0000000", "timeZone": "UTC"}
    p = probe_of([("/me/events", None, ({"value": [{"start": day, "end": day}]}, ""))])
    assert (await live_native.check_all_day(p))[0] == "PASS"
    assert p.seen[0][2]["Prefer"] == 'outlook.timezone="UTC"'
    assert (await live_native.check_all_day(probe_of([("/me/events", None, ({"value": []}, ""))])))[0] \
        == "UNKNOWN"


async def test_item5_delta_baseline_filter():
    ok = probe_of([("/delta", None, ({}, ""))])
    assert (await live_native.check_delta_baseline(ok))[0] == "PASS"
    assert "receivedDateTime ge" in ok.seen[0][1]["$filter"]
    bad = ("/delta", None, (None, "invalid_argument"))
    assert await verdict_of(live_native.check_delta_baseline, bad) == "FAIL"
    assert await verdict_of(live_native.check_delta_baseline, ("/delta", None, (None, "auth"))) == "UNKNOWN"


async def test_item6_probes_the_production_chat_requests_in_order():
    from mavis.tools.integrations.native.graph_teams import chat_list_steps

    full = probe_of([("/me/chats", None, ({}, ""))])
    verdict, hint = await live_native.check_chat_list(full)
    assert verdict == "PASS" and "production chat list request works" in hint
    assert full.seen[0][1] == chat_list_steps(5)[0]
    members_bad = ("/me/chats", lambda q: "members" in q["$expand"], (None, "invalid_argument"))
    rejects_members = probe_of([members_bad, ("/me/chats", None, ({}, ""))])
    verdict, hint = await live_native.check_chat_list(rejects_members)
    assert verdict == "PASS" and "fallback step 2 of 3" in hint
    assert [q for _, q, _ in rejects_members.seen] == list(chat_list_steps(5)[:2])
    rejects_order = probe_of([("/me/chats", lambda q: "$orderby" in q, (None, "invalid_argument")),
                              ("/me/chats", None, ({}, ""))])
    verdict, hint = await live_native.check_chat_list(rejects_order)
    assert verdict == "PASS" and "fallback step 3 of 3" in hint and "without $orderby" in hint
    assert [q for _, q, _ in rejects_order.seen] == list(chat_list_steps(5))
    bad = ("/me/chats", None, (None, "invalid_argument"))
    assert await verdict_of(live_native.check_chat_list, bad) == "FAIL"
    denied = ("/me/chats", None, (None, "permission_missing"))
    assert await verdict_of(live_native.check_chat_list, denied) == "UNKNOWN"


async def test_a_hung_graph_call_times_out_as_unknown(monkeypatch):
    import asyncio

    class Hung:
        async def json(self, *a, **k):
            await asyncio.sleep(60)

    monkeypatch.setattr(live_native, "PROBE_TIMEOUT_S", 0.01)
    provider = SimpleNamespace(executors={NativeProvider.MICROSOFT: SimpleNamespace(graph=Hung())})
    probe = live_native.graph_probe(provider, 1)
    assert await probe("/me/chats") == (None, "error")


def test_item7_shared_with_me_verdicts():
    assert live_native.judge_shared("")[0] == "PASS"
    assert live_native.judge_shared("not_found")[0] == "FAIL"
    assert live_native.judge_shared("rate_limited")[0] == "UNKNOWN"


async def test_live_check_items_count_only_real_failures_and_hide_probe_content():
    lines = []
    p = probe_of([("/me/chats", None, (None, "invalid_argument")), ("/", None, (None, "error"))])
    failures = await live_native.live_check_items(p, "not_found", lines.append)
    assert failures == 1   # chat list FAIL counts; UNKNOWNs and sharedWithMe do not
    assert len(lines) == 7 and all(" - " in line for line in lines)
    lines = []
    assert await live_native.live_check_items(None, "", lines.append) == 0
    assert sum("UNKNOWN" in line for line in lines) == 6


async def test_microsoft_send_test_is_one_self_addressed_mail():
    p, lines = MsProvider(), []
    assert await live_native.send_microsoft_test(p, 1, "jai@corp.test", lines.append) == 0
    assert [a for a, _ in p.calls] == ["mail.send"] and p.calls[0][1]["to"] == ["jai@corp.test"]
    assert "cc" not in p.calls[0][1] and "jai@corp.test" not in "\n".join(lines)
    assert await live_native.send_microsoft_test(MsProvider(), 1, "", lines.append) == 1


async def test_first_sync_runs_only_the_held_capabilities():
    from mavis.domain.policy import Capability
    from mavis.tools.integrations.native.microsoft_spec import PERSONAL_TENANT

    class Sync:
        def __init__(self):
            self.ran = []

        async def run(self, uid, cap):
            self.ran.append(cap)
            return []

    s = Sync()
    await live_native.run_first_sync(
        s, 1, lambda _l: None,
        {NativeProvider.MICROSOFT: SimpleNamespace(account={"tid": "t1"})})
    assert s.ran == [Capability.OUTLOOK, Capability.OUTLOOK_CALENDAR, Capability.TEAMS, Capability.ONEDRIVE]
    s = Sync()
    await live_native.run_first_sync(
        s, 1, lambda _l: None,
        {NativeProvider.MICROSOFT: SimpleNamespace(account={"tid": PERSONAL_TENANT})})
    assert Capability.TEAMS not in s.ran and len(s.ran) == 3

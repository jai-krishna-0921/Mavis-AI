# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Live check of the native Google and Slack connectors for one Mavis user. Run inside the api or worker
container (it needs the same environment, database and sealed token key as the running stack):

    sudo docker compose --env-file /run/mavis/mavis.env -f docker-compose.prod.yml exec api \
        uv run python -m scripts.live_native <mavis_user_id> [--sync] [--send-test]

Prints, never a secret: grant status per provider (masked account, scope count, expiry), mail.profile,
a count for mail.search newer_than:2d, calendar.list for the next 7 days, slack.channels, the poll and
first-sync state, and node and edge counts with tp: provenance in the graph. With --sync it also runs
the first sync for Gmail, Calendar and Slack the way a new connection does (queues real LEARN jobs).
With --send-test it sends ONE self-addressed email and ONE Slack message to the user's own DM; no other
recipient can be given. Exit code 1 when a required check fails.

Microsoft 365 (after registering the Entra app, see "Microsoft 365 owner setup" in the README): the same
run prints an Outlook, Teams and OneDrive section (counts and masked accounts only, read-only) and the
open live-check items, each as PASS, FAIL or UNKNOWN with a one-line hint:
  1 quoted phrase in $search, 2 reply lookup filter with $orderby, 3 Authentication-Results and
  X-MS-Exchange-Organization-AuthAs headers, 4 all-day events as midnight dates in UTC, 5 mail delta
  baseline with a receivedDateTime filter, 6 chat list with $expand=members and $orderby (or the fallback
  step used), 7 sharedWithMe. A FAIL counts as a failed check (except 7, informational); UNKNOWN does not.
With --send-test a Microsoft grant also sends ONE self-addressed mail (no Teams message).
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from mavis.domain import timeutil
from mavis.domain.integrations import UserRef
from mavis.domain.policy import Capability
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.normalize import extract_calendar_items, extract_list, extract_messages

Out = Callable[[str], None]


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def describe_account(provider: NativeProvider, account: dict[str, Any]) -> str:
    if provider is NativeProvider.GOOGLE:
        return mask_email(str(account.get("email") or ""))
    if provider is NativeProvider.MICROSOFT:
        from mavis.tools.integrations.native.microsoft_spec import PERSONAL_TENANT

        if account.get("tid") == PERSONAL_TENANT:
            return mask_email(str(account.get("mail") or "")) + " (personal account)" \
                if account.get("mail") else "(personal account)"
        return mask_email(str(account.get("mail") or "")) + " (work tenant)"
    return f"team {account.get('team_id') or '?'} user {account.get('user_id') or '?'}"


async def grant_report(tokens: Any, user_id: int, out: Out) -> dict[NativeProvider, Any]:
    found = {}
    grants = {g.provider: g for g in await tokens.grants(user_id)}
    for provider in NativeProvider:
        g = grants.get(provider)
        if g is None:
            out(f"grant {provider.value}: none")
            continue
        found[provider] = g
        exp = g.expires_at.isoformat(timespec="minutes") if g.expires_at else "no expiry"
        out(f"grant {provider.value}: {g.status} account={describe_account(provider, g.account)} "
            f"scopes={len(g.scopes)} expires={exp}")
    return found


async def call(provider: Any, user_id: int, action: str, args: dict, out: Out) -> Any | None:
    res = await provider.execute(UserRef(user_id=user_id), action, args)
    if not res.ok:
        kind = res.error_kind.value if res.error_kind else "error"
        out(f"{action}: FAILED ({kind})")
        return None
    return res.data


async def read_checks(provider: Any, user_id: int, out: Out) -> tuple[int, str]:
    """Returns (failures, own email)."""
    failures, email = 0, ""
    data = await call(provider, user_id, "mail.profile", {}, out)
    if isinstance(data, dict):
        email = str(data.get("emailAddress") or data.get("email") or "")
        out(f"mail.profile: ok account={mask_email(email)} messages_total={data.get('messagesTotal', '?')}")
    else:
        failures += 1
    data = await call(provider, user_id, "mail.search", {"query": "newer_than:2d", "max_results": 50}, out)
    if data is None:
        failures += 1
    else:
        out(f"mail.search newer_than:2d: {len(extract_messages(data))} messages")
    now = timeutil.now()
    data = await call(provider, user_id, "calendar.list", {
        "time_min": now.isoformat(), "time_max": (now + timedelta(days=7)).isoformat(),
        "max_results": 50}, out)
    if data is None:
        failures += 1
    else:
        out(f"calendar.list next 7 days: {len(extract_calendar_items(data))} events")
    data = await call(provider, user_id, "slack.channels", {}, out)
    if data is None:
        failures += 1
    else:
        channels = extract_list(data, "channels", "data.channels")
        dms = sum(1 for c in channels if c.get("kind") in ("dm", "group_dm"))
        out(f"slack.channels: {len(channels)} conversations ({dms} direct)")
    return failures, email


async def state_report(user_id: int, out: Out) -> None:
    from mavis.store.repo import users

    st = await users.get_state(user_id)
    out(f"state synced={sorted(st.get('synced') or {})} polling={st.get('polling') or {}}")
    out(f"state cursors={sorted(st.get('cursors') or {})}")
    ident = st.get("identities") or {}
    out(f"identities emails={len(ident.get('emails', []))} slack_ids={len(ident.get('slack_ids', []))} "
        f"team={'set' if ident.get('team') else 'missing'}")
    mute = st.get("mute") or {}
    out(f"mute senders={len(mute.get('senders', []))} domains={len(mute.get('domains', []))} "
        f"slack={len(mute.get('slack', []))}")


async def graph_report(memory: Any, user_id: int, out: Out) -> tuple[int, int]:
    """(nodes, edges) learned from third-party records (source_ref tp:...)."""
    from mavis.memory.graph import is_third_party

    dump = await memory.graph.dump(user_id)
    tp = [d for d in dump if is_third_party(d.get("source_ref"))]
    nodes = {n for d in tp for n in (d["subject"], d["object"])} - {"User"}
    kinds: dict[str, int] = {}
    for d in tp:
        kind = str(d["source_ref"]).split(":")[1] if ":" in str(d["source_ref"]) else "?"
        kinds[kind] = kinds.get(kind, 0) + 1
    out(f"graph: {len(dump)} current edges, {len(tp)} with tp: provenance {kinds}, "
        f"{len(nodes)} nodes touched by them, {len(await memory.graph.entities(user_id))} entities total")
    return len(nodes), len(tp)


GOOGLE_SLACK_SYNC = {NativeProvider.GOOGLE: (Capability.GMAIL, Capability.CALENDAR),
                     NativeProvider.SLACK: (Capability.SLACK,)}
MICROSOFT_SYNC = (Capability.OUTLOOK, Capability.OUTLOOK_CALENDAR, Capability.TEAMS, Capability.ONEDRIVE)


async def run_first_sync(sync: Any, user_id: int, out: Out, grants: Any = None) -> None:
    """Only the capabilities the user holds a grant for (all Google and Slack ones when `grants` is None)."""
    caps: list[Capability] = []
    for provider in (NativeProvider.GOOGLE, NativeProvider.SLACK):
        if grants is None or provider in grants:
            caps += GOOGLE_SLACK_SYNC[provider]
    if grants is not None and NativeProvider.MICROSOFT in grants:
        personal = _is_personal(grants[NativeProvider.MICROSOFT].account)
        caps += [c for c in MICROSOFT_SYNC if not (personal and c is Capability.TEAMS)]
    for capability in caps:
        noticed = await sync.run(user_id, capability)
        out(f"first sync {capability.value}: queued, {len(noticed)} notices")


def _is_personal(account: dict[str, Any]) -> bool:
    from mavis.tools.integrations.native.microsoft_spec import PERSONAL_TENANT

    return account.get("tid") == PERSONAL_TENANT


async def ncall(provider: Any, user_id: int, action: str, args: dict, out: Out, *, quiet: bool = False
                ) -> tuple[Any | None, str]:
    """One Microsoft-only call. Returns (data or None, failure kind or ""). Vendor text is never printed."""
    res = await provider.execute_native(NativeProvider.MICROSOFT, UserRef(user_id=user_id), action, args)
    if res.ok:
        return res.data, ""
    kind = res.error_kind.value if res.error_kind else "error"
    if not quiet:
        out(f"{action}: FAILED ({kind})")
    return None, kind


Probe = Callable[..., Awaitable[tuple[Any, str]]]


def graph_probe(provider: Any, user_id: int) -> Probe | None:
    """A read-only Graph GET for the live-check items: probe(path, params=None, headers=None) returns
    (parsed JSON or None, failure kind or ""). None when the provider has no Microsoft executor."""
    from mavis.tools.integrations.native.graph import GRAPH, GraphError

    executor = getattr(provider, "executors", {}).get(NativeProvider.MICROSOFT)
    graph = getattr(executor, "graph", None)
    if graph is None:
        return None

    async def probe(path: str, params: dict | None = None, headers: dict | None = None):
        try:
            return await graph.json(user_id, "GET", GRAPH + path, params=params, headers=headers), ""
        except GraphError as exc:
            return None, exc.kind.value
        except Exception:  # noqa: BLE001 - reauth, transport: unknown, never printed
            return None, "error"

    return probe


Verdict = tuple[str, str]  # (PASS | FAIL | UNKNOWN, one-line hint)
_BAD_REQUEST = ("invalid_argument",)


def _unknown(kind: str) -> Verdict:
    return "UNKNOWN", f"could not be tried ({kind}); run it again"


async def check_search_phrase(probe: Probe) -> Verdict:
    """1: a quoted phrase with an escaped quote inside is accepted by $search."""
    _, kind = await probe("/me/messages", {"$search": '"\\"hello world\\""', "$top": "1"})
    if not kind:
        return "PASS", "Graph accepts the escaped quotes in $search"
    if kind in _BAD_REQUEST:
        return "FAIL", "Graph rejects \\\" inside $search; change the escaping in graph_mail"
    return _unknown(kind)


async def check_reply_lookup(probe: Probe) -> Verdict:
    """2: receivedDateTime ge ... and conversationId eq '...' with $orderby is accepted."""
    data, kind = await probe("/me/messages", {"$select": "conversationId", "$top": "1"})
    rows = (data or {}).get("value", [])
    cid = next((str(m["conversationId"]) for m in rows if m.get("conversationId")), "")
    if kind or not cid:
        return "UNKNOWN", "needs at least one message in the mailbox" if not kind else _unknown(kind)[1]
    cid = cid.replace("'", "''")
    _, kind = await probe("/me/messages", {
        "$filter": f"receivedDateTime ge 1900-01-01T00:00:00Z and conversationId eq '{cid}'",
        "$orderby": "receivedDateTime desc", "$top": "1", "$select": "id"})
    if not kind:
        return "PASS", "the reply lookup filter with $orderby is accepted"
    if kind in _BAD_REQUEST:
        return "FAIL", "Graph rejects the reply lookup filter with $orderby; drop $orderby and sort locally"
    return _unknown(kind)


def judge_auth_headers(headers: Any) -> Verdict:
    """3: from internetMessageHeaders only. Values are looked at, never printed."""
    rows = [h for h in headers or [] if isinstance(h, dict)]
    if not rows:
        return "UNKNOWN", "this message came back without headers; try with a message received from outside"
    names = {str(h.get("name") or "").lower(): None for h in rows}
    top = next((str(h.get("value") or "") for h in rows
                if str(h.get("name") or "").lower() == "authentication-results"), "")
    authserv = top.split(";", 1)[0].strip().lower()
    exchange_ar = authserv.endswith(("outlook.com", "protection.outlook.com")) or bool(
        re.match(r"^(spf|dkim|dmarc|compauth|arc)=", authserv))
    has_authas = "x-ms-exchange-organization-authas" in names
    if exchange_ar and has_authas:
        return "PASS", "topmost Authentication-Results is Exchange's and AuthAs is present"
    if not top:
        return "FAIL", "no Authentication-Results header; sender trust must not rely on it"
    return "FAIL", ("topmost Authentication-Results is not Exchange's" if not exchange_ar
                    else "X-MS-Exchange-Organization-AuthAs is missing") + "; revisit the sender check"


async def check_auth_headers(probe: Probe) -> Verdict:
    data, kind = await probe("/me/mailFolders/inbox/messages", {
        "$top": "1", "$select": "id", "$orderby": "receivedDateTime desc"})
    ids = [str(m.get("id")) for m in (data or {}).get("value", []) if m.get("id")]
    if kind or not ids:
        return "UNKNOWN", "needs a received message in the inbox" if not kind else _unknown(kind)[1]
    from mavis.tools.integrations.native.graph import seg

    msg, kind = await probe(f"/me/messages/{seg(ids[0])}", {"$select": "internetMessageHeaders"})
    if kind:
        return _unknown(kind)
    return judge_auth_headers((msg or {}).get("internetMessageHeaders"))


_MIDNIGHT = re.compile(r"T00:00:00(\.0+)?Z?$")


def judge_all_day(start: Any, end: Any) -> Verdict:
    sd, ed = str((start or {}).get("dateTime") or ""), str((end or {}).get("dateTime") or "")
    if _MIDNIGHT.search(sd) and _MIDNIGHT.search(ed) and str((start or {}).get("timeZone")) == "UTC":
        return "PASS", "an all-day event arrives as midnight to midnight in UTC"
    return "FAIL", "an all-day event is not midnight to midnight in UTC; adjust the all-day handling"


async def check_all_day(probe: Probe) -> Verdict:
    data, kind = await probe("/me/events", {
        "$filter": "isAllDay eq true", "$top": "1", "$select": "start,end,isAllDay"},
        {"Prefer": 'outlook.timezone="UTC"'})
    if kind:
        return _unknown(kind)
    rows = (data or {}).get("value") or []
    if not rows:
        return "UNKNOWN", "needs an all-day event in the calendar; add one and run again"
    return judge_all_day(rows[0].get("start"), rows[0].get("end"))


async def check_delta_baseline(probe: Probe) -> Verdict:
    since = (timeutil.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _, kind = await probe("/me/mailFolders/inbox/messages/delta", {
        "$select": "id", "$filter": f"receivedDateTime ge {since}"}, {"Prefer": "odata.maxpagesize=1"})
    if not kind:
        return "PASS", "the mail delta baseline with a receivedDateTime filter is accepted"
    if kind in _BAD_REQUEST:
        return "FAIL", "Graph rejects the filter on the mail delta; baseline without it"
    return _unknown(kind)


async def check_chat_list(probe: Probe) -> Verdict:
    steps = (
        ("expand members and orderby",
         {"$expand": "members", "$orderby": "lastMessagePreview/createdDateTime desc"}),
        ("expand members, no orderby", {"$expand": "members"}),
        ("no expand, no orderby", {}),
    )
    for n, (label, extra) in enumerate(steps):
        _, kind = await probe("/me/chats", {"$top": "5", **extra})
        if not kind:
            return "PASS", ("the chat list works with $expand=members and $orderby" if n == 0
                            else f"fallback step {n + 1} was needed ({label})")
        if kind in ("permission_missing", "not_found"):
            return "UNKNOWN", "Teams chats are not available for this account"
        if kind not in _BAD_REQUEST:
            return _unknown(kind)
    return "FAIL", "Graph rejects every chat list variant; check the Chat.ReadWrite consent"


def judge_shared(kind: str) -> Verdict:
    if not kind:
        return "PASS", "sharedWithMe answers"
    if kind in (*_BAD_REQUEST, "not_found", "unavailable"):
        return "FAIL", "sharedWithMe is unavailable; the share poll switches itself off, search still works"
    return _unknown(kind)


async def live_check_items(probe: Probe | None, shared_kind: str, out: Out) -> int:
    """Prints the seven live-check items; returns the failures that count (a FAIL, except sharedWithMe)."""
    failures = 0
    items: list[tuple[str, Verdict]] = []
    if probe is None:
        unknown: Verdict = ("UNKNOWN", "raw Graph checks need the native router in this process")
        items = [(n, unknown) for n in ("1 search phrase", "2 reply lookup", "3 auth headers",
                                        "4 all-day events", "5 mail delta baseline", "6 chat list")]
    else:
        for name, check in (
                ("1 search phrase", check_search_phrase), ("2 reply lookup", check_reply_lookup),
                ("3 auth headers", check_auth_headers), ("4 all-day events", check_all_day),
                ("5 mail delta baseline", check_delta_baseline), ("6 chat list", check_chat_list)):
            items.append((name, await check(probe)))
    items.append(("7 sharedWithMe", judge_shared(shared_kind)))
    for name, (verdict, hint) in items:
        out(f"live-check {name}: {verdict} - {hint}")
        failures += verdict == "FAIL" and not name.startswith("7")
    return failures


async def microsoft_checks(provider: Any, user_id: int, out: Out) -> tuple[int, str]:
    """Returns (failures, own address). Counts and masked addresses only, never subjects, names or ids."""
    failures, email = 0, ""
    data, _ = await ncall(provider, user_id, "mail.profile", {}, out)
    if isinstance(data, dict):
        email = str(data.get("emailAddress") or "")
        out(f"outlook mail.profile: ok account={mask_email(email)}")
    else:
        failures += 1
    data, _ = await ncall(provider, user_id, "mail.search",
                          {"query": "newer_than:2d", "max_results": 50}, out)
    if data is None:
        failures += 1
    else:
        out(f"outlook mail.search newer_than:2d: {len(extract_messages(data))} messages")
    data, _ = await ncall(provider, user_id, "mail.search", {"query": 'subject:"a b"', "max_results": 1}, out,
                          quiet=True)
    out(f"outlook phrase search: {'ok' if data is not None else 'FAILED'}")
    failures += data is None
    data, _ = await ncall(provider, user_id, "mail.inbox_delta", {}, out, quiet=True)
    out(f"outlook inbox delta baseline: {'ok' if data is not None else 'FAILED'}")
    failures += data is None
    now = timeutil.now()
    data, _ = await ncall(provider, user_id, "calendar.list", {
        "time_min": now.isoformat(), "time_max": (now + timedelta(days=7)).isoformat(),
        "max_results": 50}, out)
    if data is None:
        failures += 1
    else:
        out(f"outlook calendar next 7 days: {len(extract_calendar_items(data))} events")
    data, _ = await ncall(provider, user_id, "calendar.delta", {}, out, quiet=True)
    out(f"outlook calendar delta baseline: {'ok' if data is not None else 'FAILED'}")
    failures += data is None
    data, kind = await ncall(provider, user_id, "teams.chats", {"limit": 50}, out, quiet=True)
    if data is not None:
        chats = extract_list(data, "chats")
        direct = sum(1 for c in chats if c.get("type") == "oneOnOne")
        out(f"teams chats: {len(chats)} ({direct} direct)")
    elif kind == "permission_missing":
        out("teams: not available for this account")
    else:
        out(f"teams.chats: FAILED ({kind})")
        failures += 1
    data, _ = await ncall(provider, user_id, "onedrive.list_recent", {"max_results": 25}, out)
    files = extract_list(data, "files") if data is not None else []
    if data is None:
        failures += 1
    else:
        out(f"onedrive recent: {len(files)} files")
    shared, shared_kind = await ncall(provider, user_id, "onedrive.list_recent", {
        "shared_with_me": True, "max_results": 25}, out, quiet=True)
    out("onedrive sharedWithMe: " + (f"ok ({len(extract_list(shared, 'files'))})" if shared is not None
                                     else "unavailable"))
    text_file = next((f for f in files if str(f.get("mimeType") or "").startswith("text/") and f.get("id")),
                     None)
    if text_file is None:
        out("onedrive read first text file: skipped")
    else:
        read, _ = await ncall(provider, user_id, "onedrive.read", {"item_id": text_file["id"]}, out,
                              quiet=True)
        out(f"onedrive read first text file: {'ok' if read is not None else 'FAILED'}")
        failures += read is None
    failures += await live_check_items(graph_probe(provider, user_id), shared_kind, out)
    return failures, email


async def send_microsoft_test(provider: Any, user_id: int, email: str, out: Out) -> int:
    """ONE self-addressed mail through Graph. No Teams message, no other recipient."""
    if not email:
        out("outlook mail.send to self: skipped (own address unknown)")
        return 1
    stamp = timeutil.now().strftime("%H:%M:%S")
    res, _ = await ncall(provider, user_id, "mail.send", {
        "to": [email], "subject": f"Mavis native check {stamp}",
        "body": "A self-addressed test from the native Microsoft connector. Nothing to do."}, out)
    out("outlook mail.send to self: " + ("sent" if res is not None else "not sent"))
    return int(res is None)


async def send_tests(provider: Any, user_id: int, email: str, slack_user: str, out: Out) -> int:
    """One email to the user's own address and one Slack message in the user's own DM. Nothing else."""
    failures = 0
    stamp = timeutil.now().strftime("%H:%M:%S")
    if email:
        res = await call(provider, user_id, "mail.send", {
            "to": [email], "subject": f"Mavis native check {stamp}",
            "body": "A self-addressed test from the native Google connector. Nothing to do."}, out)
        out("mail.send to self: " + ("sent" if res is not None else "not sent"))
        failures += res is None
    else:
        out("mail.send to self: skipped (own address unknown)")
        failures += 1
    channels = await call(provider, user_id, "slack.channels", {}, out)
    own_dm = next((c for c in extract_list(channels, "channels", "data.channels")
                   if c.get("kind") == "dm" and slack_user and c.get("user") == slack_user), None)
    if own_dm is None:
        out("slack.send to self: skipped (no DM with yourself is listed; open one in Slack first)")
        return failures + 1
    res = await call(provider, user_id, "slack.send", {
        "channel": own_dm["id"], "text": f"Mavis native check {stamp}. A self test, nothing to do."}, out)
    out("slack.send to self: " + ("sent" if res is not None else "not sent"))
    return failures + (res is None)


async def main(user_id: int, *, sync: bool, send: bool, out: Out = print) -> int:
    from mavis.memory.service import get_memory
    from mavis.tools.integrations import get_provider
    from mavis.tools.integrations.wiring import get_first_sync

    provider = get_provider()
    tokens = getattr(provider, "tokens", None)
    if tokens is None:
        out("INTEGRATION_PROVIDER is not native in this process: nothing to check")
        return 1
    grants = await grant_report(tokens, user_id, out)
    if not grants:
        out("no native grant for this user: connect Google, Slack or Microsoft 365 in Telegram first")
        return 1
    failures, email, ms_email = 0, "", ""
    if grants.keys() & {NativeProvider.GOOGLE, NativeProvider.SLACK}:
        failures, email = await read_checks(provider, user_id, out)
    if NativeProvider.MICROSOFT in grants:
        ms_failures, ms_email = await microsoft_checks(provider, user_id, out)
        failures += ms_failures
    await state_report(user_id, out)
    if sync:
        await run_first_sync(get_first_sync(), user_id, out, grants)
    await graph_report(get_memory(), user_id, out)
    if send:
        if grants.keys() & {NativeProvider.GOOGLE, NativeProvider.SLACK}:
            slack_account = grants[NativeProvider.SLACK].account if NativeProvider.SLACK in grants else {}
            slack_user = str(slack_account.get("user_id") or "")
            failures += await send_tests(provider, user_id, email, slack_user, out)
        if NativeProvider.MICROSOFT in grants:
            failures += await send_microsoft_test(provider, user_id, ms_email, out)
    out("RESULT: " + ("all checks passed" if not failures else f"{failures} check(s) failed"))
    return 1 if failures else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("user_id", type=int, help="the Mavis user id")
    ap.add_argument("--sync", action="store_true", help="run the first sync like a new connection")
    ap.add_argument("--send-test", action="store_true", help="send one self-addressed email and Slack DM")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.user_id, sync=args.sync, send=args.send_test)))

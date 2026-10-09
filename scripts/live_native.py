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
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
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


async def run_first_sync(sync: Any, user_id: int, out: Out) -> None:
    for capability in (Capability.GMAIL, Capability.CALENDAR, Capability.SLACK):
        noticed = await sync.run(user_id, capability)
        out(f"first sync {capability.value}: queued, {len(noticed)} notices")


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
        out("no native grant for this user: connect Google and Slack in Telegram first")
        return 1
    failures, email = await read_checks(provider, user_id, out)
    await state_report(user_id, out)
    if sync:
        await run_first_sync(get_first_sync(), user_id, out)
    await graph_report(get_memory(), user_id, out)
    if send:
        slack_account = grants[NativeProvider.SLACK].account if NativeProvider.SLACK in grants else {}
        failures += await send_tests(provider, user_id, email, str(slack_account.get("user_id") or ""), out)
    out("RESULT: " + ("all checks passed" if not failures else f"{failures} check(s) failed"))
    return 1 if failures else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("user_id", type=int, help="the Mavis user id")
    ap.add_argument("--sync", action="store_true", help="run the first sync like a new connection")
    ap.add_argument("--send-test", action="store_true", help="send one self-addressed email and Slack DM")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.user_id, sync=args.sync, send=args.send_test)))

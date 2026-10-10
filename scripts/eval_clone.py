"""Operator-only eval helper: lend a real user's Google grant to the synthetic TEST user, then take it back.

The live evals (scripts/eval_google.py) speak as TEST_TELEGRAM_CHAT_ID, whose replies land in the test sink,
never in anyone's Telegram, and whose memory is its own. To test against real Google data, the TEST user
gets a copy of the source user's grant: the same tokens, re-sealed for the TEST user, under an
`eval-clone:` account key so the one-account-one-user rule keeps holding for every real lookup (sign-in,
connect, ownership checks never match the clone). Run inside the api container:

    python -m scripts.eval_clone clone <source_user_id>     # copy, record the identity, start first sync
    python -m scripts.eval_clone remove                     # delete the TEST user's Google grant

The clone is refused for any target other than the active synthetic test chat. Nothing is printed but ids,
statuses and a masked address.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import delete, select

from mavis.bus import get_bus
from mavis.channels.test_sink import active_test_chat
from mavis.domain import timeutil
from mavis.domain.events import Job, JobKind
from mavis.domain.policy import Capability
from mavis.store import db as dbm
from mavis.store.models import NativeGrant, User
from mavis.store.repo import connections
from mavis.tools.integrations.actions import GOOGLE_ANCHOR, workspace_enabled
from mavis.tools.integrations.native import crypto
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.tokens import _ctx

EVAL_KEY = "eval-clone:"
G = NativeProvider.GOOGLE


async def test_user_id() -> int:
    chat = active_test_chat()
    if chat is None:
        raise SystemExit("LIVE_TEST_ENABLED with a synthetic TEST_TELEGRAM_CHAT_ID is required")
    async with dbm.Session() as s:
        uid = await s.scalar(select(User.id).where(User.telegram_chat_id == chat))
    if uid is None:
        raise SystemExit("the TEST user does not exist yet: send it one message with scripts.live_e2e first")
    return uid


async def clone(source: int) -> None:
    target = await test_user_id()
    if target == source:
        raise SystemExit("source and target are the same user")
    async with dbm.Session() as s:
        row = await s.scalar(select(NativeGrant).where(NativeGrant.user_id == source,
                                                       NativeGrant.provider == G.value))
        if row is None or row.status != "ACTIVE":
            raise SystemExit(f"user {source} has no active Google grant")
        access = crypto.open_text(row.access_token, _ctx(source, G, "access_token")) or ""
        refresh = crypto.open_text(row.refresh_token, _ctx(source, G, "refresh_token"))
        account, expires = dict(row.account or {}), row.expires_at
        key = f"{EVAL_KEY}{row.account_key or ''}"[:255]
        await s.execute(delete(NativeGrant).where(NativeGrant.user_id == target,
                                                  NativeGrant.provider == G.value))
        now = timeutil.now()
        s.add(NativeGrant(user_id=target, provider=G.value, account=account, account_key=key,
                          access_token=crypto.seal_text(access, _ctx(target, G, "access_token")),
                          refresh_token=crypto.seal_text(refresh, _ctx(target, G, "refresh_token")),
                          expires_at=expires, status="ACTIVE", created_at=now, updated_at=now))
        await s.commit()
    from mavis.attention.connector_ingest import remember_identity

    await remember_identity(target, emails=(str(account.get("email") or ""),))
    capability = GOOGLE_ANCHOR if workspace_enabled() else Capability.GMAIL
    pending = await connections.create_pending(target, capability, "", None)
    await get_bus().enqueue(Job(id=f"conncheck:{pending}:eval", user_id=target,
                                kind=JobKind.CONNECTION_CHECK, payload={"pending_id": pending}))
    email = str(account.get("email") or "")
    masked = f"{email[:1]}***@{email.partition('@')[2]}"
    print(f"cloned google grant of user {source} to test user {target} ({masked}); "
          "connection check queued (announcement, first sync, polling)")


async def remove() -> None:
    target = await test_user_id()
    async with dbm.Session() as s:
        n = (await s.execute(delete(NativeGrant).where(
            NativeGrant.user_id == target, NativeGrant.provider == G.value,
            NativeGrant.account_key.like(f"{EVAL_KEY}%")))).rowcount
        await s.commit()
    print(f"removed {n} eval google grant(s) from test user {target}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("clone")
    c.add_argument("source", type=int)
    sub.add_parser("remove")
    args = ap.parse_args()
    asyncio.run(clone(args.source) if args.cmd == "clone" else remove())
    sys.exit(0)

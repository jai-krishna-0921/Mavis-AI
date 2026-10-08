"""Owner /invite commands (spec 4.4)."""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mavis.access import UserTier
from mavis.access.codes import InviteError, deep_link_param, display
from mavis.access.commands import parse_kv, register_owner_command
from mavis.config import get_settings
from mavis.domain.events import Event
from mavis.store.db import utcnow
from mavis.store.models import User
from mavis.store.repo import invites, users

BOT_LINK = "https://t.me/{bot}?start={param}"
HELP = ("Use: /invite new [uses=1] [days=14] [tier=standard] [tz=Area/City] [cur=INR] [label], "
        "/invite list, /invite revoke <hint>, /invite users <id>")


def _valid_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return "/" in name


async def _new(user: User, args: list[str]) -> str:
    kv, label = parse_kv(args)
    s = get_settings()
    try:
        uses = int(kv.get("uses", "1"))
        days = int(kv.get("days", str(s.invite_default_days)))
    except ValueError:
        return "uses and days must be whole numbers."
    tier = kv.get("tier", UserTier.STANDARD.value)
    if tier not in (UserTier.STANDARD.value, UserTier.TRUSTED.value):
        return "tier must be standard or trusted."
    tz = kv.get("tz")
    if tz and not _valid_zone(tz):
        return f"I don't know the time zone {tz}. Use a name like Europe/Lisbon."
    cur = kv.get("cur")
    if cur and not (len(cur) == 3 and cur.isalpha()):
        return "cur must be a 3-letter currency code like INR or EUR."
    try:
        row, plain = await invites.mint(created_by=user.id, uses=uses, days=days, tier=tier, tz=tz,
                                        currency=cur, label=" ".join(label))
    except InviteError:
        return (f"I can't make that one: at most {s.invite_max_active} open codes and "
                f"{s.invite_max_uses} uses per code.")
    link = BOT_LINK.format(bot=s.telegram_bot_username or "Mavis247_bot", param=deep_link_param(plain))
    return (f"Invite {display(plain)} ({row.max_uses} use{'s' if row.max_uses != 1 else ''}, "
            f"expires {row.expires_at:%d %b}).\n{link}\nThis is the only time I show the full code.")


async def invite(event: Event, user: User, args: list[str]) -> str:
    sub, rest = (args[0].lower(), args[1:]) if args else ("new", [])
    if sub == "new":
        return await _new(user, rest)
    if sub == "list":
        rows = await invites.list_active(utcnow())
        if not rows:
            return "No open invite codes."
        return "\n".join(f"#{r.id} ...{r.code_hint} {r.label or '(no label)'} {r.uses}/{r.max_uses} "
                         f"until {r.expires_at:%d %b}" for r in rows)
    if sub == "revoke" and rest:
        row = await invites.revoke(rest[0])
        if row is None:
            return "No open code like that."
        return f"Revoked ...{row.code_hint}. People who joined keep access."
    if sub == "users" and rest and rest[0].isdigit():
        names = []
        for r in await invites.redemptions_for(int(rest[0])):
            u = await users.get(r.user_id)
            names.append(f"{u.name or 'someone'} (#{u.id}, {r.redeemed_at:%d %b})")
        return "\n".join(names) or "Nobody has used that code yet."
    return HELP


def register() -> None:
    register_owner_command("invite", invite)

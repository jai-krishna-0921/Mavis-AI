# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""One assistant across Google and Microsoft: mail.* and calendar.* fan out to every connected account.

Reads run on each account and merge (every item tagged with its account). Writes on an existing item go
to the provider its id belongs to (`ms:` ids are Microsoft's). A NEW mail or event goes to the account the
user named, else their remembered default, else the only one connected; with two and no default the model
is told to ask which. Nothing here engages unless the user has an ACTIVE Microsoft grant for that kind of
service, so a Google-only user's calls reach the router's single-provider path exactly as before.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from mavis.domain.errors import ConnectionRequired, FailureKind
from mavis.domain.integrations import ToolResult, UserRef
from mavis.domain.policy import Capability
from mavis.tools.integrations.native import providers
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.google import MIN_FREE_MINUTES
from mavis.tools.integrations.native.graph import MS_ID_PREFIX
from mavis.tools.integrations.native.microsoft_spec import PERSONAL_TENANT
from mavis.tools.integrations.native.tokens import ACTIVE
from mavis.tools.integrations.normalize import extract_calendar_items, extract_messages, to_datetime

if TYPE_CHECKING:
    from mavis.tools.integrations.native.router import NativeRouter

GOOGLE, MICROSOFT = NativeProvider.GOOGLE, NativeProvider.MICROSOFT
MERGED: dict[str, tuple[Capability, Capability]] = {
    "mail": (Capability.GMAIL, Capability.OUTLOOK),
    "calendar": (Capability.CALENDAR, Capability.OUTLOOK_CALENDAR),
}
READ_MERGE = frozenset({"mail.search", "mail.profile", "calendar.list", "calendar.find",
                        "calendar.free_slots", "calendar.calendars"})
_IDS = ("message_ids", "thread_ids")
ID_ROUTED: dict[str, tuple[str, ...]] = {
    "mail.read": ("message_id",), "mail.thread": ("thread_id",), "mail.reply": ("thread_id",),
    "mail.archive": _IDS, "mail.mark_read": _IDS, "mail.mark_unread": _IDS, "mail.label": _IDS,
    "mail.trash": _IDS, "mail.untrash": _IDS,
    "calendar.get": ("event_id",), "calendar.update_event": ("event_id",),
    "calendar.delete_event": ("event_id",), "calendar.respond": ("event_id",),
}
NEW_ITEM = {"mail.send": "mail", "mail.draft": "mail", "calendar.create_event": "calendar"}
_MS_NOT_CONNECTED = "the Microsoft 365 account is not connected; the user must reconnect it"
_COUNTED = ("messages", "threads")
_EPOCH = datetime.min.replace(tzinfo=UTC)
_ALIASES = {"google": GOOGLE, "gmail": GOOGLE, "microsoft": MICROSOFT, "outlook": MICROSOFT,
            "microsoft365": MICROSOFT, "office": MICROSOFT}


@dataclass(frozen=True)
class Account:
    provider: NativeProvider
    capability: Capability
    address: str
    tag: str
    active: bool = True  # False: a Microsoft grant that was revoked or stopped working


@dataclass(frozen=True)
class AskWhich:
    """More than one account could take a new item and nothing says which: the model must ask."""

    kind: str
    tags: tuple[str, ...]
    text: str

    def result(self) -> ToolResult:
        return ToolResult(ok=False, error_kind=FailureKind.INVALID_ARGUMENT, error_field="account",
                          error=self.text)


def kind_of(action: str) -> str | None:
    head = action.split(".", 1)[0]
    return head if head in MERGED else None


def is_ms(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(MS_ID_PREFIX)


def _ids(value: Any) -> list[str]:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v]
    return [value] if isinstance(value, str) and value else []


def _failure(exc: BaseException) -> ToolResult:
    kind = FailureKind.AUTH if isinstance(exc, ConnectionRequired) else FailureKind.UNAVAILABLE
    return ToolResult(ok=False, error_kind=kind, error=type(exc).__name__)


def _as_result(out: ToolResult | BaseException) -> ToolResult:
    return out if isinstance(out, ToolResult) else _failure(out)


def _words(google: str | None, microsoft_personal: bool | None, microsoft: bool) -> tuple[str, str]:
    """The work/personal word for (google, microsoft). Both of one user's accounts never share a word:
    Google is personal for a gmail address, and for any other address while the Microsoft account is a
    work one (the two are told apart by contrast); the Microsoft account is personal on the consumer
    tenant."""
    gmail = (google or "").lower().endswith(("@gmail.com", "@googlemail.com"))
    if not microsoft:
        return ("personal" if gmail else "work"), ""
    g = "personal" if gmail or not microsoft_personal else "work"
    return g, "personal" if microsoft_personal else "work"


class MailCalendarMerge:
    def __init__(self, router: NativeRouter) -> None:
        self.router = router

    # --- who is connected -------------------------------------------------------------------------

    async def _microsoft(self, user_id: int, kind: str) -> tuple[Capability, str, bool, bool] | None:
        """(capability, address, personal, active) when the user has a Microsoft grant serving this kind,
        whatever its status: a revoked one still counts, so nothing silently falls back to Google."""
        spec = providers.spec_of("microsoft")
        if spec is None or not self.router._ready(MICROSOFT):
            return None
        grant = await self.router.tokens.grant(user_id, MICROSOFT)
        cap = MERGED[kind][1]
        if grant is None or cap not in spec.caps_for(grant):
            return None
        return (cap, str(grant.account.get("mail") or ""), grant.account.get("tid") == PERSONAL_TENANT,
                grant.status == ACTIVE)

    async def engaged(self, user_id: int, kind: str) -> bool:
        """Does the user have a Microsoft account serving this kind (so the merge applies)?"""
        return await self._microsoft(user_id, kind) is not None

    async def _google(self, user_id: int, kind: str) -> tuple[Capability, str] | None:
        from mavis.tools.integrations.native.router import covers  # router imports this module

        cap = MERGED[kind][0]
        if self.router._ready(GOOGLE):
            grant = await self.router._active_grant(user_id, GOOGLE)
            if grant is not None and covers(grant, cap):
                return cap, str(grant.account.get("email") or "")
        if await self.router._composio_active(UserRef(user_id=user_id), cap):
            return cap, ""  # a legacy Composio account: reachable, address unknown
        return None

    async def accounts(self, user_id: int, kind: str) -> list[Account]:
        ms = await self._microsoft(user_id, kind)
        g = await self._google(user_id, kind)
        gword, mword = _words(g[1] if g else None, ms[2] if ms else None, ms is not None)
        if g and ms and gword == mword:
            gword, mword = "account 1", "account 2"
        out: list[Account] = []
        if g:
            out.append(Account(GOOGLE, g[0], g[1], f"{gword}: {g[1] or 'Google account'}"))
        if ms:
            out.append(Account(MICROSOFT, ms[0], ms[1], f"{mword}: {ms[1] or 'Microsoft account'}", ms[3]))
        return out

    # --- defaults and sender choice ---------------------------------------------------------------

    async def _stored_default(self, user_id: int, kind: str) -> str | None:
        from mavis.store.repo import users

        defaults = (await users.get_state(user_id)).get("defaults")
        value = defaults.get(kind) if isinstance(defaults, dict) else None
        return value if isinstance(value, str) else None

    async def remember_default(self, user_id: int, kind: str, account: Account) -> None:
        from mavis.store.repo import users

        old = (await users.get_state(user_id)).get("defaults")
        old = dict(old) if isinstance(old, dict) else {}
        await users.update_state(user_id, {"defaults": {**old, kind: account.provider.value}})

    @staticmethod
    def _match(accounts: list[Account], wanted: str) -> Account | None:
        w = wanted.strip().lower()
        for a in accounts:
            if w and (w == a.address.lower() or w == a.tag.lower()):
                return a
        if w in _ALIASES:
            return next((a for a in accounts if a.provider is _ALIASES[w]), None)
        if w in ("work", "personal", "account 1", "account 2"):
            return next((a for a in accounts if a.tag.startswith(f"{w}:")), None)
        return None

    async def resolve_sender(self, user_id: int, kind: str, account_arg: str | None) -> Account | AskWhich:
        accounts = await self.accounts(user_id, kind)
        tags = tuple(a.tag for a in accounts)
        listing = ", ".join(tags) or "none"
        example = "Send from your work or personal email?" if kind == "mail" else \
            "Put it on your work or personal calendar?"
        ask = (f"Ask the user which to use (for example: {example}), then call "
               "again with account set to that address")
        if account_arg and account_arg.strip():
            hit = self._match(accounts, account_arg)
            if hit is not None:
                return hit
            return AskWhich(kind, tags, f"no connected {kind} account matches the account you gave. "
                                        f"Connected: {listing}. {ask}")
        stored = await self._stored_default(user_id, kind)
        if stored:
            hit = next((a for a in accounts if a.provider.value == stored), None)
            if hit is not None:
                return hit
        if len(accounts) == 1:
            return accounts[0]
        return AskWhich(kind, tags, f"two accounts are connected for {kind}: {listing}. {ask}")

    # --- the router hook --------------------------------------------------------------------------

    async def execute(self, user: UserRef, action: str, args: dict) -> ToolResult | None:
        kind = kind_of(action)
        if kind is None:
            return None
        if action in ID_ROUTED:
            return await self._by_id(user, kind, action, args)
        if action not in READ_MERGE and action not in NEW_ITEM:
            return None
        if action in NEW_ITEM and args.get("sender_pin"):
            return await self._pinned(user, kind, action, args)
        if await self._microsoft(user.user_id, kind) is None:
            return None
        if action in NEW_ITEM:
            return await self._new_item(user, kind, action, args)
        return await self._read(user, kind, action, args)

    async def _run(self, user: UserRef, account: Account, action: str, args: dict) -> ToolResult:
        if account.provider is not MICROSOFT:
            return await self.router._execute_single(user, action, args)
        executor = self.router.executors[MICROSOFT]
        if not account.active:
            result = ToolResult(ok=False, error_kind=FailureKind.AUTH, error=_MS_NOT_CONNECTED)
        elif not executor.handles(action):
            result = ToolResult(ok=False, error_kind=FailureKind.UNKNOWN,
                                error=f"microsoft does not implement {action!r}")
        else:
            result = await executor.execute(user, action, args)
        # which account a failure came from, so the tool layer names Microsoft, not Google
        return result if result.ok else result.model_copy(update={"source": account.capability.value})

    # --- new items --------------------------------------------------------------------------------

    async def _new_item(self, user: UserRef, kind: str, action: str, args: dict) -> ToolResult:
        wanted = args.get("account")
        picked = await self.resolve_sender(user.user_id, kind, wanted if isinstance(wanted, str) else None)
        if isinstance(picked, AskWhich):
            return picked.result()
        if not picked.active:  # chosen (or the default) but not usable: reconnect it, never send from Gmail
            raise ConnectionRequired(picked.capability, _purpose(picked.capability), revoked=True)
        stored = await self._stored_default(user.user_id, kind) if wanted else None
        result = await self._run(user, picked, action, {k: v for k, v in args.items() if k != "account"})
        if result.ok and wanted and not stored:
            await self.remember_default(user.user_id, kind, picked)
        return result

    async def _pinned(self, user: UserRef, kind: str, action: str, args: dict) -> ToolResult | None:
        """Send from exactly the account the card named. If it is gone or stopped working, ask to reconnect
        it; never pick another account (the card said who it was from)."""
        provider, _, address = str(args["sender_pin"]).partition(":")
        accounts = await self.accounts(user.user_id, kind)
        hit = next((a for a in accounts
                    if a.provider.value == provider and a.address.lower() == address.lower()), None)
        has_ms = any(a.provider is MICROSOFT for a in accounts)
        if hit is None and provider != MICROSOFT.value and not has_ms:
            return None  # no Microsoft account in play: Google's own path, as for any Google-only user
        if hit is None or not hit.active:
            cap = MERGED[kind][1 if provider == MICROSOFT.value else 0]
            raise ConnectionRequired(cap, _purpose(cap), revoked=True)
        wanted = args.get("account")
        named = isinstance(wanted, str) and bool(wanted.strip())
        stored = await self._stored_default(user.user_id, kind) if named else None
        result = await self._run(user, hit, action,
                                 {k: v for k, v in args.items() if k not in ("account", "sender_pin")})
        if result.ok and named and not stored:
            await self.remember_default(user.user_id, kind, hit)  # "asks once": the answer sticks
        return result

    # --- existing items ---------------------------------------------------------------------------

    async def _by_id(self, user: UserRef, kind: str, action: str, args: dict) -> ToolResult | None:
        names = ID_ROUTED[action]
        every = [i for n in names for i in _ids(args.get(n))]
        if not any(is_ms(i) for i in every):
            return None  # nothing Microsoft's: the single-provider path, untouched
        ms = await self._microsoft(user.user_id, kind)
        if ms is None or not ms[3]:  # never let a Microsoft id reach Google
            cap = MERGED[kind][1]
            return ToolResult(ok=False, error_kind=FailureKind.AUTH, error=_MS_NOT_CONNECTED,
                              source=cap.value)
        ms_args, g_args = dict(args), dict(args)
        for name in names:
            value = args.get(name)
            if isinstance(value, list):
                ms_args[name] = [v for v in value if is_ms(v)]
                g_args[name] = [v for v in value if not is_ms(v)]
        halves: list[tuple[Account | None, dict]] = []
        ms_account = Account(MICROSOFT, ms[0], ms[1], "", ms[3])
        if any(is_ms(i) for i in every):
            halves.append((ms_account, ms_args))
        if any(not is_ms(i) for i in every):
            halves.append((None, g_args))
        outs = await asyncio.gather(
            *(self._run(user, a or Account(GOOGLE, MERGED[kind][0], "", ""), action, x) for a, x in halves),
            return_exceptions=True)
        results = [_as_result(o) for o in outs]
        if len(results) == 1:
            return results[0]
        return _join_halves(results)

    # --- merged reads -----------------------------------------------------------------------------

    async def _read(self, user: UserRef, kind: str, action: str, args: dict) -> ToolResult:
        accounts = await self.accounts(user.user_id, kind)
        if action == "mail.profile":
            default = await self._stored_default(user.user_id, kind)
            usable = [a for a in accounts if a.active] or accounts
            first = next((a for a in usable if a.provider.value == default), usable[0])
            return ToolResult(ok=True, data={"emailAddress": first.address,
                                             "accounts": [a.tag for a in accounts]})
        if action in ("calendar.list", "calendar.find"):  # a calendar id belongs to one provider
            accounts = _owner_of_calendar(accounts, args.get("calendar_id"))
        outs = await asyncio.gather(*(self._run(user, a, action, args) for a in accounts),
                                    return_exceptions=True)
        sides = list(zip(accounts, outs, strict=True))
        good = [(a, o) for a, o in sides if isinstance(o, ToolResult) and o.ok]
        if not good:
            first = next((o for _, o in sides if isinstance(o, ToolResult)), None)
            if first is not None:
                return first
            raise sides[0][1]  # every side raised: the old path would have raised too
        notes = [_unreachable(a, _as_result(o)) for a, o in sides if a not in {g for g, _ in good}]
        return ToolResult(ok=True, data=_combine(action, args, good, notes))


def _owner_of_calendar(accounts: list[Account], calendar_id: Any) -> list[Account]:
    """"primary" (or none) means every account's own calendar; any other id is Microsoft's when it carries
    the ms: prefix and Google's otherwise. No owner connected: keep all, so the failure is reported."""
    if not isinstance(calendar_id, str) or calendar_id in ("", "primary"):
        return accounts
    owner = MICROSOFT if is_ms(calendar_id) else GOOGLE
    mine = [a for a in accounts if a.provider is owner]
    return mine or accounts


def _purpose(capability: Capability) -> str:
    from mavis.tools.integrations.actions import CAPABILITY_PURPOSE  # actions has no dependency back here

    return CAPABILITY_PURPOSE[capability]


def _unreachable(account: Account, result: ToolResult) -> str:
    gone = result.error_kind in (FailureKind.AUTH, FailureKind.PERMISSION_MISSING)
    return f"Could not reach {account.tag} ({'needs reconnecting' if gone else 'temporarily unavailable'})"


def _join_halves(results: list[ToolResult]) -> ToolResult:
    """The two halves of one id list: Microsoft's ids first, then Google's."""
    names = ("the Microsoft part", "the Google part")
    bad = [i for i, r in enumerate(results) if not r.ok]
    if bad:
        first = results[bad[0]]
        if len(bad) == len(results):
            return first
        done = next(names[i] for i, r in enumerate(results) if r.ok)
        said = f"{done} was done, {names[bad[0]]} failed: {first.error or ''}"
        return first.model_copy(update={"error": said})
    data: dict[str, Any] = {}
    for r in results:
        for key, value in (r.data if isinstance(r.data, dict) else {}).items():
            if key in _COUNTED and isinstance(value, int) and isinstance(data.get(key), int):
                data[key] += value
            elif isinstance(value, list) and isinstance(data.get(key), list):
                data[key] = [*data[key], *value]
            elif key == "partial":
                data[key] = bool(data.get(key)) or bool(value)
            else:
                data.setdefault(key, value)
    return ToolResult(ok=True, data=data)


def _limit(args: dict, items: list) -> list:
    cap = args.get("max_results")
    return items[:cap] if isinstance(cap, int) and cap > 0 else items


def _tagged(account: Account, items: list[dict]) -> list[dict]:
    return [{**i, "account": account.tag} for i in items if isinstance(i, dict)]


def _combine(action: str, args: dict, good: list[tuple[Account, ToolResult]], down: list[str]) -> dict:
    out: dict[str, Any]
    own = [n for _, r in good for n in _list_of(r.data, "notes") if isinstance(n, str)]
    if action == "mail.search":
        msgs = [m for a, r in good for m in _tagged(a, extract_messages(r.data))]
        msgs.sort(key=lambda m: to_datetime(m.get("messageTimestamp")) or _EPOCH, reverse=True)
        out = {"messages": _limit(args, msgs)}
    elif action in ("calendar.list", "calendar.find"):
        events = [e for a, r in good for e in _tagged(a, extract_calendar_items(r.data))]
        events.sort(key=lambda e: to_datetime(e.get("start")) or _EPOCH)  # an agenda reads soonest first
        out = {"items": _limit(args, events), "timeZone": _first(good, "timeZone")}
    elif action == "calendar.calendars":
        out = {"calendars": [c for a, r in good for c in _tagged(a, _list_of(r.data, "calendars"))]}
    else:  # calendar.free_slots
        out = _free_slots(good, partial=bool(down))
    if own or down:
        out["notes"] = [*own, *down]
    return out


def _list_of(data: Any, key: str) -> list:
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, list) else []


def _list(data: Any, key: str) -> list[dict]:
    return [v for v in _list_of(data, key) if isinstance(v, dict)]


def _first(good: list[tuple[Account, ToolResult]], key: str) -> Any:
    return next((r.data[key] for _, r in good if isinstance(r.data, dict) and r.data.get(key)), "")


def _free_slots(good: list[tuple[Account, ToolResult]], *, partial: bool) -> dict:
    """Free time is time free everywhere; busy time is busy anywhere."""
    spans = [[s for s in _list(r.data, "free_slots") if _window(s)] for _, r in good]
    common = spans[0]
    for other in spans[1:]:
        common = [piece for a in common for b in other if (piece := _overlap(a, b)) is not None]
    common.sort(key=lambda s: _window(s)[0])
    busy = sorted((b for _, r in good for b in _list(r.data, "busy")),
                  key=lambda b: to_datetime(b.get("start")) or _EPOCH)
    base = good[0][1].data if isinstance(good[0][1].data, dict) else {}
    out: dict[str, Any] = {"time_min": base.get("time_min"), "time_max": base.get("time_max"),
                           "busy": busy,
                           "free_slots": [s for s in common if s["minutes"] >= MIN_FREE_MINUTES]}
    if partial or any(isinstance(r.data, dict) and r.data.get("partial") for _, r in good):
        out["partial"] = True  # an account was not heard from: the free time shown is not guaranteed
    return out


def _window(slot: dict) -> tuple[datetime, datetime] | tuple:
    s, e = to_datetime(slot.get("start")), to_datetime(slot.get("end"))
    return (s, e) if s and e and e > s else ()


def _overlap(a: dict, b: dict) -> dict | None:
    (sa, ea), (sb, eb) = _window(a), _window(b)
    start_from, start = (a, sa) if sa >= sb else (b, sb)
    end_from, end = (a, ea) if ea <= eb else (b, eb)
    if end <= start:
        return None
    return {"start": start_from["start"], "end": end_from["end"],
            "minutes": int((end - start) / timedelta(minutes=1))}

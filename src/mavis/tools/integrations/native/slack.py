"""In-house Slack executor: slack.channels, slack.history, slack.send over the Slack Web API.

Calls use the user's token (xoxp), so Mavis reads what the user can read and posts as the user. Slack answers
HTTP 200 with `ok:false` for most failures, so errors are mapped from the machine `error` code (never prose)
and from HTTP status. Everything returned is third-party content; callers render it through
base.render_result.

Account facts expected from `TokenSource.account(user_id, SLACK)` (saved at connect time):
`team_id` and the authorizing user's Slack id under `slack_user_id` (or `authed_user_id`, or `user_id`).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import structlog

from mavis.domain.errors import FailureKind
from mavis.domain.integrations import ToolResult, UserRef
from mavis.tools.integrations.failures import classify, kind_for_status
from mavis.tools.integrations.native.base import NativeProvider, ReauthRequired, TokenSource

log = structlog.get_logger()

API = "https://slack.com/api/"
ACTIONS = frozenset({"slack.channels", "slack.history", "slack.send"})
CONVERSATION_TYPES = "public_channel,private_channel,im,mpim"
PAGE = 200  # conversations.list page size
MAX_PAGES = 10
MAX_RETRY_AFTER_S = 10.0
TEXT_LIMIT = 4000  # per message handed to the model; the bus snippet is cut again by normalize
USER_TTL_S = 3600.0
USER_MISS_TTL_S = 300.0
CHANNELS_TTL_S = 300.0
MAX_USER_LOOKUPS = 40  # users.info calls one tool call may make (Tier 4: 100 a minute)
MAX_CACHE = 5000
_ID = re.compile(r"^[CGD][A-Z0-9]{8,}$")

# Machine error codes, grouped by what the caller should do. Anything unlisted falls to failures.classify.
_AUTH = frozenset({"invalid_auth", "token_revoked", "account_inactive", "not_authed", "token_expired",
                   "not_allowed_token_type", "missing_scope"})
_NOT_FOUND = frozenset({"channel_not_found", "thread_not_found", "message_not_found", "user_not_found",
                        "not_in_channel", "users_not_found", "no_permission"})
_INVALID = frozenset({"invalid_arguments", "invalid_arg_name", "invalid_array_arg", "invalid_charset",
                      "invalid_cursor", "invalid_ts_latest", "invalid_ts_oldest", "invalid_form_data",
                      "invalid_thread_ts", "msg_too_long", "no_text", "is_archived", "channel_is_archived",
                      "restricted_action", "too_many_attachments", "invalid_limit", "missing_post_type"})
_REFRESH_CODES = frozenset({"invalid_auth", "token_expired", "not_authed"})  # worth one forced token fetch


class SlackError(Exception):
    def __init__(self, kind: FailureKind, code: str) -> None:
        super().__init__(code)
        self.kind, self.code = kind, code

    def result(self) -> ToolResult:
        if self.kind is FailureKind.AUTH:
            detail = f"Slack unauthorized ({self.code}): the Slack grant needs reconnecting"
        else:
            detail = f"Slack error {self.code}"
        return ToolResult(ok=False, error=detail, error_kind=self.kind)


def kind_for_code(code: str, status: int | None = None) -> FailureKind:
    if code == "ratelimited":
        return FailureKind.RATE_LIMITED
    if code in _AUTH:
        return FailureKind.AUTH
    if code in _NOT_FOUND:
        return FailureKind.NOT_FOUND
    if code in _INVALID:
        return FailureKind.INVALID_ARGUMENT
    if code in {"internal_error", "fatal_error", "service_unavailable", "request_timeout"}:
        return FailureKind.UNAVAILABLE
    kind, _ = classify(code, status=status)
    return kind


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def is_dm_id(channel: str) -> bool:
    return channel.startswith("D")


def conversation_kind(c: dict) -> str:
    if c.get("is_im"):
        return "dm"
    if c.get("is_mpim"):
        return "group_dm"
    if c.get("is_private") or c.get("is_group"):
        return "private"
    return "public"


class SlackExecutor:
    provider = NativeProvider.SLACK

    def __init__(
        self, tokens: TokenSource, client: httpx.AsyncClient, *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tokens, self._client, self._sleep, self._clock = tokens, client, sleep, clock
        self._users: dict[tuple[str, str], tuple[float, dict | None]] = {}
        self._channels: dict[tuple[str, int], tuple[float, list[dict]]] = {}

    def handles(self, action: str) -> bool:
        return action in ACTIONS

    async def execute(self, user: UserRef, action: str, args: dict) -> ToolResult:
        try:
            if action == "slack.channels":
                return ToolResult(ok=True, data=await self._channels_action(user.user_id))
            if action == "slack.history":
                return ToolResult(ok=True, data=await self._history_action(user.user_id, args))
            if action == "slack.send":
                return ToolResult(ok=True, data=await self._send_action(user.user_id, args))
        except SlackError as exc:
            log.warning("slack.failed", action=action, code=exc.code, kind=exc.kind.value)
            return exc.result()
        return ToolResult(ok=False, error=f"unsupported action {action}", error_kind=FailureKind.INVALID_ARGUMENT)

    # --- transport -------------------------------------------------------------------------------

    async def _api(self, user_id: int, method: str, params: dict[str, Any]) -> dict:
        """One Web API call. Retries once on AUTH (forced token fetch, covers rotation), once after a
        Retry-After of at most 10 s, and once on a 5xx or transport failure."""
        forced = waited = server_retried = False
        form = {k: ("true" if v is True else "false" if v is False else str(v))
                for k, v in params.items() if v not in (None, "")}
        while True:
            try:
                token = await self._tokens.access_token(user_id, NativeProvider.SLACK, force=forced)
            except ReauthRequired:
                raise SlackError(FailureKind.AUTH, "token_revoked") from None
            try:
                resp = await self._client.post(
                    API + method, data=form, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.TransportError:
                if server_retried:
                    raise SlackError(FailureKind.UNAVAILABLE, "network_error") from None
                server_retried = True
                await self._sleep(0.5)
                continue
            if resp.status_code == 429:
                delay = _retry_after(resp)
                if waited or delay > MAX_RETRY_AFTER_S:
                    raise SlackError(FailureKind.RATE_LIMITED, "ratelimited")
                waited = True
                await self._sleep(delay)
                continue
            if resp.status_code >= 500:
                if server_retried:
                    raise SlackError(FailureKind.UNAVAILABLE, f"http_{resp.status_code}")
                server_retried = True
                await self._sleep(0.5)
                continue
            try:
                body = resp.json()
            except ValueError:
                body = None
            if not isinstance(body, dict):
                kind = kind_for_status(resp.status_code) or FailureKind.UNAVAILABLE
                raise SlackError(kind, f"http_{resp.status_code}")
            if body.get("ok") is True:
                return body
            code = _s(body.get("error")) or "unknown_error"
            kind = kind_for_code(code, resp.status_code if resp.status_code >= 400 else None)
            if kind is FailureKind.AUTH and code in _REFRESH_CODES and not forced:
                forced = True
                continue
            if kind is FailureKind.RATE_LIMITED and not waited:
                waited = True
                await self._sleep(min(_retry_after(resp), MAX_RETRY_AFTER_S))
                continue
            raise SlackError(kind, code)

    # --- identity and names ----------------------------------------------------------------------

    async def _account(self, user_id: int) -> tuple[str, str]:
        try:
            acct = await self._tokens.account(user_id, NativeProvider.SLACK)
        except ReauthRequired:
            acct = None
        if not acct:
            raise SlackError(FailureKind.AUTH, "not_authed")
        me = next((_s(acct[k]) for k in ("slack_user_id", "authed_user_id", "user_id") if acct.get(k)), "")
        return _s(acct.get("team_id")), me

    async def user_info(self, user_id: int, team: str, slack_user: str) -> dict | None:
        """Cached users.info -> {"id","name","email"} or None. Internal helper, not a model-facing action."""
        key = (team, slack_user)
        hit = self._users.get(key)
        if hit and hit[0] > self._clock():
            return hit[1]
        info: dict | None = None
        try:
            body = await self._api(user_id, "users.info", {"user": slack_user})
            u = body.get("user") or {}
            prof = u.get("profile") or {}
            name = _s(prof.get("display_name") or u.get("real_name") or prof.get("real_name") or u.get("name"))
            info = {"id": slack_user, "name": name, "email": _s(prof.get("email")),
                    "is_bot": bool(u.get("is_bot"))}
        except SlackError as exc:
            if exc.kind is FailureKind.AUTH:
                raise
            log.info("slack.user_unresolved", code=exc.code)
        if len(self._users) >= MAX_CACHE:
            now = self._clock()
            self._users = {k: v for k, v in self._users.items() if v[0] > now}
            if len(self._users) >= MAX_CACHE:
                self._users.clear()
        self._users[key] = (self._clock() + (USER_TTL_S if info else USER_MISS_TTL_S), info)
        return info

    async def _names(self, user_id: int, team: str, ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        lookups = 0
        for uid in dict.fromkeys(i for i in ids if i):
            cached = self._users.get((team, uid))
            if not (cached and cached[0] > self._clock()):
                if lookups >= MAX_USER_LOOKUPS:
                    continue
                lookups += 1
            info = await self.user_info(user_id, team, uid)
            if info:
                out[uid] = info
        return out

    # --- channels --------------------------------------------------------------------------------

    async def list_conversations(self, user_id: int, *, members_only: bool = True) -> list[dict]:
        team, _ = await self._account(user_id)
        key = (team, int(members_only))
        hit = self._channels.get(key)
        if hit and hit[0] > self._clock():
            return hit[1]
        raw: list[dict] = []
        cursor = ""
        for _ in range(MAX_PAGES):
            body = await self._api(user_id, "conversations.list", {
                "types": CONVERSATION_TYPES, "exclude_archived": True, "limit": PAGE, "cursor": cursor,
            })
            raw.extend(c for c in body.get("channels") or [] if isinstance(c, dict))
            cursor = _s((body.get("response_metadata") or {}).get("next_cursor"))
            if not cursor:
                break
        kept = [c for c in raw if not c.get("is_archived") and (not members_only or _is_member(c))]
        dm_ids = [_s(c.get("user")) for c in kept if c.get("is_im") and c.get("user")]
        names = await self._names(user_id, team, dm_ids)
        out = []
        for c in kept:
            kind = conversation_kind(c)
            name = _s(c.get("name"))
            if kind == "dm":
                person = names.get(_s(c.get("user")))
                name = person["name"] if person and person["name"] else _s(c.get("user"))
            out.append({
                "id": _s(c.get("id")), "name": name, "kind": kind,
                "is_member": _is_member(c), "num_members": c.get("num_members"),
                "user": _s(c.get("user")) if kind == "dm" else "",
                "topic": _s((c.get("topic") or {}).get("value"))[:200],
                "updated": _num(c.get("updated")),
            })
        self._channels[key] = (self._clock() + CHANNELS_TTL_S, out)
        return out

    async def _channels_action(self, user_id: int) -> dict:
        return {"channels": await self.list_conversations(user_id)}

    async def _resolve_channel(self, user_id: int, ref: str) -> str:
        ref = ref.strip()
        if _ID.match(ref):
            return ref
        wanted = ref.lstrip("#@").strip().lower()
        if not wanted:
            raise SlackError(FailureKind.INVALID_ARGUMENT, "invalid_arguments")
        for c in await self.list_conversations(user_id):
            if c["name"].lower() == wanted:
                return c["id"]
        raise SlackError(FailureKind.NOT_FOUND, "channel_not_found")

    # --- history ---------------------------------------------------------------------------------

    async def _history_action(self, user_id: int, args: dict) -> dict:
        team, me = await self._account(user_id)
        channel = await self._resolve_channel(user_id, _s(args.get("channel")))
        limit = max(1, min(int(args.get("limit") or 20), 200))
        thread = _s(args.get("thread_ts"))
        params: dict[str, Any] = {"channel": channel, "limit": limit, "cursor": _s(args.get("cursor")),
                                  "oldest": _s(args.get("oldest")), "inclusive": False}
        if thread:
            params["ts"] = thread
            params.pop("inclusive")
            body = await self._api(user_id, "conversations.replies", params)
        else:
            body = await self._api(user_id, "conversations.history", params)
        raw = [m for m in body.get("messages") or [] if isinstance(m, dict)]
        names = await self._names(user_id, team, [_s(m.get("user")) for m in raw])
        return {
            "channel": channel,
            "messages": [message_view(m, channel, me, names) for m in raw],
            "has_more": bool(body.get("has_more")),
            "next_cursor": _s((body.get("response_metadata") or {}).get("next_cursor")),
        }

    # --- send ------------------------------------------------------------------------------------

    async def _send_action(self, user_id: int, args: dict) -> dict:
        text = _s(args.get("text"))
        if not text.strip():
            raise SlackError(FailureKind.INVALID_ARGUMENT, "no_text")
        channel = await self._resolve_channel(user_id, _s(args.get("channel")))
        body = await self._api(user_id, "chat.postMessage", {
            "channel": channel, "text": text, "thread_ts": _s(args.get("thread_ts")),
        })
        return {"ok": True, "channel": _s(body.get("channel")) or channel, "ts": _s(body.get("ts"))}


def _retry_after(resp: httpx.Response) -> float:
    try:
        return max(0.0, float(resp.headers.get("Retry-After", "1")))
    except ValueError:
        return 1.0


def _is_member(c: dict) -> bool:
    if c.get("is_im") or c.get("is_mpim"):
        return True
    if c.get("is_private") or c.get("is_group"):
        return c.get("is_member") is not False  # a user token only lists private channels the user is in
    return bool(c.get("is_member"))


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def message_view(m: dict, channel: str, me: str, names: dict[str, dict]) -> dict:
    """A Slack message in the shape normalize.normalize_slack reads (channel, ts, user, text, thread_ts),
    plus resolved names and structural flags."""
    uid = _s(m.get("user"))
    person = names.get(uid) or {}
    return {
        "channel": channel,
        "ts": _s(m.get("ts")),
        "user": uid,
        "from": person.get("name") or uid or _s(m.get("username")),
        "from_email": person.get("email", ""),
        "text": _s(m.get("text"))[:TEXT_LIMIT],
        "thread_ts": _s(m.get("thread_ts")),
        "reply_count": int(m.get("reply_count") or 0),
        "subtype": _s(m.get("subtype")),
        "bot": bool(m.get("bot_id")) or m.get("subtype") == "bot_message",
        "from_me": bool(me) and uid == me,
        "files": [_s(f.get("name")) for f in m.get("files") or [] if isinstance(f, dict)][:10],
    }

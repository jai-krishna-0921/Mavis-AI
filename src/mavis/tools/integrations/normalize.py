"""Provider data to canonical event payloads. Webhook and poller both go through here."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Any

from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, Trust

SNIPPET_LIMIT = 1000
HEADER_KEYS = frozenset({"list-unsubscribe", "from", "to", "cc", "subject", "precedence", "auto-submitted"})


_AUTH_PASS = re.compile(r"\b(?:dmarc|dkim)\s*=\s*pass\b", re.I)
# header.d=domain and header.from=domain carry a domain; header.i=@domain or user@domain an identity
_AUTH_DOMAIN = re.compile(r"\bheader\.(?:d|i|from)\s*=\s*\"?(?P<v>[^\s;\"()]+)", re.I)
TRUSTED_AUTHSERV = frozenset({"mx.google.com"})  # the receiving server whose verdict we accept


def sender_authenticated(headers: dict[str, str], from_address: str) -> bool:
    """True only if the receiving server (authserv-id in TRUSTED_AUTHSERV) recorded dmarc=pass or dkim=pass
    for the From domain (relaxed alignment: the signing domain equals the From domain or is a parent of
    it). Returns a bool: the raw header is parsed here and never stored or shown."""
    value = str(headers.get("authentication-results", "") or "")
    domain = from_address.rpartition("@")[2].strip().lower().strip(".")
    if not value or not domain:
        return False
    authserv, _, results = value.partition(";")
    tokens = authserv.split()
    if not tokens or tokens[0].lower().strip(".") not in TRUSTED_AUTHSERV:
        return False
    for clause in results.split(";"):
        if not _AUTH_PASS.search(clause):
            continue
        for m in _AUTH_DOMAIN.finditer(clause):
            d = m["v"].rpartition("@")[2].lower().strip(".")
            if d and (domain == d or domain.endswith("." + d)):
                return True
    return False


def pick(d: Any, *keys: str, default: Any = None) -> Any:
    """First non-empty value among dotted keys ('preview.body')."""
    for key in keys:
        cur: Any = d
        for part in key.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                break
        if cur not in (None, "", [], {}):
            return cur
    return default


def to_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        return to_datetime(value.get("dateTime") or value.get("date"))
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        n = float(value)
        return datetime.fromtimestamp(n / 1000 if n > 1e12 else n, UTC)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def _iso(value: Any) -> str | None:
    dt = to_datetime(value)
    return dt.isoformat() if dt else None


def _headers(d: dict) -> dict[str, str]:
    raw = pick(d, "payload.headers", "headers", default=[])
    if isinstance(raw, dict):
        return {str(k).lower(): str(v) for k, v in raw.items()}
    return {
        str(h["name"]).lower(): str(h.get("value", "")) for h in raw if isinstance(h, dict) and "name" in h
    }


def _first_auth_results(d: dict) -> dict[str, str]:
    """The topmost Authentication-Results header only: the receiving server prepends its own, so a copy
    lower down was written by the sender and must never override it (the `_headers` dict keeps the last)."""
    raw = pick(d, "payload.headers", "headers", default=[])
    if isinstance(raw, dict):
        items = [{"name": k, "value": v} for k, v in raw.items()]
    else:
        items = [h for h in raw if isinstance(h, dict) and "name" in h]
    for h in items:
        if str(h["name"]).lower() == "authentication-results":
            return {"authentication-results": str(h.get("value", ""))}
    return {}


def normalize_email(d: dict) -> dict[str, Any]:
    headers = _headers(d)
    sender = str(pick(d, "sender", "from", default=headers.get("from", "")))
    name, address = parseaddr(sender)
    labels = pick(d, "labelIds", "label_ids", "labels", default=[])
    label_list = [str(x) for x in labels] if isinstance(labels, list) else []
    return {
        "message_id": str(pick(d, "messageId", "message_id", "id", default="")),
        "thread_id": str(pick(d, "threadId", "thread_id", default="")),
        "from": sender,
        "from_name": name or address,
        "from_address": address.lower(),
        "to": str(pick(d, "to", default=headers.get("to", ""))),
        "subject": str(pick(d, "subject", "preview.subject", default=headers.get("subject", ""))),
        "snippet": str(pick(d, "messageText", "message_text", "snippet", "preview.body", default=""))[
            :SNIPPET_LIMIT
        ],
        "labels": label_list,
        "list_unsubscribe": bool(pick(d, "list_unsubscribe") or "list-unsubscribe" in headers),
        "received_at": _iso(
            pick(d, "messageTimestamp", "message_timestamp", "internalDate", "internal_date")
        ),
        "headers": {k: v for k, v in headers.items() if k in HEADER_KEYS},
        "from_me": "SENT" in label_list,
        # parsed to a bool here: the raw Authentication-Results header is never stored
        "sender_authenticated": sender_authenticated(_first_auth_results(d), address.lower()),
    }


def normalize_calendar_event(d: dict) -> dict[str, Any]:
    attendees = [
        str(a.get("email") or a.get("displayName") or "") if isinstance(a, dict) else str(a)
        for a in pick(d, "attendees", default=[]) or []
    ]
    out: dict[str, Any] = {
        "event_id": str(pick(d, "id", "event_id", "eventId", default="")),
        "summary": str(pick(d, "summary", "title", default="(no title)")),
        "start": _iso(pick(d, "start.dateTime", "start.date", "start_time", "start")),
        "end": _iso(pick(d, "end.dateTime", "end.date", "end_time", "end")),
        "attendees": [a for a in attendees if a],
        "updated": str(pick(d, "updated", "updated_at", default="")),
        "status": str(pick(d, "status", default="confirmed")),
        "description": str(pick(d, "description", default=""))[:SNIPPET_LIMIT],
    }
    out["title"] = out["summary"]
    out["starts_at"] = out["start"]
    return out


def normalize_slack(d: dict) -> dict[str, Any]:
    out = {
        "channel": str(pick(d, "channel", "channel_id", "event.channel", default="")),
        "team": str(pick(d, "team", "team_id", "event.team", "user_team", default="")),
        "subtype": str(pick(d, "subtype", "event.subtype", default="")),
        "bot_id": str(pick(d, "bot_id", "event.bot_id", "bot_profile.id", default="")),
        "user_name": str(pick(d, "user_profile.real_name", "user_profile.display_name", "user_name",
                              "event.user_profile.real_name", default="")),
        "user_email": str(pick(d, "user_profile.email", "user_email", "event.user_profile.email",
                               default="")),
        "ts": str(pick(d, "ts", "event.ts", "message_ts", default="")),
        "user": str(pick(d, "user", "user_id", "event.user", default="")),
        "text": str(pick(d, "text", "event.text", default=""))[:SNIPPET_LIMIT],
        "thread_ts": str(pick(d, "thread_ts", "event.thread_ts", default="")),
    }
    out["from"] = out["user"]
    return out


def normalize_notion(d: dict) -> dict[str, Any]:
    return {
        "page_id": str(pick(d, "id", "page_id", default="")),
        "title": str(pick(d, "title", "properties.title.title.0.plain_text", default="")),
        "last_edited": str(pick(d, "last_edited_time", "updated_at", default="")),
        "url": str(pick(d, "url", default="")),
    }


def _event(
    event_id: str, user_id: int, etype: EventType, occurred: datetime | None, source: str, payload: dict
) -> Event:
    return Event(
        id=event_id,
        user_id=user_id,
        type=etype,
        occurred_at=occurred or timeutil.now(),
        source=source,
        payload=payload,
        trust=Trust.UNTRUSTED,
    )


def email_event(user_id: int, raw: dict, source: str) -> Event | None:
    m = normalize_email(raw)
    if not m["message_id"]:
        return None
    return _event(
        f"gmail:{user_id}:msg:{m['message_id']}",
        user_id,
        EventType.EMAIL_RECEIVED,
        to_datetime(m["received_at"]),
        source,
        m,
    )


def calendar_event(user_id: int, raw: dict, source: str) -> Event | None:
    c = normalize_calendar_event(raw)
    if not c["event_id"]:
        return None
    version = c["updated"] or c["start"] or ""
    return _event(
        f"gcal:{user_id}:{c['event_id']}:{version}",
        user_id,
        EventType.CALENDAR_CHANGED,
        to_datetime(c["updated"]),
        source,
        c,
    )


def slack_event(user_id: int, raw: dict, source: str) -> Event | None:
    s = normalize_slack(raw)
    if not (s["channel"] and s["ts"]):
        return None
    event_id = f"slack:{user_id}:{s['channel']}:{s['ts']}"
    return _event(event_id, user_id, EventType.SLACK_MESSAGE, None, source, s)


def notion_event(user_id: int, raw: dict, source: str) -> Event | None:
    n = normalize_notion(raw)
    if not n["page_id"]:
        return None
    return _event(
        f"notion:{user_id}:{n['page_id']}:{n['last_edited']}",
        user_id,
        EventType.NOTION_CHANGED,
        to_datetime(n["last_edited"]),
        source,
        n,
    )


def workspace_event(user_id: int, raw: dict, source: str, *, kind: str) -> Event | None:
    """googlesuper share/comment/task triggers -> WORKSPACE_SIGNAL. The id dedupes repeats of one change."""
    if kind == "share":
        perms = [p for p in raw.get("new_permissions") or [] if isinstance(p, dict)]
        files = ",".join(sorted({str(p.get("file_id") or "") for p in perms}))
        grants = "-".join(sorted(str(p.get("permission_id") or "") for p in perms))
        key = f"{files}:{grants}" if perms else f"poll:{timeutil.now().isoformat(timespec='minutes')}"
    elif kind == "comment":
        key = str(raw.get("comment_id") or "")
    elif kind == "task":
        task = raw.get("task") if isinstance(raw.get("task"), dict) else {}
        key = f"{task.get('id', '')}:{task.get('updated', '')}" if task.get("id") else ""
    else:
        return None
    if not key:
        return None
    return _event(f"gws:{user_id}:{kind}:{key}"[:200], user_id, EventType.WORKSPACE_SIGNAL, None, source,
                  {"kind": kind, "raw": raw})


def extract_list(data: Any, *keys: str) -> list[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in keys:
            value = pick(data, key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def extract_messages(data: Any) -> list[dict]:
    return extract_list(data, "messages", "data.messages", "response_data.messages")


def extract_calendar_items(data: Any) -> list[dict]:
    return extract_list(data, "items", "events", "data.items", "response_data.items")

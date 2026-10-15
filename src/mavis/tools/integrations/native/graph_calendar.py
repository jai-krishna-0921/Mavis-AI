# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Outlook calendar over Microsoft Graph, mapped to the Google-shaped events normalize.py and renderers read.

Every call sends `Prefer: outlook.timezone="UTC"` so times come back in UTC; a response naming another
zone is still converted with that zone. All-day events become `{"date": ...}` like Google's. Event ids
carry an `ms:` prefix. Everything Graph returns is third-party data. Inviting or answering people is
approved by the caller (actions.py risk classes); the safety rule enforced here is that an organiser's
delete always tells the guests, so it is refused unless the user agreed to that.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mavis.domain.errors import FailureKind
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.google import FIND_WINDOW_BACK, MIN_FREE_MINUTES
from mavis.tools.integrations.native.graph import GRAPH, MAX_PAGES, GraphError, seg, with_ms
from mavis.tools.integrations.normalize import to_datetime

PRIMARY = "primary"
PREFER_UTC = {"Prefer": 'outlook.timezone="UTC"'}
VIEW_TOP = 100  # calendarView page size
FIND_WINDOW_AHEAD = timedelta(days=180)
FIND_RESULTS = 25
SCAN_LIMIT = 500  # events read for a local filter (find, updated_min)
DELTA_AHEAD_DAYS = 14
BASELINE_PAGES = 40
BUSY = frozenset({"busy", "oof", "tentative", "workingElsewhere"})
SELECT = (
    "id,subject,bodyPreview,start,end,isAllDay,isCancelled,showAs,location,organizer,isOrganizer,attendees,"
    "onlineMeeting,responseStatus,webLink,lastModifiedDateTime,seriesMasterId,iCalUId"
)
RESPONSES = {"accepted": "accepted", "declined": "declined", "tentativelyAccepted": "tentative",
             "organizer": "accepted"}
ACTION_FOR = {"accepted": "accept", "tentative": "tentativelyAccept", "declined": "decline"}
NOT_A_GUEST = "the user is not a guest on this event, so there is no invite to answer"
_UTC_NAMES = frozenset({"", "utc", "etc/utc", "gmt", "z", "coordinated universal time",
                        "tzone://microsoft/utc"})


def calendar_base(calendar_id: str) -> str:
    return "/me/calendar" if calendar_id == PRIMARY else f"/me/calendars/{seg(calendar_id)}"


def _events_url(calendar_id: str, event_id: str) -> str:
    base = "/me" if calendar_id == PRIMARY else f"/me/calendars/{seg(calendar_id)}"
    return f"{GRAPH}{base}/events/{seg(event_id)}"


def graph_dt(dt: datetime) -> dict[str, str]:
    """A Graph start/end: UTC wall clock and the zone name. Naive input is UTC (LocalTimes already
    localised wall-clock times upstream)."""
    aware = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return {"dateTime": aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "UTC"}


def _utc(dt: datetime) -> datetime:
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)


def _stamp(dt: datetime) -> str:
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _moment(spec: Any) -> datetime | None:
    """A Graph dateTimeTimeZone to an aware UTC datetime. The zone field is honoured: an IANA name keeps its
    DST rules; an unknown name (a Windows zone name) is read as UTC, which is what was asked for."""
    if not isinstance(spec, dict):
        return None
    naive = to_datetime(spec.get("dateTime"))
    if naive is None:
        return None
    name = str(spec.get("timeZone") or "")
    if name.lower() in _UTC_NAMES:
        return naive.astimezone(UTC) if naive.utcoffset() is not None else naive.replace(tzinfo=UTC)
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return naive.replace(tzinfo=UTC)
    return naive.replace(tzinfo=zone).astimezone(UTC)


def _day(spec: Any) -> str | None:
    """The calendar date of an all-day start or end (Graph states it as midnight of that day)."""
    raw = str(spec.get("dateTime") or "")[:10] if isinstance(spec, dict) else ""
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        return None


def _when(raw: dict, key: str) -> dict[str, str]:
    spec = raw.get(key)
    if raw.get("isAllDay"):
        day = _day(spec)
        if day:
            return {"date": day}
    moment = _moment(spec)
    return {"dateTime": moment.isoformat(), "timeZone": "UTC"} if moment else {}


def _addr(entry: Any) -> tuple[str, str]:
    who = entry.get("emailAddress") if isinstance(entry, dict) else None
    who = who if isinstance(who, dict) else {}
    return str(who.get("address") or ""), str(who.get("name") or "")


def map_event(raw: dict, me: str = "") -> dict:
    """A Graph event as a Google-shaped one. `me` is the signed-in user's address (lower case)."""
    me = me.lower()
    org_email, org_name = _addr(raw.get("organizer"))
    org_self = raw.get("isOrganizer") is True or bool(me and org_email.lower() == me)
    attendees = []
    for entry in raw.get("attendees") or []:
        email, name = _addr(entry)
        state = str((entry.get("status") or {}).get("response") or "") if isinstance(entry, dict) else ""
        attendees.append({
            "email": email, "displayName": name, "responseStatus": RESPONSES.get(state, "needsAction"),
            "self": bool(me and email.lower() == me),
            "organizer": bool(email) and email.lower() == org_email.lower(),
        })
    online = raw.get("onlineMeeting") if isinstance(raw.get("onlineMeeting"), dict) else {}
    place = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    out: dict[str, Any] = {
        "id": with_ms(str(raw.get("id") or "")),
        "summary": str(raw.get("subject") or ""),
        "start": _when(raw, "start"), "end": _when(raw, "end"),
        "attendees": attendees,
        "organizer": {"email": org_email, "self": org_self},
        "status": "cancelled" if raw.get("isCancelled") else "confirmed",
        "updated": str(raw.get("lastModifiedDateTime") or ""),
        "description": str(raw.get("bodyPreview") or ""),
        "location": str(place.get("displayName") or ""),
        "htmlLink": str(raw.get("webLink") or ""),
        "iCalUId": str(raw.get("iCalUId") or ""),
    }
    if online.get("joinUrl"):
        out["hangoutLink"] = str(online["joinUrl"])  # named like Google's so renderers show a join link
    if raw.get("seriesMasterId"):
        out["recurringEventId"] = with_ms(str(raw["seriesMasterId"]))
    return out


def _start_of(raw: dict) -> datetime | None:
    if raw.get("isAllDay"):
        day = _day(raw.get("start"))
        return datetime.combine(date.fromisoformat(day), time.min, UTC) if day else None
    return _moment(raw.get("start"))


def busy_spans(events: Iterable[dict]) -> list[tuple[datetime, datetime]]:
    """Sorted (start, end) UTC spans of Graph events that block time: not cancelled, showAs busy-like."""
    spans = []
    for raw in events:
        if raw.get("isCancelled") or str(raw.get("showAs") or "busy") not in BUSY:
            continue
        if raw.get("isAllDay"):
            first, last = _day(raw.get("start")), _day(raw.get("end"))
            if not first or not last:
                continue
            start = datetime.combine(date.fromisoformat(first), time.min, UTC)
            end = datetime.combine(date.fromisoformat(last), time.min, UTC)
        else:
            start, end = _moment(raw.get("start")), _moment(raw.get("end"))
        if start and end and end > start:
            spans.append((start, end))
    return sorted(spans)


class CalendarActions:
    def __init__(self, ex: Any) -> None:
        self.graph = ex.graph
        self._tokens = ex._tokens

    async def _me(self, uid: int) -> str:
        account = await self._tokens.account(uid, NativeProvider.MICROSOFT) or {}
        return str(account.get("mail") or account.get("upn") or "").lower()

    async def _view(
        self, uid: int, calendar_id: str, lo: datetime, hi: datetime, limit: int,
    ) -> tuple[list[dict], bool]:
        params = {"startDateTime": _stamp(lo), "endDateTime": _stamp(hi), "$top": min(limit, VIEW_TOP),
                  "$orderby": "start/dateTime", "$select": SELECT}
        items, more, _ = await self.graph.pages(
            uid, f"{GRAPH}{calendar_base(calendar_id)}/calendarView", params, limit, headers=PREFER_UTC)
        return items, bool(more)

    async def list(self, uid: int, a: Any) -> dict:
        local = a.updated_min is not None
        items, more = await self._view(uid, a.calendar_id, a.time_min, a.time_max,
                                       SCAN_LIMIT if local else a.max_results)
        notes: list[str] = []
        if local:
            since = _utc(a.updated_min)
            items = [e for e in items if (to_datetime(e.get("lastModifiedDateTime")) or since) >= since]
            notes.append("changes since the given time were picked out locally from the events in the window")
            if more:
                notes.append(f"only the first {SCAN_LIMIT} events of the window were checked for changes; "
                             "narrow the window")
            if len(items) > a.max_results:
                notes.append(f"{len(items)} changed events matched; only the first {a.max_results} are shown")
        elif more:
            notes.append("there are more events in this window than were returned; narrow the window")
        me = await self._me(uid)
        out: dict[str, Any] = {"items": [map_event(e, me) for e in items[: a.max_results]], "timeZone": "UTC"}
        if notes:
            out["notes"] = notes
        return out

    async def delta(self, uid: int, a: Any) -> dict:
        """Changes over the brief window. The window goes on the FIRST request only; a stored delta link
        carries it. An unknown link (gone) answers `reset`; a removed event reads as cancelled."""
        baseline = not a.cursor
        base = f"{GRAPH}/me/calendarView/delta"
        now = datetime.now(UTC)
        params = ({"startDateTime": _stamp(now - timedelta(days=1)),
                   "endDateTime": _stamp(now + timedelta(days=DELTA_AHEAD_DAYS))} if baseline else None)
        try:
            items, more, link = await self.graph.pages(
                uid, a.cursor or base, params, 100_000, max_pages=BASELINE_PAGES if baseline else MAX_PAGES,
                headers={"Prefer": 'odata.maxpagesize=50, outlook.timezone="UTC"'})
        except GraphError as exc:
            if a.cursor and exc.kind is FailureKind.NOT_FOUND:
                return {"items": [], "cursor": "", "baseline": False, "reset": True, "more": False}
            raise
        out: dict[str, Any] = {"items": [], "cursor": link or more or "", "baseline": baseline,
                               "reset": False, "more": bool(more and not link)}
        if baseline:
            return out
        me = await self._me(uid)
        out["items"] = [
            {"id": with_ms(str(e.get("id") or "")), "status": "cancelled", "summary": ""}
            if "@removed" in e else map_event(e, me) for e in items]
        return out

    async def find(self, uid: int, a: Any) -> dict:
        now = datetime.now(UTC)
        items, more = await self._view(
            uid, a.calendar_id, now - FIND_WINDOW_BACK, now + FIND_WINDOW_AHEAD, SCAN_LIMIT)
        needle = a.query.strip().lower()

        def hit(e: dict) -> bool:
            guests = [_addr(g) for g in e.get("attendees") or []]
            hay = [str(e.get("subject") or ""), str((e.get("location") or {}).get("displayName") or ""),
                   *(address for address, _ in guests), *(name for _, name in guests)]
            return any(needle in h.lower() for h in hay)

        me = await self._me(uid)
        notes = ["matched by title, place and guests"]
        if more:
            notes.append(f"searched only the first {len(items)} events (by start time) of the window; "
                         "events after that were not checked, so ask about a narrower period")
        return {"items": [map_event(e, me) for e in items if hit(e)][:FIND_RESULTS], "timeZone": "UTC",
                "notes": notes}

    async def free_slots(self, uid: int, a: Any) -> dict:
        lo, hi = a.time_min, a.time_max
        lo = lo if lo.tzinfo else lo.replace(tzinfo=UTC)
        hi = hi if hi.tzinfo else hi.replace(tzinfo=UTC)
        items, more = await self._view(uid, PRIMARY, lo, hi, SCAN_LIMIT)
        notes: list[str] = []
        if more:  # ordered by start: time before the last event read is complete, nothing after it is known
            seen = _start_of(items[-1]) if items else None
            hi = max(lo, min(hi, seen)) if seen else lo
            notes.append(f"the window holds more than {SCAN_LIMIT} events; free time is reported only up to "
                         f"{hi.isoformat(timespec='seconds')} and nothing after that was checked")
        spans = [(max(s, lo), min(e, hi)) for s, e in busy_spans(items)]
        spans = sorted((s, e) for s, e in spans if e > s)
        free, cursor = [], _utc(lo)
        for s, e in spans:
            if s > cursor:
                free.append((cursor, s))
            cursor = max(cursor, e)
        if cursor < _utc(hi):
            free.append((cursor, _utc(hi)))
        zone = lo.tzinfo

        def show(d: datetime) -> str:
            return d.astimezone(zone).isoformat(timespec="seconds")

        out: dict[str, Any] = {
            "time_min": show(lo), "time_max": show(hi),
            "busy": [{"start": show(s), "end": show(e)} for s, e in spans],
            "free_slots": [
                {"start": show(s), "end": show(e), "minutes": int((e - s).total_seconds() // 60)}
                for s, e in free if (e - s) >= timedelta(minutes=MIN_FREE_MINUTES)
            ],
        }
        if notes:
            out.update(partial=True, notes=notes)
        return out

    async def create(self, uid: int, a: Any) -> dict:
        start = _utc(a.start)  # a length is elapsed time: add it in UTC, not on a zone's wall clock
        body: dict[str, Any] = {
            "subject": a.summary, "start": graph_dt(start),
            "end": graph_dt(start + timedelta(minutes=a.duration_minutes)),
        }
        if a.description:
            body["body"] = {"contentType": "text", "content": a.description}
        if a.location:
            body["location"] = {"displayName": a.location}
        if a.attendees:  # Graph sends the invites itself
            body["attendees"] = [{"emailAddress": {"address": e}, "type": "required"} for e in a.attendees]
        made = await self.graph.json(uid, "POST", f"{GRAPH}/me/events", body=body, headers=PREFER_UTC,
                                     idempotent=False)
        return map_event(made, await self._me(uid))

    async def update(self, uid: int, a: Any) -> dict:
        url = _events_url(PRIMARY, a.event_id)
        body: dict[str, Any] = {}
        if a.summary is not None:
            body["subject"] = a.summary
        if a.start is not None and a.duration_minutes is not None:
            start = _utc(a.start)
            body["start"] = graph_dt(start)
            body["end"] = graph_dt(start + timedelta(minutes=a.duration_minutes))
        if a.description is not None:
            body["body"] = {"contentType": "text", "content": a.description}
        if a.location is not None:
            body["location"] = {"displayName": a.location}
        if a.attendees is not None:
            current = await self.graph.json(
                uid, "GET", url, params={"$select": "attendees"}, headers=PREFER_UTC)
            existing = {_addr(g)[0].lower(): g for g in current.get("attendees") or [] if isinstance(g, dict)}
            body["attendees"] = [
                existing.get(e.lower()) or {"emailAddress": {"address": e}, "type": "required"}
                for e in a.attendees]
        # Changing guests makes Graph send updates: a repeat after an unknown outcome could tell them twice.
        done = await self.graph.json(uid, "PATCH", url, body=body, headers=PREFER_UTC,
                                     idempotent="attendees" not in body)
        return map_event(done, await self._me(uid))

    async def calendars(self, uid: int, a: Any) -> dict:
        me = await self._me(uid)
        items, _, _ = await self.graph.pages(uid, f"{GRAPH}/me/calendars", {"$top": VIEW_TOP}, 250)
        out = []
        for c in items:
            owner = _addr({"emailAddress": c.get("owner")})[0].lower()
            role = ("owner" if c.get("isDefaultCalendar") or (me and owner == me)
                    else "writer" if c.get("canEdit") else "reader")
            out.append({"id": with_ms(str(c.get("id") or "")), "summary": str(c.get("name") or ""),
                        "primary": bool(c.get("isDefaultCalendar")), "accessRole": role, "timeZone": ""})
        return {"calendars": out}

    async def get(self, uid: int, a: Any) -> dict:
        raw = await self.graph.json(uid, "GET", _events_url(a.calendar_id, a.event_id), headers=PREFER_UTC)
        return map_event(raw, await self._me(uid))

    async def delete(self, uid: int, a: Any) -> dict:
        url = _events_url(a.calendar_id, a.event_id)
        raw = await self.graph.json(
            uid, "GET", url, headers=PREFER_UTC,
            params={"$select": "id,isOrganizer,organizer,attendees,isCancelled"})
        event = map_event(raw, await self._me(uid))
        guests = [g for g in event["attendees"] if not g["self"]]
        tells_guests = bool(guests) and event["organizer"]["self"] and event["status"] != "cancelled"
        if tells_guests and not a.notify_guests:
            raise GraphError(
                FailureKind.INVALID_ARGUMENT,
                "Outlook always tells guests when the organiser removes a meeting; ask the user and retry "
                "with notify_guests true", "notify_guests")
        if tells_guests:
            await self.graph.json(uid, "POST", f"{url}/cancel", body={"comment": ""}, idempotent=False)
        else:  # nobody to tell, or only the user's copy of someone else's invite: a repeat is harmless
            await self.graph.json(uid, "DELETE", url, idempotent=True)
        return {"id": a.event_id, "deleted": True, "guestsNotified": tells_guests}

    async def respond(self, uid: int, a: Any) -> dict:
        url = f"{_events_url(a.calendar_id, a.event_id)}/{ACTION_FOR[a.response]}"
        try:  # the organiser is told, so a repeat after an unknown outcome could tell them twice
            await self.graph.json(uid, "POST", url, body={"comment": a.comment, "sendResponse": True},
                                  idempotent=False)
        except GraphError as exc:
            if exc.kind is FailureKind.INVALID_ARGUMENT and "ErrorInvalidRequest" in exc.detail:
                raise GraphError(FailureKind.INVALID_ARGUMENT, NOT_A_GUEST, "event_id") from None
            raise
        return {"id": a.event_id, "responseStatus": a.response}


def calendar_handlers(ex: Any) -> dict[str, Callable[[int, Any], Awaitable[Any]]]:
    c = CalendarActions(ex)
    return {"calendar.delta": c.delta,
            "calendar.list": c.list, "calendar.find": c.find, "calendar.free_slots": c.free_slots,
            "calendar.create_event": c.create, "calendar.update_event": c.update,
            "calendar.calendars": c.calendars, "calendar.get": c.get, "calendar.delete_event": c.delete,
            "calendar.respond": c.respond}

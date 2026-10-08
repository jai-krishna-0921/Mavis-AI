"""/settings: show the user's preferences and let them change the time zone, apart from onboarding. It never
touches the onboarding state, never asks the first-value question, and never changes the currency."""

from __future__ import annotations

from mavis.access import preferences
from mavis.access.commands import register_user_command
from mavis.access.tz_resolve import from_city, from_location, from_text_llm
from mavis.agents.buttons import register_button_handler
from mavis.domain.events import Event, EventType
from mavis.domain.messages import Button, Outbound
from mavis.store.db import utcnow
from mavis.store.models import User
from mavis.store.repo import outbox, users
from mavis.worker.gates import register_event_gate

PREFIX = "st:"
KEY = "settings"  # users.state["settings"] = {"await": "place" | None, "candidates": [...], "tries": n}
MAX_GUESSES = 3
ASK = "Tell me your city, or share your location."
PICK = "Which one is yours?"


async def _say(user_id: int, text: str, key: str, buttons: list[list[Button]] | None = None) -> None:
    await outbox.enqueue_now(Outbound(user_id=user_id, text=text, buttons=buttons or [],
                                      dedupe_key=f"settings:{user_id}:{key}"))


async def _state(user_id: int, **patch) -> dict:
    return await users.update_nested(user_id, KEY, patch)


async def settings_command(event: Event, user: User, args: list[str]) -> str | None:
    await _say(user.id, "Want to change your time zone?", f"offer:{event.id}",
               [[Button(label="Change time zone", data="st:tz")]])
    return (f"Time zone: {user.timezone}\nCurrency: {user.currency or '(default)'}\n"
            f"Name: {user.name or '(none)'}\nSay 'use EUR' or 'call me Sam' to change the others.")


async def _apply(user_id: int, zone: str) -> None:
    change = await preferences.set_timezone(user_id, zone)
    await _state(user_id, **{"await": None, "candidates": [], "tries": 0})
    await _say(user_id, f"Done, your time zone is {change.new}.", f"done:{utcnow().isoformat()}")


async def on_button(event: Event, data: str) -> None:
    uid = event.user_id
    if data == "st:tz":
        await _state(uid, **{"await": "place", "candidates": [], "tries": 0})
        await _say(uid, ASK, f"ask:{event.id}")
    elif data.startswith("st:pick:") and data[8:].isdigit():
        st = (await users.get_state(uid)).get(KEY) or {}
        cands, idx = st.get("candidates") or [], int(data[8:])
        if st.get("await") == "place" and idx < len(cands):
            await _apply(uid, cands[idx])


async def settings_gate(event: Event) -> bool:
    """Event gate (order 31): while the user changes their time zone, a place or a location is the answer."""
    if event.type is not EventType.USER_MESSAGE:
        return True
    user = await users.get(event.user_id)
    st = (user.state or {}).get(KEY) or {}
    if st.get("await") != "place":
        return True
    text = str(event.payload.get("text", ""))
    if text.startswith("/"):
        await _state(user.id, **{"await": None})  # a command ends the question; it runs as usual
        return True
    if (loc := event.payload.get("location")) and isinstance(loc, dict):
        if zone := from_location(float(loc["latitude"]), float(loc["longitude"])):
            await _apply(user.id, zone)
        return False
    matches = from_city(text)
    tries = int(st.get("tries", 0))
    if len(matches) == 1:
        await _apply(user.id, matches[0].zone)
    elif matches:
        await _state(user.id, candidates=[m.zone for m in matches])
        rows = [[Button(label=f"{m.name}, {m.country} ({m.zone.split('/')[-1].replace('_', ' ')})",
                        data=f"st:pick:{i}")] for i, m in enumerate(matches)]
        await _say(user.id, PICK, f"pick:{event.id}", rows)
    elif tries < MAX_GUESSES and (zone := await from_text_llm(text)):
        await _apply(user.id, zone)
    else:
        await _state(user.id, tries=tries + 1)
        await _say(user.id, ASK, f"ask_again:{event.id}")
    return False


def register() -> None:
    register_user_command("settings", settings_command)
    register_button_handler(PREFIX, on_button)
    register_event_gate("settings", settings_gate, order=31)

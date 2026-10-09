"""Onboarding a newly invited user (spec 5): at most three questions, buttons where possible, first value
inside two minutes. State lives in users.state["onboarding"] so a restart resumes."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from mavis.access import gate, preferences
from mavis.access.currency import for_zone
from mavis.access.tz_resolve import from_city, from_location, from_text_llm
from mavis.agents import settings_flow
from mavis.agents.buttons import register_button_handler
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.events import Event, EventType
from mavis.domain.messages import Button, Outbound
from mavis.domain.policy import Capability
from mavis.domain.wakeups import WakeupKind
from mavis.store.db import utcnow
from mavis.store.models import InviteCode, User
from mavis.store.repo import outbox, users
from mavis.timers.service import WakeupService

PREFIX = "ob:"
MAX_PLACE_GUESSES = 3  # model-assisted place guesses per user while onboarding
CITY_TABLE: Path | None = None
WELCOME = ("Hi {name}, I'm Mavis, your personal assistant on Telegram. I remember things, remind you, and "
           "can watch your email and calendar if you want.")
CLOCK = "Quick check: is it {clock} where you are?"
ASK_PLACE = "Tell me your city, or share your location."
PICK = "Which one is yours?"
CURRENCY = "I'll show money in {money}. Say 'use USD' any time to change it."
FIRST_VALUE = "What's one thing you want off your mind this week? A bill, a call, a deadline."
CONNECT = "Want me to keep an eye on your Gmail and calendar too?"
CONNECT_WITH_SLACK = "Want me to keep an eye on your email, calendar and Slack too?"


async def _say(user_id: int, text: str, key: str, buttons: list[list[Button]] | None = None) -> None:
    await outbox.enqueue_now(Outbound(user_id=user_id, text=text, buttons=buttons or [],
                                      dedupe_key=f"onboarding:{user_id}:{key}"))


async def _state(user_id: int, **patch) -> dict:
    return await users.update_nested(user_id, "onboarding", patch)


async def start(user: User, invite: InviteCode | None) -> None:
    s = get_settings()
    first = (user.name or "there").split()[0]
    await _state(user.id, step="clock", started_at=utcnow().isoformat(), tz_confirmed=False)
    await _say(user.id, WELCOME.format(name=first), "welcome")
    local = timeutil.to_local(utcnow(), user.timezone or s.default_timezone)
    clock = local.strftime("%I:%M %p %A").lstrip("0")
    await _say(user.id, CLOCK.format(clock=clock), "clock",
               [[Button(label="Yes", data="ob:tz:yes"), Button(label="No", data="ob:tz:no")]])
    if user.currency is None and (code := for_zone(user.timezone)):
        await preferences.set_currency(user.id, code)  # a guess from the unconfirmed zone: redone on confirm
        await _state(user.id, currency_guess=True)


async def _after_zone(user_id: int) -> None:
    user = await users.get(user_id)
    guessed = ((user.state or {}).get("onboarding") or {}).get("currency_guess", False)
    kept = None if guessed else user.currency
    code = kept or for_zone(user.timezone) or get_settings().attention_currency
    if user.currency != code:
        await preferences.set_currency(user_id, code)
    await _state(user_id, currency_guess=False)
    await _state(user_id, step="first_value", tz_confirmed=True)
    from mavis.access.currency import symbol

    await _say(user_id, CURRENCY.format(money=f"{symbol(code)} {code}".strip()), "currency")
    await _say(user_id, FIRST_VALUE, "first_value")


async def on_button(event: Event, data: str) -> None:
    uid = event.user_id
    if data.startswith("ob:tz:"):
        step = ((await users.get_state(uid)).get("onboarding") or {}).get("step")
        if step not in ("clock", "place"):
            return  # a stale button from an earlier message: onboarding is over, nothing to redo
    if data == "ob:tz:yes":
        await _after_zone(uid)
    elif data == "ob:tz:no":
        await _state(uid, step="place")
        await _say(uid, ASK_PLACE, "ask_place")  # the channel adds a location reply keyboard (Task 17)
    elif data.startswith("ob:tz:") and data[6:].isdigit():
        cands = (await users.get_state(uid)).get("onboarding", {}).get("candidates", [])
        idx = int(data[6:])
        if idx < len(cands):
            await preferences.set_timezone(uid, cands[idx])
            await _after_zone(uid)
    elif data == "ob:conn:google":
        from mavis.agents.commands import _flow
        from mavis.tools.integrations.actions import GOOGLE_ANCHOR

        await _flow(None).start(uid, GOOGLE_ANCHOR, "")
    elif data == "ob:conn:slack":
        from mavis.agents.commands import _flow

        await _flow(None).start(uid, Capability.SLACK, "")
    elif data == "ob:conn:later":
        await _state(uid, connect_declined_at=utcnow().isoformat())


async def on_text(user: User, text: str) -> bool:
    """A reply while onboarding waits for a place. True when it was consumed."""
    st = (await users.get_state(user.id)).get("onboarding", {})
    if st.get("step") != "place":
        return False
    matches = from_city(text, table=CITY_TABLE)
    if len(matches) == 1:
        await preferences.set_timezone(user.id, matches[0].zone)
        await _after_zone(user.id)
    elif matches:
        await _state(user.id, candidates=[m.zone for m in matches])
        rows = [[Button(label=f"{m.name}, {m.country} ({m.zone.split('/')[-1].replace('_', ' ')})",
                        data=f"ob:tz:{i}")] for i, m in enumerate(matches)]
        await _say(user.id, PICK, f"pick:{len(matches)}", rows)
    elif (tries := int(st.get("place_tries", 0))) < MAX_PLACE_GUESSES and (zone := await from_text_llm(text)):
        await _state(user.id, place_tries=tries + 1)
        await preferences.set_timezone(user.id, zone)
        await _after_zone(user.id)
    else:
        # bounded: after a few misses the model is not asked again, only the city name or a location works
        await _state(user.id, place_tries=int(st.get("place_tries", 0)) + 1)
        await _say(user.id, ASK_PLACE, f"ask_place_again:{utcnow():%H%M}")
    return True


async def on_location(user: User, lat: float, lon: float) -> None:
    if zone := from_location(lat, lon):
        await preferences.set_timezone(user.id, zone)
        await _after_zone(user.id)


async def after_first_value(user_id: int, reason: str = "") -> None:
    """The connector offer, a minute after the first chat turn that followed onboarding."""
    st = (await users.get_state(user_id)).get("onboarding", {})
    if st.get("step") != "first_turn":
        return
    await _state(user_id, step="done")
    from mavis.tools.integrations.native.base import NativeProvider
    from mavis.tools.integrations.native.oauth import configured

    slack = configured(NativeProvider.SLACK)  # offered only where the Slack sign-in is set up
    row = [Button(label="Connect Google", data="ob:conn:google"),
           *([Button(label="Connect Slack", data="ob:conn:slack")] if slack else []),
           Button(label="Later", data="ob:conn:later")]
    await _say(user_id, CONNECT_WITH_SLACK if slack else CONNECT, "connect", [row])


async def onboarding_gate(event: Event) -> bool:
    """Event gate (order 30): place answers and shared locations while onboarding are not chat turns, and the
    first chat turn after the questions books the connector offer for a minute later."""
    if event.type is not EventType.USER_MESSAGE:
        return True
    user = await users.get(event.user_id)
    st = (user.state or {}).get("onboarding") or {}
    step = st.get("step")
    if step not in ("clock", "place", "first_value"):
        return True
    if step == "first_value":
        if not str(event.payload.get("text", "")).startswith("/"):
            await _state(user.id, step="first_turn")
            await WakeupService().wake_me(user.id, utcnow() + timedelta(seconds=60), "onboarding",
                                          kind=WakeupKind.SYSTEM_ONBOARD_CONNECT, scale=False,
                                          dedupe_key=f"onboarding_connect:{user.id}")
        return True
    if (loc := event.payload.get("location")) and isinstance(loc, dict):
        await on_location(user, float(loc["latitude"]), float(loc["longitude"]))
        return False
    return not await on_text(user, str(event.payload.get("text", "")))


ALREADY_IN = ("You're already in, {name}. Tell me what's on your mind, or send /connect to link your Google "
              "or Slack.")


async def start_command(event: Event, user: User, args: list[str]) -> str:
    """/start from someone who is already in (the invite link tapped again, or Start pressed twice). The
    access gate has dealt with pending people and the web login gate with sign-in links, so what reaches
    here is never a chat turn for the model."""
    return ALREADY_IN.format(name=(user.name or "there").split()[0])


def register() -> None:
    from mavis.access.commands import register_user_command

    from mavis.timers.system import register_system_wakeup
    from mavis.worker.gates import register_event_gate

    if start not in gate.on_activated:
        gate.on_activated.append(start)
    register_user_command("start", start_command)
    register_button_handler(PREFIX, on_button)
    register_event_gate("onboarding", onboarding_gate, order=30)
    register_system_wakeup(WakeupKind.SYSTEM_ONBOARD_CONNECT.value, after_first_value)
    settings_flow.register()

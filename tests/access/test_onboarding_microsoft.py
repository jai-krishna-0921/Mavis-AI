from mavis.agents import onboarding
from mavis.domain.events import Event, EventType, Trust
from mavis.store.db import utcnow

from .test_onboarding import _activate, _btn, _texts


async def _offer(channel, chat):
    u, _ = await _activate(chat, "Noor", "Europe/Lisbon", None)
    await onboarding.on_button(_btn(u.id, "ob:tz:yes"), "ob:tz:yes")
    await onboarding.onboarding_gate(Event(id=f"m:{chat}", user_id=u.id, type=EventType.USER_MESSAGE,
                                           occurred_at=utcnow(), source="telegram", payload={"text": "hello"},
                                           trust=Trust.USER))
    await onboarding.after_first_value(u.id)
    return u, await _texts(channel)


def _env(monkeypatch, **kv):
    from mavis.config import get_settings
    for k, v in {"NATIVE_TOKEN_KEK": "k" * 44, **kv}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


async def test_microsoft_offer(db, channel, monkeypatch):
    _env(monkeypatch, MICROSOFT_CLIENT_ID="mid", MICROSOFT_CLIENT_SECRET="ms")
    _, texts = await _offer(channel, 6501)
    assert texts[-1] == "Want me to keep an eye on your email, calendar and Microsoft 365 too?"
    assert [b.label for row in channel.sent[-1].buttons for b in row] == [
        "Connect Google", "Connect Microsoft 365", "Later"]


async def test_microsoft_and_slack_offer(db, channel, monkeypatch):
    _env(monkeypatch, MICROSOFT_CLIENT_ID="mid", MICROSOFT_CLIENT_SECRET="ms", SLACK_CLIENT_ID="s",
         SLACK_CLIENT_SECRET="s")
    _, texts = await _offer(channel, 6502)
    assert texts[-1] == "Want me to keep an eye on your email, calendar, Slack and Microsoft 365 too?"
    assert [b.label for row in channel.sent[-1].buttons for b in row] == [
        "Connect Google", "Connect Microsoft 365", "Connect Slack", "Later"]


async def test_microsoft_off_keeps_the_old_offer(db, channel):
    _, texts = await _offer(channel, 6503)
    assert texts[-1] == onboarding.CONNECT


async def test_pressing_connect_microsoft_starts_the_flow(db, channel, monkeypatch):
    started = []

    class Flow:
        async def start(self, uid, capability, reason, **kw):
            started.append((uid, capability))
    from mavis.agents import commands
    from mavis.domain.policy import Capability
    monkeypatch.setattr(commands, "_flow", lambda _=None: Flow())
    u, _ = await _activate(6504, "Noor", "Europe/Lisbon", None)
    await onboarding.on_button(_btn(u.id, "ob:conn:microsoft"), "ob:conn:microsoft")
    assert started == [(u.id, Capability.OUTLOOK)]

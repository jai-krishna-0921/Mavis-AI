"""Event gate (order 15, after the access gate): `/start login_<nonce>` from an admitted chat signs the
waiting browser in. A new chat in invite mode is handled by the access gate, which redeems the invite carried
in the link first and then calls the same `logins.complete`."""

from __future__ import annotations

from mavis.domain.events import Event, EventType
from mavis.web import logins


async def web_login_gate(event: Event) -> bool:
    if event.type is not EventType.USER_MESSAGE:
        return True
    login = logins.parse_start(str(event.payload.get("text", "")))
    if login is None:
        return True
    await logins.complete(event.user_id, login, event.id)
    return False


def register() -> None:
    from mavis.worker.gates import register_event_gate

    register_event_gate("web_login", web_login_gate, order=15)

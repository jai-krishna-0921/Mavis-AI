"""Who may be served right now, as one pure rule shared by every entry point that is not the Telegram gate.

The access gate (access.gate) decides per Telegram event and has side effects (invite codes, replies). The
Slack inbound lookup and the OAuth connect flows must apply the SAME decision without those side effects, so
a banned, pending, deleting or deleted user can neither chat through Slack nor attach a Google or Slack
account. tests/access/test_admission.py keeps this function and the gate in agreement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mavis.access import UserStatus
from mavis.config import Settings, get_settings

if TYPE_CHECKING:
    from mavis.store.models import User

# Never served, in any access mode: the account is being or has been erased, or the owner paused it.
_REFUSED = frozenset({UserStatus.BANNED, UserStatus.DELETING, UserStatus.DELETED})


def is_owner(user: User, s: Settings | None = None) -> bool:
    s = s or get_settings()
    return user.telegram_chat_id is not None and user.telegram_chat_id in s.owner_telegram_chat_ids


def admitted(user: User | None, s: Settings | None = None) -> bool:
    """True when this user may be served. Rules (mirroring access.gate.access_gate):
    - no row, or an account that is deleting or deleted: never;
    - the owner (a chat in OWNER_TELEGRAM_CHAT_IDS) is grandfathered as active, even if the row says banned;
    - ACCESS_MODE allowlist and shadow do not enforce status (pending users already exist only for chats the
      old allowlist let in), but a banned user is still refused outside the owner;
    - ACCESS_MODE invite: only an active user."""
    if user is None:
        return False
    s = s or get_settings()
    status = UserStatus(user.status)
    if status in (UserStatus.DELETING, UserStatus.DELETED):
        return False
    from mavis.channels.test_sink import is_test_chat  # lazy: the channels package imports a lot

    if is_owner(user, s) or is_test_chat(user.telegram_chat_id, s):
        return True
    if status is UserStatus.BANNED:
        return False
    if s.access_mode in ("allowlist", "shadow"):
        return True
    return status is UserStatus.ACTIVE


async def admitted_id(user_id: int | None) -> bool:
    if user_id is None:
        return False
    from mavis.store.repo import users

    try:
        return admitted(await users.get(user_id))
    except Exception:  # noqa: BLE001 - an unreadable user row is not an admitted one
        return False

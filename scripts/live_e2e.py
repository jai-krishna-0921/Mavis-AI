"""Live E2E harness: post Telegram updates to the local webhook as the TEST user and print Mavis's replies.

    LIVE_TEST_ENABLED=true TEST_TELEGRAM_CHAT_ID=-1000000000000001 \
        uv run python -m scripts.live_e2e "remind me to call Ravi at 6"

It only ever speaks as TEST_TELEGRAM_CHAT_ID (the running stack must have the same settings, so the chat
is admitted and every send to it goes to data_dir/test_sink.jsonl instead of Telegram). The id
must be synthetic (below -10**15, which no Telegram chat can have) and not in ALLOWED_TELEGRAM_CHAT_IDS.
Replies are read from that user's rows only. Never clean up by editing rows: use the product's own
tools as the test user.
"""

from __future__ import annotations

import asyncio
import sys
import time

import httpx
from sqlalchemy import select

from mavis.channels.test_sink import SYNTHETIC_BELOW, active_test_chat
from mavis.config import Settings, get_settings
from mavis.store.db import Session
from mavis.store.models import Message, User

WEBHOOK = "http://localhost:8000/telegram/webhook"


class HarnessError(RuntimeError):
    pass


def target_chat(s: Settings) -> int:
    """The same rule the stack applies (channels.test_sink.active_test_chat): enabled, synthetic, not
    allowlisted. Anything else is refused, so the harness never speaks as a real user."""
    chat = active_test_chat(s)
    if chat is None:
        raise HarnessError("set LIVE_TEST_ENABLED=true and a synthetic TEST_TELEGRAM_CHAT_ID below "
                           f"{SYNTHETIC_BELOW} that is not in ALLOWED_TELEGRAM_CHAT_IDS")
    return chat


async def _user_id(chat: int) -> int | None:
    async with Session() as s:
        return await s.scalar(select(User.id).where(User.telegram_chat_id == chat))


async def _messages_after(user_id: int | None, after: int) -> list[Message]:
    if user_id is None:
        return []
    async with Session() as s:
        rows = await s.scalars(select(Message).where(Message.user_id == user_id, Message.id > after)
                               .order_by(Message.id))
        return list(rows)


async def _last_id(user_id: int | None) -> int:
    rows = await _messages_after(user_id, 0)
    return rows[-1].id if rows else 0


async def _send(chat: int, n: int, text: str, secret: str) -> int:
    now = int(time.time())
    update = {"update_id": 900_000_000 + now % 1_000_000 + n,
              "message": {"message_id": 900_000 + n, "date": now, "text": text,
                          "chat": {"id": chat, "type": "private"},
                          "from": {"id": chat, "is_bot": False, "first_name": "Test"}}}
    async with httpx.AsyncClient() as c:
        headers = {"X-Telegram-Bot-Api-Secret-Token": secret}
        r = await c.post(WEBHOOK, json=update, headers=headers, timeout=30)
        return r.status_code


async def main(texts: list[str]) -> None:
    s = get_settings()
    chat = target_chat(s)
    for n, text in enumerate(texts):
        user_id = await _user_id(chat)
        start, t0 = await _last_id(user_id), time.time()
        print(f">>> {text}  (webhook {await _send(chat, n, text, s.telegram_webhook_secret)})", flush=True)
        seen = quiet = 0
        while time.time() - t0 < 90:
            await asyncio.sleep(2)
            user_id = user_id or await _user_id(chat)  # created by the first update
            out = [m for m in await _messages_after(user_id, start) if m.role != "user"]
            if len(out) > seen:
                for m in out[seen:]:
                    print(f"[{time.time() - t0:5.1f}s] {m.content}\n", flush=True)
                seen, quiet = len(out), 0
            elif seen:
                quiet += 1
                if quiet >= 5:
                    break


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))

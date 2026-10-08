"""A log sink for the designated test chat (LIVE_TEST_ENABLED + TEST_TELEGRAM_CHAT_ID).

The live E2E harness talks to the real stack as its own user (own rows, own loops, own pings), and
nothing it triggers is sent through Telegram: every send to the test chat is logged and appended to
`<data_dir>/test_sink.jsonl` instead. Every other chat passes through to the real channel.

The test id must be synthetic, an id no Telegram chat can have, so admitting it never admits a person:
private chats (users) are positive, groups are negative with at most 12 digits, and supergroups and
channels are "-100" followed by the channel id (about 13 digits today). Anything below SYNTHETIC_BELOW
(-10**15, 16 digits, three orders of magnitude of headroom, still inside a signed 64-bit column) cannot
come from Telegram, so updates with it can only arrive on the webhook route, which checks the webhook
secret first. The test path is off unless explicitly enabled, the id is synthetic, and it is not in
ALLOWED_TELEGRAM_CHAT_IDS; otherwise an error is logged and the path stays off.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from mavis.channels.base import Channel
from mavis.config import Settings, get_settings
from mavis.domain import timeutil
from mavis.domain.messages import Button

log = structlog.get_logger()
SINK_FILE = "test_sink.jsonl"
SYNTHETIC_BELOW = -(10**15)
_warned: set[tuple] = set()
FIXTURE_DIR = Path(__file__).resolve().parents[3] / "scripts" / "fixtures" / "machine" / "files"
FIXTURE_PREFIX = "fixture:"
MIRROR_HEADER = "[test]"


def active_test_chat(s: Settings | None = None) -> int | None:
    """The live test chat id when the path is enabled and safe, else None (logged once if misconfigured)."""
    s = s or get_settings()
    chat = s.test_telegram_chat_id
    if not s.live_test_enabled or chat is None:
        return None
    if chat in s.allowed_telegram_chat_ids:
        reason = "the test chat is in ALLOWED_TELEGRAM_CHAT_IDS (a real user's replies would be swallowed)"
    elif chat >= SYNTHETIC_BELOW:
        reason = f"the test chat id must be below {SYNTHETIC_BELOW} so it can never be a real Telegram chat"
    else:
        return chat
    if (chat, reason) not in _warned:
        _warned.add((chat, reason))
        log.error("channel.test_chat_rejected", reason=reason)
    return None


def _mirror_chat(s: Settings) -> int | None:
    """The owner's chat to mirror into: set, allowlisted, and never the test chat itself."""
    chat = s.test_mirror_chat_id
    if chat is None or chat not in s.allowed_telegram_chat_ids:
        return None
    return chat


def sink_path(data_dir: Path) -> Path:
    return Path(data_dir) / SINK_FILE


def read_sink(data_dir: Path) -> list[dict[str, Any]]:
    path = sink_path(data_dir)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class SinkChannel:
    def __init__(self, inner: Channel, test_chat_id: int) -> None:
        self._inner, self._test = inner, test_chat_id
        self._next_id = 1
        self._mirror_ids: dict[int, int] = {}  # sink message id -> mirrored message id

    def _is_test(self, chat_id: int) -> bool:
        """Shared contract E: the single place that decides whether a chat is the sink's."""
        return chat_id == self._test

    def _record(self, kind: str, chat_id: int, text: str, **extra: Any) -> int:
        row = {"at": timeutil.now().isoformat(), "kind": kind, "chat_id": chat_id, "text": text, **extra}
        log.info("channel.test_sink", kind=kind, chat_id=chat_id, text=text[:200])
        path = sink_path(get_settings().data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._next_id += 1
        return -self._next_id  # never a real Telegram message id

    async def _mirror_send(self, sink_id: int, text: str) -> None:
        """Copy a test-chat message into the owner's chat with the [test] header and no buttons."""
        chat = _mirror_chat(get_settings())
        if chat is None:
            return
        try:
            ids = await self._inner.send_text(chat, f"{MIRROR_HEADER} {text}")
            if ids:
                self._mirror_ids[sink_id] = ids[-1]
        except Exception as exc:  # noqa: BLE001 - the mirror is cosmetic; the sink row is the record
            log.warning("channel.test_mirror_failed", error=type(exc).__name__)

    async def send_text(self, chat_id: int, text: str,
                        buttons: list[list[Button]] | None = None) -> list[int]:
        if not self._is_test(chat_id):
            return await self._inner.send_text(chat_id, text, buttons)
        labels = [[b.label for b in row] for row in (buttons or [])]
        sink_id = self._record("text", chat_id, text, buttons=labels)
        await self._mirror_send(sink_id, text)
        return [sink_id]

    async def send_document(self, chat_id: int, path: str, caption: str = "") -> int:
        if not self._is_test(chat_id):
            return await self._inner.send_document(chat_id, path, caption)
        sink_id = self._record("document", chat_id, caption, path=path)
        if (chat := _mirror_chat(get_settings())) is not None:
            try:
                await self._inner.send_document(chat, path, f"{MIRROR_HEADER} {caption}".strip())
            except Exception as exc:  # noqa: BLE001
                log.warning("channel.test_mirror_failed", error=type(exc).__name__)
        return sink_id

    async def edit_text(self, chat_id: int, message_id: int, text: str,
                        buttons: list[list[Button]] | None = None) -> None:
        if not self._is_test(chat_id):
            await self._inner.edit_text(chat_id, message_id, text, buttons)
            return
        labels = [[b.label for b in row] for row in (buttons or [])]
        self._record("edit", chat_id, text, message_id=message_id, buttons=labels)
        chat, mirrored = _mirror_chat(get_settings()), self._mirror_ids.get(message_id)
        if chat is not None and mirrored is not None:
            try:
                await self._inner.edit_text(chat, mirrored, f"{MIRROR_HEADER} {text}")
            except Exception as exc:  # noqa: BLE001
                log.warning("channel.test_mirror_failed", error=type(exc).__name__)

    async def send_photo(self, chat_id: int, path: str, caption: str = "") -> int:
        if not self._is_test(chat_id):
            return await self._inner.send_photo(chat_id, path, caption)
        sink_id = self._record("photo", chat_id, caption, path=path)
        if (chat := _mirror_chat(get_settings())) is not None:
            try:
                await self._inner.send_photo(chat, path, f"{MIRROR_HEADER} {caption}".strip())
            except Exception as exc:  # noqa: BLE001
                log.warning("channel.test_mirror_failed", error=type(exc).__name__)
        return sink_id

    async def send_media_group(self, chat_id: int, paths: list[str],
                               captions: list[str] | None = None) -> list[int]:
        if not self._is_test(chat_id):
            return await self._inner.send_media_group(chat_id, paths, captions)
        caps = list(captions or [""] * len(paths))
        sink_id = self._record("album", chat_id, " | ".join(caps), paths=list(paths))
        if (chat := _mirror_chat(get_settings())) is not None:
            try:
                mirrored = [f"{MIRROR_HEADER} {c}".strip() for c in caps]
                await self._inner.send_media_group(chat, paths, mirrored)
            except Exception as exc:  # noqa: BLE001
                log.warning("channel.test_mirror_failed", error=type(exc).__name__)
        return [sink_id]

    async def send_typing(self, chat_id: int) -> None:
        if not self._is_test(chat_id):
            await self._inner.send_typing(chat_id)

    async def react(self, chat_id: int, message_id: int, emoji: str) -> None:
        if not self._is_test(chat_id):
            await self._inner.react(chat_id, message_id, emoji)

    async def download_file(self, file_id: str, dest_path: str) -> str:
        if not file_id.startswith(FIXTURE_PREFIX):
            return await self._inner.download_file(file_id, dest_path)
        name = file_id[len(FIXTURE_PREFIX):]
        root = FIXTURE_DIR.resolve()
        src = (root / name).resolve()
        if name != Path(name).name or not src.is_relative_to(root) or not src.is_file():
            raise ValueError(f"unknown fixture {name!r}")
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - small local copy
        Path(dest_path).write_bytes(src.read_bytes())  # noqa: ASYNC240
        return dest_path

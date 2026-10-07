"""A log sink for the designated test chat (TEST_TELEGRAM_CHAT_ID).

The live E2E harness talks to the real stack as its own user (own rows, own loops, own pings), and
nothing it triggers is sent through Telegram: every send to the test chat is logged and appended to
`<data_dir>/test_sink.jsonl` instead. Every other chat passes through to the real channel.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from mavis.channels.base import Channel
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.messages import Button

log = structlog.get_logger()
SINK_FILE = "test_sink.jsonl"


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

    def _record(self, kind: str, chat_id: int, text: str, **extra: Any) -> int:
        row = {"at": timeutil.now().isoformat(), "kind": kind, "chat_id": chat_id, "text": text, **extra}
        log.info("channel.test_sink", kind=kind, chat_id=chat_id, text=text[:200])
        path = sink_path(get_settings().data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._next_id += 1
        return -self._next_id  # never a real Telegram message id

    async def send_text(self, chat_id: int, text: str,
                        buttons: list[list[Button]] | None = None) -> list[int]:
        if chat_id != self._test:
            return await self._inner.send_text(chat_id, text, buttons)
        labels = [[b.label for b in row] for row in (buttons or [])]
        return [self._record("text", chat_id, text, buttons=labels)]

    async def send_document(self, chat_id: int, path: str, caption: str = "") -> int:
        if chat_id != self._test:
            return await self._inner.send_document(chat_id, path, caption)
        return self._record("document", chat_id, caption, path=path)

    async def send_typing(self, chat_id: int) -> None:
        if chat_id != self._test:
            await self._inner.send_typing(chat_id)

    async def react(self, chat_id: int, message_id: int, emoji: str) -> None:
        if chat_id != self._test:
            await self._inner.react(chat_id, message_id, emoji)

    async def download_file(self, file_id: str, dest_path: str) -> str:
        return await self._inner.download_file(file_id, dest_path)

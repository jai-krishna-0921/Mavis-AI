"""In-memory channels: FakeChannel for tests, ConsoleChannel for `mavis chat` and token-less dev."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from mavis.channels.base import MessageGone
from mavis.channels.formatting import to_plain
from mavis.channels.text import split_text
from mavis.config import get_settings
from mavis.domain.messages import Button


@dataclass
class SentItem:
    kind: Literal["text", "document", "typing", "photo", "album"]
    chat_id: int
    text: str = ""
    buttons: list[list[Button]] = field(default_factory=list)
    path: str | None = None


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[SentItem] = []
        self.fail_next: list[Exception] = []
        self.reactions: list[tuple[int, int, str]] = []  # (chat_id, message_id, emoji)
        self.edits: list[tuple[int, int, str, list[list[Button]]]] = []  # (chat, message, text, buttons)
        self.photos: list[tuple[int, str, str]] = []  # (chat_id, path, caption)
        self.albums: list[tuple[int, list[str], list[str]]] = []  # (chat_id, paths, captions)
        self.gone: set[int] = set()  # message ids whose edit raises MessageGone
        self._next_id = 1

    @property
    def texts(self) -> list[str]:
        return [s.text for s in self.sent if s.kind == "text"]

    def _maybe_fail(self) -> None:
        if self.fail_next:
            raise self.fail_next.pop(0)

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id - 1

    async def send_text(
        self, chat_id: int, text: str, buttons: list[list[Button]] | None = None
    ) -> list[int]:
        self._maybe_fail()
        chunks = split_text(text)
        ids: list[int] = []
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            self.sent.append(SentItem("text", chat_id, chunk, (buttons or []) if last else []))
            ids.append(self._id())
        return ids

    async def send_document(self, chat_id: int, path: str, caption: str = "") -> int:
        self._maybe_fail()
        self.sent.append(SentItem("document", chat_id, caption, path=path))
        return self._id()

    async def send_typing(self, chat_id: int) -> None:
        self.sent.append(SentItem("typing", chat_id))

    async def edit_text(self, chat_id: int, message_id: int, text: str,
                        buttons: list[list[Button]] | None = None) -> None:
        self._maybe_fail()
        if message_id in self.gone:
            raise MessageGone(f"message {message_id} not found")
        self.edits.append((chat_id, message_id, text, buttons or []))

    async def send_photo(self, chat_id: int, path: str, caption: str = "") -> int:
        self._maybe_fail()
        self.photos.append((chat_id, path, caption))
        self.sent.append(SentItem("photo", chat_id, caption, path=path))
        return self._id()

    async def send_media_group(self, chat_id: int, paths: list[str],
                               captions: list[str] | None = None) -> list[int]:
        self._maybe_fail()
        caps = list(captions or [""] * len(paths))
        self.albums.append((chat_id, list(paths), caps))
        self.sent.append(SentItem("album", chat_id, " | ".join(caps)))
        return [self._id() for _ in paths]

    async def react(self, chat_id: int, message_id: int, emoji: str) -> None:
        self.reactions.append((chat_id, message_id, emoji))

    async def download_file(self, file_id: str, dest_path: str) -> str:
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - test double
        Path(dest_path).write_bytes(b"fake-file")  # noqa: ASYNC240
        return dest_path


class ConsoleChannel(FakeChannel):
    """Prints what the agent says. Used by `mavis chat` and by `mavis dev` without a bot token."""

    async def send_text(
        self, chat_id: int, text: str, buttons: list[list[Button]] | None = None
    ) -> list[int]:
        ids = await super().send_text(chat_id, text, buttons)
        name = get_settings().agent_name
        print(f"\n{name}: {to_plain(text)}")
        if buttons:
            print("   " + "  ".join(f"[{b.label} → {b.data}]" for row in buttons for b in row))
        return ids

    async def send_document(self, chat_id: int, path: str, caption: str = "") -> int:
        msg_id = await super().send_document(chat_id, path, caption)
        print(f"\n{get_settings().agent_name}: 📎 {path} {caption}")
        return msg_id

    async def edit_text(self, chat_id: int, message_id: int, text: str,
                        buttons: list[list[Button]] | None = None) -> None:
        await super().edit_text(chat_id, message_id, text, buttons)
        print(f"\n{get_settings().agent_name} (edit): {to_plain(text)}")

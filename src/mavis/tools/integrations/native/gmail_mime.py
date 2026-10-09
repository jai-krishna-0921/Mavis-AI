"""Gmail message payloads to text and back: a MIME walk for reading, EmailMessage for sending.

Reading: text/plain is preferred, html is reduced to text with the stdlib parser (scripts, styles and
hidden head content dropped), charsets are decoded without ever raising, attachments are listed by name
and size and never fetched. The body is capped at BODY_CAP characters. Gmail has already undone the
content-transfer-encoding (quoted-printable, base64) of every part, so only the charset is applied here.

Writing: RFC 5322 through email.message.EmailMessage, base64url for the Gmail `raw` field.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import re
from collections.abc import Iterable
from email.message import EmailMessage, Message
from email.policy import SMTP
from email.utils import formatdate, make_msgid
from html.parser import HTMLParser
from typing import Any

BODY_CAP = 20_000
# Headers kept on a result: the ones normalize.py, sender authentication and replying read. Order is
# preserved (the topmost Authentication-Results is the receiving server's own).
KEPT_HEADERS = frozenset({
    "from", "to", "cc", "bcc", "reply-to", "subject", "date", "message-id", "in-reply-to", "references",
    "list-unsubscribe", "list-id", "precedence", "auto-submitted", "authentication-results",
})
_BLOCK_TAGS = frozenset({
    "p", "div", "br", "tr", "table", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
    "pre", "hr", "section", "article", "header", "footer",
})
_SKIP_TAGS = frozenset({"script", "style", "head", "title", "template", "noscript"})
_SPACES = re.compile(r"[ \t\r\f\v ​-‍﻿]+")
_BLANK_LINES = re.compile(r"\n[ \t]*(?:\n[ \t]*)+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n- " if tag == "li" else "\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def tidy(text: str) -> str:
    """Normalise newlines and runs of blanks; drop control characters."""
    text = _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup must degrade to whatever text was seen
        pass
    return tidy("".join(parser.parts))


def b64url_decode(data: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return b""


def decode_bytes(raw: bytes, charset: str | None) -> str:
    """Bytes to text in the declared charset. Unknown or wrong charsets never raise: UTF-8 is tried when no
    usable charset is declared, then Windows-1252 (a superset of latin-1 for printable text)."""
    if charset:
        try:
            codecs.lookup(charset)
            return raw.decode(charset, errors="replace")
        except (LookupError, ValueError):
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def header_list(part: dict) -> list[dict]:
    return [h for h in part.get("headers") or [] if isinstance(h, dict) and "name" in h]


def header(part: dict, name: str) -> str:
    wanted = name.lower()
    for h in header_list(part):
        if str(h["name"]).lower() == wanted:
            return str(h.get("value", ""))
    return ""


def _charset(part: dict) -> str | None:
    value = header(part, "content-type")
    if not value:
        return None
    try:
        msg = Message()
        msg["content-type"] = value
        return msg.get_content_charset()
    except Exception:  # noqa: BLE001
        return None


def _part_text(part: dict) -> str:
    data = (part.get("body") or {}).get("data")
    if not isinstance(data, str) or not data:
        return ""
    return decode_bytes(b64url_decode(data), _charset(part))


def _is_attachment(part: dict) -> bool:
    if str(part.get("filename") or "").strip():
        return True
    return header(part, "content-disposition").strip().lower().startswith("attachment")


def _render(part: dict, mode: str) -> str:
    """Text of a part: mode "plain" keeps text/plain, "html" keeps text/html only."""
    if _is_attachment(part):
        return ""
    mime = str(part.get("mimeType") or "").lower()
    children = [c for c in part.get("parts") or [] if isinstance(c, dict)]
    if mime == "multipart/alternative":
        return _join(_render(c, "plain") for c in children) or _join(_render(c, "html") for c in children)
    if children:
        return _join(_render(c, mode) for c in children)
    if mime == "text/plain" and mode == "plain":
        return tidy(_part_text(part))
    if mime == "text/html" and mode == "html":
        return html_to_text(_part_text(part))
    return ""


def _join(texts: Iterable[str]) -> str:
    return "\n\n".join(t for t in texts if t.strip())


def attachments(part: dict) -> list[dict[str, Any]]:
    """Filename, type and size of every attachment in the tree. Nothing is downloaded."""
    found: list[dict[str, Any]] = []

    def walk(p: dict) -> None:
        if _is_attachment(p):
            body = p.get("body") or {}
            found.append({
                "filename": str(p.get("filename") or "(unnamed)"),
                "mimeType": str(p.get("mimeType") or ""),
                "size": int(body.get("size") or 0),
            })
            return
        for c in p.get("parts") or []:
            if isinstance(c, dict):
                walk(c)

    walk(part)
    return found


def body_text(payload: dict, cap: int = BODY_CAP) -> tuple[str, bool]:
    """(text, truncated): plain text preferred, html reduced to text when there is no plain part."""
    if not isinstance(payload, dict):
        return "", False
    text = _render(payload, "plain") or _render(payload, "html")
    if len(text) > cap:
        return text[:cap].rstrip(), True
    return text, False


def kept_headers(payload: dict) -> list[dict[str, str]]:
    return [
        {"name": str(h["name"]), "value": str(h.get("value", ""))}
        for h in header_list(payload) if str(h["name"]).lower() in KEPT_HEADERS
    ]


# --- writing -------------------------------------------------------------------------------------------


def reply_subject(subject: str) -> str:
    stripped = subject.strip()
    return stripped if re.match(r"(?i)^re\s*:", stripped) else f"Re: {stripped}".strip()


def build_message(
    *, sender: str | None, to: list[str], subject: str, body: str, cc: list[str] | None = None,
    in_reply_to: str = "", references: str = "",
) -> EmailMessage:
    """An RFC 5322 message. Raises ValueError when a header value would be unsafe (newlines)."""
    msg = EmailMessage(policy=SMTP)
    if sender:
        msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=(sender or "mavis.local").rpartition("@")[2] or "mavis.local")
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = " ".join(x for x in (references.strip(), in_reply_to) if x)
    msg.set_content(body)
    return msg


def encode_raw(msg: EmailMessage) -> str:
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

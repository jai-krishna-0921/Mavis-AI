"""Workspace path guard, empty environment and output clipping, shared by every adapter."""

from __future__ import annotations

import posixpath
import re

from mavis.machine.errors import SandboxPathError

HIDDEN_PREFIX = ".mavis/"
SAFE_ENV_KEYS = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "PYTHONIOENCODING", "TMPDIR", "MPLBACKEND",
                           "PYTHONDONTWRITEBYTECODE"})
_NAME_BAD = re.compile(r"[^A-Za-z0-9._-]+")


def guard(path: str) -> str:
    raw = str(path or "")
    if not raw or "\x00" in raw or "\\" in raw or raw.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", raw):
        raise SandboxPathError(f"not a workspace path: {raw[:80]!r}")
    norm = posixpath.normpath(raw)
    if norm in (".", "") or norm == ".." or norm.startswith("../"):
        raise SandboxPathError(f"path leaves the workspace: {raw[:80]!r}")
    return norm


def is_hidden(path: str) -> bool:
    return guard(path).startswith(HIDDEN_PREFIX)


def safe_env(home: str) -> dict[str, str]:
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8", "TMPDIR": f"{home}/.mavis/tmp", "MPLBACKEND": "Agg",
            "PYTHONDONTWRITEBYTECODE": "1"}


def clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return f"[... {len(text) - limit} earlier characters cut]\n" + text[-limit:]


def safe_name(name: str, default: str = "file") -> str:
    base = posixpath.basename(str(name or "").replace("\\", "/"))
    stem, dot, ext = base.rpartition(".")
    stem, ext = (stem, ext) if dot else (base, "")
    stem = _NAME_BAD.sub("_", stem).strip("._")
    ext = _NAME_BAD.sub("", ext)[:10]
    if not stem:
        return default
    return f"{stem[:80]}.{ext}" if ext else stem[:80]

"""Workspace path guard, empty environment and output clipping, shared by every adapter."""

from __future__ import annotations

import posixpath
import re

from mavis.machine.errors import SandboxPathError

HIDDEN_PREFIX = ".mavis/"
WORK_DIRS = ("inbox", "out", "work", ".mavis/tmp")  # created in every fresh workspace
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


# Tool state a code interpreter leaves in its working directory (IPython history, caches): never user files.
TOOL_STATE_DIRS = (".ipython/", ".cache/", ".local/", ".config/", ".jupyter/", ".matplotlib/")


def is_hidden(path: str) -> bool:
    return guard(path).startswith((HIDDEN_PREFIX, *TOOL_STATE_DIRS))


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


def artifact_file(user_id: int, task_id: int, name: str):
    """Where a machine output is kept on the host before delivery: the per-user artifact directory, so the
    outbox and drive_upload guards (store.artifacts.guard) accept it."""
    from mavis.store.artifacts import user_dir

    return user_dir(user_id, task_id) / safe_name(name)

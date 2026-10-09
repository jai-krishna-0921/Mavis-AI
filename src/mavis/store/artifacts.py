"""Per-user artifact paths and the path guard (spec 6.1).

Every writer puts files under `user_dir(user_id, task_id)` (`artifacts_dir/u<uid>/t<task>/`). Files from
before multi-user sit flat in `artifacts_dir`; they all belonged to the owner, who may still read them
(`legacy_ok`), read-only and only when they exist. Anything else is refused."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from mavis.config import get_settings

_USER_DIR = re.compile(r"u\d+")


def user_dir(user_id: int, task_id: int | None = None) -> Path:
    base = get_settings().artifacts_dir / f"u{int(user_id)}"
    return base / f"t{int(task_id)}" if task_id is not None else base


def _is_legacy_flat(resolved: Path) -> bool:
    root = get_settings().artifacts_dir.resolve()
    if root not in resolved.parents:
        return False
    return _USER_DIR.fullmatch(resolved.relative_to(root).parts[0]) is None


def guard(user_id: int, path: str | Path, *, legacy_ok: bool = False) -> Path:
    """The resolved path, or PermissionError when it is not this user's to read."""
    resolved = Path(path).resolve()
    root = user_dir(user_id).resolve()
    if resolved == root or root in resolved.parents:
        return resolved
    if legacy_ok and resolved.is_file() and _is_legacy_flat(resolved):
        return resolved
    raise PermissionError("path is outside the user's artifacts")


def stage(user_id: int, source: str | Path, task_id: int | None = None, name: str | None = None) -> Path:
    """Copy a file produced elsewhere into the user's directory and return the new path."""
    src = Path(source)
    dest_dir = user_dir(user_id, task_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (name or src.name)
    shutil.copyfile(src, dest)
    return dest

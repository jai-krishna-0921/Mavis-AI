"""Per-user artifact paths and the path guard (spec 6.1)."""

from __future__ import annotations

from pathlib import Path

from mavis.config import get_settings


def user_dir(user_id: int, task_id: int | None = None) -> Path:
    base = get_settings().artifacts_dir / f"u{int(user_id)}"
    return base / f"t{int(task_id)}" if task_id is not None else base


def guard(user_id: int, path: str | Path) -> Path:
    """The resolved path, or PermissionError when it is not inside this user's artifacts directory."""
    resolved = Path(path).resolve()
    root = user_dir(user_id).resolve()
    if resolved != root and root not in resolved.parents:
        raise PermissionError("path is outside the user's artifacts")
    return resolved

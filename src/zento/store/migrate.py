"""Programmatic Alembic upgrade (used by `zento migrate` and tests). Synchronous by design."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def upgrade(url: str | None = None, revision: str = "head") -> None:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if url:
        cfg.attributes["url"] = url
    command.upgrade(cfg, revision)

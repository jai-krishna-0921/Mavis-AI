"""Programmatic Alembic upgrade (used by `mavis migrate` and tests). Synchronous by design."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Alembic filter: ignore LangGraph's checkpoint tables (created by AsyncPostgresSaver.setup() in
    the app database, not by Alembic), so autogenerate never proposes dropping them."""
    table = name if type_ == "table" else getattr(getattr(obj, "table", None), "name", None)
    return not (table or "").startswith("checkpoint")


def upgrade(url: str | None = None, revision: str = "head") -> None:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if url:
        cfg.attributes["url"] = url
    command.upgrade(cfg, revision)

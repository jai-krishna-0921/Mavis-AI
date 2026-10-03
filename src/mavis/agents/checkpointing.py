"""Durable LangGraph checkpointer: Postgres in prod, a SQLite file in dev.

Follows the app database choice (`Settings.db_url`). Postgres URLs use SQLAlchemy's driver suffix
(`postgresql+psycopg://`), which libpq does not accept, so it is stripped. The checkpoint tables are
created by `setup()` once per process and are not managed by Alembic. Each task run opens its own
connection and closes it when the run ends, so there is no long-lived client to close at shutdown.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.base import BaseCheckpointSaver

from mavis.config import get_settings

_pg_ready = False
_pg_setup_lock = asyncio.Lock()  # two first tasks (two users) must not run the DDL at once
_DRIVER = re.compile(r"^postgres(?:ql)?\+[a-z0-9_]+://")


def libpq_url(url: str) -> str:
    """`postgresql+psycopg://u:p@h/db` -> `postgresql://u:p@h/db`."""
    return _DRIVER.sub("postgresql://", url, count=1)


@asynccontextmanager
async def open_checkpointer() -> AsyncIterator[BaseCheckpointSaver]:
    global _pg_ready
    s = get_settings()
    url = s.db_url
    if url.startswith("postgres"):
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(libpq_url(url)) as saver:
            if not _pg_ready:
                async with _pg_setup_lock:
                    if not _pg_ready:
                        await saver.setup()
                        _pg_ready = True
            yield saver
    else:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        s.data_dir.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(str(s.data_dir / "checkpoints.db")) as saver:
            yield saver

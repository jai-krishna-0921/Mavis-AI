import sqlite3

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from mavis.store import models  # noqa: F401
from mavis.store.db import Base
from mavis.store.migrate import upgrade


def test_upgrade_creates_tables(tmp_path) -> None:
    db_file = tmp_path / "m.db"
    upgrade(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    query = "select name from sqlite_master where type='table'"
    names = {r[0] for r in sqlite3.connect(db_file).execute(query)}
    assert {"users", "messages", "outbox", "processed_events", "alembic_version"} <= names


def test_migrations_match_models(tmp_path) -> None:
    """Guards every later phase: a new ORM table without a matching revision fails here."""
    db_file = tmp_path / "m.db"
    upgrade(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": False})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []


def test_checkpoint_tables_are_ignored_by_autogenerate(tmp_path) -> None:
    from mavis.store.migrate import include_object

    db_file = tmp_path / "m.db"
    upgrade(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    con = sqlite3.connect(db_file)
    con.execute("create table checkpoints (thread_id text, checkpoint_id text)")
    con.execute("create table checkpoint_writes (thread_id text)")
    con.commit()
    con.close()
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    with engine.connect() as conn:
        raw = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": False}), Base.metadata)
        filtered = compare_metadata(
            MigrationContext.configure(conn, opts={"compare_type": False, "include_object": include_object}),
            Base.metadata,
        )
    assert {d[1].name for d in raw if d[0] == "remove_table"} == {"checkpoints", "checkpoint_writes"}
    assert filtered == []

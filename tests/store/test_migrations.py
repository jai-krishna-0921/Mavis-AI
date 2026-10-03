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


def test_0011_downgrade_drops_workspace_rows_with_the_source_column(tmp_path) -> None:
    from alembic import command
    from alembic.config import Config

    from mavis.store.migrate import MIGRATIONS_DIR

    db_file = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    upgrade(url, "0011_attention_source")
    con = sqlite3.connect(db_file)
    cols = ("user_id, message_id, thread_id, origin, status, attempts, method, sender_domain, sender_name, "
            "kind, needs_user, verdict, urgency, score, reasons, facts, summary, action, delivery, "
            "received_at, created_at, source")
    for mid, source in (("m1", "mail"), ("drive:f1", "drive"), ("docs:d1:c1", "docs"), ("tasks:t1", "tasks")):
        con.execute(f"insert into attention_observations ({cols}) values "
                    "(1, ?, '', 'x', 'done', 0, 'x', '', '', 'x', 0, 'brief', 0, 0, '[]', '{}', 'x', '', "
                    "'none', '2026-10-03 00:00:00', '2026-10-03 00:00:00', ?)", (mid, source))
    con.commit()
    con.close()
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["url"] = url
    command.downgrade(cfg, "0010_hotfix_approval_taint")
    rows = [r[0] for r in sqlite3.connect(db_file).execute("select message_id from attention_observations")]
    assert rows == ["m1"]  # a Workspace row would read as an email once the column is gone


def test_0012_loop_provenance_backfill(tmp_path) -> None:
    """Legacy loops read as untrusted; only writer constants (not origin prefixes) are recognised."""
    db_file = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    upgrade(url, "0011_attention_source")
    con = sqlite3.connect(db_file)
    con.execute("insert into users (id, telegram_chat_id, name, timezone, onboarded, state, created_at) "
                "values (1, 5, 'x', 'UTC', 0, '{}', '2026-10-03 00:00:00')")
    sources = ["tool:track_loop", "onboarding", "tg:update:412982316", "cli:abc", "gmail:msg:1",
               "untrusted:gmail:msg:2", "wakeup:32", ""]
    for source in sources:
        con.execute("insert into loops (user_id, kind, title, entities, status, importance, source, "
                    "created_at, updated_at, version) values (1, 'COMMITMENT', ?, '[]', 'OPEN', 3, ?, "
                    "'2026-10-03 00:00:00', '2026-10-03 00:00:00', 1)", (f"t {source}", source))
    con.commit()
    con.close()
    upgrade(url)
    rows = sqlite3.connect(db_file).execute("select source, trust, origin from loops")
    got = {r[0]: (r[1], r[2]) for r in rows}
    assert got.pop("tool:track_loop") == ("untrusted", "conversation")  # trust not determinable: safe default
    assert got.pop("onboarding") == ("system", "routine")
    assert set(got.values()) == {("untrusted", "unknown")}

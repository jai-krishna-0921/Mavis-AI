"""Phase 11 migration: existing owner rows become active owners, others active standard; new rows pending."""

from __future__ import annotations

import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from mavis.store.migrate import MIGRATIONS_DIR, upgrade


def _cfg(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["url"] = url
    return cfg


def _this_and_previous() -> tuple[str, str]:
    script = ScriptDirectory.from_config(_cfg(""))
    rev = next(r for r in script.walk_revisions() if r.revision.endswith("_multiuser_access"))
    return rev.revision, rev.down_revision


@pytest.mark.parametrize("owner_chat,other_chat", [(5001, 7302), (9944, 4410), (123456, 654321)])
def test_existing_rows_are_activated_and_owner_is_tiered(tmp_path, monkeypatch, owner_chat, other_chat):
    rev, prev = _this_and_previous()
    db_file = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    upgrade(url, prev)
    con = sqlite3.connect(db_file)
    for chat, name in ((owner_chat, "Priya"), (other_chat, "Tomas")):
        con.execute("insert into users (telegram_chat_id, name, timezone, onboarded, state, created_at) "
                    "values (?, ?, 'Asia/Tokyo', 1, '{}', '2026-10-01 00:00:00')", (chat, name))
    con.commit()
    con.close()
    monkeypatch.setenv("OWNER_TELEGRAM_CHAT_IDS", f"[{owner_chat}]")
    upgrade(url, rev)
    con = sqlite3.connect(db_file)
    rows = dict(con.execute("select telegram_chat_id, status || ':' || tier from users"))
    assert rows == {owner_chat: "active:owner", other_chat: "active:standard"}
    ids = dict(con.execute("select telegram_chat_id, composio_user_id from users"))
    assert all(v is None for v in ids.values())  # legacy mavis-<id> identity kept (spec 6.2)


def test_downgrade_drops_the_new_tables(tmp_path):
    rev, prev = _this_and_previous()
    db_file = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    upgrade(url, rev)
    command.downgrade(_cfg(url), prev)
    query = "select name from sqlite_master where type='table'"
    names = {r[0] for r in sqlite3.connect(db_file).execute(query)}
    assert not {"invite_codes", "invite_redemptions", "llm_usage"} & names


@pytest.mark.parametrize("chat,name", [(31, "Aiko"), (32, "Bruno"), (33, None)])
async def test_new_chat_rows_start_pending(db, chat, name):
    from mavis.store.repo import users

    u, created = await users.get_or_create_by_chat(chat, name, telegram_user_id=chat)
    assert created and u.status == "pending" and u.tier == "standard" and u.telegram_user_id == chat

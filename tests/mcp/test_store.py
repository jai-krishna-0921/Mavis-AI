"""MCP server rows: sealed secrets, per-user isolation, limits, single-use consent state, the migration."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import select

from mavis.domain import timeutil
from mavis.mcp import sealing, slugs
from mavis.mcp.limits import MAX_SERVERS_PER_USER
from mavis.store import db as dbm
from mavis.store.migrate import upgrade
from mavis.store.models import McpOAuthState, McpServer
from mavis.store.repo import mcp_servers as repo
from mavis.store.repo.deletion import USER_TABLES, delete_user_rows


async def make(user_id: int, slug: str = "notes", **kw) -> McpServer:
    kw.setdefault("display_name", slug.title())
    kw.setdefault("url", f"https://mcp.example.com/{slug}")
    kw.setdefault("auth_kind", "none")
    return await repo.create(user_id, slug=slug, **kw)


# --- sealing --------------------------------------------------------------------------------------

async def test_a_bearer_token_is_sealed_at_rest_and_opens_for_its_owner(mcp_env, two_users) -> None:
    a, _ = two_users
    row = await make(a.id, auth_kind="bearer", bearer="tok-secret-123")
    async with dbm.Session() as s:
        raw = await s.scalar(select(McpServer.access_token).where(McpServer.id == row.id))
    assert raw.startswith("n1:") and "tok-secret-123" not in raw
    assert sealing.tokens_of(await repo.get(a.id, row.id)).access == "tok-secret-123"


async def test_a_sealed_value_cannot_be_opened_for_another_user_server_or_column(mcp_env, two_users) -> None:
    a, b = two_users
    sealed = sealing.seal("tok", a.id, 5, sealing.ACCESS_TOKEN)
    assert sealing.unseal(sealed, a.id, 5, sealing.ACCESS_TOKEN) == "tok"
    for user_id, server_id, field in ((b.id, 5, sealing.ACCESS_TOKEN), (a.id, 6, sealing.ACCESS_TOKEN),
                                      (a.id, 5, sealing.REFRESH_TOKEN)):
        with pytest.raises(InvalidTag):
            sealing.unseal(sealed, user_id, server_id, field)


async def test_tokens_and_a_client_secret_are_all_sealed_and_listed_for_redaction(mcp_env, two_users) -> None:
    a, _ = two_users
    row = await make(a.id, auth_kind="oauth", status=repo.NEEDS_AUTH)
    await repo.save_oauth(a.id, row.id, {
        "issuer": "https://auth.example.com", "client_id": "cid",
        "client_secret": sealing.seal("client-secret-9", a.id, row.id, sealing.CLIENT_SECRET)})
    await repo.save_tokens(a.id, row.id, access="acc-1", refresh="ref-1", expires_at=None)
    got = await repo.get(a.id, row.id)
    assert "client-secret-9" not in str(got.oauth) and "acc-1" not in got.access_token
    assert set(sealing.secrets_of(got)) == {"acc-1", "ref-1", "client-secret-9"}


async def test_save_tokens_keeps_the_refresh_token_when_the_server_sends_none(mcp_env, two_users) -> None:
    a, _ = two_users
    row = await make(a.id, auth_kind="oauth")
    await repo.save_tokens(a.id, row.id, access="a1", refresh="r1", expires_at=None)
    await repo.save_tokens(a.id, row.id, access="a2", refresh=None, expires_at=None, keep_refresh=True)
    t = sealing.tokens_of(await repo.get(a.id, row.id))
    assert (t.access, t.refresh) == ("a2", "r1")
    await repo.save_tokens(a.id, row.id, access="a3", refresh=None, expires_at=None)
    assert sealing.tokens_of(await repo.get(a.id, row.id)).refresh is None


# --- isolation ------------------------------------------------------------------------------------

async def test_no_function_reaches_another_users_server(mcp_env, two_users) -> None:
    a, b = two_users
    mine = await make(a.id, "notes")
    assert await repo.get(b.id, mine.id) is None
    assert await repo.by_slug(b.id, "notes") is None
    assert await repo.list_for(b.id) == [] and await repo.count(b.id) == 0
    await repo.set_status(b.id, mine.id, repo.ERROR, "x")
    await repo.save_tools(b.id, mine.id, [{"name": "t"}], timeutil.now())
    await repo.save_tokens(b.id, mine.id, access="stolen", refresh=None, expires_at=None)
    assert await repo.delete_server(b.id, mine.id) is False
    after = await repo.get(a.id, mine.id)
    assert (after.status, after.tools, after.access_token) == (repo.ACTIVE, [], None)


async def test_two_users_may_use_the_same_slug(mcp_env, two_users) -> None:
    a, b = two_users
    await make(a.id, "notes")
    await make(b.id, "notes")
    assert (await repo.by_slug(a.id, "notes")).user_id == a.id


async def test_a_slug_is_unique_per_user(mcp_env, two_users) -> None:
    a, _ = two_users
    await make(a.id, "notes")
    with pytest.raises(repo.SlugTaken):
        await make(a.id, "notes", url="https://other.example.com/mcp")
    assert await repo.count(a.id) == 1


async def test_a_user_may_have_ten_servers_and_no_more(mcp_env, two_users) -> None:
    a, b = two_users
    for i in range(MAX_SERVERS_PER_USER):
        await make(a.id, f"s{i}")
    with pytest.raises(repo.LimitReached):
        await make(a.id, "one_too_many")
    await make(b.id, "fine")  # the cap is per user


async def test_has_active_ignores_servers_that_need_attention(mcp_env, two_users) -> None:
    a, _ = two_users
    assert await repo.has_active(a.id) is False
    row = await make(a.id, status=repo.NEEDS_AUTH)
    assert await repo.has_active(a.id) is False
    await repo.set_status(a.id, row.id, repo.ACTIVE)
    assert await repo.has_active(a.id) is True


async def test_deleting_a_server_takes_its_tokens_and_pending_consent_with_it(mcp_env, two_users) -> None:
    a, _ = two_users
    row = await make(a.id, auth_kind="oauth")
    await repo.save_tokens(a.id, row.id, access="a", refresh="r", expires_at=None)
    state = await repo.create_state(a.id, row.id, "verifier")
    assert await repo.delete_server(a.id, row.id) is True
    assert await repo.get(a.id, row.id) is None
    assert await repo.consume_state(state) is None
    async with dbm.Session() as s:
        assert await s.scalar(select(McpServer.id)) is None


async def test_account_deletion_removes_mcp_rows_children_first(mcp_env, two_users) -> None:
    a, b = two_users
    row = await make(a.id, auth_kind="oauth")
    await repo.create_state(a.id, row.id, "v")
    await make(b.id, "keep")
    counts = await delete_user_rows(a.id)
    assert counts["mcp_servers"] == 1 and counts["mcp_oauth_states"] == 1
    assert USER_TABLES.index("mcp_oauth_states") < USER_TABLES.index("mcp_servers")
    assert [r.slug for r in await repo.list_for(b.id)] == ["keep"]


# --- consent state --------------------------------------------------------------------------------

async def test_a_state_is_single_use_and_keeps_the_verifier_sealed(mcp_env, two_users) -> None:
    a, _ = two_users
    row = await make(a.id, auth_kind="oauth")
    state = await repo.create_state(a.id, row.id, "pkce-verifier-xyz")
    async with dbm.Session() as s:
        stored = await s.scalar(select(McpOAuthState.code_verifier))
    assert "pkce-verifier-xyz" not in stored
    assert await repo.consume_state(state) == (a.id, row.id, "pkce-verifier-xyz")
    assert await repo.consume_state(state) is None  # replay


async def test_an_expired_state_is_refused_and_spent(mcp_env, two_users) -> None:
    a, _ = two_users
    row = await make(a.id, auth_kind="oauth")
    state = await repo.create_state(a.id, row.id, "v")
    async with dbm.Session() as s:
        obj = await s.get(McpOAuthState, state)
        obj.expires_at = timeutil.now() - timedelta(seconds=1)
        await s.commit()
    assert await repo.consume_state(state) is None
    async with dbm.Session() as s:
        assert await s.get(McpOAuthState, state) is None


@pytest.mark.parametrize("bad", ["", "nope", "x" * 65])
async def test_unknown_or_oversized_states_are_refused(mcp_env, db, bad) -> None:
    assert await repo.consume_state(bad) is None


async def test_old_states_are_purged_when_a_new_one_is_made(mcp_env, two_users) -> None:
    a, _ = two_users
    row = await make(a.id, auth_kind="oauth")
    old = await repo.create_state(a.id, row.id, "v")
    async with dbm.Session() as s:
        obj = await s.get(McpOAuthState, old)
        obj.expires_at = timeutil.now() - timedelta(hours=1)
        await s.commit()
    await repo.create_state(a.id, row.id, "v2")
    async with dbm.Session() as s:
        assert await s.get(McpOAuthState, old) is None


# --- slugs ----------------------------------------------------------------------------------------

@pytest.mark.parametrize(("name", "slug"), [
    ("Linear", "linear"), ("My Notes  Server!", "my_notes_server"), ("  ", "server"), ("ÅÄÖ", "server"),
    ("a" * 60, "a" * 24), ("Context7 (docs)", "context7_docs"),
])
def test_slugify(name, slug) -> None:
    assert slugs.slugify(name) == slug
    assert slugs.is_slug(slug)


def test_unique_slug_adds_a_suffix_inside_the_length_limit() -> None:
    assert slugs.unique_slug("Linear", []) == "linear"
    assert slugs.unique_slug("Linear", ["linear"]) == "linear_2"
    assert slugs.unique_slug("Linear", ["linear", "linear_2"]) == "linear_3"
    long = "x" * 24
    got = slugs.unique_slug(long, [long])
    assert got == "x" * 22 + "_2" and slugs.is_slug(got)


# --- the migration --------------------------------------------------------------------------------

def tables(db_file) -> set[str]:
    query = "select name from sqlite_master where type='table'"
    return {r[0] for r in sqlite3.connect(db_file).execute(query)}


def test_migration_0024_creates_and_drops_the_tables(tmp_path) -> None:
    from alembic import command
    from alembic.config import Config

    from mavis.store.migrate import MIGRATIONS_DIR

    db_file = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    upgrade(url)
    names = tables(db_file)
    assert {"mcp_servers", "mcp_oauth_states"} <= names
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["url"] = url
    command.downgrade(cfg, "0023_outbox_dedupe_per_user")
    names = tables(db_file)
    assert "mcp_servers" not in names and "mcp_oauth_states" not in names


def test_the_migration_chain_is_linear_from_0023(tmp_path) -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from mavis.store.migrate import MIGRATIONS_DIR

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    script = ScriptDirectory.from_config(cfg)
    assert script.get_revision("0024_mcp_servers").down_revision == "0023_outbox_dedupe_per_user"
    assert len(script.get_heads()) == 1

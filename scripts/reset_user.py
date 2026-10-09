"""Owner-only: reset one user to a fresh start on the server (the same as the user's "Start fresh" in /clear).

It clears the user's Telegram chat (messages Mavis may delete, within Telegram's 48 hour window), erases every
store (disconnects Google, Slack and Composio accounts, forgets memory, tasks, reminders, files), then
re-admits the same chat so the user can carry on at once. An owner chat (OWNER_TELEGRAM_CHAT_IDS) comes
back an active owner; anyone else keeps their tier and needs no new invite. The user then gets the onboarding
greeting from the running worker (it delivers the outbox).

There is no backup: only the per-table row counts are logged before the reset, so the operator has a record
of what went. Run it where the app's environment (.env, DATABASE_URL, bot token) is loaded:

    uv run python scripts/reset_user.py 42             # prints the counts and asks you to retype the id
    uv run python scripts/reset_user.py 42 --yes       # no prompt
"""

from __future__ import annotations

import asyncio

import typer
from sqlalchemy import func, select

from mavis.store.db import Base, Session
from mavis.store.repo import deletion as repo

app = typer.Typer(add_completion=False, help=__doc__)


async def row_counts(user_id: int) -> dict[str, int]:
    """Rows this user has in each per-user table (the data a reset erases)."""
    tables = Base.metadata.tables
    counts: dict[str, int] = {}
    async with Session() as s:
        for name in repo.USER_TABLES:
            table = tables.get(name)
            if table is not None and "user_id" in table.c:
                counts[name] = int(await s.scalar(
                    select(func.count()).select_from(table).where(table.c.user_id == user_id)) or 0)
    return counts


async def reset(user_id: int, out=typer.echo) -> dict:
    from mavis.access import deletion
    from mavis.store.repo import users

    try:
        user = await users.get(user_id)
    except Exception:  # noqa: BLE001 - an unknown id is an operator typo, not a crash
        raise typer.BadParameter(f"no user with id {user_id}") from None
    if user.status in ("deleting", "deleted"):
        raise typer.BadParameter(f"user {user_id} is {user.status}; nothing to reset")
    counts = await row_counts(user_id)
    out(f"user {user_id} (chat {user.telegram_chat_id}, tier {user.tier}): rows before reset")
    for name, n in counts.items():
        if n:
            out(f"  {name}: {n}")
    await deletion.mark_deleting(user_id, by_owner=True, reset=True)
    report = await deletion.run_steps(user_id, reset=True)
    out(f"user {user_id} reset and re-admitted")
    return report


async def _main(user_id: int, yes: bool) -> None:
    from mavis.cli import _cleanup, bootstrap
    from mavis.config import get_settings

    bus = await bootstrap(create_tables=get_settings().is_sqlite, warm=False)
    try:
        if not yes:
            counts = await row_counts(user_id)
            typer.echo(f"Rows that will be erased for user {user_id}: {sum(counts.values())}")
            if typer.prompt("Retype the user id to confirm", type=int) != user_id:
                raise typer.Exit(1)
        await reset(user_id)
    finally:
        await _cleanup([], bus)


@app.command()
def main(user_id: int = typer.Argument(..., help="users.id to reset"),
         yes: bool = typer.Option(False, "--yes", help="skip the confirmation prompt")) -> None:
    asyncio.run(_main(user_id, yes))


if __name__ == "__main__":
    app()

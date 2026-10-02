"""Process entrypoints. One image, many roles: `zento api | worker | dev | chat | migrate`."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from uuid import uuid4

import structlog
import typer

from zento.bus import get_bus, set_bus
from zento.bus.base import EventBus
from zento.config import get_settings
from zento.logging import configure_logging
from zento.store.db import dispose_engine, init_db

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Zento personal assistant")
log = structlog.get_logger(__name__)
LOCAL_CHAT_ID = -1


async def bootstrap(create_tables: bool) -> EventBus:
    """Common start-up for worker-like roles: logging, schema (dev), handlers, bus."""
    from zento.worker.handlers import register_default_handlers

    configure_logging()
    if create_tables:
        await init_db()
    register_default_handlers()
    return get_bus()


def _run(coro) -> None:
    """Run a role until done; Ctrl+C cancels the main task (cleanup runs in `finally`) and exits quietly."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(coro)


async def _run_tasks(tasks: list[asyncio.Task], bus: EventBus) -> None:
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*tasks, return_exceptions=True)
        await bus.close()
        await dispose_engine()


async def _dev() -> None:
    from zento.channels.outbox_sender import OutboxSender
    from zento.channels.telegram_poller import run_polling
    from zento.worker.runner import run_worker

    bus = await bootstrap(create_tables=True)
    s = get_settings()
    tasks = [
        asyncio.create_task(run_worker(bus, "dev")),
        asyncio.create_task(OutboxSender().run_forever()),
    ]
    if s.telegram_bot_token:
        tasks.append(asyncio.create_task(run_polling(bus, s.telegram_bot_token)))
    else:
        log.warning("dev.no_telegram_token", hint="set TELEGRAM_BOT_TOKEN or use `zento chat`")
    log.info("dev.started", telegram=bool(s.telegram_bot_token))
    await _run_tasks(tasks, bus)


async def _worker(name: str) -> None:
    from zento.channels.outbox_sender import OutboxSender
    from zento.worker.runner import run_worker

    bus = await bootstrap(create_tables=get_settings().is_sqlite)
    tasks = [asyncio.create_task(run_worker(bus, name)), asyncio.create_task(OutboxSender().run_forever())]
    log.info("worker.started", consumer=name)
    await _run_tasks(tasks, bus)


async def _chat() -> None:
    from zento.bus.inprocess import InProcessBus
    from zento.channels import set_channel
    from zento.channels.fake import ConsoleChannel
    from zento.channels.outbox_sender import OutboxSender
    from zento.domain.events import Event, EventType, Trust
    from zento.store.db import utcnow
    from zento.store.repo import users
    from zento.worker.handlers import register_default_handlers
    from zento.worker.runner import run_worker

    configure_logging(level="WARNING")
    await init_db()
    register_default_handlers()
    bus = InProcessBus()
    set_bus(bus)
    console = ConsoleChannel()
    set_channel(console)
    user, _ = await users.get_or_create_by_chat(LOCAL_CHAT_ID, None)
    worker = asyncio.create_task(run_worker(bus, "chat"))
    sender = OutboxSender(console)
    print(f"Chatting with {get_settings().agent_name} locally. /quit to exit.")
    try:
        while True:
            line = (await asyncio.to_thread(input, "\nyou> ")).strip()
            if line in {"/quit", "/exit"}:
                break
            if not line:
                continue
            payload: dict = {"text": line}
            if line.startswith("/"):
                payload["command"] = line[1:].split()[0].lower()
            await bus.publish(Event(id=f"cli:{uuid4()}", user_id=user.id, type=EventType.USER_MESSAGE,
                                    occurred_at=utcnow(), source="cli", payload=payload, trust=Trust.USER))
            await bus.wait_idle()
            await sender.run_once()
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        worker.cancel()
        await bus.close()
        await dispose_engine()


@app.command()
def dev() -> None:
    """Run every role in one process (Telegram long-polling, worker, outbox sender)."""
    _run(_dev())


@app.command()
def api(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Run the webhook/health API (stateless; scale horizontally)."""
    import uvicorn

    uvicorn.run("zento.api.app:create_app", factory=True, host=host, port=port, proxy_headers=True)


@app.command()
def worker(name: str = typer.Option(default_factory=lambda: f"worker-{socket.gethostname()}")) -> None:
    """Consume events and jobs and deliver the outbox."""
    _run(_worker(name))


@app.command()
def chat() -> None:
    """Local REPL with the agent (Mavis) — no Telegram needed."""
    _run(_chat())


@app.command()
def migrate(revision: str = "head") -> None:
    """Apply database migrations (required for Postgres)."""
    from zento.store.migrate import upgrade

    upgrade(get_settings().db_url, revision)
    typer.echo(f"migrated to {revision}")

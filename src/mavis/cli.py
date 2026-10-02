"""Process entrypoints. One image, many roles: `mavis api | worker | dev | chat | migrate`."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import socket
from uuid import uuid4

import structlog
import typer

from mavis.bus import get_bus, set_bus
from mavis.bus.base import EventBus
from mavis.config import get_settings
from mavis.logging import configure_logging
from mavis.store.db import dispose_engine, init_db

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Mavis personal assistant")
log = structlog.get_logger(__name__)
LOCAL_CHAT_ID = -1
_background: set[asyncio.Task] = set()


async def bootstrap(create_tables: bool) -> EventBus:
    """Common start-up for worker-like roles: logging, schema (dev), handlers, bus."""
    from mavis.worker.handlers import register_default_handlers

    configure_logging()
    if create_tables:
        await init_db()
    register_default_handlers()
    _start_memory_warmup()
    return get_bus()


def _start_memory_warmup() -> asyncio.Task:
    """Background warm-up (store init + embedding model load); failures are logged, never raised."""

    async def _warm() -> None:
        from mavis.memory.service import get_memory

        try:
            await get_memory().warm()
        except Exception:
            log.warning("memory.warm_failed", exc_info=True)

    task = asyncio.create_task(_warm())
    _background.add(task)  # keep a reference so it isn't garbage-collected mid-flight
    task.add_done_callback(_background.discard)
    return task


async def _main(coro) -> None:
    """Run `coro` as a task that SIGINT/SIGTERM cancel, so its `finally` cleanup runs."""
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(coro)
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # non-Unix event loops
            loop.add_signal_handler(sig, task.cancel)
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _run(coro) -> None:
    """Run a role until done; Ctrl+C / SIGTERM cancel it gracefully and exit quietly."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main(coro))


async def _cleanup(tasks: list[asyncio.Task], bus: EventBus) -> None:
    """Cancel and await tasks, then close the bus; the engine is disposed even if the bus close raises."""
    try:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bus.close()
    finally:
        await dispose_engine()


async def _run_tasks(tasks: list[asyncio.Task], bus: EventBus) -> None:
    try:
        await asyncio.gather(*tasks)
    finally:
        await _cleanup(tasks, bus)


async def _dev() -> None:
    from mavis.channels.outbox_sender import OutboxSender
    from mavis.channels.telegram_poller import run_polling
    from mavis.worker.runner import run_worker

    bus = await bootstrap(create_tables=True)
    s = get_settings()
    tasks = [
        asyncio.create_task(run_worker(bus, "dev")),
        asyncio.create_task(OutboxSender().run_forever()),
    ]
    if s.telegram_bot_token:
        tasks.append(asyncio.create_task(run_polling(bus, s.telegram_bot_token)))
    else:
        log.warning("dev.no_telegram_token", hint="set TELEGRAM_BOT_TOKEN or use `mavis chat`")
    log.info("dev.started", telegram=bool(s.telegram_bot_token))
    await _run_tasks(tasks, bus)


async def _worker(name: str) -> None:
    from mavis.channels.outbox_sender import OutboxSender
    from mavis.worker.runner import run_worker

    bus = await bootstrap(create_tables=get_settings().is_sqlite)
    tasks = [asyncio.create_task(run_worker(bus, name)), asyncio.create_task(OutboxSender().run_forever())]
    log.info("worker.started", consumer=name)
    await _run_tasks(tasks, bus)


async def _chat() -> None:
    from mavis.bus.inprocess import InProcessBus
    from mavis.channels import set_channel
    from mavis.channels.fake import ConsoleChannel
    from mavis.channels.outbox_sender import OutboxSender
    from mavis.domain.events import Event, EventType, Trust
    from mavis.store.db import utcnow
    from mavis.store.repo import users
    from mavis.worker.handlers import register_default_handlers
    from mavis.worker.runner import run_worker

    configure_logging(level="WARNING")
    await init_db()
    register_default_handlers()
    _start_memory_warmup()
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
        await _cleanup([worker], bus)


@app.command()
def dev() -> None:
    """Run every role in one process (Telegram long-polling, worker, outbox sender)."""
    _run(_dev())


def _require_redis(role: str) -> None:
    """api/worker are multi-process roles: without Redis their in-process bus would drop messages."""
    if not get_settings().redis_url:
        typer.echo(f"error: REDIS_URL is required for the `{role}` role (use `mavis dev` for one process).",
                   err=True)
        raise typer.Exit(1)


@app.command()
def api(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Run the webhook/health API (stateless; scale horizontally)."""
    import uvicorn

    _require_redis("api")

    uvicorn.run("mavis.api.app:create_app", factory=True, host=host, port=port, proxy_headers=True)


@app.command()
def worker(name: str = typer.Option(default_factory=lambda: f"worker-{socket.gethostname()}")) -> None:
    """Consume events and jobs and deliver the outbox."""
    _require_redis("worker")
    _run(_worker(name))


@app.command()
def chat() -> None:
    """Local REPL with the agent (Mavis) — no Telegram needed."""
    _run(_chat())


@app.command()
def migrate(revision: str = "head") -> None:
    """Apply database migrations (required for Postgres)."""
    from mavis.store.migrate import upgrade

    upgrade(get_settings().db_url, revision)
    typer.echo(f"migrated to {revision}")

"""Serve an ASGI app on 127.0.0.1 inside the test's event loop (a real socket, so the net guard and the HTTP
libraries are exercised end to end)."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn


@asynccontextmanager
async def serve(app) -> AsyncIterator[str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, log_level="critical", lifespan="off", timeout_graceful_shutdown=1)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.005)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
        sock.close()

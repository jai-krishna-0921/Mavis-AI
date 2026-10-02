import pytest
from typer.testing import CliRunner

from mavis.cli import app


def test_cli_lists_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("dev", "api", "worker", "chat", "migrate"):
        assert command in result.output


def test_migrate_runs_against_temp_db(settings) -> None:
    result = CliRunner().invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output
    assert (settings.data_dir.parent / "test.db").exists()


class _Bus:
    def __init__(self, fail: bool = False) -> None:
        self.closed = False
        self.fail = fail

    async def close(self) -> None:
        self.closed = True
        if self.fail:
            raise RuntimeError("close failed")


async def test_run_tasks_cancel_awaits_tasks_and_disposes_engine(monkeypatch) -> None:
    import asyncio

    from mavis import cli

    disposed: list[bool] = []

    async def fake_dispose() -> None:
        disposed.append(True)

    monkeypatch.setattr(cli, "dispose_engine", fake_dispose)
    states: list[str] = []

    async def worker() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(0)  # cleanup must wait for this
            states.append("cleaned")
            raise

    bus = _Bus()
    tasks = [asyncio.create_task(worker()), asyncio.create_task(worker())]
    main = asyncio.create_task(cli._run_tasks(tasks, bus))
    await asyncio.sleep(0.01)
    main.cancel()  # what the SIGTERM handler does
    with pytest.raises(asyncio.CancelledError):
        await main
    assert states == ["cleaned", "cleaned"]
    assert all(t.done() for t in tasks)
    assert bus.closed and disposed == [True]


async def test_cleanup_disposes_engine_when_bus_close_raises(monkeypatch) -> None:
    from mavis import cli

    disposed: list[bool] = []

    async def fake_dispose() -> None:
        disposed.append(True)

    monkeypatch.setattr(cli, "dispose_engine", fake_dispose)
    with pytest.raises(RuntimeError, match="close failed"):
        await cli._cleanup([], _Bus(fail=True))
    assert disposed == [True]


async def test_main_cancels_on_sigterm() -> None:
    import asyncio
    import os
    import signal

    from mavis import cli

    cleaned: list[bool] = []

    async def role() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            cleaned.append(True)

    main = asyncio.create_task(cli._main(role()))
    await asyncio.sleep(0.05)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(main, 5)
    assert cleaned == [True]


@pytest.mark.parametrize("role", ["api", "worker"])
def test_multi_process_roles_require_redis(settings, monkeypatch, role) -> None:
    monkeypatch.setenv("REDIS_URL", "")
    started: list[str] = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: started.append("uvicorn"))
    monkeypatch.setattr("mavis.cli._run", lambda coro: (coro.close(), started.append("run")))
    result = CliRunner().invoke(app, [role])
    assert result.exit_code == 1
    assert "REDIS_URL" in result.output
    assert started == []

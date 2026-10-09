"""Live smoke test for AgentCore (spec 13.3). Run on the box after iam-role.sh and machine.sh:

    docker compose -f docker-compose.prod.yml exec -T worker python -m scripts.verify_agentcore

Prints PASS/FAIL per check with timings and an upper-bound cost. Uses synthetic user ids 0 and -1 that
never exist in the users table; touches no real user's workspace."""

from __future__ import annotations

import asyncio
import sys
import time

from mavis.machine.agentcore import AgentCoreSandbox
from mavis.machine.ports import ExecRequest
from mavis.machine.quota import Meter

CHECKS: list[tuple[str, bool, float]] = []


async def check(name, coro):
    t = time.monotonic()
    try:
        ok = bool(await coro)
    except Exception as exc:  # noqa: BLE001
        print(f"  error: {type(exc).__name__}: {exc}")
        ok = False
    CHECKS.append((name, ok, time.monotonic() - t))
    print(f"{'PASS' if ok else 'FAIL'} {name} ({time.monotonic() - t:.1f}s)")


async def main() -> int:
    sb = AgentCoreSandbox()
    started = time.monotonic()
    a = await sb.open(user_id=0, task_id=0, timeout_s=300)
    b = await sb.open(user_id=-1, task_id=0, timeout_s=300)
    try:
        await check("python runs", _eq(a, "print(6 * 7)", "42"))
        await check("file write and read back", _roundtrip(a))
        await check("network is blocked", _no_network(a))
        await check("sessions do not share files", _isolated(a, b))
        await check("timeout stops the session", _timeout(sb))
    finally:
        await a.close()
        await b.close()
    wall = time.monotonic() - started
    print(f"estimated cost (upper bound): ${Meter().cost('code', wall * 2):.4f}")
    return 0 if all(ok for _, ok, _ in CHECKS) else 1


async def _eq(s, code, want):
    return (await s.exec(ExecRequest(language="python", code=code, timeout_s=60))).stdout.strip() == want


async def _roundtrip(s):
    await s.write("work/verify.txt", b"mavis-verify")
    return await s.read("work/verify.txt") == b"mavis-verify"


async def _no_network(s):
    code = (
        "import socket\ntry:\n    socket.create_connection(('1.1.1.1', 443), timeout=5)\n    print('OPEN')\n"
        "except OSError:\n    print('BLOCKED')"
    )
    out = (await s.exec(ExecRequest(language="python", code=code, timeout_s=30))).stdout.strip()
    if out != "BLOCKED":
        print("  the managed interpreter has network: run deploy/aws/machine.sh --apply --custom-interpreter")
    return out == "BLOCKED"


async def _isolated(a, b):
    await a.write("work/only-a.txt", b"a")
    return "work/only-a.txt" not in {e.path for e in await b.list()}


async def _timeout(sb):
    s = await sb.open(user_id=0, task_id=1, timeout_s=120)
    res = await s.exec(
        ExecRequest(language="python", code="import time\nwhile True: time.sleep(1)", timeout_s=5)
    )
    return res.timed_out


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

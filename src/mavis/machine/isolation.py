"""Network isolation check for the code interpreter. The managed `aws.codeinterpreter.v1` may have egress;
the machine runs model-written code, so in prod it is only switched on when a probe from inside a session
proves there is none (or MACHINE_ALLOW_EGRESS=true says the owner accepts it). Fails closed: any answer
other than BLOCKED, including an error, counts as egress. The result is cached for the process."""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from mavis.machine.ports import ExecRequest, Sandbox

log = structlog.get_logger(__name__)
PROBE = (
    "import socket\n"
    "for host in ('1.1.1.1', '8.8.8.8'):\n"
    "    try:\n"
    "        socket.create_connection((host, 443), timeout=5).close()\n"
    "        print('OPEN')\n"
    "        break\n"
    "    except OSError:\n"
    "        pass\n"
    "else:\n"
    "    try:\n"
    "        socket.getaddrinfo('example.com', 443)\n"
    "        print('OPEN')\n"
    "    except OSError:\n"
    "        print('BLOCKED')\n"
)


@dataclass(frozen=True)
class IsolationResult:
    isolated: bool
    detail: str


_cached: IsolationResult | None = None


def cached() -> IsolationResult | None:
    return _cached


def reset() -> None:
    global _cached
    _cached = None


async def check_isolation(sandbox: Sandbox, *, force: bool = False) -> IsolationResult:
    global _cached
    if _cached is not None and not force:
        return _cached
    try:
        session = await sandbox.open(user_id=0, task_id=0, timeout_s=120)
        try:
            res = await session.exec(ExecRequest(language="python", code=PROBE, timeout_s=30))
        finally:
            await session.close()
        out = res.stdout.strip()
        result = (IsolationResult(True, "no egress") if res.ok and out == "BLOCKED"
                  else IsolationResult(False, "egress possible" if out == "OPEN" else "probe inconclusive"))
    except Exception as exc:  # noqa: BLE001 - fail closed
        result = IsolationResult(False, f"probe failed: {type(exc).__name__}")
    _cached = result
    log.info("machine.isolation_check", isolated=result.isolated, detail=result.detail)
    return result

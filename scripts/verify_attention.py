"""Scripted end-to-end check of the attention layer.

Offline (default): runs the attention test suite against fixtures (fake LLM, fake provider, SQLite, in-memory
Qdrant), including the end-to-end scenario, and exits with its code.

--live: reads your newest Gmail messages through Composio (the read-only mail.search action), runs each
through the real Understander (the FAST model on OLLAMA_BASE_URL) and prints what was extracted and the
cold-start verdict it would get (empty baselines, so nothing here is personalised).
It never sends anything, never calls a write action, and never opens a database, Qdrant or Telegram.
It refuses to run when DATABASE_URL points at a non-local host, so a production shell cannot be used
by accident. Needs OLLAMA_API_KEY and COMPOSIO_API_KEY.

    uv run python scripts/verify_attention.py
    uv run python scripts/verify_attention.py --live [--user 1] [--max 10] [--query "newer_than:2d"]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from urllib.parse import urlsplit

LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}


def database_is_local(url: str) -> bool:
    """True for an unset URL, a sqlite file, a unix socket or a loopback host. Anything else is refused."""
    if not url.strip():
        return True
    parts = urlsplit(url.strip())
    if parts.scheme.startswith("sqlite"):
        return True
    return (parts.hostname or "").lower() in LOCAL_HOSTS


def guard_database() -> str | None:
    """An error message when the configured database is not local, else None."""
    urls = {os.environ.get("DATABASE_URL", "")}
    try:
        from mavis.config import get_settings

        urls.add(get_settings().database_url)
    except Exception:  # noqa: BLE001 - settings are optional for the guard, the env var still counts
        pass
    for url in urls:
        if not database_is_local(url):
            host = urlsplit(url).hostname
            return f"refusing to run: DATABASE_URL points at a non-local host ({host})"
    return None


async def live(user_id: int, query: str, limit: int) -> int:
    from mavis.attention.anomaly import MoneyContext, combine, score_money, score_security
    from mavis.attention.baselines import MoneySnapshot
    from mavis.attention.policy import PolicyInputs, decide
    from mavis.attention.schema import METHOD_LABELS, NO_ANOMALY, EmailKind
    from mavis.attention.understand import Understander, heuristic
    from mavis.config import get_settings
    from mavis.domain import timeutil
    from mavis.domain.errors import LLMError
    from mavis.domain.integrations import UserRef
    from mavis.tools.integrations.actions import ACTIONS
    from mavis.tools.integrations.composio import ComposioProvider
    from mavis.tools.integrations.normalize import email_event, extract_messages, to_datetime

    s = get_settings()
    if not s.composio_api_key or not s.ollama_api_key:
        print("COMPOSIO_API_KEY and OLLAMA_API_KEY must both be set", file=sys.stderr)
        return 2
    if ACTIONS["mail.search"].risk.needs_approval:  # defence in depth: only a read action may run here
        print("mail.search is not a read action, refusing", file=sys.stderr)
        return 2
    provider = ComposioProvider(
        api_key=s.composio_api_key, base_url=s.composio_base_url, timeout_s=s.composio_timeout_s
    )
    try:
        res = await provider.execute(
            UserRef(user_id=user_id), "mail.search", {"query": query, "max_results": limit}
        )
    finally:
        await provider.aclose()
    if not res.ok:
        print(f"mail.search failed: {str(res.error)[:200]}", file=sys.stderr)
        return 1
    tz = "Asia/Kolkata"
    failures = 0
    seen = 0
    for raw in extract_messages(res.data):
        event = email_event(user_id, raw, source="verify")
        if event is None or event.payload.get("from_me"):
            continue
        p = event.payload
        seen += 1
        method = "llm"
        try:
            u = await Understander().understand(p, tz)
        except LLMError as exc:
            u, method = heuristic(p), "heuristic"
            print(f"  (model unavailable: {type(exc).__name__}, using the keyword fallback)")
            failures += 1
        received = to_datetime(p.get("received_at")) or timeutil.now()
        money_result = NO_ANOMALY
        if u.money is not None and u.money.amount > 0:
            currency = u.money.currency or s.attention_currency
            money_result = score_money(
                MoneyContext(
                    u.money.amount,
                    u.money.direction,
                    METHOD_LABELS[u.money.method],
                    timeutil.to_local(received, tz).hour,
                    MoneySnapshot(key=""),
                    large_amount=s.attention_large_amounts.get(currency),
                )
            )
        security = score_security(u.risk_flags) if u.kind is EmailKind.SECURITY else NO_ANOMALY
        d = decide(
            PolicyInputs(understanding=u, anomaly=combine(money_result, security), novelty=1.0, now=received),
            s,
        )
        subject = str(p.get("subject", ""))[:50]
        print(
            f"[{method}] {subject!r}: kind={u.kind.value} money={u.money} "
            f"verdict={d.verdict.value} urgency={d.urgency}"
        )
    print(f"\n{seen} message(s) checked, nothing sent, nothing stored")
    return 1 if failures or seen == 0 else 0


def offline() -> int:
    return subprocess.call(
        [sys.executable, "-m", "pytest", "-q", "tests/attention", "tests/agents/test_context_hooks.py"]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--live", action="store_true", help="read Gmail via Composio and call the real FAST model"
    )
    parser.add_argument("--user", type=int, default=1, help="Mavis user id whose Composio account to read")
    parser.add_argument("--max", type=int, default=10, help="messages to read (1 to 50)")
    parser.add_argument("--query", default="newer_than:2d", help="Gmail search query")
    args = parser.parse_args()
    if (problem := guard_database()) is not None:
        print(problem, file=sys.stderr)
        return 2
    if args.live:
        return asyncio.run(live(args.user, args.query, max(1, min(50, args.max))))
    return offline()


if __name__ == "__main__":
    sys.exit(main())

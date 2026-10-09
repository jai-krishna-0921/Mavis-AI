"""Every user table in FK-safe delete order (children first), one transaction, plus the extension point
other plans use for stores this module does not know (contract F)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy import delete

from mavis.store.db import Base, Session

# Children before parents. The meta-test fails when a table with user_id is missing here.
USER_TABLES: tuple[str, ...] = (
    "machine_sessions", "compute_usage", "workspace_files", "user_quotas",
    "web_sessions", "web_login_nonces", "user_emails",
    "personal_layers", "learning_suppressions", "personal_signals",
    "invite_redemptions", "native_oauth_states", "native_grants", "artifacts", "task_cards",
    "pending_approvals", "tasks", "initiative_decisions",
    "wakeups",
    "ping_log", "loops", "connections_pending", "attention_prefs", "attention_money_baselines",
    "attention_senders", "attention_observations", "policy_rules", "outbox", "messages", "llm_usage",
    "graph_edges", "graph_nodes", "profile_cards", "conversation_summaries", "audit_log",
)
# Tables that carry user_id but must outlive the user row: none. audit_log rows for the user are deleted
# and one fresh row with counts only is written after the cascade.

StepFn = Callable[[int], Awaitable[dict]]
EXTERNAL_STEPS: dict[str, StepFn] = {}


def register_deletion_step(name: str, fn: StepFn) -> None:
    EXTERNAL_STEPS[name] = fn


async def delete_user_rows(user_id: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    tables = Base.metadata.tables
    async with Session() as s:
        for name in USER_TABLES:
            table = tables.get(name)
            if table is None or "user_id" not in table.c:
                continue
            res = await s.execute(delete(table).where(table.c.user_id == user_id))
            counts[name] = res.rowcount or 0
        await s.commit()
    return counts

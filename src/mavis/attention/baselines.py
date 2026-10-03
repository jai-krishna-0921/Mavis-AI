"""Per-user rolling statistics: who emails the user and how money usually leaves. Deterministic, no LLM."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from mavis.store.db import Session
from mavis.store.models import AttentionMoneyBaseline, AttentionSender

SAMPLE = 50  # bounded recent sample per baseline row
ALL = "*"
ESTABLISHED_COUNT = 3
ESTABLISHED_AGE = timedelta(days=7)
SCOPES = ("counterparty", "method", "all")
ADDRESS_MAX = 200  # attention_senders.address column length (an attacker chooses the From address)


def _address(address: str) -> str:
    return str(address or "").lower()[:ADDRESS_MAX]


@dataclass(frozen=True)
class SenderStats:
    count: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None

    def established(self, now: datetime) -> bool:
        return (
            self.count >= ESTABLISHED_COUNT
            and self.first_seen is not None
            and now - self.first_seen >= ESTABLISHED_AGE
        )


@dataclass(frozen=True)
class MoneyStats:
    count: int = 0
    median: float = 0.0
    mad: float = 0.0
    hours: tuple[int, ...] = (0,) * 24
    first_seen: datetime | None = None
    last_seen: datetime | None = None


@dataclass(frozen=True)
class MoneySnapshot:
    key: str
    counterparty: MoneyStats = field(default_factory=MoneyStats)
    method: MoneyStats = field(default_factory=MoneyStats)
    overall: MoneyStats = field(default_factory=MoneyStats)


def robust(amounts: list[float]) -> tuple[float, float]:
    """Median and median absolute deviation: one fraudulent outlier cannot move them much."""
    if not amounts:
        return 0.0, 0.0
    med = float(statistics.median(amounts))
    return med, float(statistics.median(abs(a - med) for a in amounts))


def _stats(row: AttentionMoneyBaseline | None) -> MoneyStats:
    if row is None:
        return MoneyStats()
    return MoneyStats(
        row.count, row.median, row.mad, tuple(row.hours or (0,) * 24), row.first_seen, row.last_seen
    )


class Baselines:
    async def sender(self, user_id: int, address: str) -> SenderStats:
        async with Session() as s:
            row = await s.scalar(
                select(AttentionSender).where(
                    AttentionSender.user_id == user_id, AttentionSender.address == _address(address)
                )
            )
        return SenderStats(row.count, row.first_seen, row.last_seen) if row else SenderStats()

    async def established_domains(self, user_id: int, now: datetime) -> set[str]:
        async with Session() as s:
            rows = list(
                await s.scalars(
                    select(AttentionSender).where(
                        AttentionSender.user_id == user_id, AttentionSender.count >= ESTABLISHED_COUNT
                    )
                )
            )
        return {r.domain for r in rows if r.domain and now - r.first_seen >= ESTABLISHED_AGE}

    async def touch_sender(self, user_id: int, address: str, domain: str, at: datetime) -> None:
        address = _address(address)
        if not address:
            return
        for _ in range(2):  # one retry if a concurrent first insert wins the unique constraint
            async with Session() as s:
                row = await s.scalar(
                    select(AttentionSender).where(
                        AttentionSender.user_id == user_id, AttentionSender.address == address
                    )
                )
                if row is None:
                    s.add(
                        AttentionSender(
                            user_id=user_id,
                            address=address,
                            domain=domain[:120],
                            count=1,
                            first_seen=at,
                            last_seen=at,
                        )
                    )
                else:
                    row.count += 1
                    row.first_seen, row.last_seen = min(row.first_seen, at), max(row.last_seen, at)
                try:
                    await s.commit()
                    return
                except IntegrityError:
                    await s.rollback()

    async def counterparty_keys(self, user_id: int, currency: str) -> list[str]:
        async with Session() as s:
            rows = await s.scalars(
                select(AttentionMoneyBaseline.key)
                .where(
                    AttentionMoneyBaseline.user_id == user_id,
                    AttentionMoneyBaseline.currency == currency,
                    AttentionMoneyBaseline.scope == "counterparty",
                )
                .order_by(AttentionMoneyBaseline.key)
            )
            return list(rows)

    async def snapshot(self, user_id: int, currency: str, key: str, method: str) -> MoneySnapshot:
        wanted = {("counterparty", key), ("method", method), ("all", ALL)}
        async with Session() as s:
            rows = list(
                await s.scalars(
                    select(AttentionMoneyBaseline).where(
                        AttentionMoneyBaseline.user_id == user_id, AttentionMoneyBaseline.currency == currency
                    )
                )
            )
        found = {(r.scope, r.key): r for r in rows if (r.scope, r.key) in wanted}
        return MoneySnapshot(
            key=key,
            counterparty=_stats(found.get(("counterparty", key))) if key else MoneyStats(),
            method=_stats(found.get(("method", method))),
            overall=_stats(found.get(("all", ALL))),
        )

    async def record_money(
        self, user_id: int, currency: str, key: str, method: str, amount: float, local_hour: int, at: datetime
    ) -> None:
        for scope, k in zip(SCOPES, (key, method, ALL), strict=True):
            if k:
                await self._record(user_id, scope, k, currency, float(amount), local_hour % 24, at)

    async def _record(
        self, user_id: int, scope: str, key: str, currency: str, amount: float, hour: int, at: datetime
    ) -> None:
        for _ in range(2):
            async with Session() as s:
                row = await s.scalar(
                    select(AttentionMoneyBaseline).where(
                        AttentionMoneyBaseline.user_id == user_id,
                        AttentionMoneyBaseline.scope == scope,
                        AttentionMoneyBaseline.key == key,
                        AttentionMoneyBaseline.currency == currency,
                    )
                )
                if row is None:
                    row = AttentionMoneyBaseline(
                        user_id=user_id,
                        scope=scope,
                        key=key,
                        currency=currency,
                        count=0,
                        amounts=[],
                        median=0.0,
                        mad=0.0,
                        hours=[0] * 24,
                        first_seen=at,
                        last_seen=at,
                    )
                    s.add(row)
                amounts = [*(row.amounts or []), amount][-SAMPLE:]
                hours = list(row.hours or [0] * 24)
                hours[hour] += 1
                row.amounts, row.hours = (
                    amounts,
                    hours,
                )  # reassign: plain JSON columns are not mutation-tracked
                row.count = (row.count or 0) + 1
                row.median, row.mad = robust(amounts)
                row.first_seen, row.last_seen = min(row.first_seen, at), max(row.last_seen, at)
                try:
                    await s.commit()
                    return
                except IntegrityError:
                    await s.rollback()

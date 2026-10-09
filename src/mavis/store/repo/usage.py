from __future__ import annotations

from datetime import date

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from mavis.store.db import Session
from mavis.store.models import LlmUsage


async def add(user_id: int, day: date, provider: str, model: str, purpose: str, prompt: int, completion: int,
              cost: int) -> None:
    key = (LlmUsage.user_id == user_id, LlmUsage.day == day, LlmUsage.provider == provider,
           LlmUsage.model == model, LlmUsage.purpose == purpose)
    vals = {"calls": LlmUsage.calls + 1, "prompt_tokens": LlmUsage.prompt_tokens + prompt,
            "completion_tokens": LlmUsage.completion_tokens + completion,
            "cost_micros": LlmUsage.cost_micros + cost}
    for _ in range(3):
        async with Session() as s:
            res = await s.execute(update(LlmUsage).where(*key).values(**vals))
            if res.rowcount == 0:
                s.add(LlmUsage(user_id=user_id, day=day, provider=provider, model=model, purpose=purpose,
                               calls=1, prompt_tokens=prompt, completion_tokens=completion,
                               cost_micros=cost))
            try:
                await s.commit()
                return
            except IntegrityError:
                await s.rollback()  # a concurrent insert won: retry as an update


async def daily(user_id: int, day: date) -> list[LlmUsage]:
    async with Session() as s:
        return list(await s.scalars(select(LlmUsage).where(LlmUsage.user_id == user_id, LlmUsage.day == day)))


async def all_for_user(user_id: int) -> list[LlmUsage]:
    async with Session() as s:
        return list(await s.scalars(select(LlmUsage).where(LlmUsage.user_id == user_id)))


async def month_micros(since: date) -> int:
    from sqlalchemy import func

    async with Session() as s:
        return int(await s.scalar(select(func.coalesce(func.sum(LlmUsage.cost_micros), 0))
                                  .where(LlmUsage.day >= since)) or 0)

from sqlalchemy import select

from mavis.domain.decisions import ComposedMessage, NotifyIntent
from mavis.domain.messages import Button
from mavis.initiative.wiring import build_initiative
from mavis.store.db import Session
from mavis.store.models import OutboxMessage

ROWS = [[Button(label="Yes", data="at:y:1")]]
DUMPED = [[{"label": "Yes", "data": "at:y:1", "url": None}]]


async def no_embed(texts):
    return [[1.0, 0.0] for _ in texts]


async def _outbox() -> list[OutboxMessage]:
    async with Session() as s:
        return list(await s.scalars(select(OutboxMessage).order_by(OutboxMessage.id)))


async def test_deliver_attaches_buttons_to_last_bubble(user, clock, recording_bus, fake_memory):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    await init.executor.deliver(user, ["one", "two"], "k1", 4, buttons=ROWS)
    assert [o.buttons for o in await _outbox()] == [[], DUMPED]


async def test_deliver_without_buttons_is_unchanged(user, clock, recording_bus, fake_memory):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    await init.executor.deliver(user, ["solo"], "k0")
    assert [o.buttons for o in await _outbox()] == [[]]


async def test_notify_passes_buttons(user, clock, recording_bus, fake_memory, fake_llm):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Heads up."]))
    assert await init.executor.notify(
        user, NotifyIntent(urgency=3, intent="x", dedupe_key="k2"), buttons=ROWS
    )
    assert [o.buttons for o in await _outbox()] == [DUMPED]

"""A real attention stack on SQLite + in-memory Qdrant, with the scripted FakeLLM and a fake provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient

from mavis.attention.baselines import Baselines
from mavis.attention.feedback import FeedbackHandler
from mavis.attention.index import AttentionIndex
from mavis.attention.intake import Intake
from mavis.attention.learning import Thresholds
from mavis.attention.pipeline import AttentionPipeline
from mavis.attention.speaker import Speaker
from mavis.attention.understand import Understander
from mavis.initiative import wiring
from mavis.initiative.wiring import build_initiative
from mavis.policy.pings import PingPolicy


@dataclass
class Stack:
    init: Any
    index: AttentionIndex
    baselines: Baselines
    thresholds: Thresholds
    pipeline: AttentionPipeline
    intake: Intake
    feedback: FeedbackHandler
    forwarded: list = field(default_factory=list)
    first_looks: list = field(default_factory=list)


@pytest.fixture
async def stack(user, clock, recording_bus, fake_memory, fake_llm, embedder, provider):
    async def no_embed(texts):
        return [[1.0, 0.0] for _ in texts]

    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    wiring.set_current(init)
    client = AsyncQdrantClient(location=":memory:")
    index = AttentionIndex(client, embedder)
    baselines, thresholds = Baselines(), Thresholds()
    speaker = Speaker(lambda: init.executor, PingPolicy(), init.wakeups)
    pipeline = AttentionPipeline(
        understander=Understander(), baselines=baselines, index=index, speaker=speaker, thresholds=thresholds
    )
    forwarded: list = []
    first_looks: list = []

    async def forward(event):
        forwarded.append(event)

    async def on_empty(u):
        first_looks.append(u.id)

    intake = Intake(
        pipeline=pipeline,
        loops=init.loops,
        wakeups=init.wakeups,
        thresholds=thresholds,
        forward=forward,
        provider=provider,
        on_backlog_empty=on_empty,
    )
    feedback = FeedbackHandler(
        loops=init.loops, wakeups=init.wakeups, baselines=baselines, index=index, thresholds=thresholds
    )
    yield Stack(init, index, baselines, thresholds, pipeline, intake, feedback, forwarded, first_looks)
    await client.close()

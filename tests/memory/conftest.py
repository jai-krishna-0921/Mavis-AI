"""Fixtures for the personal layer tests: three personas and an installable scripted phrasing model."""

from __future__ import annotations

import pytest

from mavis.llm import models as llm
from tests.memory.personas import ENGINEER, FOUNDER, STUDENT, Persona, Phraser, create


def _fresh(p: Persona) -> Persona:
    return Persona(**{**p.__dict__, "jobs": []})


@pytest.fixture
async def founder(db):
    return await create(_fresh(FOUNDER))


@pytest.fixture
async def engineer(db):
    return await create(_fresh(ENGINEER))


@pytest.fixture
async def student(db):
    return await create(_fresh(STUDENT))


@pytest.fixture
def phraser(monkeypatch):
    def install(mode="faithful", extractions=None):
        p = Phraser(mode, extractions)
        monkeypatch.setattr(llm, "structured", p.structured)
        return p

    return install

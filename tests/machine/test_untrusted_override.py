from __future__ import annotations

import pytest
from pydantic import BaseModel

from mavis.domain.policy import RiskClass
from mavis.domain.results import ToolOutput
from mavis.tools.registry import MavisTool, ToolRun, current_run


class A(BaseModel):
    x: str = ""


@pytest.mark.parametrize(
    "declared,override,wrapped",
    [(False, True, True), (True, False, False), (True, None, True), (False, None, False)],
)
async def test_result_level_untrusted_overrides_the_tool_default(
    db, user, fresh_registry, declared, override, wrapped
):
    async def fn(user_id, args):
        return ToolOutput(model_note="exit 0: 41 lines", untrusted=override)

    fresh_registry.register(
        MavisTool(
            f"t_{declared}_{override}",
            "d",
            A,
            RiskClass.READ,
            fn,
            frozenset({"analyst"}),
            untrusted_output=declared,
        )
    )
    run = ToolRun()
    token = current_run.set(run)
    try:
        out = await fresh_registry.invoke(fresh_registry.get(f"t_{declared}_{override}"), user.id, A())
    finally:
        current_run.reset(token)
    assert ("<untrusted" in out) is wrapped
    assert run.untrusted_seen is wrapped

"""Built-in tool loading. Later tasks and phases append their tool modules here."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mavis.tools.registry import ToolRegistry


def load_builtin_tools(registry: ToolRegistry) -> None:
    from mavis.tools import assistant, web

    for module in (assistant, web):
        for tool in module.TOOLS:
            registry.register(tool)

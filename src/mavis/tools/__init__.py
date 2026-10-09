"""Built-in tool loading. Later tasks and phases append their tool modules here."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mavis.tools.registry import ToolRegistry


def load_builtin_tools(registry: ToolRegistry) -> None:
    from mavis.tools import assistant, chat_tools, preferences, web

    for tool in (*assistant.TOOLS, *web.TOOLS, *chat_tools.current_tools(), *preferences.TOOLS):
        registry.register(tool)

    from mavis.tools.integrations.tools import register_integration_tools

    register_integration_tools(registry)

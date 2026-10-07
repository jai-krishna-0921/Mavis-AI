"""What a tool returns when its result can reach the user as well as the model (hotfix4 H3).

A tool result has two readers. The model reads everything (what happened plus how to talk about it); the
user, when an approved action's receipt or a task completion is rendered, must read only plain words about
what happened. A tool that has something worth showing the user returns `ToolOutput`; a tool that returns a
plain string is model-facing only, and a user-facing path shows nothing of it (just its own receipt).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolOutput:
    user_text: str = ""  # plain words safe to show the user as written; may be empty
    model_note: str = ""  # for the model only: instructions, ids, raw details

    def for_model(self) -> str:
        return "\n".join(p for p in (self.user_text, self.model_note) if p)

    @staticmethod
    def of(value: object) -> ToolOutput:
        """Any tool return value as a ToolOutput: a plain string or data is model-only."""
        if isinstance(value, ToolOutput):
            return value
        return ToolOutput(model_note=value if isinstance(value, str) else str(value))

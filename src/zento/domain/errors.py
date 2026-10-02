from __future__ import annotations

from zento.domain.policy import Capability


class ZentoError(Exception):
    """Base error."""


class LLMError(ZentoError):
    """Model unreachable, timed out, or produced unusable output after retries."""


class ConnectionRequired(ZentoError):
    def __init__(self, capability: Capability, reason: str) -> None:
        super().__init__(f"{capability.value} not connected: {reason}")
        self.capability = capability
        self.reason = reason


class ApprovalRequired(ZentoError):
    def __init__(self, action: str, preview: str, arguments: dict) -> None:
        super().__init__(f"approval required for {action}")
        self.action = action
        self.preview = preview
        self.arguments = arguments


class BudgetExceeded(ZentoError):
    """A task exceeded its step/token/time budget."""

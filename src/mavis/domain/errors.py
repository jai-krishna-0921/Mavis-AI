from __future__ import annotations

from mavis.domain.policy import Capability


class MavisError(Exception):
    """Base error."""


class LLMError(MavisError):
    """Model unreachable, timed out, or produced unusable output after retries."""


class ConnectionRequired(MavisError):
    def __init__(self, capability: Capability, reason: str, *, revoked: bool = False) -> None:
        super().__init__(f"{capability.value} not connected: {reason}")
        self.capability = capability
        self.reason = reason
        self.revoked = revoked


class ApprovalRequired(MavisError):
    def __init__(self, action: str, preview: str, arguments: dict) -> None:
        super().__init__(f"approval required for {action}")
        self.action = action
        self.preview = preview
        self.arguments = arguments


class BudgetExceeded(MavisError):
    """A task exceeded its step/token/time budget."""


class IntegrationError(MavisError):
    """Provider unreachable, misconfigured or refused. Message is safe to show; never contains credentials."""


class WebhookVerificationError(IntegrationError):
    """Inbound webhook failed signature/timestamp verification."""

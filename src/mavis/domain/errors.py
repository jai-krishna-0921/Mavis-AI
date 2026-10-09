from __future__ import annotations

from enum import StrEnum

from mavis.domain.policy import Capability


class FailureKind(StrEnum):
    """The one vocabulary for why an outside action did not happen (hotfix4 H3). Provider adapters
    classify their failures into these; what the user reads is built from the kind, never from a
    provider's own error body."""

    INVALID_ARGUMENT = "invalid_argument"
    AUTH = "auth"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    UNCONFIRMED = "unconfirmed"  # sent, no answer: it may have happened
    PERMISSION_MISSING = "permission_missing"  # the user's grant lacks the permission this action needs
    UNKNOWN = "unknown"


_FAILURE_TEXT: dict[FailureKind, str] = {
    FailureKind.INVALID_ARGUMENT: "{service} did not accept some of the details{field}",
    FailureKind.AUTH: "{service} refused access, so it may need reconnecting",
    FailureKind.NOT_FOUND: "{service} could not find it{field}",
    FailureKind.RATE_LIMITED: "{service} is busy right now, try again in a few minutes",
    FailureKind.UNAVAILABLE: "{service} is unreachable right now",
    FailureKind.UNCONFIRMED: "{service} did not confirm whether that went through, so it may or may not "
                             "have happened. Check there before trying again",
    FailureKind.PERMISSION_MISSING: "{service} was not given permission for that. Reconnect it and allow "
                                    "the extra access",
    FailureKind.UNKNOWN: "{service} reported an error",
}


def failure_text(kind: FailureKind, service: str, field: str | None = None) -> str:
    """Plain words for the user: the service and the kind of failure, plus the argument it was about
    (an argument name of ours, e.g. "attendees"), never the provider's message."""
    about = f" (the {field.replace('_', ' ')})" if field else ""
    return _FAILURE_TEXT[kind].format(service=service, field=about)


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
        # Filled by agents.react.react_loop when a tool raises this mid-run, so the caller can still
        # report what did happen (e.g. "reminder set, now connect Gmail") and attach queued approvals.
        self.partial_messages: list = []  # ToolMessages of the interrupted step, in call order
        self.queued_approvals: list[int] = []  # pending_approvals ids queued earlier in the run


class ApprovalRequired(MavisError):
    def __init__(self, action: str, preview: str, arguments: dict) -> None:
        super().__init__(f"approval required for {action}")
        self.action = action
        self.preview = preview
        self.arguments = arguments


class ActionFailed(MavisError):
    """A tool ran but its action did not happen (provider refused or was unreachable).

    `str(exc)` is the sentence for the model; `reason` is the short cause safe to show the user.
    The registry turns it back into a tool result for model-driven calls and lets it propagate from
    `execute_approved`, so an approved action that failed is never recorded as executed.
    """

    def __init__(self, message: str, reason: str = "", kind: FailureKind = FailureKind.UNKNOWN,
                 field: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason or message
        self.kind = kind
        self.field = field


class NeedsUserDetail(ValueError):
    """Raised by an args model validator. `str(exc)` is for the model; `user_text` is what the user
    sees if a saved (approved) request fails that rule at execution time."""

    def __init__(self, message: str, user_text: str) -> None:
        super().__init__(message)
        self.user_text = user_text


class BudgetExceeded(MavisError):
    """A task exceeded its step/token/time budget."""


class IntegrationError(MavisError):
    """Provider unreachable, misconfigured or refused. Message is safe to show; never contains credentials.

    `status` is the HTTP status when the provider answered one (None: never reached, or no status)."""

    def __init__(self, message: str = "", *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NoSuchConnection(IntegrationError):
    """Disconnect asked for an account the user does not have."""


class WebhookVerificationError(IntegrationError):
    """Inbound webhook failed signature/timestamp verification."""

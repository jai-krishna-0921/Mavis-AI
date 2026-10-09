from __future__ import annotations

from mavis.domain.errors import MavisError


class SandboxPathError(ValueError):
    """A workspace path that is absolute, climbs out with '..', or resolves outside the workspace."""


class MachineBusy(MavisError):
    """The backend throttled us or every global slot is taken; try again shortly."""


class MachineUnavailable(MavisError):
    """The backend is down or unreachable."""


class SessionUserMismatch(MavisError):
    """A session row belongs to another user than the caller (never reuse across users)."""


class QuotaExceeded(MavisError):
    def __init__(self, user_text: str) -> None:
        super().__init__(user_text)
        self.user_text = user_text

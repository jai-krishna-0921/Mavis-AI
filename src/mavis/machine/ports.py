"""The three machine ports (sandbox, browser, workspace store) and their shared shapes.

Adapters (fake, local, AgentCore, S3) implement these; the runtime and tools only see the ports."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from mavis.machine.paths import guard


class Provenance(StrEnum):
    USER_UPLOAD = "user_upload"
    FETCHED = "fetched"
    GENERATED_CLEAN = "generated_clean"
    GENERATED_TAINTED = "generated_tainted"
    MAVIS = "mavis"

    @property
    def untrusted(self) -> bool:
        return self in (Provenance.USER_UPLOAD, Provenance.FETCHED, Provenance.GENERATED_TAINTED)


class FileClass(StrEnum):
    INBOX = "inbox"
    OUT = "out"
    WORK = "work"
    META = "meta"


def class_of(path: str) -> FileClass:
    head = guard(path).split("/", 1)[0]
    known = {"inbox": FileClass.INBOX, "out": FileClass.OUT, ".mavis": FileClass.META}
    return known.get(head, FileClass.WORK)


class FileEntry(BaseModel):
    path: str  # relative to the workspace root
    size: int
    sha256: str | None = None
    mtime: float | None = None


class ExecRequest(BaseModel):
    language: Literal["python", "shell"]
    code: str
    timeout_s: int = 60  # clamped to SANDBOX_EXEC_MAX_S by the runtime


class ExecResult(BaseModel):
    ok: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str | None = None  # adapter or transport error, plain words
    duration_s: float = 0.0
    changed: list[FileEntry] = Field(default_factory=list)  # filled by the runtime from a listing diff


class BackendHealth(BaseModel):
    ok: bool
    detail: str = ""


OutputFn = Callable[[str], Awaitable[None]]


class SandboxSession(Protocol):
    id: str

    async def exec(self, req: ExecRequest, on_output: OutputFn | None = None) -> ExecResult: ...
    async def write(self, path: str, data: bytes) -> None: ...
    async def read(self, path: str) -> bytes: ...
    async def list(self, path: str = "") -> list[FileEntry]: ...
    async def remove(self, path: str) -> None: ...
    async def close(self) -> None: ...  # idempotent


class Sandbox(Protocol):
    name: str

    async def open(self, *, user_id: int, task_id: int, timeout_s: int) -> SandboxSession: ...
    async def stop(self, session_id: str) -> None: ...  # reaper and cancel, by id
    async def health(self) -> BackendHealth: ...


class WorkspaceFile(BaseModel):
    user_id: int
    path: str
    size: int
    sha256: str | None = None
    cls: FileClass
    provenance: Provenance
    task_id: int | None = None


class WorkspaceStore(Protocol):
    """Bytes in object storage; metadata rows are the source of truth."""

    async def get(self, user_id: int, path: str) -> bytes: ...
    async def put(self, user_id: int, path: str, data: bytes, *, provenance: Provenance,
                  cls: FileClass | None = None, task_id: int | None = None) -> WorkspaceFile: ...
    async def delete(self, user_id: int, path: str) -> None: ...
    async def list(self, user_id: int, prefix: str = "") -> list[WorkspaceFile]: ...
    async def meta(self, user_id: int, path: str) -> WorkspaceFile | None: ...
    async def purge_user(self, user_id: int) -> None: ...


# --- browser shapes (Slice C builds the adapters) ---------------------------------------


class BrowserAction(BaseModel):
    kind: Literal["click", "type", "select", "press", "scroll", "back"]
    ref: int | None = None
    value: str | None = None


class PageLink(BaseModel):
    ref: int
    text: str  # untrusted
    href: str  # absolute, code-checked


class PageState(BaseModel):
    url: str  # final URL after redirects, observed by code
    title: str
    text: str  # ARIA snapshot rendered with [ref] ids; untrusted
    links: list[PageLink] = Field(default_factory=list)
    screenshot_ref: str | None = None


class ElementFacts(BaseModel):
    ref: int
    role: str
    tag: str
    input_type: str | None = None
    autocomplete: str | None = None
    form_method: str | None = None
    form_action: str | None = None
    href: str | None = None
    label: str = ""  # untrusted


class BrowserSession(Protocol):
    id: str

    async def goto(self, url: str) -> PageState: ...
    async def snapshot(self) -> PageState: ...
    async def element(self, ref: int) -> ElementFacts: ...
    async def act(self, action: BrowserAction) -> PageState: ...
    async def screenshot(self, *, full_page: bool = False) -> bytes: ...
    async def live_view_url(self, ttl_s: int) -> str | None: ...
    async def close(self) -> None: ...


class BrowserBackend(Protocol):
    name: str

    async def open(self, *, user_id: int, task_id: int, timeout_s: int) -> BrowserSession: ...
    async def stop(self, session_id: str) -> None: ...

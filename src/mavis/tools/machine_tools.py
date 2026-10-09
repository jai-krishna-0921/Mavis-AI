"""Code and file tools for the machine specialists (spec 7.1). Never offered to chat: a chat request that
needs the machine starts a task, and the card makes the work visible. All run at background priority
inside a task; labels for the card are code-made."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from mavis import machine
from mavis.config import get_settings
from mavis.domain.errors import ActionFailed
from mavis.domain.policy import Capability, RiskClass
from mavis.domain.results import ToolOutput
from mavis.machine.errors import MachineBusy, QuotaExceeded, SandboxPathError
from mavis.machine.paths import guard
from mavis.machine.ports import ExecRequest, ExecResult, Provenance
from mavis.tools.registry import (
    MavisTool,
    Prepared,
    TaintPolicy,
    ToolContext,
    ToolRegistry,
    current_run,
    current_task_id,
    host_of,
)

AGENTS = frozenset({"analyst", "docs", "operator", "spawn"})
NO_TASK_TEXT = "The machine only runs inside a task."
FETCH_REFUSED = "I can't fetch that address."


def _ctx() -> tuple[object, int]:
    rt, task_id = machine.get_runtime(), current_task_id.get()
    if rt is None or task_id is None:
        raise ActionFailed(NO_TASK_TEXT, reason=NO_TASK_TEXT)
    return rt, task_id


def _guarded(fn):
    """Quota, busy and path problems become plain sentences for the model (the step can wrap up)."""

    async def wrapped(user_id, args):
        try:
            return await fn(user_id, args)
        except QuotaExceeded as exc:
            return ToolOutput(user_text=exc.user_text)
        except MachineBusy as exc:
            return ToolOutput(model_note=f"The machine is busy: {exc}. Try once more later, or wrap up.")
        except SandboxPathError as exc:
            return ToolOutput(model_note=f"Bad path: {exc}. Use a relative path like out/chart.png.")
        except ActionFailed as exc:
            return ToolOutput(model_note=str(exc))

    wrapped.__name__ = fn.__name__
    return wrapped


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} MB"


def _render(res: ExecResult) -> str:
    head = (
        "timed out" if res.timed_out else (f"exit {res.exit_code}" if res.exit_code is not None else "error")
    )
    parts = [head]
    if res.error and not res.timed_out:
        parts.append(f"error: {res.error}")
    if res.stdout:
        parts.append(f"stdout:\n{res.stdout}")
    if res.stderr:
        parts.append(f"stderr:\n{res.stderr}")
    if res.changed:
        parts.append("files changed: " + ", ".join(f"{c.path} ({_size(c.size)})" for c in res.changed))
    return "\n".join(parts)


class RunPythonArgs(BaseModel):
    code: str = Field(description="Python source to run in the user's workspace (cwd is the workspace root)")
    purpose: str = Field(default="", max_length=80, description="Short label for logs")
    timeout_s: int | None = Field(default=None, ge=1, le=300)


class RunShellArgs(BaseModel):
    cmd: str
    purpose: str = Field(default="", max_length=80)
    timeout_s: int | None = Field(default=None, ge=1, le=300)


async def _exec(user_id: int, language: str, code: str, timeout_s: int | None) -> ToolOutput:
    rt, task_id = _ctx()
    req = ExecRequest(
        language=language, code=code, timeout_s=timeout_s or get_settings().sandbox_exec_timeout_s
    )
    res = await rt.exec(user_id, task_id, req)
    return ToolOutput(model_note=_render(res), untrusted=rt.untrusted(task_id))


@_guarded
async def machine_run_python(user_id: int, args: RunPythonArgs) -> ToolOutput:
    return await _exec(user_id, "python", args.code, args.timeout_s)


@_guarded
async def machine_run_shell(user_id: int, args: RunShellArgs) -> ToolOutput:
    return await _exec(user_id, "shell", args.cmd, args.timeout_s)


def _attempt_label(lang: str):
    def label(args, out) -> str:
        rt, task_id = machine.get_runtime(), current_task_id.get()
        log = rt.exec_log(task_id) if rt is not None and task_id is not None else []
        if not log:
            return f"ran {lang}"
        last = log[-1]
        state = "timed out" if last.timed_out else f"exit {last.exit_code}"
        tail = ", fixing" if not last.ok else ""
        return f"ran {lang}, attempt {len(log)} ({state}){tail}"

    return label


class InstallArgs(BaseModel):
    packages: list[str] = Field(min_length=1, max_length=10)


@_guarded
async def machine_install(user_id: int, args: InstallArgs) -> ToolOutput:
    rt, task_id = _ctx()
    try:
        res = await rt.install(user_id, task_id, args.packages)
    except ValueError as exc:
        return ToolOutput(model_note=f"Could not install: {exc}")
    return ToolOutput(model_note=_render(res))


class FetchArgs(BaseModel):
    url: str
    path: str = Field(description="Where to save it, e.g. inbox/data.csv")


async def _fetch_prepare(ctx: ToolContext, args: FetchArgs) -> Prepared:
    from mavis.tools import web

    try:
        await web.assert_public_url(args.url)
    except ValueError:
        return Prepared(refusal=FETCH_REFUSED)
    deny = {d.lower() for d in get_settings().browser_domain_deny}
    if host_of(args.url) in deny:
        return Prepared(refusal=FETCH_REFUSED)
    return Prepared()


@_guarded
async def machine_fetch(user_id: int, args: FetchArgs) -> ToolOutput:
    from mavis.tools import web

    rt, task_id = _ctx()
    try:
        body, final, ctype = await web.guarded_get_bytes(args.url, get_settings().max_upload_mb * 1024 * 1024)
    except ValueError:
        return ToolOutput(model_note=FETCH_REFUSED)
    await rt.write_in(user_id, task_id, guard(args.path), body, provenance=Provenance.FETCHED)
    return ToolOutput(
        model_note=(
            f"saved {guard(args.path)} ({_size(len(body))}, {ctype or 'unknown type'}) from {host_of(final)}"
        ),
        untrusted=True,
    )


class PathArgs(BaseModel):
    path: str


class ListArgs(BaseModel):
    path: str = ""


@_guarded
async def files_list(user_id: int, args: ListArgs) -> ToolOutput:
    rt, _task = _ctx()
    rows = await rt.store.list(user_id, guard(args.path) if args.path else "")
    if not rows:
        return ToolOutput(model_note="no files")
    return ToolOutput(model_note="\n".join(f"{f.path}  {_size(f.size)}  {f.provenance.value}" for f in rows))


class ReadArgs(BaseModel):
    path: str
    max_chars: int = Field(default=6000, ge=200, le=20000)


@_guarded
async def files_read(user_id: int, args: ReadArgs) -> ToolOutput:
    rt, task_id = _ctx()
    meta = await rt.store.meta(user_id, guard(args.path))
    if meta is None:
        return ToolOutput(model_note=f"{args.path} does not exist")
    text = await rt.extract_text(user_id, task_id, meta.path, args.max_chars)  # Task 18 builder
    if meta.provenance.untrusted:
        rt.mark_untrusted(task_id)
    return ToolOutput(model_note=text, untrusted=meta.provenance.untrusted)


class WriteArgs(BaseModel):
    path: str
    content: str = Field(max_length=500_000)


@_guarded
async def files_write(user_id: int, args: WriteArgs) -> ToolOutput:
    rt, task_id = _ctx()
    run = current_run.get()
    prov = Provenance.GENERATED_TAINTED if (run is not None and run.tainted) else Provenance.GENERATED_CLEAN
    await rt.write_in(user_id, task_id, guard(args.path), args.content.encode(), provenance=prov)
    return ToolOutput(model_note=f"wrote {guard(args.path)} ({_size(len(args.content.encode()))})")


@_guarded
async def files_attach(user_id: int, args: PathArgs) -> ToolOutput:
    rt, task_id = _ctx()
    await rt.attach(user_id, task_id, guard(args.path))
    return ToolOutput(model_note=f"{guard(args.path)} is in the machine now")


async def _delete_prepare(ctx: ToolContext, args: PathArgs) -> Prepared:
    rt, task_id = machine.get_runtime(), current_task_id.get()
    if rt is None or task_id is None:
        return Prepared(refusal=NO_TASK_TEXT)
    meta = await rt.store.meta(ctx.user_id, guard(args.path))
    if meta is None:
        return Prepared(refusal=f"{args.path} does not exist")
    if meta.task_id != task_id:
        return Prepared(
            risk=RiskClass.DESTRUCTIVE, note=f"File: {meta.path} ({_size(meta.size)}), made earlier"
        )
    return Prepared()


@_guarded
async def files_delete(user_id: int, args: PathArgs) -> ToolOutput:
    rt, _task = _ctx()
    await rt.store.delete(user_id, guard(args.path))
    return ToolOutput(model_note=f"deleted {guard(args.path)}")


class SendArgs(BaseModel):
    path: str
    caption: str = Field(default="", max_length=200, description="Ignored: captions are made by code")


@_guarded
async def files_send(user_id: int, args: SendArgs) -> ToolOutput:
    from mavis.machine.paths import safe_name

    rt, task_id = _ctx()
    meta = await rt.store.meta(user_id, guard(args.path))
    if meta is None:
        return ToolOutput(model_note=f"{args.path} does not exist")
    data = await rt.store.get(user_id, meta.path)
    name = safe_name(Path(meta.path).name)
    aid = await rt.add_artifact(user_id, task_id, name, data, title=f"{name} ({_size(len(data))})")
    await rt.deliver(user_id, task_id, aid)
    return ToolOutput(user_text=f"Sent {name}.", model_note=f"sent {name} ({_size(len(data))})")


_EXEC_TIMEOUT = lambda: get_settings().sandbox_exec_max_s + 30  # noqa: E731


def _tools() -> list[MavisTool]:
    t = _EXEC_TIMEOUT()
    return [
        MavisTool(
            "machine_run_python",
            "Run Python in the user's private machine (no internet). Save "
            "deliverables under out/; they are sent to the user as soon as they exist.",
            RunPythonArgs,
            RiskClass.WRITE_SELF,
            machine_run_python,
            AGENTS,
            requires=Capability.SANDBOX,
            timeout_s=t,
            progress_label=_attempt_label("Python"),
        ),
        MavisTool(
            "machine_run_shell",
            "Run a shell command in the user's private machine (no internet).",
            RunShellArgs,
            RiskClass.WRITE_SELF,
            machine_run_shell,
            AGENTS,
            requires=Capability.SANDBOX,
            timeout_s=t,
            progress_label=_attempt_label("a command"),
        ),
        MavisTool(
            "machine_install",
            "Install Python packages into the machine (fetched for it; no internet inside).",
            InstallArgs,
            RiskClass.WRITE_SELF,
            machine_install,
            AGENTS,
            requires=Capability.SANDBOX,
            timeout_s=240,
            progress_label=lambda a, out: f"installed {len(a.packages)} package(s)",
        ),
        MavisTool(
            "machine_fetch",
            "Download a public URL into the workspace.",
            FetchArgs,
            RiskClass.READ,
            machine_fetch,
            AGENTS,
            requires=Capability.SANDBOX,
            untrusted_output=True,
            prepare=_fetch_prepare,
            progress_label=lambda a, out: f"downloaded from {host_of(a.url)}",
        ),
        MavisTool(
            "files_list",
            "List the user's workspace files.",
            ListArgs,
            RiskClass.READ,
            files_list,
            AGENTS,
            requires=Capability.SANDBOX,
            progress_label=lambda a, out: "listed files",
        ),
        MavisTool(
            "files_read",
            "Read a workspace file as text (PDF, DOCX, XLSX, CSV and text).",
            ReadArgs,
            RiskClass.READ,
            files_read,
            AGENTS,
            requires=Capability.SANDBOX,
            progress_label=lambda a, out: "read a file",
        ),
        MavisTool(
            "files_write",
            "Write a text file into the workspace.",
            WriteArgs,
            RiskClass.WRITE_SELF,
            files_write,
            AGENTS,
            requires=Capability.SANDBOX,
            progress_label=lambda a, out: f"wrote {Path(a.path).name}",
        ),
        MavisTool(
            "files_attach",
            "Copy a workspace file into the running machine.",
            PathArgs,
            RiskClass.READ,
            files_attach,
            AGENTS,
            requires=Capability.SANDBOX,
            progress_label=lambda a, out: "opened a file",
        ),
        MavisTool(
            "files_delete",
            "Delete a workspace file.",
            PathArgs,
            RiskClass.WRITE_SELF,
            files_delete,
            AGENTS,
            requires=Capability.SANDBOX,
            prepare=_delete_prepare,
            on_taint=TaintPolicy.APPROVE,
            preview=lambda a: f"Delete {a.path} from your files",
            progress_label=lambda a, out: "deleted a file",
        ),
        MavisTool(
            "files_send",
            "Send a workspace file to the user now.",
            SendArgs,
            RiskClass.WRITE_SELF,
            files_send,
            AGENTS,
            requires=Capability.SANDBOX,
            progress_label=lambda a, out: f"sent {Path(a.path).name}",
        ),
    ]


MACHINE_TOOLS = _tools()


def register_machine_tools(registry: ToolRegistry) -> None:
    for tool in _tools():
        if registry.find(tool.name) is None:
            registry.register(tool)

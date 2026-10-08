"""Progress card state and rendering (Phase 12, spec section 8.1). Pure: no I/O.

The card is UI chrome. Its header comes from the task goal, step lines from PlanStep.title, the "Last:"
line from code-made tool labels; nothing here is model prose or third-party text, and tainted titles are
scrubbed. It is never logged into history."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from mavis.domain.messages import Button
from mavis.domain.plans import PlanStep

TASK_BUTTON_PREFIX = "tk:"
HEADER_CHARS = 60


class StepState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    PARTIAL = "partial"
    FAILED = "failed"
    WAITING = "waiting"  # waiting for the user's approval


class CardFinal(StrEnum):
    DONE = "done"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


ICONS = {StepState.QUEUED: "▫️", StepState.RUNNING: "⏳", StepState.DONE: "✅", StepState.PARTIAL: "🟡",
         StepState.FAILED: "❌", StepState.WAITING: "✋"}
FINAL_WORDS = {CardFinal.DONE: "Done", CardFinal.PARTIAL: "Partly done", CardFinal.FAILED: "Couldn't finish",
               CardFinal.CANCELLED: "Cancelled"}


class CardStep(BaseModel):
    id: str
    title: str
    state: StepState = StepState.QUEUED
    started_at: float | None = None
    finished_at: float | None = None


class CardState(BaseModel):
    task_id: int
    header: str
    steps: list[CardStep] = Field(default_factory=list)
    last: str = ""
    started_at: float
    files_sent: int = 0
    final: CardFinal | None = None
    finished_at: float | None = None
    live_url: str | None = None


def fmt_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _clean(text: str, tainted: bool) -> str:
    text = " ".join(str(text or "").split()).replace("\u2014", ", ").replace("\u2013", "-")
    if tainted:
        from mavis.initiative.composer import scrub_untrusted_origin  # lazy: composer imports the LLM layer

        text = scrub_untrusted_origin(text)
    return text


def card_from_plan(task_id: int, goal: str, steps: list[PlanStep], *, tainted: bool, now: float) -> CardState:
    header = _clean(goal, tainted)[:HEADER_CHARS].rstrip()
    rows = [CardStep(id=s.id, title=_clean(s.title, tainted) or f"{s.agent.capitalize()} step")
            for s in steps]
    return CardState(task_id=task_id, header=header, steps=rows, started_at=now)


def cancel_button(task_id: int) -> Button:
    return Button(label="Cancel", data=f"{TASK_BUTTON_PREFIX}{task_id}:x")


def render_card(state: CardState, now: float) -> tuple[str, list[list[Button]]]:
    word = FINAL_WORDS[state.final] if state.final else "Working on"
    lines = [f"{word}: {state.header}"]
    for i, step in enumerate(state.steps, start=1):
        line = f"{ICONS[step.state]} {i}. {step.title}"
        if step.state is StepState.RUNNING and step.started_at is not None and state.final is None:
            line += f"  · {fmt_elapsed(now - step.started_at)}"
        lines.append(line)
    if state.last and state.final is None:
        lines.append(f"Last: {state.last}")
    end = state.finished_at if state.final and state.finished_at is not None else now
    footer = f"⏱ {fmt_elapsed(end - state.started_at)}"
    if state.files_sent:
        footer += f" · {state.files_sent} file{'s' if state.files_sent != 1 else ''} sent"
    lines.append(footer)
    if state.final is not None:
        return "\n".join(lines), []
    row = [cancel_button(state.task_id)]
    if state.live_url:
        row.append(Button(label="Watch live", url=state.live_url))
    return "\n".join(lines), [row]

# ruff: noqa: E501
"""Demo suite runner (Phase 12, spec 13.4): real tasks through the live-test sink, saved and reported.

    uv run python -m scripts.machine_demo --all            # every case
    uv run python -m scripts.machine_demo --case D2        # one case
    ... --report-to-owner                                  # summary, key screenshots and report.html to the owner

Speaks only as the synthetic test chat (the same rule as scripts/live_e2e.py). With TEST_MIRROR_CHAT_ID
set on the stack, the owner watches the cards tick in their own chat, tagged [test], without buttons.
Runs on demand and after deploys (deploy.sh --verify-machine), never on a schedule (owner decision 5).
Each run is saved to data/e2e/<run_id>/<case>/ (transcript.md, sink.jsonl, metrics.json, files/)."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import shutil
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from mavis.channels.test_sink import FIXTURE_PREFIX, SYNTHETIC_BELOW, active_test_chat, read_sink
from mavis.config import Settings, get_settings
from mavis.store.db import Session
from mavis.store.models import Artifact, Message, Task, User

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "scripts" / "machine_demos.toml"
WEBHOOK = "http://localhost:8000/telegram/webhook"
FINAL_WORDS = ("Done", "Partly done", "Couldn't finish", "Cancelled")
QUIET_S = 6.0


class HarnessError(RuntimeError):
    pass


def target_chat(s: Settings) -> int:
    chat = active_test_chat(s)
    if chat is None:
        raise HarnessError("set LIVE_TEST_ENABLED=true and a synthetic TEST_TELEGRAM_CHAT_ID below "
                           f"{SYNTHETIC_BELOW} that is not in ALLOWED_TELEGRAM_CHAT_IDS")
    return chat


@dataclass
class DemoCase:
    id: str
    prompt: str
    upload: str | None
    actions: list[dict]
    expect: dict
    timeout_s: float
    quotas: dict[str, float]


def load_cases(path: Path = CASES) -> list[DemoCase]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return [DemoCase(id=c["id"], prompt=c["prompt"], upload=c.get("upload"), actions=list(c.get("actions", [])),
                     expect=dict(c.get("expect", {})), timeout_s=float(c.get("timeout_s", 300)),
                     quotas=dict(c.get("quotas", {}))) for c in data.get("case", [])]


@dataclass
class RunRecord:
    case: DemoCase
    sink: list[dict]
    replies: list[str]
    tasks: list[dict]
    artifacts: list[dict]
    started: float
    finished: float
    user_id: int | None
    notes: dict[str, Any] = field(default_factory=dict)

    def card_frames(self) -> list[dict]:
        frames = []
        for row in self.sink:
            text = str(row.get("text", ""))
            if row.get("kind") in ("text", "edit") and text.split(":", 1)[0] in ("Working on", *FINAL_WORDS):
                frames.append(row)
        return frames

    def final_word(self) -> str | None:
        for row in reversed(self.card_frames()):
            head = str(row.get("text", "")).split(":", 1)[0]
            if head in FINAL_WORDS:
                return head
        return None

    def files(self) -> list[dict]:
        return [r for r in self.sink if r.get("kind") == "document"]

    def photos(self) -> list[dict]:
        out = [r for r in self.sink if r.get("kind") == "photo"]
        for r in self.sink:
            if r.get("kind") == "album":
                out += [{"kind": "photo", "path": p} for p in r.get("paths", [])]
        return out


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


CheckFn = Callable[[RunRecord, Any], CheckResult]
CHECKS: dict[str, CheckFn] = {}


def register_check(name: str) -> Callable[[CheckFn], CheckFn]:
    def deco(fn: CheckFn) -> CheckFn:
        CHECKS[name] = fn
        return fn
    return deco


@register_check("card_final_in")
def _card_final_in(rec: RunRecord, allowed: list[str]) -> CheckResult:
    word = rec.final_word()
    return CheckResult("card_final_in", word in allowed, f"card ended as {word!r}, wanted one of {allowed}")


@register_check("min_card_frames")
def _min_frames(rec: RunRecord, n: int) -> CheckResult:
    got = len(rec.card_frames())
    return CheckResult("min_card_frames", got >= int(n), f"{got} card frames (want at least {n})")


@register_check("min_files")
def _min_files(rec: RunRecord, n: int) -> CheckResult:
    got = len(rec.files())
    return CheckResult("min_files", got >= int(n), f"{got} files delivered (want at least {n})")


@register_check("file_ext")
def _file_ext(rec: RunRecord, exts: list[str]) -> CheckResult:
    names = [Path(str(r.get("path") or r.get("text", ""))).suffix.lower() for r in rec.files()]
    ok = any(e.lower() in names for e in exts)
    return CheckResult("file_ext", ok, f"delivered {names}, wanted one of {exts}")


@register_check("min_screenshots")
def _min_shots(rec: RunRecord, n: int) -> CheckResult:
    got = len(rec.photos())
    return CheckResult("min_screenshots", got >= int(n), f"{got} screenshots (want at least {n})")


@register_check("reply_contains")
def _reply_contains(rec: RunRecord, needles: list[str]) -> CheckResult:
    text = "\n".join(rec.replies)
    missing = [n for n in needles if n not in text]
    return CheckResult("reply_contains", not missing, f"missing {missing}" if missing else "all present")


def run_checks(rec: RunRecord) -> list[CheckResult]:
    out = []
    for name, arg in rec.case.expect.items():
        fn = CHECKS.get(name)
        out.append(fn(rec, arg) if fn else CheckResult(name, False, f"unknown check {name!r}"))
    return out


def _clean(text: str) -> str:
    return str(text).replace("\u2014", ", ").replace("\u2013", "-")


def render_transcript(rec: RunRecord) -> str:
    lines = [f"# Case {rec.case.id}", "", f"Prompt: {rec.case.prompt}", ""]
    for row in rec.sink:
        lines.append(f"- `{row.get('at', '')}` **{row.get('kind')}**: {_clean(row.get('text', ''))}")
    lines += ["", "## Replies", *[f"- {_clean(r)}" for r in rec.replies],
              "", f"Total time: {rec.finished - rec.started:.1f} s"]
    return "\n".join(lines) + "\n"


def render_report(results: list[tuple[RunRecord, list[CheckResult]]]) -> tuple[str, str]:
    md = ["# Mavis AI machine demo", ""]
    rows = []
    for rec, checks in results:
        verdict = "PASS" if checks and all(c.ok for c in checks) else "FAIL"
        md.append(f"- {rec.case.id}: {verdict} ({rec.finished - rec.started:.0f} s)")
        md += [f"  - {'ok' if c.ok else 'FAIL'} {c.name}: {_clean(c.detail)}" for c in checks]
        cells = "".join(f"<li>{'ok' if c.ok else 'FAIL'} {html.escape(c.name)}: {html.escape(_clean(c.detail))}</li>"
                        for c in checks)
        rows.append(f"<section><h2>{html.escape(rec.case.id)} {verdict}</h2><p>{html.escape(rec.case.prompt)}</p>"
                    f"<ul>{cells}</ul><a href='{html.escape(rec.case.id)}/transcript.md'>transcript</a></section>")
    page = ("<!doctype html><meta charset='utf-8'><title>Mavis AI machine demo</title>"
            "<style>body{font:15px system-ui;margin:2rem;max-width:60rem}</style>"
            "<h1>Mavis AI machine demo</h1>" + "".join(rows))
    return page, "\n".join(md) + "\n"


# --- live parts (not unit tested; exercised by the owner's runs) -----------------------------------

async def _user_id(chat: int) -> int | None:
    async with Session() as s:
        return await s.scalar(select(User.id).where(User.telegram_chat_id == chat))


async def _post(client: httpx.AsyncClient, url: str, secret: str, update: dict) -> None:
    r = await client.post(url, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": secret})
    r.raise_for_status()


def _update(chat: int, n: int, text: str, upload: str | None) -> dict:
    msg: dict[str, Any] = {"message_id": n, "date": int(time.time()), "chat": {"id": chat, "type": "private"},
                           "from": {"id": chat, "first_name": "Demo"}, "text": text}
    if upload:
        msg["document"] = {"file_id": f"{FIXTURE_PREFIX}{upload}", "file_name": upload}
        msg["caption"] = msg.pop("text")
    return {"update_id": int(time.time() * 1000) % 2_000_000_000, "message": msg}


async def _snapshot(user_id: int | None, since: datetime) -> tuple[list[str], list[dict], list[dict]]:
    if user_id is None:
        return [], [], []
    async with Session() as s:
        msgs = await s.scalars(select(Message).where(Message.user_id == user_id, Message.role == "assistant",
                                                     Message.created_at >= since).order_by(Message.id))
        ts = await s.scalars(select(Task).where(Task.user_id == user_id, Task.created_at >= since))
        tasks = [{"id": t.id, "status": t.status, "goal": t.goal} for t in ts]
        arts = await s.scalars(select(Artifact).where(Artifact.task_id.in_([t["id"] for t in tasks] or [-1])))
        return ([m.content for m in msgs], tasks,
                [{"id": a.id, "path": a.path, "size": a.size, "delivered_at": str(a.delivered_at)} for a in arts])


async def run_case(case: DemoCase, s: Settings, url: str, out_dir: Path) -> RunRecord:
    chat = target_chat(s)
    since = datetime.now(UTC)
    offset = len(read_sink(s.data_dir))
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30) as client:
        await _post(client, url, s.telegram_webhook_secret, _update(chat, 1, case.prompt, case.upload))
        last_change, last_len, done_actions = time.monotonic(), 0, set()
        while time.monotonic() - started < case.timeout_s:
            await asyncio.sleep(1.0)
            rows = [r for r in read_sink(s.data_dir)[offset:] if r.get("chat_id") == chat]
            for i, act in enumerate(case.actions):
                if i not in done_actions and time.monotonic() - started >= float(act.get("at_s", 0)):
                    await _act(client, url, s, chat, act, await _user_id(chat), since)
                    done_actions.add(i)
            if len(rows) != last_len:
                last_len, last_change = len(rows), time.monotonic()
            rec = RunRecord(case, rows, [], [], [], started, time.monotonic(), await _user_id(chat))
            if rec.final_word() and time.monotonic() - last_change > QUIET_S:
                break
    uid = await _user_id(chat)
    replies, tasks, arts = await _snapshot(uid, since)
    rows = [r for r in read_sink(s.data_dir)[offset:] if r.get("chat_id") == chat]
    rec = RunRecord(case, rows, replies, tasks, arts, started, time.monotonic(), uid)
    _save(rec, out_dir / case.id)
    return rec


async def _act(client, url, s, chat, act, user_id, since) -> None:
    """Mid-run actions are data: {"at_s": 20, "tap": "cancel"} taps the newest task's Cancel button."""
    if act.get("tap") == "cancel" and user_id is not None:
        _, tasks, _ = await _snapshot(user_id, since)
        if tasks:
            tid = max(t["id"] for t in tasks)
            update = {"update_id": int(time.time() * 1000) % 2_000_000_000,
                      "callback_query": {"id": f"demo-{tid}", "data": f"tk:{tid}:x", "from": {"id": chat},
                                         "message": {"message_id": 1, "chat": {"id": chat, "type": "private"}}}}
            await _post(client, url, s.telegram_webhook_secret, update)


def _save(rec: RunRecord, folder: Path) -> None:
    (folder / "files").mkdir(parents=True, exist_ok=True)
    (folder / "screenshots").mkdir(parents=True, exist_ok=True)
    (folder / "transcript.md").write_text(render_transcript(rec), encoding="utf-8")
    (folder / "sink.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rec.sink))
    for r in rec.files():
        if r.get("path") and Path(r["path"]).is_file():
            shutil.copy(r["path"], folder / "files" / Path(r["path"]).name)
    for i, r in enumerate(rec.photos()):
        if r.get("path") and Path(r["path"]).is_file():
            shutil.copy(r["path"], folder / "screenshots" / f"{i:02d}{Path(r['path']).suffix}")
    metrics = {"total_s": round(rec.finished - rec.started, 1), "card_frames": len(rec.card_frames()),
               "files": len(rec.files()), "screenshots": len(rec.photos()), "outcome": rec.final_word(),
               "tasks": rec.tasks}
    (folder / "metrics.json").write_text(json.dumps(metrics, indent=2))


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="machine_demo")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--case", action="append", default=[])
    ap.add_argument("--report-to-owner", action="store_true")
    ap.add_argument("--webhook", default=WEBHOOK)
    args = ap.parse_args(argv)
    s = get_settings()
    target_chat(s)
    cases = [c for c in load_cases() if args.all or c.id in args.case]
    if not cases:
        print("no cases selected (use --all or --case ID)")
        return 2
    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = s.data_dir / "e2e" / run_id
    results = []
    for case in cases:
        rec = await run_case(case, s, args.webhook, out)
        checks = run_checks(rec)
        results.append((rec, checks))
        print(f"{case.id}: {'PASS' if all(c.ok for c in checks) else 'FAIL'}")
    page, summary = render_report(results)
    (out / "report.html").write_text(page, encoding="utf-8")
    (out / "summary.md").write_text(summary, encoding="utf-8")
    print(f"saved {out}")
    if args.report_to_owner:
        await report_to_owner(s, out, summary, results)
    return 0 if all(all(c.ok for c in ch) for _, ch in results) else 1


async def report_to_owner(s: Settings, out: Path, summary: str, results) -> None:
    """The summary, an album of up to 4 key screenshots and report.html, through the outbox."""
    from mavis.domain.messages import Outbound
    from mavis.store.repo import outbox, users

    if not s.allowed_telegram_chat_ids:
        print("no owner chat configured")
        return
    owner = await users.get_by_chat(s.allowed_telegram_chat_ids[0])
    if owner is None:
        return
    shots = [str(p) for p in sorted(out.glob("*/screenshots/*"))[:4]]  # noqa: ASYNC240
    run = out.name
    await outbox.enqueue_now(Outbound(user_id=owner.id, text=f"[test] Machine demo {run}\n{summary}",
                                      dedupe_key=f"e2e:{run}:summary"))
    if len(shots) >= 2:
        await outbox.enqueue_now(Outbound(user_id=owner.id, media=shots, text="\n".join(Path(p).parent.parent.name
                                          for p in shots), dedupe_key=f"e2e:{run}:album"))
    await outbox.enqueue_now(Outbound(user_id=owner.id, text="report.html", document_path=str(out / "report.html"),
                                      dedupe_key=f"e2e:{run}:report"))


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))

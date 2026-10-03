"""Compact, model-facing text for Google Workspace results (spec 2026-10-03 section 4.2).

Search and list results carry ids, titles, owners, dates and the kind of file, never content previews.
Document text is capped at BODY_CHARS with URLs stripped, the same as mail_render. Sheets render at most
50 rows by 20 columns with cells cut to 80 characters. The registry wraps every result as untrusted.
"""

from __future__ import annotations

import json
import re
from typing import Any

from mavis.tools.integrations.mail_render import BODY_CHARS, strip_urls
from mavis.tools.integrations.normalize import extract_list, pick

TITLE_CHARS = 120
CELL_CHARS = 80
MAX_ROWS, MAX_COLS = 50, 20
# Keys a create action may return its new id under (Drive file, Doc, Sheet, task). Shared with
# workspace_guard.created_ids, so an id that is rendered is also recorded for the allowlist.
ID_KEYS = ("id", "document_id", "documentId", "spreadsheet_id", "spreadsheetId")
_BLANKS = re.compile(r"\n\s*\n\s*\n+")
KINDS = {
    "application/vnd.google-apps.document": "Doc",
    "application/vnd.google-apps.spreadsheet": "Sheet",
    "application/vnd.google-apps.presentation": "Slides",
    "application/vnd.google-apps.folder": "Folder",
    "application/vnd.google-apps.form": "Form",
    "application/pdf": "PDF",
}


def kind_of(mime: str) -> str:
    mime = str(mime or "")
    if mime in KINDS:
        return KINDS[mime]
    if mime.startswith("image/"):
        return "Image"
    if mime.startswith("text/"):
        return "Text"
    return mime.rsplit("/", 1)[-1][:24] or "File"


def one_line(text: Any, limit: int = TITLE_CHARS) -> str:
    flat = " ".join(strip_urls(str(text or "")).split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "..."


def clip_body(text: str) -> str:
    body = _BLANKS.sub("\n\n", strip_urls(text.replace("\r\n", "\n"))).strip()
    return body if len(body) <= BODY_CHARS else body[:BODY_CHARS].rstrip() + " ...[truncated]"


def _person(p: Any) -> str:
    if not isinstance(p, dict):
        return ""
    if p.get("me"):
        return "you"
    return one_line(p.get("displayName") or p.get("emailAddress") or "", 60)


def _date(value: Any) -> str:
    return str(value or "")[:10] or "unknown"


def file_line(f: dict) -> str:
    owners = ", ".join(x for x in (_person(o) for o in f.get("owners") or []) if x) or "unknown"
    parts = [
        f"file_id={f.get('id', '')}", one_line(f.get("name") or "(untitled)"), kind_of(f.get("mimeType")),
        f"owner: {owners}", f"modified: {_date(f.get('modifiedTime'))}",
    ]
    if f.get("sharedWithMeTime"):
        sharer = _person(f.get("sharingUser")) or owners
        parts.append(f"shared with you {_date(f['sharedWithMeTime'])} by {sharer}")
    return "- " + " | ".join(parts)


def render_files(data: Any) -> str:
    files = extract_list(data, "files", "data.files", "response_data.files")
    if not files:
        return "No Drive files matched."
    lines = [f"{len(files)} file(s). Use the file_id with drive_read, docs_read or sheets_read."]
    lines += [file_line(f) for f in files]
    return "\n".join(lines)


def _doc(data: Any) -> dict:
    if isinstance(data, dict):
        inner = pick(data, "response_data", "data.response_data", "document")
        return inner if isinstance(inner, dict) else data
    return {}


def _structural_text(content: Any) -> list[str]:
    out: list[str] = []
    for element in content or []:
        if not isinstance(element, dict):
            continue
        for run in pick(element, "paragraph.elements", default=[]) or []:
            text = pick(run, "textRun.content")
            if isinstance(text, str):
                out.append(text)
        for row in pick(element, "table.tableRows", default=[]) or []:
            cells = [
                " ".join(_structural_text(c.get("content"))).strip() for c in row.get("tableCells") or []
            ]
            out.append(" | ".join(cells) + "\n")
    return out


def doc_text(data: Any) -> tuple[str, str]:
    """(title, plain text) of a Google Docs API document resource."""
    doc = _doc(data)
    return str(doc.get("title") or "(untitled)"), "".join(_structural_text(pick(doc, "body.content")))


def doc_end_index(data: Any) -> int | None:
    """Index just before the document's final newline: where appended text goes (INSERT_TEXT_ACTION)."""
    content = pick(_doc(data), "body.content", default=[]) or []
    ends = [int(e["endIndex"]) for e in content if isinstance(e, dict) and isinstance(e.get("endIndex"), int)]
    return max(ends) - 1 if ends else None


def render_doc(data: Any) -> str:
    title, text = doc_text(data)
    doc = _doc(data)
    return f"document_id={doc.get('documentId', '')} | {one_line(title)}\n\n{clip_body(text) or '(empty)'}"


def _value_ranges(data: Any) -> list[dict]:
    sheet = pick(data, "spreadsheet_data", "data.spreadsheet_data", default=data)
    ranges = pick(sheet, "valueRanges", default=[]) if isinstance(sheet, dict) else []
    if isinstance(ranges, dict):
        return [ranges]
    return [r for r in ranges or [] if isinstance(r, dict)]


def sheet_rows(data: Any) -> tuple[str, list[list[str]]]:
    ranges = _value_ranges(data)
    if not ranges:
        return "", []
    first = ranges[0]
    values = first.get("values") or []
    rows = [[str(c) for c in row] if isinstance(row, list) else [str(row)] for row in values]
    return str(first.get("range") or ""), rows


def _cell(value: str) -> str:
    flat = " ".join(strip_urls(value).split()).replace("|", "/")
    return flat if len(flat) <= CELL_CHARS else flat[:CELL_CHARS].rstrip() + "..."


def render_sheet(data: Any) -> str:
    where, rows = sheet_rows(data)
    if not rows:
        return "That range is empty."
    shown = rows[:MAX_ROWS]
    head = f"range={where} | rows 1-{len(shown)} of {len(rows)}"
    if any(len(r) > MAX_COLS for r in shown):
        head += f" | first {MAX_COLS} columns"
    return "\n".join([head, *(" | ".join(_cell(c) for c in row[:MAX_COLS]) for row in shown)])


def render_sheet_list(data: Any) -> str:
    sheets = extract_list(data, "spreadsheets", "data.spreadsheets", "files")
    if not sheets:
        return "No spreadsheets matched."
    lines = [f"{len(sheets)} spreadsheet(s). Use the spreadsheet_id with sheets_read."]
    for s in sheets:
        lines.append(f"- spreadsheet_id={s.get('id', '')} | {one_line(s.get('name') or '(untitled)')} | "
                     f"modified: {_date(s.get('modifiedTime'))}")
    return "\n".join(lines)


def render_tasks(data: Any) -> str:
    tasks = extract_list(data, "tasks", "data.tasks", "items")
    if not tasks:
        return "The to-do list is empty."
    lines = [f"{len(tasks)} task(s). Use the task_id with tasks_complete or tasks_update."]
    for t in tasks:
        done = " (done)" if t.get("status") == "completed" else ""
        due = f" | due {_date(t['due'])}" if t.get("due") else ""
        notes = f" | notes: {one_line(t['notes'], 120)}" if t.get("notes") else ""
        title = one_line(t.get("title") or "(untitled)")
        lines.append(f"- task_id={t.get('id', '')} | {title}{due}{done}{notes}")
    return "\n".join(lines)


def render_contacts(data: Any) -> str:
    results = extract_list(data, "response_data.results", "results", "data.response_data.results")
    people = [r.get("person") for r in results if isinstance(r.get("person"), dict)]
    if not people:
        return "No contacts matched."
    lines = []
    for p in people:
        names = [n for n in p.get("names") or [] if isinstance(n, dict)]
        name = one_line((names[0].get("displayName") if names else "") or "(no name)", 80)
        emails = ", ".join(str(e.get("value")) for e in p.get("emailAddresses") or [] if e.get("value"))
        phones = ", ".join(str(n.get("value")) for n in p.get("phoneNumbers") or [] if n.get("value"))
        lines.append(f"- {name} | email: {emails or 'none'} | phone: {phones or 'none'}")
    return "\n".join(lines)


def render_transcripts(data: Any) -> str:
    items = extract_list(data, "response_data.transcripts", "transcripts", "data.response_data.transcripts")
    if not items:
        return "No transcripts for that meeting."
    lines = []
    for t in items:
        doc = pick(t, "docsDestination.document", default="")
        lines.append(f"- transcript {one_line(t.get('name', ''), 80)} | state: {t.get('state', 'unknown')} | "
                     f"document_id={doc}")
    return "\n".join(lines)


def render_created(data: Any) -> str:
    """Write results: just the ids the model needs next, never the whole resource."""
    if not isinstance(data, dict):
        return "Done."
    found = {k: v for k in (*ID_KEYS, "name", "title")
             if isinstance(v := pick(data, k, f"response_data.{k}", f"data.{k}"), (str, int))}
    return "Done. " + json.dumps(found, ensure_ascii=False) if found else "Done."


MEET_PREFIX = "https://meet.google.com/"


def render_meet(data: Any) -> str:
    """The one link this module ever returns: the Meet the user just asked for, only if it is Google's."""
    uri = pick(data, "response_data.meetingUri", "meetingUri", "data.response_data.meetingUri")
    if isinstance(uri, str) and uri.startswith(MEET_PREFIX):
        return f"Meet link: {uri}"
    return "Created the Meet, but Google returned no link."


RENDERERS = {
    "drive.search": render_files,
    "drive.list_recent": render_files,
    "docs.read": render_doc,
    "sheets.find": render_sheet_list,
    "sheets.read": render_sheet,
    "tasks.list": render_tasks,
    "contacts.search": render_contacts,
    "meet.transcript": render_transcripts,
    "drive.move": render_created,
    "docs.comment": render_created,
    "tasks.delete": render_created,
    "meet.create": render_meet,
}

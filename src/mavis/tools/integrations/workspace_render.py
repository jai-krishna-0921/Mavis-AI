"""Compact, model-facing text for Google Workspace results (spec 2026-10-03 section 4.2).

Search and list results carry ids, titles, owners, dates and the kind of file, never content previews.
Document text is capped at BODY_CHARS with URLs stripped, the same as mail_render. Sheets render at most
50 rows by 20 columns with cells cut to 80 characters. The registry wraps every result as untrusted.
"""

from __future__ import annotations

import json
import re
from typing import Any

from mavis.domain.results import ToolOutput
from mavis.tools.integrations.mail_render import BODY_CHARS, strip_urls
from mavis.tools.integrations.normalize import extract_list, pick

TITLE_CHARS = 120
CELL_CHARS = 80
MAX_ROWS, MAX_COLS = 50, 20
# Keys a create action may return its new id under (Drive file, Doc, Sheet, task). Shared with
# workspace_guard.created_ids, so an id that is rendered is also recorded for the allowlist.
ID_KEYS = ("id", "document_id", "documentId", "spreadsheet_id", "spreadsheetId", "presentationId")
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


def has_value_ranges(data: Any) -> bool:
    """True when a BATCH_GET reply carries at least one valueRange (an empty range still has one)."""
    return bool(_value_ranges(data))


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


NO_TRANSCRIPT = ("No transcript is available for that meeting. Google only records transcripts for "
                 "Workspace accounts that turned transcription on for the call; personal Google accounts "
                 "have none. (If the id did not come from meet_recent, check it.)")
MAX_TRANSCRIPT_LINES = 200


def render_contacts(data: Any) -> str:
    results = extract_list(data, "response_data.results", "results", "data.response_data.results")
    rows = [(r.get("person"), str(r.get("source") or "contacts")) for r in results
            if isinstance(r.get("person"), dict)]
    if not rows:
        skipped = pick(data, "not_searched", default=[]) if isinstance(data, dict) else []
        tail = f" (not searched: {', '.join(map(str, skipped))})" if skipped else ""
        return "No contacts matched." + tail
    lines = []
    for p, source in rows:
        names = [n for n in p.get("names") or [] if isinstance(n, dict)]
        name = one_line((names[0].get("displayName") if names else "") or "(no name)", 80)
        emails = ", ".join(str(e.get("value")) for e in p.get("emailAddresses") or [] if e.get("value"))
        phones = ", ".join(str(n.get("value")) for n in p.get("phoneNumbers") or [] if n.get("value"))
        where = {"other": " | from your email history", "directory": " | company directory"}.get(source, "")
        rid = f" | id: {p['resourceName']}" if source == "contacts" and p.get("resourceName") else ""
        lines.append(f"- {name} | email: {emails or 'none'} | phone: {phones or 'none'}{where}{rid}")
    return "\n".join(lines)


def render_transcripts(data: Any) -> str:
    items = extract_list(data, "response_data.transcripts", "transcripts", "data.response_data.transcripts")
    if not items:
        return NO_TRANSCRIPT
    lines = []
    for t in items:
        doc = pick(t, "docsDestination.document", default="")
        link = f" | transcript Doc: document_id={doc} (docs_read)" if doc else ""
        state = t.get("state", "unknown")
        lines.append(f"- transcript {one_line(t.get('name', ''), 80)} | state: {state}{link}")
        entries = [e for e in t.get("entries") or [] if isinstance(e, dict) and e.get("text")]
        for e in entries[:MAX_TRANSCRIPT_LINES]:
            lines.append(f"  {one_line(e.get('speaker') or 'Participant', 40)}: {one_line(e['text'], 400)}")
        if len(entries) > MAX_TRANSCRIPT_LINES:
            lines.append(f"  ...{len(entries) - MAX_TRANSCRIPT_LINES} more lines; the Doc has the rest")
        if not entries:
            lines.append("  (no spoken text yet; it may still be processing)")
    return clip_body("\n".join(lines))


def render_meet_recent(data: Any) -> str:
    calls = extract_list(data, "conferences", "data.conferences")
    if not calls:
        return "No Google Meet calls found in that period."
    lines = [f"{len(calls)} call(s). Use the conference_record_id with meet_transcript."]
    for c in calls:
        who = ", ".join(one_line(p, 40) for p in (c.get("participants") or [])[:10]) or "unknown"
        began, ended = str(c.get("startTime") or "unknown")[:16], str(c.get("endTime") or "ongoing")[:16]
        lines.append(f"- conference_record_id={c.get('conferenceRecordId', '')} | started: {began} | "
                     f"ended: {ended} | with: {who}")
    return "\n".join(lines)


def render_calendars(data: Any) -> str:
    cals = extract_list(data, "calendars", "data.calendars", "items")
    if not cals:
        return "No calendars found."
    lines = [f"{len(cals)} calendar(s). Use the calendar_id with calendar_list or calendar_find."]
    for c in cals:
        mine = " (primary)" if c.get("primary") else ""
        name = one_line(c.get("summaryOverride") or c.get("summary") or "(untitled)")
        lines.append(f"- calendar_id={c.get('id', '')} | {name}{mine} | "
                     f"access: {c.get('accessRole', 'unknown')} | tz: {c.get('timeZone', '')}")
    return "\n".join(lines)


def _runs(elements: Any) -> str:
    """The text of Slides textElements (text runs only)."""
    return "".join(t["textRun"]["content"] for t in elements or []
                   if isinstance(t, dict) and isinstance(t.get("textRun"), dict)
                   and isinstance(t["textRun"].get("content"), str))


def _slide_text(slide: dict) -> str:
    parts: list[str] = []
    for el in slide.get("pageElements") or []:
        if not isinstance(el, dict):
            continue
        parts.append(_runs(pick(el, "shape.text.textElements", default=[])))
        for row in pick(el, "table.tableRows", default=[]) or []:
            cells = [_runs(pick(c, "text.textElements", default=[])).strip()
                     for c in row.get("tableCells") or []]
            parts.append(" | ".join(cells) + "\n")
    return "\n".join(p for p in parts if p.strip()).strip()


def render_slides(data: Any) -> str:
    deck = data if isinstance(data, dict) else {}
    slides = [s for s in deck.get("slides") or [] if isinstance(s, dict)]
    title = one_line(deck.get("title") or "(untitled)")
    out = [f"presentation_id={deck.get('presentationId', '')} | {title} | {len(slides)} slide(s)"]
    for n, slide in enumerate(slides, 1):
        out.append(f"--- Slide {n} ---\n{_slide_text(slide) or '(no text)'}")
    return clip_body("\n".join(out))


def _question_titles(form: dict) -> dict[str, str]:
    titles: dict[str, str] = {}
    for item in form.get("items") or []:
        q = pick(item, "questionItem.question", default=None) if isinstance(item, dict) else None
        if isinstance(q, dict) and q.get("questionId"):
            titles[str(q["questionId"])] = one_line(item.get("title") or "(untitled question)")
        for sub in pick(item, "questionGroupItem.questions", default=[]) or []:
            if isinstance(sub, dict) and sub.get("questionId"):
                titles[str(sub["questionId"])] = one_line(item.get("title") or "") + " / " + one_line(
                    pick(sub, "rowQuestion.title", default="") or "")
    return titles


def render_form(data: Any) -> str:
    form = data if isinstance(data, dict) else {}
    info = form.get("info") if isinstance(form.get("info"), dict) else {}
    lines = [f"form_id={form.get('formId', '')} | {one_line(info.get('title') or '(untitled)')}"]
    if info.get("description"):
        lines.append(one_line(info["description"], 300))
    n = 0
    for item in form.get("items") or []:
        if not isinstance(item, dict):
            continue
        q = pick(item, "questionItem.question", default=None)
        if not isinstance(q, dict):
            continue
        n += 1
        kind = next((k for k in ("choiceQuestion", "textQuestion", "scaleQuestion", "dateQuestion",
                                 "timeQuestion", "fileUploadQuestion", "rowQuestion") if k in q), "question")
        extra = ""
        if kind == "choiceQuestion":
            raw = pick(q, "choiceQuestion.options", default=[]) or []
            opts = [one_line(o.get("value", ""), 60) for o in raw if isinstance(o, dict)]
            extra = " | options: " + ", ".join(opts[:20])
        req = " (required)" if q.get("required") else ""
        label = one_line(item.get("title") or "(untitled)")
        lines.append(f"{n}. {label}{req} | {kind.replace('Question', '')}{extra}")
    return clip_body("\n".join(lines))


MAX_SAMPLE_ANSWERS = 5
MAX_CHOICE_ROWS = 15


def render_form_responses(data: Any) -> str:
    data = data if isinstance(data, dict) else {}
    form = data.get("form") if isinstance(data.get("form"), dict) else {}
    responses = [r for r in data.get("responses") or [] if isinstance(r, dict)]
    info = form.get("info") if isinstance(form.get("info"), dict) else {}
    title = one_line(info.get("title") or "(untitled)")
    more = " (more exist; this is the newest batch Google returned)" if data.get("more") else ""
    if not responses:
        return f"form_id={form.get('formId', '')} | {title}\nNo responses yet."
    titles = _question_titles(form)
    per_q: dict[str, list[str]] = {}
    for r in responses:
        for qid, ans in (r.get("answers") or {}).items():
            for a in pick(ans, "textAnswers.answers", default=[]) or []:
                if isinstance(a, dict) and a.get("value") not in (None, ""):
                    per_q.setdefault(str(qid), []).append(str(a["value"]))
    lines = [f"form_id={form.get('formId', '')} | {title} | {len(responses)} response(s){more}"]
    for qid, values in per_q.items():
        counts: dict[str, int] = {}
        for v in values:
            counts[v] = counts.get(v, 0) + 1
        label = titles.get(qid, qid)
        if len(counts) <= MAX_CHOICE_ROWS and len(counts) < len(values):  # repeated answers: a tally
            ranked = sorted(counts.items(), key=lambda x: -x[1])
            tally = "; ".join(f"{one_line(v, 60)}: {c}" for v, c in ranked)
            lines.append(f"- {label} ({len(values)} answers) | {tally}")
        else:
            sample = " / ".join(one_line(v, 120) for v in values[:MAX_SAMPLE_ANSWERS])
            lines.append(f"- {label} ({len(values)} answers) | e.g. {sample}")
    return clip_body("\n".join(lines))


def render_created(data: Any) -> str:
    """Write results: just the ids the model needs next, never the whole resource."""
    if not isinstance(data, dict):
        return "Done."
    found = {k: v for k in (*ID_KEYS, "name", "title")
             if isinstance(v := pick(data, k, f"response_data.{k}", f"data.{k}"), (str, int))}
    return "Done. " + json.dumps(found, ensure_ascii=False) if found else "Done."


MEET_PREFIX = "https://meet.google.com/"


def render_meet(data: Any) -> ToolOutput:
    """The one link this module ever returns: the Meet the user just asked for, only if it is Google's."""
    uri = pick(data, "response_data.meetingUri", "meetingUri", "data.response_data.meetingUri")
    if isinstance(uri, str) and uri.startswith(MEET_PREFIX):
        return ToolOutput(f"Meet link: {uri}")
    return ToolOutput("Created the Meet, but Google returned no link.")


RENDERERS = {
    "drive.search": render_files,
    "drive.list_recent": render_files,
    "docs.read": render_doc,
    "sheets.find": render_sheet_list,
    "sheets.read": render_sheet,
    "tasks.list": render_tasks,
    "contacts.search": render_contacts,
    "meet.transcript": render_transcripts,
    "meet.recent": render_meet_recent,
    "calendar.calendars": render_calendars,
    "slides.read": render_slides,
    "forms.read": render_form,
    "forms.responses": render_form_responses,
    "slides.create": render_created,
    "calendar.delete_event": render_created,
    "calendar.respond": render_created,
    "contacts.create": render_created,
    "contacts.update": render_created,
    "docs.comment": render_created,
    "tasks.delete": render_created,
    "meet.create": render_meet,
}

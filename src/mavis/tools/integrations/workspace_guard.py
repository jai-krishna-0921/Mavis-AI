"""Per-task facts the Workspace write actions rely on (spec 2026-10-03 sections 4.1 and 4.3).

`record_created` / `created_by`: files and tasks a task made itself, the second source of the tainted-task
allowlist. Kept in process memory on purpose, like web.py's search URLs: a restart forgets them, which
fails closed (the task can no longer touch them without the user naming them).
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

from mavis.tools.integrations.normalize import pick

MAX_TRACKED_TASKS = 200
_created: OrderedDict[int, set[str]] = OrderedDict()
_ID_KEYS = ("id", "document_id", "documentId", "spreadsheet_id", "spreadsheetId")


def created_ids(data: Any) -> list[str]:
    """Ids a create action returned (Drive file, Doc, Sheet, task), wherever Composio nests them."""
    found: list[str] = []
    for key in _ID_KEYS:
        value = pick(data, key, f"response_data.{key}", f"data.{key}")
        if isinstance(value, str) and value and value not in found:
            found.append(value)
    return found


def record_created(task_id: int | None, ids: Iterable[str]) -> None:
    ids = [i for i in ids if i]
    if task_id is None or not ids:
        return
    _created.setdefault(task_id, set()).update(ids)
    _created.move_to_end(task_id)
    while len(_created) > MAX_TRACKED_TASKS:
        _created.popitem(last=False)


def created_by(task_id: int) -> set[str]:
    return set(_created.get(task_id, ()))

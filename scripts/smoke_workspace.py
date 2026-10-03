"""Read-only smoke run of the Google Workspace actions on a real account (Workspace spec section 9).

    uv run python scripts/smoke_workspace.py --user 1

Runs drive.search, drive.meta, drive.permissions, docs.read, sheets.find, sheets.read, tasks.list,
tasks.get, contacts.search and mail.profile through the real adapter, then checks that the googlesuper
NEW_MESSAGE trigger type exists. Prints only ok/failed, counts and key names
(so we learn the live response shapes, e.g. whether permissions carry emailAddress); never file content,
names, addresses or secrets. Strictly read-only: every action is asserted to be in READ_ONLY. Exit code 1
if any call failed.

If permissions show email_present=False, ownership of shared docs falls back to the single-owner rule (more
approvals, still safe) and comment notifications also degrade, because the commenter cannot be matched to
the people who have access.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import typer

from mavis.config import get_settings
from mavis.domain.integrations import UserRef
from mavis.tools.integrations.composio import ComposioProvider
from mavis.tools.integrations.composio_map import GOOGLESUPER_TRIGGERS
from mavis.tools.integrations.normalize import extract_list, pick

READ_ONLY = (
    "drive.search", "drive.meta", "drive.permissions", "docs.read", "sheets.find", "sheets.read",
    "tasks.list", "tasks.get", "contacts.search", "mail.profile",
)


def _keys(data: Any) -> list[str]:
    return sorted(data.keys())[:20] if isinstance(data, dict) else [type(data).__name__]


async def main(user_id: int) -> int:
    s = get_settings()
    if not s.composio_api_key:
        print("COMPOSIO_API_KEY not set")
        return 2
    provider = ComposioProvider(api_key=s.composio_api_key, base_url=s.composio_base_url, workspace=True)
    user = UserRef(user_id=user_id)
    failures = 0

    async def run(action: str, args: dict) -> Any:
        nonlocal failures
        assert action in READ_ONLY, action
        res = await provider.execute(user, action, args)
        print(f"{'OK    ' if res.ok else 'FAILED'} {action:18} keys={_keys(res.data) if res.ok else '-'}")
        failures += not res.ok
        return res.data if res.ok else None

    states = await provider.status(user)
    print("google states:", {k: v.value for k, v in states.items() if k not in ("slack", "notion")})
    docs = await run("drive.search", {"query": "mimeType = 'application/vnd.google-apps.document'",
                                      "max_results": 3})
    files = extract_list(docs, "files", "data.files")
    print(f"       drive.search files={len(files)}")
    if files:
        doc_id = str(files[0].get("id"))
        await run("drive.meta", {"file_id": doc_id})
        listed = await run("drive.permissions", {"file_id": doc_id})
        perms = extract_list(listed, "permissions", "data.permissions")
        print(f"       permissions={len(perms)} email_present={any('emailAddress' in p for p in perms)}")
        doc = await run("docs.read", {"document_id": doc_id})
        print(f"       docs.read has_body={bool(pick(doc, 'response_data.body', 'body'))}")
    sheets = await run("sheets.find", {"max_results": 3})
    found = extract_list(sheets, "spreadsheets", "data.spreadsheets")
    print(f"       sheets.find spreadsheets={len(found)}")
    if found:
        sheet = await run("sheets.read", {"spreadsheet_id": str(found[0].get("id"))})
        ranges = extract_list(sheet, "spreadsheet_data.valueRanges", "valueRanges", "data.valueRanges")
        print(f"       sheets.read value_ranges={len(ranges)} "
              f"range_keys={_keys(ranges[0]) if ranges else '-'}")
    tasks = await run("tasks.list", {"max_results": 10})
    listed_tasks = extract_list(tasks, "tasks", "data.tasks", "response_data.items", "items")
    print(f"       tasks.list tasks={len(listed_tasks)}")
    if listed_tasks and listed_tasks[0].get("id"):
        one = await run("tasks.get", {"task_id": str(listed_tasks[0]["id"])})
        print(f"       tasks.get has_title={bool(pick(one, 'title', 'data.title', 'response_data.title'))}")
    await run("contacts.search", {"query": "an", "max_results": 3})
    profile = await run("mail.profile", {})
    email = pick(profile, "emailAddress", "response_data.emailAddress")
    print(f"       mail.profile email_present={bool(email)}")
    slug = GOOGLESUPER_TRIGGERS["mail.new_message"]
    async with httpx.AsyncClient(base_url=s.composio_base_url, headers={"x-api-key": s.composio_api_key},
                                 timeout=30) as client:
        r = await client.get(f"/triggers_types/{slug}")
    print(f"{'OK    ' if r.status_code == 200 else 'FAILED'} trigger {slug} status={r.status_code}")
    failures += r.status_code != 200
    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


def cli(user: int = typer.Option(1, help="Mavis user id")) -> None:
    raise typer.Exit(asyncio.run(main(user)))


if __name__ == "__main__":
    typer.run(cli)

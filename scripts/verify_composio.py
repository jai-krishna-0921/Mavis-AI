"""Check Mavis's Composio assumptions against the live API.

    uv run python scripts/verify_composio.py                  # catalog checks (needs COMPOSIO_API_KEY)
    uv run python scripts/verify_composio.py --connect google # prints a googlesuper consent link for mavis-1
    uv run python scripts/verify_composio.py --execute mail.search
    uv run python scripts/verify_composio.py --triggers       # active trigger names for mavis-1

Exit code 1 if any slug, argument key or trigger is missing. Required keys Mavis does not send are WARN
lines only (the 11 legacy mappings predate this check). Prints names and keys, never secrets or content.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, date, datetime, timedelta

import httpx
import typer

from mavis.domain.integrations import UserRef
from mavis.domain.policy import Capability, RiskClass
from mavis.tools.integrations.actions import (
    ACTIONS,
    CalendarCreateArgs,
    CalendarListArgs,
    CalendarSlotsArgs,
    CalendarUpdateArgs,
    ContactsSearchArgs,
    DocInsertArgs,
    DriveShareArgs,
    MailComposeArgs,
    SheetAppendArgs,
    SheetUpdateArgs,
    TaskAddArgs,
    TaskPatchArgs,
)
from mavis.tools.integrations.composio import ComposioProvider
from mavis.tools.integrations.composio_map import (
    COMPOSIO_ACTIONS,
    COMPOSIO_TRIGGERS,
    GOOGLESUPER,
    GOOGLESUPER_TRIGGERS,
    slug_for,
)

BASE = os.environ.get("COMPOSIO_BASE_URL", "https://backend.composio.dev/api/v3")
NOW = datetime(2026, 10, 5, 4, 30, tzinfo=UTC)
SAMPLES = {
    "mail.draft": MailComposeArgs(to=["a@x.com", "b@x.com"], subject="s", body="b", cc=["c@x.com"]),
    "mail.send": MailComposeArgs(to=["a@x.com", "b@x.com"], subject="s", body="b", cc=["c@x.com"]),
    "calendar.list": CalendarListArgs(time_min=NOW, time_max=NOW + timedelta(days=1), updated_min=NOW),
    "calendar.free_slots": CalendarSlotsArgs(time_min=NOW, time_max=NOW + timedelta(days=1)),
    "calendar.create_event": CalendarCreateArgs(
        summary="x", start=NOW, attendees=["a@x.com"], description="d"
    ),
    "calendar.update_event": CalendarUpdateArgs(
        event_id="e", summary="x", start=NOW, duration_minutes=30, attendees=[], description="d"
    ),
    "drive.share": DriveShareArgs(file_id="f", email="a@x.com", role="commenter"),
    "contacts.search": ContactsSearchArgs(query="ab"),
    "sheets.append_row": SheetAppendArgs(spreadsheet_id="s", values=["a", 1]),
    "sheets.update_range": SheetUpdateArgs(spreadsheet_id="s", sheet_name="Sheet1", start_cell="A1",
                                           values=[["a", 1]]),
    "docs.insert_text": DocInsertArgs(document_id="d", text="x", index=1),
    "tasks.add": TaskAddArgs(title="x", notes="n", due=date(2026, 10, 5)),
    "tasks.patch": TaskPatchArgs(task_id="t", title="x", status="completed", notes="n",
                                 due=date(2026, 10, 5)),
}
_LEGACY_TWINS = (Capability.GMAIL, Capability.CALENDAR)


def sample_args(action: str):
    if action in SAMPLES:
        return SAMPLES[action]
    model = ACTIONS[action].args_model
    fill = {name: ("x" if f.annotation is str else f.default) for name, f in model.model_fields.items()
            if f.is_required()}
    return model.model_validate(fill)


def slugs_to_check(action: str) -> list[str]:
    slugs = [COMPOSIO_ACTIONS[action].slug]
    if ACTIONS[action].capability in _LEGACY_TWINS:
        slugs.append(slug_for(action, GOOGLESUPER))
    return slugs


def schema(tool: dict) -> tuple[set[str], set[str]]:
    params = tool.get("input_parameters") or tool.get("inputParameters") or {}
    return set((params.get("properties") or {}).keys()), set(params.get("required") or [])


async def check_catalog(client: httpx.AsyncClient) -> int:
    problems = 0
    for action, mapping in COMPOSIO_ACTIONS.items():
        sent = set(mapping.translate(sample_args(action)).keys())
        if mapping.file_arg:
            sent.add(mapping.file_arg)
        for slug in slugs_to_check(action):
            r = await client.get(f"/tools/{slug}")
            if r.status_code != 200:
                print(f"MISSING  {action:24} {slug}  ({r.status_code})")
                problems += 1
                continue
            accepted, required = schema(r.json())
            unknown = sent - accepted if accepted else set()
            problems += bool(unknown)
            flag = "OK      " if not unknown else "ARGS    "
            print(f"{flag} {action:24} {slug}  unknown={sorted(unknown)}  accepts={sorted(accepted)}")
            if missing := required - sent:
                print(f"WARN     {action:24} {slug}  required but not sent={sorted(missing)}")
    triggers = [*COMPOSIO_TRIGGERS.items(), *GOOGLESUPER_TRIGGERS.items()]
    for name, slug in triggers:
        r = await client.get(f"/triggers_types/{slug}")
        if r.status_code == 200:
            cfg = (r.json().get("config") or {}).get("properties") or {}
            print(f"OK       trigger {name:24} {slug}  config={sorted(cfg)}")
            continue
        toolkit = slug.split("_", 1)[0].lower()
        listing = await client.get("/triggers_types", params={"toolkit_slugs": toolkit})
        items = listing.json().get("items") or [] if listing.status_code == 200 else []
        options = [t.get("slug") for t in items]
        print(f"MISSING  trigger {name:24} {slug}  ({r.status_code}); available for {toolkit}: {options}")
        problems += 1
    return problems


async def list_triggers(client: httpx.AsyncClient, user: UserRef) -> int:
    r = await client.get("/trigger_instances/active", params={"user_ids": user.provider_id, "limit": 100})
    if r.status_code != 200:
        print(f"could not list triggers ({r.status_code})", file=sys.stderr)
        return 1
    for item in r.json().get("items") or []:
        print(f"{item.get('trigger_name') or item.get('triggerName')}  state={item.get('state')}")
    return 0


def executable(action: str) -> bool:
    """--execute runs plain read actions only: never a write (even WRITE_SELF) or a risk_fn action."""
    spec = ACTIONS.get(action)
    return spec is not None and spec.risk is RiskClass.READ and spec.risk_fn is None


class _Args:
    def __init__(self, connect: str | None, execute: str | None, user: int, triggers: bool) -> None:
        self.connect, self.execute, self.user, self.triggers = connect, execute, user, triggers


async def main(args: _Args) -> int:
    key = os.environ.get("COMPOSIO_API_KEY", "")
    if not key:
        print("COMPOSIO_API_KEY not set", file=sys.stderr)
        return 2
    provider = ComposioProvider(api_key=key, base_url=BASE, workspace=True)
    user = UserRef(user_id=args.user)
    if args.connect:
        print(await provider.connect_link(user, args.connect, "http://localhost:8000/connect/callback"))
        return 0
    if args.execute:
        if not executable(args.execute):
            print("refusing to execute anything but a read action from a script", file=sys.stderr)
            return 2
        print(await provider.status(user))
        res = await provider.execute(user, args.execute, sample_args(args.execute).model_dump(mode="json"))
        print(res.model_dump_json(indent=2)[:4000])
        return 0 if res.ok else 1
    async with httpx.AsyncClient(base_url=BASE, headers={"x-api-key": key}, timeout=30) as client:
        if args.triggers:
            return await list_triggers(client, user)
        problems = await check_catalog(client)
    print(f"\n{problems} problem(s)")
    return 1 if problems else 0


def cli(
    connect: str | None = typer.Option(None, help="toolkit or 'google' to print a consent link for"),
    execute: str | None = typer.Option(None, help="Mavis action to run for --user (read actions only)"),
    user: int = typer.Option(1, help="Mavis user id"),
    triggers: bool = typer.Option(False, help="list the user's active trigger instances"),
) -> None:
    raise typer.Exit(asyncio.run(main(_Args(connect, execute, user, triggers))))


if __name__ == "__main__":
    typer.run(cli)

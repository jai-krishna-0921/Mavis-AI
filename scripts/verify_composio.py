"""Check Mavis's Composio assumptions against the live API.

    uv run python scripts/verify_composio.py                 # catalog checks (needs COMPOSIO_API_KEY)
    uv run python scripts/verify_composio.py --connect gmail # prints a consent link for mavis-1
    uv run python scripts/verify_composio.py --execute mail.search

Exit code 1 if any slug, argument key or trigger is missing.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta

import httpx
import typer

from mavis.domain.integrations import UserRef
from mavis.tools.integrations.actions import (
    ACTIONS,
    CalendarCreateArgs,
    CalendarListArgs,
    CalendarSlotsArgs,
    CalendarUpdateArgs,
    MailComposeArgs,
)
from mavis.tools.integrations.composio import ComposioProvider
from mavis.tools.integrations.composio_map import COMPOSIO_ACTIONS, COMPOSIO_TRIGGERS

BASE = os.environ.get("COMPOSIO_BASE_URL", "https://backend.composio.dev/api/v3")
NOW = datetime.now(UTC)
SAMPLES = {
    "mail.draft": MailComposeArgs(to=["a@x.com", "b@x.com"], subject="s", body="b", cc=["c@x.com"]),
    "mail.send": MailComposeArgs(to=["a@x.com", "b@x.com"], subject="s", body="b", cc=["c@x.com"]),
    "calendar.list": CalendarListArgs(time_min=NOW, time_max=NOW + timedelta(days=1), updated_min=NOW),
    "calendar.free_slots": CalendarSlotsArgs(time_min=NOW, time_max=NOW + timedelta(days=1)),
    "calendar.create_event": CalendarCreateArgs(summary="x", start=NOW, attendees=["a@x.com"], description="d"),
    "calendar.update_event": CalendarUpdateArgs(
        event_id="e", summary="x", start=NOW, duration_minutes=30, attendees=[], description="d"
    ),
}


def sample_args(action: str):
    if action in SAMPLES:
        return SAMPLES[action]
    model = ACTIONS[action].args_model
    fill = {name: ("x" if f.annotation is str else f.default) for name, f in model.model_fields.items()
            if f.is_required()}
    return model.model_validate(fill)


def schema_keys(tool: dict) -> set[str]:
    params = tool.get("input_parameters") or tool.get("inputParameters") or {}
    return set((params.get("properties") or {}).keys())


async def check_catalog(client: httpx.AsyncClient) -> int:
    problems = 0
    for action, mapping in COMPOSIO_ACTIONS.items():
        r = await client.get(f"/tools/{mapping.slug}")
        if r.status_code != 200:
            print(f"MISSING  {action:24} {mapping.slug}  ({r.status_code})")
            problems += 1
            continue
        sent = set(mapping.translate(sample_args(action)).keys())
        accepted = schema_keys(r.json())
        unknown = sent - accepted if accepted else set()
        flag = "OK      " if not unknown else "ARGS    "
        problems += bool(unknown)
        print(f"{flag} {action:24} {mapping.slug}  unknown={sorted(unknown)}  accepts={sorted(accepted)}")
    for name, slug in COMPOSIO_TRIGGERS.items():
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


class _Args:
    def __init__(self, connect: str | None, execute: str | None, user: int) -> None:
        self.connect, self.execute, self.user = connect, execute, user


async def main(args: _Args) -> int:
    key = os.environ.get("COMPOSIO_API_KEY", "")
    if not key:
        print("COMPOSIO_API_KEY not set", file=sys.stderr)
        return 2
    provider = ComposioProvider(api_key=key, base_url=BASE)
    user = UserRef(user_id=args.user)
    if args.connect:
        print(await provider.connect_link(user, args.connect, "http://localhost:8000/connect/callback"))
        return 0
    if args.execute:
        if ACTIONS[args.execute].risk.needs_approval:
            print("refusing to execute an outward action from a script", file=sys.stderr)
            return 2
        print(await provider.status(user))
        res = await provider.execute(user, args.execute, sample_args(args.execute).model_dump(mode="json"))
        print(res.model_dump_json(indent=2)[:4000])
        return 0 if res.ok else 1
    async with httpx.AsyncClient(base_url=BASE, headers={"x-api-key": key}, timeout=30) as client:
        problems = await check_catalog(client)
    print(f"\n{problems} problem(s)")
    return 1 if problems else 0


def cli(
    connect: str | None = typer.Option(None, help="toolkit slug to print a consent link for"),
    execute: str | None = typer.Option(None, help="Mavis action to run for --user (read actions only)"),
    user: int = typer.Option(1, help="Mavis user id"),
) -> None:
    raise typer.Exit(asyncio.run(main(_Args(connect, execute, user))))


if __name__ == "__main__":
    typer.run(cli)

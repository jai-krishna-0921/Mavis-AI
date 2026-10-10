"""`mavis telegram set-webhook|delete-webhook|info|set-profile|set-photo`."""

from __future__ import annotations

import asyncio
import json

import typer

from mavis.channels import telegram_webhook as tw

app = typer.Typer(help="Manage the Telegram webhook", no_args_is_help=True)


def _run(coro) -> None:
    try:
        result = asyncio.run(coro)
    except RuntimeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command("set-webhook")
def set_webhook() -> None:
    """Point Telegram at PUBLIC_BASE_URL/telegram/webhook (stop any `mavis dev` poller first)."""
    _run(tw.set_webhook())


@app.command("delete-webhook")
def delete_webhook() -> None:
    """Remove the webhook so getUpdates polling (`mavis dev`) works again."""
    _run(tw.delete_webhook())


@app.command("info")
def info() -> None:
    """Show Telegram's current webhook state."""
    _run(tw.webhook_info())


@app.command("set-profile")
def set_profile() -> None:
    """Publish the bot's name, descriptions and command menu (only what differs)."""
    _run(tw.set_profile())


@app.command("set-photo")
def set_photo() -> None:
    """Upload the Mavis avatar as the bot's profile photo (each run adds a new photo)."""
    _run(tw.set_photo())

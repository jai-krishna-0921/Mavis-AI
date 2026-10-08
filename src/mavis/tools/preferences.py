"""set_preferences: the user changes their own timezone, currency, name or locale in chat (WRITE_SELF)."""

from __future__ import annotations

from pydantic import Field

from mavis.access import preferences
from mavis.domain.args import ToolArgs
from mavis.domain.policy import RiskClass
from mavis.domain.results import ToolOutput
from mavis.tools.registry import MavisTool


class SetPreferencesArgs(ToolArgs):
    timezone: str | None = Field(default=None, description="IANA zone like Europe/Lisbon")
    currency: str | None = Field(default=None, description="ISO 4217 code like EUR")
    name: str | None = Field(default=None, min_length=1, max_length=60,
                            description="What the user wants to be called")
    locale: str | None = Field(default=None, description="Language tag like pt-PT")


async def _run(user_id: int, args: SetPreferencesArgs) -> ToolOutput:
    done: list[str] = []
    if args.timezone:
        try:
            change = await preferences.set_timezone(user_id, args.timezone)
        except ValueError:
            return ToolOutput(model_note=f"Unknown time zone {args.timezone!r}; ask the user for their city.")
        done.append(f"time zone {change.new} (reminders already set keep their exact time)")
    if args.currency:
        done.append(f"currency {await preferences.set_currency(user_id, args.currency)}")
    if args.name:
        done.append(f"name {await preferences.set_name(user_id, args.name)}")
    if args.locale:
        from mavis.store.repo import users

        await users.update(user_id, locale=args.locale[:16])
        done.append(f"locale {args.locale[:16]}")
    return ToolOutput(user_text="Updated: " + ", ".join(done) + "." if done else "Nothing to change.")


TOOLS = [MavisTool(name="set_preferences", description="Change the user's own time zone, currency, name or "
                   "locale when they ask.", args_model=SetPreferencesArgs, risk=RiskClass.WRITE_SELF, fn=_run,
                   agents=frozenset({"conversation"}), priority=45)]

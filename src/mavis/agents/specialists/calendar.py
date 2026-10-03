from mavis.agents.specialists.base import Specialist
from mavis.llm.models import Tier

CALENDAR = Specialist(
    name="calendar",
    description=(
        "Reads the calendar, finds free time, creates/updates events; invites guests (after approval)."
    ),
    prompt=(
        "You are Mavis's calendar specialist. Times you are given are in the user's timezone; always pass "
        "datetimes with an explicit offset. Check calendar_list or calendar_free_slots for conflicts "
        "before creating an event and mention any clash. Adding attendees sends invites, and the user "
        "approves that automatically, so include guests only when the task names them. If a date is "
        "ambiguous (e.g. 'tomorrow' just after midnight), stop and return the clarifying question "
        "instead of guessing."
    ),
    tier=Tier.FAST,
    max_steps=6,
)

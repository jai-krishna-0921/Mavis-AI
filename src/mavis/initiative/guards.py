"""Rules the initiative engine applies in code to what the model proposes.

Connecting a service is the user's decision, made when they hit a wall and /connect it themselves. The
reasoner may not schedule or send anything about linking an account on its own initiative (live E2E: a
failed 'send an email' left a wakeup 'check whether Gmail is now linked', which nobody asked for).

The rule is structural, not a word list: the model classifies what it proposes (`about_connection` on a
wakeup request or notify intent) and code allows such a proposal only while the user has a connect flow
open (a connections_pending row their own /connect or a ConnectionRequired created). The system's own
connection plumbing (system_connection_check wakeups, the connect prompt in the turn that needs it) does
not go through the reasoner.
"""

from __future__ import annotations

from mavis.store.repo import connections


async def connection_proposal_allowed(user_id: int, about_connection: bool) -> bool:
    return not about_connection or await connections.has_open(user_id)

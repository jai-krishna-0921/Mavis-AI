"""Rules the initiative engine applies in code to what the model proposes.

Connecting a service is the user's decision, made when they hit a wall and /connect it themselves. Mavis
does not schedule checks or send pings about linking an account the user did not ask about (live E2E: a
failed 'send an email' left a wakeup 'check whether Gmail is now linked', which the user never asked for).
The system's own connection flow (system_connection_check wakeups, the connect prompt in the turn that
needs it) is separate plumbing and does not go through the reasoner.
"""

from __future__ import annotations

import re

_SERVICE = re.compile(
    r"\b(gmail|google|calendar|drive|docs?|sheets?|notion|slack|outlook|microsoft|office|teams|github|gitlab|"
    r"jira|linear|asana|trello|dropbox|zoom|composio|account|accounts|integrations?|inbox|mailbox)\b",
    re.IGNORECASE)
_CONNECT = re.compile(
    r"\b(linked|linking|re-?link\w*|connected|connecting|re-?connect\w*|connection|authori[sz]\w*|"
    r"authenticat\w*|oauth|sign(?:ed|ing)?[ -]?in|log(?:ged|ging)?[ -]?in|"
    r"link\s+(?:their|your|his|her|the|a|it)|connect\s+(?:their|your|his|her|the|a|it)|"
    r"grant\w*\s+access)\b",
    re.IGNORECASE)


def is_connection_nudge(text: str) -> bool:
    """The text is about linking / connecting / authorising a service (a model-proposed wakeup reason or
    notification intent). Both a service and a connecting word must be present."""
    return bool(text) and bool(_SERVICE.search(text)) and bool(_CONNECT.search(text))

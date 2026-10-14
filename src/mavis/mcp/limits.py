# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Every number the MCP client is bound by (spec 2026-10-14, D2 D5 D8 D9). One place, so a limit is never
restated and a test can name it."""

from __future__ import annotations

import re
from datetime import timedelta

MAX_SERVERS_PER_USER = 10
MAX_TOOLS_PER_SERVER = 128  # the first 128 by name, after filtering
DESCRIPTION_CHARS = 600
SCHEMA_MAX_BYTES = 8 * 1024  # a tool whose normalized input schema is bigger is skipped
MAX_RESULT_CHARS = 12_000  # output to the model: 40% head, 60% tail
MAX_PAGES = 50  # tools/list follows nextCursor up to this many pages
TOOLS_TTL = timedelta(minutes=15)
STATE_TTL = timedelta(minutes=10)  # an OAuth state row expires after this and is single use
REFRESH_AHEAD = timedelta(seconds=60)  # refresh an access token this close to its expiry
SLUG_RE = re.compile(r"^[a-z0-9_]{1,24}$")
PREVIEW_VALUE_CHARS = 200  # one argument value on an approval card
PREVIEW_ARGS = 12  # argument lines on an approval card
TOKEN_MAX_CHARS = 4096  # a pasted bearer token
NAME_MAX_CHARS = 80
URL_MAX_CHARS = 2048
MAX_CONCURRENT_SESSIONS = 8  # MCP sessions open at once in this process (a 4 GB box)
SESSION_WAIT_S = 30.0  # how long an operation waits for one of those slots
LISTING_REFRESH_S = 15.0  # mcp_tools refreshes stale caches for at most this long, then shows what it has
CALL_DEADLINE_S = 40.0  # one tool call, session setup included

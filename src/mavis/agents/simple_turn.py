"""Compatibility shim: the chat turn now lives in agents/conversation.py (helpers in turn_support.py).

Kept so existing imports and tests keep working; Phase 4 Task 12 points the USER_MESSAGE handler at
`conversation.run_turn`. Module attributes here are aliases: patch `conversation` (or
`turn_support`) to change behaviour, not this module.
"""

from __future__ import annotations

from mavis.agents.conversation import (  # noqa: F401
    CHAT_DEADLINE_S,
    CHAT_EXCLUDED,
    CHAT_MAX_STEPS,
    CHAT_TOOL_LIMIT,
    CHAT_TOOL_TIMEOUT_S,
    TOOL_RULES,
    TOOLS_GUIDE,
    WRAP_UP_FALLBACK,
    _connect_hint,
    _connect_prompt,
    chat_tools,
    run_turn,
)
from mavis.agents.turn_support import (  # noqa: F401
    _STATE_NAMES,
    CONNECTION_RETRY_AFTER_S,
    CONNECTION_TIMEOUT_S,
    HISTORY_LIMIT,
    RESTART_HINT,
    START_HINT,
    TAINT_SUFFIX,
    _failed_until,
    build_context,
    build_context_ex,
    clarified_request,
    connection_states,
    enqueue_learn,
    initiative_hook,
    is_tainted,
    known_name,
    previous_message,
    previous_reply,
    previous_tainted,
    reply_event_id,
    to_langchain,
    user_text,
)

# Old private names.
_initiative_hook = initiative_hook
_to_langchain = to_langchain
_previous_message = previous_message
_previous_reply = previous_reply
_previous_tainted = previous_tainted
_clarified_request = clarified_request
_known_name = known_name
_reply_event_id = reply_event_id

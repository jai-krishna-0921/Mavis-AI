"""H3: provider failures are classified at the integration boundary; the user reads plain words only."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mavis.domain.errors import ActionFailed, FailureKind, IntegrationError, failure_text
from mavis.domain.integrations import ConnectionState, ToolResult, UserRef
from mavis.domain.policy import Capability
from mavis.tools.integrations.actions import (
    CalendarCreateArgs,
    MailComposeArgs,
    NotionCreateArgs,
    SlackSendArgs,
)
from mavis.tools.integrations.composio import ComposioProvider
from mavis.tools.integrations.failures import classify
from mavis.tools.integrations.tools import gated
from mavis.tools.registry import ToolContext
from tests.tools.integrations.fakes import FakeProvider

CTX = ToolContext(user_id=1, timezone="Asia/Kolkata", task_id=1)
GOOGLE_400 = json.dumps({"error": {"errors": [{"domain": "global", "reason": "invalid",
                                               "message": "Invalid attendee email."}],
                                   "code": 400, "message": "Invalid attendee email."}})


@pytest.mark.parametrize("body,status,kind,field", [
    (GOOGLE_400, None, FailureKind.INVALID_ARGUMENT, None),
    ("calendar.create_event failed: " + GOOGLE_400, None, FailureKind.INVALID_ARGUMENT, None),
    ({"error": {"code": 404, "message": "File not found: 1AbC."}}, None, FailureKind.NOT_FOUND, None),
    ({"error": {"status": "PERMISSION_DENIED", "message": "The caller does not have permission"}}, None,
     FailureKind.AUTH, None),
    ({"error": {"status": "RESOURCE_EXHAUSTED"}}, None, FailureKind.RATE_LIMITED, None),
    ({"ok": False, "error": "channel_not_found"}, None, FailureKind.NOT_FOUND, None),
    ("ratelimited", None, FailureKind.RATE_LIMITED, None),
    ({"object": "error", "code": "validation_error", "message": "body.parent.page_id should be a uuid"},
     None, FailureKind.INVALID_ARGUMENT, None),
    ({"error": {"errors": [{"reason": "required", "location": "summary"}]}}, None,
     FailureKind.INVALID_ARGUMENT, "summary"),
    ({"error": {"code": 503}}, None, FailureKind.UNAVAILABLE, None),
    ("anything at all", 401, FailureKind.AUTH, None),
    ("", 429, FailureKind.RATE_LIMITED, None),
    ("The server is sad today", None, FailureKind.UNKNOWN, None),
    ("invalid stuff happened here", None, FailureKind.UNKNOWN, None),  # prose never decides
])
def test_classify_reads_structure_not_prose(body, status, kind, field):
    assert classify(body, status=status) == (kind, field)


class _Failing(FakeProvider):
    def __init__(self, result: ToolResult | None = None, raise_status: int | None = None) -> None:
        super().__init__()
        self._result = result
        self._raise_status = raise_status

    async def execute(self, user, action, args):
        if self._raise_status is not None:
            raise IntegrationError(f"Composio answered {self._raise_status} for POST /x",
                                   status=self._raise_status)
        return self._result


@pytest.mark.parametrize("action,args,capability,body,kind", [
    ("calendar.create_event",
     CalendarCreateArgs(summary="Block", start="2026-10-08T14:00", attendees=["a@x.io"]),
     Capability.CALENDAR, GOOGLE_400, FailureKind.INVALID_ARGUMENT),
    ("mail.send", MailComposeArgs(to=["a@x.io"], subject="s", body="b"), Capability.GMAIL,
     json.dumps({"error": {"code": 429, "message": "User-rate limit exceeded <script>"}}),
     FailureKind.RATE_LIMITED),
    ("slack.send", SlackSendArgs(channel="#x", text="hi"), Capability.SLACK,
     json.dumps({"ok": False, "error": "channel_not_found"}), FailureKind.NOT_FOUND),
    ("notion.create_page", NotionCreateArgs(parent_id="p", title="T"), Capability.NOTION,
     "{\"status\": 401, \"code\": \"unauthorized\", \"message\": \"API token is invalid.\"}",
     FailureKind.AUTH),
])
async def test_gated_reason_is_plain_words_from_the_kind(cache, action, args, capability, body, kind):
    provider = _Failing(ToolResult(ok=False, error=body))
    provider.set_state(1, capability, ConnectionState.ACTIVE)
    from mavis.tools.integrations.connections import ConnectionCache

    with pytest.raises(ActionFailed) as exc:
        await gated(CTX, action, args, provider=provider, cache=ConnectionCache(provider, ttl_s=60))
    assert exc.value.kind is kind
    reason = exc.value.reason
    assert "{" not in reason and "<" not in reason and "message" not in reason.lower()
    assert reason == failure_text(kind, _service(capability))
    assert body[:20] in str(exc.value)  # the model still gets the provider's detail to correct itself


def _service(capability: Capability) -> str:
    from mavis.tools.integrations.actions import display_name

    return display_name(capability)


# The adapter's OWN call failing (our API key, a slug) is not the user's account: never "reconnect".
@pytest.mark.parametrize("status,kind", [(400, FailureKind.INVALID_ARGUMENT), (403, FailureKind.UNAVAILABLE),
                                         (401, FailureKind.UNAVAILABLE), (404, FailureKind.UNAVAILABLE),
                                         (429, FailureKind.RATE_LIMITED), (502, FailureKind.UNAVAILABLE)])
async def test_integration_error_status_is_classified(status, kind):
    provider = _Failing(raise_status=status)
    provider.set_state(1, Capability.CALENDAR, ConnectionState.ACTIVE)
    from mavis.tools.integrations.connections import ConnectionCache

    args = CalendarCreateArgs(summary="Block", start="2026-10-08T14:00")
    with pytest.raises(ActionFailed) as exc:
        await gated(CTX, "calendar.create_event", args, provider=provider,
                    cache=ConnectionCache(provider, ttl_s=60))
    assert exc.value.kind is kind
    assert exc.value.reason == failure_text(kind, "Google Calendar")


def test_failure_text_has_no_dashes_or_raw_markers():
    for kind in FailureKind:
        for field in (None, "attendees", "file_id"):
            text = failure_text(kind, "Slack", field)
            assert "—" not in text and "–" not in text and "{" not in text and text.startswith("Slack")


@respx.mock
async def test_composio_execute_classifies_unsuccessful_answers():
    provider = ComposioProvider(api_key="k", base_url="https://composio.test/api/v3")
    respx.post("https://composio.test/api/v3/tools/execute/GOOGLECALENDAR_CREATE_EVENT").mock(
        return_value=httpx.Response(200, json={"successful": False, "error": GOOGLE_400}))
    res = await provider.execute(UserRef(user_id=7), "calendar.create_event",
                                 {"summary": "x", "start": "2026-10-08T14:00:00+05:30"})
    assert not res.ok and res.error_kind is FailureKind.INVALID_ARGUMENT
    respx.post("https://composio.test/api/v3/tools/execute/GOOGLECALENDAR_CREATE_EVENT").mock(
        return_value=httpx.Response(503, json={"error": "down"}))
    res = await provider.execute(UserRef(user_id=7), "calendar.create_event",
                                 {"summary": "x", "start": "2026-10-08T14:00:00+05:30"})
    assert not res.ok and res.error_kind is FailureKind.UNAVAILABLE
    res = await provider.execute(UserRef(user_id=7), "calendar.create_event", {"start": "2026-10-08T14:00"})
    assert res.error_kind is FailureKind.INVALID_ARGUMENT and res.error_field == "summary"


@pytest.mark.parametrize("body,kind", [
    # Google: a 403 is often a rate limit, and its reason says so (status PERMISSION_DENIED notwithstanding)
    ({"error": {"code": 403, "status": "PERMISSION_DENIED",
                "errors": [{"domain": "usageLimits", "reason": "rateLimitExceeded"}]}},
     FailureKind.RATE_LIMITED),
    ({"error": {"code": 403, "errors": [{"reason": "userRateLimitExceeded"}]}}, FailureKind.RATE_LIMITED),
    ({"error": {"code": 403, "errors": [{"reason": "quotaExceeded"}]}}, FailureKind.RATE_LIMITED),
    ({"error": {"code": 403, "errors": [{"reason": "insufficientPermissions"}]}}, FailureKind.AUTH),
    ({"error": {"code": 403, "errors": [{"reason": "forbidden"}]}}, FailureKind.AUTH),
    ({"error": {"code": 400, "errors": [{"reason": "notFound"}]}}, FailureKind.NOT_FOUND),
    ({"error": {"code": 500, "errors": [{"reason": "backendError"}]}}, FailureKind.UNAVAILABLE),
    ({"status": 400, "code": "rate_limited"}, FailureKind.RATE_LIMITED),
    ({"error": {"code": 403}}, FailureKind.AUTH),  # no code: the status decides
])
def test_a_specific_machine_code_beats_the_http_status(body, kind):
    assert classify(body)[0] is kind
    assert classify("calendar.create_event failed: " + json.dumps(body))[0] is kind  # embedded JSON too


@pytest.mark.parametrize("location,shown", [
    ("attendees", "attendees"), ("summary", "summary"), ("body.attendees[0].email", None),
    ("Please see <a href=x>the body text here</a>", None), ("nonexistent_field", None),
])
def test_only_our_own_argument_names_reach_the_user(location, shown):
    from mavis.tools.integrations.tools import failed_action

    body = json.dumps({"error": {"code": 400, "errors": [{"reason": "invalid", "location": location}]}})
    exc = failed_action("calendar.create_event", ToolResult(ok=False, error=body))
    assert exc.field == shown
    assert exc.reason == failure_text(FailureKind.INVALID_ARGUMENT, "Google Calendar", shown)
    assert "<" not in exc.reason and "body text" not in exc.reason

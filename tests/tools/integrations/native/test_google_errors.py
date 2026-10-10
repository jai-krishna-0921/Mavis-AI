"""Auth refresh, rate limits, server errors and error classification, for every API family."""

from __future__ import annotations

import httpx
import pytest

from mavis.domain.errors import FailureKind
from mavis.domain.integrations import UserRef
from mavis.tools.integrations.actions import ACTIONS

from .gfake import FakeGoogle, Tokens, error

USER = UserRef(user_id=3)
PROFILE = r"/profile$"
OK = httpx.Response(200, json={"emailAddress": "me@orbit.test"})


async def run(fake: FakeGoogle, tokens: Tokens | None = None, action="mail.profile", args=None):
    ex, sleeps = fake.executor(tokens)
    return await ex.execute(USER, action, args or {}), sleeps


async def test_401_refreshes_once_and_retries_with_the_new_token():
    tokens = Tokens()
    fake = FakeGoogle().on("GET", PROFILE, [error(401, "authError", "Invalid Credentials"), OK])
    res, _ = await run(fake, tokens)
    assert res.ok and tokens.calls == [False, True]
    assert [r.headers["authorization"] for r in fake.requests] == ["Bearer tok", "Bearer tok-forced"]


async def test_second_401_is_auth():
    fake = FakeGoogle().on("GET", PROFILE, error(401, "authError"))
    res, _ = await run(fake)
    assert not res.ok and res.error_kind is FailureKind.AUTH and len(fake.requests) == 2


async def test_invalid_grant_is_auth_without_calling_google():
    fake = FakeGoogle().on("GET", PROFILE, OK)
    res, _ = await run(fake, Tokens(revoked=True))
    assert not res.ok and res.error_kind is FailureKind.AUTH and not fake.requests


async def test_missing_scope_403_is_auth():
    fake = FakeGoogle().on("GET", PROFILE, error(403, "insufficientPermissions", "Insufficient Permission"))
    res, _ = await run(fake)
    assert res.error_kind is FailureKind.AUTH


@pytest.mark.parametrize("hdrs", [{"Retry-After": "2"}, {"Retry-After": "0.5"}, {}])
async def test_429_waits_once_then_succeeds(hdrs):
    fake = FakeGoogle().on("GET", PROFILE, [error(429, "rateLimitExceeded", headers=hdrs), OK])
    res, sleeps = await run(fake)
    assert res.ok and len(sleeps) == 1 and 0 <= sleeps[0] <= 10
    if "Retry-After" in hdrs:
        assert sleeps[0] == float(hdrs["Retry-After"])


@pytest.mark.parametrize("reason", ["rateLimitExceeded", "userRateLimitExceeded"])
async def test_403_rate_limit_is_retried_like_429(reason):
    fake = FakeGoogle().on("GET", PROFILE, [error(403, reason, headers={"Retry-After": "1"}), OK])
    res, sleeps = await run(fake)
    assert res.ok and sleeps == [1.0]


async def test_retry_after_beyond_ten_seconds_is_rate_limited_without_waiting():
    fake = FakeGoogle().on("GET", PROFILE, error(429, "rateLimitExceeded", headers={"Retry-After": "30"}))
    res, sleeps = await run(fake)
    assert res.error_kind is FailureKind.RATE_LIMITED and sleeps == [] and len(fake.requests) == 1


async def test_a_second_429_is_rate_limited():
    fake = FakeGoogle().on("GET", PROFILE, error(429, "rateLimitExceeded", headers={"Retry-After": "1"}))
    res, sleeps = await run(fake)
    assert res.error_kind is FailureKind.RATE_LIMITED and len(sleeps) == 1 and len(fake.requests) == 2


async def test_5xx_backs_off_once_then_succeeds_or_is_unavailable():
    ok = FakeGoogle().on("GET", PROFILE, [error(503, "backendError"), OK])
    res, sleeps = await run(ok)
    assert res.ok and len(sleeps) == 1
    bad = FakeGoogle().on("GET", PROFILE, error(500, "backendError"))
    res, sleeps = await run(bad)
    assert res.error_kind is FailureKind.UNAVAILABLE and len(bad.requests) == 2


async def test_network_failure_is_retried_then_unavailable():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    fake = FakeGoogle().on("GET", PROFILE, boom)
    res, sleeps = await run(fake)
    assert res.error_kind is FailureKind.UNAVAILABLE and len(sleeps) == 1


@pytest.mark.parametrize(
    ("action", "args", "field"),
    [
        ("mail.read", {"message_id": "nope"}, "message_id"),
        ("mail.thread", {"thread_id": "nope"}, "thread_id"),
        ("drive.meta", {"file_id": "nope"}, "file_id"),
        ("docs.read", {"document_id": "nope"}, "document_id"),
        ("sheets.read", {"spreadsheet_id": "nope", "range": "A1"}, "spreadsheet_id"),
        ("calendar.update_event", {"event_id": "nope", "summary": "x"}, "event_id"),
    ],
)
async def test_404_is_not_found_and_names_our_id_argument(action, args, field):
    fake = (
        FakeGoogle()
        .on("GET", r".*", error(404, "notFound", "Not Found"))
        .on("PATCH", r".*", error(404, "notFound"))
    )
    res, _ = await run(fake, action=action, args=args)
    assert res.error_kind is FailureKind.NOT_FOUND and res.error_field == field


async def test_400_names_our_field_when_google_names_its_parameter():
    fake = FakeGoogle().on(
        "GET",
        r"/events$",
        error(400, "timeRangeEmpty", "The specified time range is empty.", location="timeMax"),
    )
    res, _ = await run(
        fake,
        action="calendar.list",
        args={"time_min": "2026-10-05T10:00:00+05:30", "time_max": "2026-10-05T09:00:00+05:30"},
    )
    assert res.error_kind is FailureKind.INVALID_ARGUMENT and res.error_field == "time_max"


async def test_400_with_field_violation_details_maps_the_leaf_segment():
    body = {
        "error": {
            "code": 400,
            "message": "Invalid",
            "status": "INVALID_ARGUMENT",
            "details": [{"fieldViolations": [{"field": "attendees[0].email"}]}],
        }
    }
    fake = FakeGoogle().on("POST", r"/events$", httpx.Response(400, json=body))
    res, _ = await run(
        fake,
        action="calendar.create_event",
        args={
            "summary": "x",
            "start": "2026-10-05T10:00:00+05:30",
            "duration_minutes": 30,
            "attendees": ["a@x.com"],
        },
    )
    assert res.error_kind is FailureKind.INVALID_ARGUMENT and res.error_field == "attendees"


async def test_400_on_a_vendor_field_we_do_not_have_sets_no_field():
    fake = FakeGoogle().on("GET", PROFILE, error(400, "invalid", "bad", location="somethingElse"))
    res, _ = await run(fake)
    assert res.error_kind is FailureKind.INVALID_ARGUMENT and res.error_field is None


async def test_file_without_access_is_not_found_and_disabled_api_is_unavailable():
    res, _ = await run(
        FakeGoogle().on("GET", r".*", error(403, "forbidden", "no access")),
        action="drive.meta",
        args={"file_id": "f"},
    )
    assert res.error_kind is FailureKind.NOT_FOUND
    res, _ = await run(
        FakeGoogle().on("GET", r".*", error(403, "accessNotConfigured", "API disabled")),
        action="drive.meta",
        args={"file_id": "f"},
    )
    assert res.error_kind is FailureKind.UNAVAILABLE


async def test_invalid_arguments_never_reach_google():
    fake = FakeGoogle().on("GET", r".*", OK)
    res, _ = await run(fake, action="mail.search", args={"max_results": 500})
    assert res.error_kind is FailureKind.INVALID_ARGUMENT and res.error_field == "max_results"
    res, _ = await run(fake, action="contacts.search", args={"query": "a"})
    assert res.error_field == "query" and not fake.requests


async def test_error_text_is_bounded_vendor_detail():
    fake = FakeGoogle().on("GET", PROFILE, error(500, "backendError", "x" * 5000))
    res, _ = await run(fake)
    assert len(res.error) <= 300


async def test_non_json_error_body_is_still_classified():
    fake = FakeGoogle().on("GET", PROFILE, httpx.Response(502, text="<html>Bad gateway</html>"))
    res, _ = await run(fake)
    assert res.error_kind is FailureKind.UNAVAILABLE


def test_every_handled_action_exists_in_the_catalog_and_the_rest_stay_on_the_fallback():
    ex, _ = FakeGoogle().executor()
    handled = set(ex._handlers)
    assert handled <= set(ACTIONS)
    assert all(ex.handles(a) for a in handled) and len(handled) == 58
    for other in (
        "slack.send",
        "slack.history",
        "notion.search",
        "docs.append",
        "drive.upload",
        "tasks.complete",
        "tasks.update",
        "nonsense",
    ):
        assert not ex.handles(other)


async def test_unhandled_action_is_a_clean_failure():
    ex, _ = FakeGoogle().executor()
    res = await ex.execute(USER, "tasks.complete", {"task_id": "t1"})
    assert not res.ok and res.error_kind is FailureKind.UNKNOWN

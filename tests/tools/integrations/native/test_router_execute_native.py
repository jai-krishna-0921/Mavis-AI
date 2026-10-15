# ruff: noqa: E501, F811
from mavis.domain.errors import FailureKind as K
from mavis.domain.integrations import UserRef

from .test_router import grant  # noqa: F401
from .test_router_merge import Exec, M, both, ms_grant  # noqa: F401

U = UserRef(user_id=1)


async def test_execute_native_runs_one_provider_without_merge_or_fallback(both, tokens):
    fb, g, m, router = both
    await ms_grant(tokens)
    r = await router.execute_native(M, U, "mail.search", {"query": "x"})
    assert r.ok and len(m.calls) == 1 and not g.calls and fb.executed == []


async def test_execute_native_without_a_grant_is_an_auth_failure_not_a_fallback(both):
    fb, g, m, router = both
    r = await router.execute_native(M, U, "mail.search", {})
    assert r.error_kind is K.AUTH and fb.executed == [] and not m.calls


async def test_execute_native_serves_calendar_actions_with_the_outlook_capability(both, tokens):
    fb, g, m, router = both
    await ms_grant(tokens)
    r = await router.execute_native(M, U, "calendar.list", {})
    assert r.ok and [c[0] for c in m.calls] == ["calendar.list"] and not g.calls

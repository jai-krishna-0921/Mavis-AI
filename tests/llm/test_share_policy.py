"""The pure best_effort capacity policy (mavis.llm.share): every rule, for several slot counts."""

import dataclasses

import pytest

from mavis.llm import share
from mavis.llm.share import SlotState, best_effort_verdict

INF = float("inf")


def state(**kw) -> SlotState:
    base = dict(size=3, free=3, best_effort_inflight=0, interactive_waiting=False, chat_in_flight=0,
                since_chat_activity_s=0.0, since_chat_start_s=0.0, since_best_effort_start_s=INF,
                waited_s=0.0)
    base.update(kw)
    return SlotState(**base)


@pytest.mark.parametrize(("size", "cap", "lull_cap"), [(1, 1, 2), (2, 1, 2), (3, 1, 2), (6, 2, 4), (9, 3, 6),
                                                      (30, 10, 20)])
def test_caps(size, cap, lull_cap):
    assert share.share_cap(size, lull=False) == cap
    assert share.share_cap(size, lull=True) == lull_cap


@pytest.mark.parametrize("size", [3, 6, 12])
def test_starts_regardless_of_recent_chat_activity(size):
    s = state(size=size, free=size - 1, chat_in_flight=1, since_chat_activity_s=0.1, since_chat_start_s=0.1)
    assert best_effort_verdict(s).start
    assert best_effort_verdict(s).timeout_s == share.TIMEOUT_S


def test_needs_two_free_slots_so_a_reply_finds_one():
    assert not best_effort_verdict(state(free=1, chat_in_flight=2)).start
    assert best_effort_verdict(state(free=2, chat_in_flight=1)).start


def test_never_while_an_interactive_caller_is_queued():
    assert not best_effort_verdict(state(free=2, interactive_waiting=True)).start
    assert not best_effort_verdict(state(free=1, interactive_waiting=True, waited_s=999,
                                         since_chat_start_s=999)).start


def test_share_cap_without_lull_and_with_lull():
    busy = dict(chat_in_flight=1, free=1 + 1)
    assert not best_effort_verdict(state(best_effort_inflight=1, **busy)).start  # cap 1 reached
    lull = dict(chat_in_flight=0, since_chat_activity_s=share.LULL_QUIET_S, free=2, best_effort_inflight=1)
    assert best_effort_verdict(state(**lull)).start  # lull: cap 2
    assert not best_effort_verdict(state(**{**lull, "best_effort_inflight": 2})).start


def test_a_lull_needs_both_nothing_in_flight_and_quiet():
    base = dict(free=2, best_effort_inflight=1)
    assert not best_effort_verdict(state(chat_in_flight=1, since_chat_activity_s=99, **base)).start
    assert not best_effort_verdict(state(chat_in_flight=0, since_chat_activity_s=1, **base)).start


def test_starts_are_rate_limited():
    assert not best_effort_verdict(state(since_best_effort_start_s=1.9)).start
    assert best_effort_verdict(state(since_best_effort_start_s=2.0)).start


@pytest.mark.parametrize(("waited", "chat_start_age", "expected"), [
    (59.0, 10.0, False),  # not old enough
    (60.0, 1.9, False),  # a chat call just started
    (60.0, 2.0, True),
    (300.0, 50.0, True),
])
def test_starvation_escape_takes_the_last_slot_briefly(waited, chat_start_age, expected):
    v = best_effort_verdict(state(free=1, chat_in_flight=2, waited_s=waited,
                                  since_chat_start_s=chat_start_age, since_chat_activity_s=chat_start_age))
    assert v.start is expected
    if expected:
        assert v.timeout_s == share.ESCAPE_TIMEOUT_S < share.TIMEOUT_S


def test_escape_still_respects_the_share_cap_and_start_gap():
    old = dict(free=1, chat_in_flight=2, waited_s=100, since_chat_start_s=100, since_chat_activity_s=100)
    assert not best_effort_verdict(state(best_effort_inflight=1, **old)).start
    assert not best_effort_verdict(state(since_best_effort_start_s=0.5, **old)).start


def test_no_free_slot_never_starts():
    assert not best_effort_verdict(state(free=0, waited_s=999, since_chat_start_s=999)).start


def test_state_is_immutable_and_pure():
    s = state()
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.free = 1  # type: ignore[misc]
    assert best_effort_verdict(s) == best_effort_verdict(s)

from datetime import UTC, datetime, timedelta

from mavis.domain.wakeups import EVENT_TYPE_FOR_KIND, WakeupKind
from mavis.llm import models as llm
from mavis.store.repo import attention as repo

T0 = datetime(2026, 10, 2, 20, 40, tzinfo=UTC)


async def _insert(user_id: int, mid: str, origin: str = repo.ORIGIN_LIVE, at: datetime = T0):
    return await repo.insert_pending(
        user_id,
        mid,
        thread_id="t",
        origin=origin,
        sender_domain="example.com",
        sender_name="Someone",
        received_at=at,
        payload={"subject": "hi"},
    )


async def test_insert_pending_is_idempotent(user):
    first, created = await _insert(user.id, "m1")
    again, created_again = await _insert(user.id, "m1")
    assert created and not created_again and first.id == again.id
    assert first.status == repo.PENDING and first.pending_payload == {"subject": "hi"}


async def test_pending_orders_live_before_backfill(user):
    await _insert(user.id, "old", origin=repo.ORIGIN_BACKFILL, at=T0 - timedelta(days=3))
    live, _ = await _insert(user.id, "new")
    rows = await repo.pending(user.id, limit=5)
    assert [r.message_id for r in rows] == ["new", "old"]
    assert await repo.pending_count(user.id) == 2
    assert live.id == rows[0].id


async def test_finish_is_conditional_and_clears_payload(user, clock):
    clock.set(T0)
    obs, _ = await _insert(user.id, "m1")
    assert await repo.finish(obs.id, verdict="log", kind="other", summary="other from example: hi")
    assert not await repo.finish(obs.id, verdict="notify")
    row = await repo.get(obs.id)
    assert row.status == repo.DONE and row.verdict == "log" and row.pending_payload is None
    assert row.processed_at == T0


async def test_attempt_window_counts(user, clock):
    clock.set(T0)
    a, _ = await _insert(user.id, "a")
    b, _ = await _insert(user.id, "b")
    await repo.note_attempt(a.id, T0 - timedelta(minutes=5))
    await repo.note_attempt(b.id, T0)
    assert await repo.attempts_since(user.id, T0 - timedelta(minutes=2)) == 1
    assert (await repo.get(b.id)).attempts == 1


async def test_undelivered_and_users_needing_drain(user, clock):
    clock.set(T0)
    obs, _ = await _insert(user.id, "m1")
    await repo.finish(obs.id, delivery=repo.QUEUED)
    clock.set(T0 + timedelta(minutes=5))
    assert [r.id for r in await repo.undelivered(user.id, before=T0 + timedelta(minutes=4))] == [obs.id]
    assert await repo.users_needing_drain() == [user.id]
    await repo.set_fields(obs.id, delivery="sent")
    assert await repo.users_needing_drain() == []


async def test_recent_debits_and_recent(user, clock):
    clock.set(T0)
    for i, direction in enumerate(("debit", "debit", "credit")):
        obs, _ = await _insert(user.id, f"m{i}")
        await repo.finish(obs.id, facts={"money": {"direction": direction, "amount": 10.0}})
    assert await repo.recent_debits(user.id, T0 - timedelta(hours=1), exclude_id=-1) == 2
    assert len(await repo.recent(user.id, T0 - timedelta(hours=1))) == 3
    assert await repo.has_any(user.id)


async def test_purge_and_expire(user, clock):
    clock.set(T0)
    old, _ = await _insert(user.id, "old")
    await repo.finish(old.id, point_id="p-old")
    stale, _ = await _insert(user.id, "stale")
    clock.set(T0 + timedelta(days=3))
    assert await repo.expire_pending(T0 + timedelta(days=1)) == 1
    assert (await repo.get(stale.id)).method == "expired"
    assert await repo.purge_before(T0 + timedelta(days=1)) == ["p-old"]
    assert await repo.get(old.id) is None


async def test_prefs(user):
    pid = await repo.add_pref(user.id, None, "newsletter", "mute", "newsletter from example: digest")
    await repo.set_pref_point(pid, "pt-1")
    assert pid > 0


def test_wakeup_kinds_are_mapped():
    for kind in (
        WakeupKind.SYSTEM_ATTENTION_DRAIN,
        WakeupKind.SYSTEM_ATTENTION_SPEAK,
        WakeupKind.SYSTEM_ATTENTION_BACKFILL,
        WakeupKind.SYSTEM_EVENING_WRAP,
    ):
        assert kind.value.startswith("system_") and len(kind.value) <= 24
        assert kind in EVENT_TYPE_FOR_KIND


def test_unavailable_s_reflects_backoff(monkeypatch):
    assert llm.unavailable_s() <= 0
    llm._ollama.note_rate_limit(5.0)
    assert llm.unavailable_s() > 0


def test_settings_defaults(settings):
    assert settings.attention_enabled is True
    assert settings.attention_large_amounts["INR"] == 10000.0
    assert settings.attention_evening_time == "20:30"


async def test_recent_debits_limit_keeps_the_newest(user, clock):
    clock.set(T0)
    since = T0 - timedelta(hours=3)
    for i in range(50):  # older credits fill the 50 row window if rows are not ordered newest first
        obs, _ = await _insert(user.id, f"old{i}", at=T0 - timedelta(hours=2, minutes=i))
        await repo.finish(obs.id, facts={"money": {"direction": "credit", "amount": 1.0}})
    for i in range(3):
        obs, _ = await _insert(user.id, f"new{i}", at=T0 - timedelta(minutes=i))
        await repo.finish(obs.id, facts={"money": {"direction": "debit", "amount": 1.0}})
    assert await repo.recent_debits(user.id, since, exclude_id=-1) == 3

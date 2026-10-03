from datetime import UTC, datetime, timedelta

from mavis.attention.anomaly import MoneyContext, combine, score_money, score_security, score_sender
from mavis.attention.baselines import Baselines, MoneySnapshot, MoneyStats, SenderStats, robust
from mavis.attention.schema import Direction, RiskFlag

T0 = datetime(2026, 10, 2, 20, 40, tzinfo=UTC)


def test_robust():
    assert robust([]) == (0.0, 0.0)
    assert robust([100, 110, 90, 1000]) == (105.0, 10.0)


async def test_sender_touch_and_established(user):
    b = Baselines()
    for days in (13, 12, 11):
        await b.touch_sender(user.id, "Alerts@ExampleBank.in", "examplebank.in", T0 - timedelta(days=days))
    stats = await b.sender(user.id, "alerts@examplebank.in")
    assert stats.count == 3 and stats.established(T0)
    assert await b.established_domains(user.id, T0) == {"examplebank.in"}
    assert not (await b.sender(user.id, "new@example.com")).established(T0)


async def test_record_money_updates_three_scopes(user):
    b = Baselines()
    for amount, hour in ((320, 13), (410, 20), (380, 13)):
        await b.record_money(user.id, "INR", "swiggy", "upi", amount, hour, T0)
    snap = await b.snapshot(user.id, "INR", "swiggy", "upi")
    assert snap.counterparty.count == 3 and snap.counterparty.median == 380.0
    assert snap.method.count == 3 and snap.overall.count == 3
    assert snap.overall.hours[13] == 2 and snap.overall.hours[20] == 1
    assert await b.counterparty_keys(user.id, "INR") == ["swiggy"]
    assert (await b.snapshot(user.id, "USD", "swiggy", "upi")).overall.count == 0


def _snap(cp=MoneyStats(), method=MoneyStats(), overall=MoneyStats()):
    return MoneySnapshot(key="x", counterparty=cp, method=method, overall=overall)


def test_cold_start_large_night_debit_is_anomalous():
    r = score_money(MoneyContext(48000, Direction.DEBIT, "UPI", 2, _snap(), large_amount=10000))
    assert set(r.codes) == {"large_amount", "new_counterparty", "odd_hour"}
    assert 0.8 <= r.score <= 0.82
    assert "a large amount, and I don't know your usual spending yet" in r.reasons


def test_warm_baseline_ratio_and_new_payee():
    overall = MoneyStats(count=8, median=400.0, hours=tuple([1] * 8 + [0] * 16))
    r = score_money(
        MoneyContext(48000, Direction.DEBIT, "UPI", 13, _snap(overall=overall), large_amount=10000)
    )
    assert "amount_ratio" in r.codes and "new_counterparty" in r.codes and "large_amount" not in r.codes
    assert r.score == 1.0 and any("120x your usual" in x for x in r.reasons)


def test_routine_payment_is_quiet():
    cp = MoneyStats(count=5, median=399.0)
    overall = MoneyStats(count=5, median=399.0, hours=tuple([0] * 13 + [5] + [0] * 10))
    r = score_money(MoneyContext(349, Direction.DEBIT, "UPI", 13, _snap(cp=cp, overall=overall)))
    assert r.score == 0.0 and r.codes == ()


def test_credits_and_bursts():
    assert score_money(MoneyContext(99999, Direction.CREDIT, "UPI", 2, _snap(), large_amount=1)).score == 0.0
    cp = MoneyStats(count=5, median=500.0)
    r = score_money(
        MoneyContext(500, Direction.DEBIT, "card", 13, _snap(cp=cp, overall=cp), recent_debits_1h=2)
    )
    assert r.codes == ("burst",) and r.reasons == ("3 payments within an hour",)


def test_sender_signals():
    assert score_sender(SenderStats(), "example.com", set()).codes == ("new_sender",)
    look = score_sender(SenderStats(count=1), "examp1ebank.in", {"examplebank.in"})
    assert look.codes == ("lookalike_domain",) and "examplebank" not in look.reasons[0]
    assert score_sender(SenderStats(count=4), "examplebank.in", {"examplebank.in"}).score == 0.0


def test_security_and_combine():
    s = score_security([RiskFlag.CREDENTIAL_CHANGE, RiskFlag.OTP_CODE])
    assert s.codes == ("risk:credential_change",) and s.score == 0.6
    both = combine(s, score_sender(SenderStats(), "example.com", set()))
    assert both.score == 0.68 and both.codes == ("risk:credential_change", "new_sender")
    assert combine().score == 0.0

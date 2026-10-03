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
    assert r.score == 1.0 and any("far above your usual" in x for x in r.reasons)


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


def test_empty_counterparty_never_flags_new_payee():
    overall = MoneyStats(count=8, median=400.0, hours=tuple([1] * 8 + [0] * 16))
    snap = MoneySnapshot(key="", overall=overall)
    r = score_money(MoneyContext(400, Direction.DEBIT, "UPI", 3, snap))
    assert "new_counterparty" not in r.codes
    cold = score_money(MoneyContext(400, Direction.DEBIT, "UPI", 13, MoneySnapshot(key="")))
    assert "new_counterparty" not in cold.codes


def test_ratio_text_scopes_and_cap():
    method = MoneyStats(count=5, median=100.0)
    r = score_money(
        MoneyContext(600, Direction.DEBIT, "UPI", 13, _snap(cp=MoneyStats(count=1), method=method))
    )
    assert r.reasons[0] == "about 6x your usual UPI payment"
    overall = MoneyStats(count=5, median=100.0)
    r = score_money(
        MoneyContext(600, Direction.DEBIT, "UPI", 13, _snap(cp=MoneyStats(count=1), overall=overall))
    )
    assert r.reasons[0] == "about 6x your usual payment"
    cp = MoneyStats(count=3, median=100.0)
    r = score_money(MoneyContext(2000, Direction.DEBIT, "UPI", 13, _snap(cp=cp)))
    assert r.reasons == ("about 20x your usual for this payee",)
    r = score_money(MoneyContext(2100, Direction.DEBIT, "UPI", 13, _snap(cp=cp)))
    assert r.reasons == ("far above your usual for this payee",)
    tiny = MoneyStats(count=3, median=1e-9)
    r = score_money(MoneyContext(500, Direction.DEBIT, "UPI", 13, _snap(cp=tiny)))
    assert r.reasons == ("far above your usual for this payee",) and r.score == 1.0


def test_equal_amounts_mad_zero_and_ratio_boundary():
    assert robust([5, 5, 5, 5, 5]) == (5.0, 0.0)
    cp = MoneyStats(count=5, median=500.0, mad=0.0)
    quiet = score_money(MoneyContext(999, Direction.DEBIT, "UPI", 13, _snap(cp=cp)))
    assert quiet.codes == ()
    edge = score_money(MoneyContext(1000, Direction.DEBIT, "UPI", 13, _snap(cp=cp)))
    assert edge.codes == ("amount_ratio",) and edge.score == 0.301


def test_count_and_hour_thresholds():
    ctx = lambda cp, method, overall, amt=1000: score_money(  # noqa: E731
        MoneyContext(amt, Direction.DEBIT, "UPI", 13, _snap(cp, method, overall))
    )
    seen = MoneyStats(count=1)
    two = MoneyStats(count=2, median=100.0)
    three = MoneyStats(count=3, median=100.0)
    assert "amount_ratio" not in ctx(two, MoneyStats(), MoneyStats()).codes
    assert "amount_ratio" in ctx(three, MoneyStats(), MoneyStats()).codes
    four, five = MoneyStats(count=4, median=100.0), MoneyStats(count=5, median=100.0)
    assert "amount_ratio" not in ctx(seen, four, four).codes
    assert "amount_ratio" in ctx(seen, five, MoneyStats()).codes
    assert "amount_ratio" in ctx(seen, MoneyStats(), five).codes
    # hour switch: 9 samples use the night rule, 10 use the rare hour share
    nine = MoneyStats(count=9, median=100.0, hours=tuple([0] * 12 + [9] + [0] * 11))
    ten = MoneyStats(count=10, median=100.0, hours=tuple([0] * 12 + [10] + [0] * 11))
    cp = MoneyStats(count=3, median=100.0)
    at = lambda h, overall: score_money(  # noqa: E731
        MoneyContext(100, Direction.DEBIT, "UPI", h, _snap(cp=cp, overall=overall))
    )
    assert at(3, nine).codes == ("odd_hour",) and at(3, ten).codes == ("odd_hour",)
    assert at(7, nine).codes == () and at(7, ten).codes == ("odd_hour",)
    assert at(12, ten).codes == ()


def test_lookalike_needs_established_domains():
    assert score_sender(SenderStats(count=1), "examp1ebank.in", set()).codes == ()
    assert score_sender(SenderStats(count=1), "", {"examplebank.in"}).codes == ()

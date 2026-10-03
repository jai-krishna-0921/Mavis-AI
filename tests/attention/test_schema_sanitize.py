from mavis.attention.counterparty import display_name, match_key, normalize_counterparty
from mavis.attention.sanitize import clean, domain_label, sender_domain, summary_text
from mavis.attention.schema import (
    Direction,
    EmailKind,
    EmailUnderstanding,
    Money,
    PayMethod,
    RiskFlag,
    UrgencyHint,
)


def test_money_is_lenient():
    m = Money.model_validate(
        {
            "amount": "₹48,000.00",
            "currency": "rs.",
            "direction": "DEBIT",
            "method": "UPI",
            "counterparty": "x" * 500,
        }
    )
    assert m.amount == 48000.0 and m.currency == "INR" and m.direction is Direction.DEBIT
    assert m.method is PayMethod.UPI and len(m.counterparty) == 120
    odd = Money.model_validate(
        {"amount": 5, "currency": "dollars?", "direction": "sideways", "method": "cheque"}
    )
    assert odd.currency == "" and odd.direction is Direction.UNKNOWN and odd.method is PayMethod.OTHER


def test_understanding_is_lenient():
    u = EmailUnderstanding.model_validate(
        {
            "kind": "bank_alert",
            "urgency_hint": "URGENT!!",
            "action_requested": "a" * 400,
            "people": ["Priya", "", "B" * 90, "c", "d", "e", "f"],
            "risk_flags": ["new_signin", "made_up", "new_signin"],
        }
    )
    assert u.kind is EmailKind.OTHER and u.urgency_hint is UrgencyHint.NORMAL
    assert len(u.action_requested) == 160 and len(u.people) == 5 and len(u.people[1]) == 60
    assert u.risk_flags == [RiskFlag.NEW_SIGNIN]


def test_clean_removes_links_phones_domains():
    text = (
        "Call +91 98765 43210 or visit https://evil.example/x or evil-bank.com, "
        "mail a@b.example <b>now</b>\n\u2014ok"
    )
    out = clean(text, 200)
    for bad in ("98765", "https", "evil-bank.com", "a@b.example", "<", ">", "\n"):
        assert bad not in out
    assert "\u2014ok" in out  # third-party punctuation is kept (typography is the channel's, phase A4)
    assert "[removed]" in out
    assert len(clean("word " * 100, 30)) <= 30


def test_sender_domain_and_label():
    assert sender_domain("alerts@mail.examplebank.in") == "examplebank.in"
    assert sender_domain("x@notify.examplebank.co.in") == "examplebank.co.in"
    assert sender_domain("no-reply@accounts.example.com") == "example.com"
    assert domain_label("examplebank.co.in") == "examplebank" and domain_label("") == "an unknown sender"


def test_summary_text_has_no_domain_tld_or_links():
    s = summary_text(EmailKind.MONEY_MOVEMENT, "examplebank.in", "Debit alert: visit http://x.example now")
    assert s.startswith("money movement from examplebank:")
    assert "examplebank.in" not in s and "http" not in s


def test_normalize_counterparty():
    assert normalize_counterparty("SWIGGY LIMITED") == "swiggy"
    assert normalize_counterparty("swiggy@icici") == "swiggy"
    assert normalize_counterparty("To Ramesh Kumar.") == "ramesh kumar"
    assert normalize_counterparty("Amazon Pay India Pvt Ltd") == "amazon pay"
    assert normalize_counterparty("9876543210@ybl") == ""
    assert normalize_counterparty("Call 98765 43210 now") == "call now"


def test_match_key_and_display():
    assert match_key("swigy", ["swiggy", "zomato"]) == "swiggy"
    assert match_key("ramesh kumar", ["swiggy"]) == "ramesh kumar"
    assert match_key("", ["swiggy"]) == ""
    assert display_name("ramesh kumar") == "Ramesh Kumar" and display_name("") == "an unknown payee"


def test_clean_removes_upi_ids_and_otp_codes():
    out = clean("Pay ramesh@okicici now. Your OTP is 482913. code: A7-9921 pin 4321 ref 1234567890", 300)
    for bad in ("ramesh", "okicici", "482913", "9921", "4321", "1234567890", "@"):
        assert bad not in out
    assert "Pay" in out


def test_amount_parser_never_guesses():
    from mavis.attention.schema import parse_amount

    for bad in (
        "1e12",
        "1e3",
        "-500",
        "1.234,56",
        "Rs 4.8 widgets",
        "12.5.3",
        "nan",
        "inf",
        "",
        "0",
        True,
        None,
        -5,
        [],
    ):
        assert parse_amount(bad) is None, bad
    assert parse_amount("Rs 4.8 lakh") == 480000.0
    assert parse_amount("2 crore") == 20000000.0
    assert parse_amount("INR 1,23,456.50") == 123456.5
    assert parse_amount("48,000.00") == 48000.0 and parse_amount("48000") == 48000.0
    assert parse_amount(12.5) == 12.5


def test_bad_fields_do_not_discard_the_record():
    base = {"kind": "security", "needs_user": True, "risk_flags": ["otp_code"]}
    for money in (
        {"amount": None},
        {"amount": -3},
        {"amount": "12.5.3"},
        {"amount": float("inf")},
        "lots",
        5,
    ):
        u = EmailUnderstanding.model_validate({**base, "money": money})
        assert u.money is None and u.kind is EmailKind.SECURITY and u.needs_user
        assert u.risk_flags == [RiskFlag.OTP_CODE]
    u = EmailUnderstanding.model_validate({**base, "deadline": "soon"})
    assert u.deadline is None and u.kind is EmailKind.SECURITY
    u = EmailUnderstanding.model_validate({**base, "money": {"amount": 5, "occurred_at": "garbage"}})
    assert u.money is not None and u.money.occurred_at is None
    u = EmailUnderstanding.model_validate({**base, "deadline": "2026-10-03T10:00:00+05:30"})
    assert u.deadline is not None


def test_bare_string_flags_and_people():
    u = EmailUnderstanding.model_validate(
        {"kind": "security", "risk_flags": "otp_code, new_signin, bogus", "people": "Priya"}
    )
    assert u.risk_flags == [RiskFlag.OTP_CODE, RiskFlag.NEW_SIGNIN] and u.people == ["Priya"]
    assert EmailUnderstanding.model_validate({"kind": "other", "people": [None, {"a": 1}, "Raj"]}).people == [
        "Raj"
    ]


def test_match_key_keeps_distinct_payees_apart():
    assert match_key("amazon seller x", ["amazon seller y"]) == "amazon seller x"
    assert match_key("flipkart 2", ["flipkart 1"]) == "flipkart 2"
    assert match_key("uber trip a2", ["uber trip a1"]) == "uber trip a2"
    assert match_key("rahul sharmo", ["rahul sharma"]) == "rahul sharmo"
    assert match_key("seller b", ["seller a"]) == "seller b"
    assert match_key("swigy", ["swiggy"]) == "swiggy"


def test_counterparty_strips_phone_like_groups():
    assert normalize_counterparty("98 76 54 32 10") == ""
    assert normalize_counterparty("Ramesh 987 654 3210") == "ramesh"
    assert normalize_counterparty("+91 98765 43210 Kumar") == "kumar"
    assert normalize_counterparty("flipkart 1") == "flipkart 1"
    assert normalize_counterparty("S\u00e3o Paulo Caf\u00e9") == "sao paulo cafe"
    assert normalize_counterparty("\uff21mazon") == "amazon"


def test_clean_keeps_amounts_and_dates():
    out = clean(
        "Rs 48000 debited on 2026-10-03, due 03-10-2026, ref 2026 10 03, INR 1,25,000 and \u20b9 52000", 200
    )
    for keep in ("Rs 48000", "2026-10-03", "03-10-2026", "2026 10 03", "INR 1,25,000", "\u20b9 52000"):
        assert keep in out
    assert "[removed]" not in out
    assert "Rs 48000 2026-10-03" in clean("Rs 48000 2026-10-03", 100)


def test_clean_still_strips_long_numbers_and_codes():
    out = clean(
        "Card 4111 1111 1111 1111, acct 123456789012, 4821 is your verification code, otp 998877", 300
    )
    for bad in ("4111", "123456789012", "4821", "998877"):
        assert bad not in out


def test_clean_unicode_and_obfuscation():
    for text in (
        "visit ev\u0456l.com now",  # Cyrillic i
        "visit \uff45\uff56\uff49\uff4c.com now",  # fullwidth
        "visit ev\u200bil.com now",  # zero width split
        "visit \u03b5vil.com now",  # Greek
        "visit hxxp://evil[.]com/x",
        "visit evil . com now",
        "visit evil(.)com now",
        "mail a\uff20b.com",
        "visit evil\u3002com",
    ):
        out = clean(text, 200)
        assert "evil" not in out.replace("ev\u0456l", "").lower() or "[removed]" in out, text
        assert ".com" not in out and "\u0456" not in out and "hxxp" not in out, text

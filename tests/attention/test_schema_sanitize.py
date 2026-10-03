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
    for bad in ("98765", "https", "evil-bank.com", "a@b.example", "<", ">", "\u2014", "\n"):
        assert bad not in out
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
    out = clean("Pay ramesh@okicici now. Your OTP is 482913. code: A7-9921 pin 4321 ref 123456", 300)
    for bad in ("ramesh", "okicici", "482913", "9921", "4321", "123456", "@"):
        assert bad not in out
    assert "Pay" in out

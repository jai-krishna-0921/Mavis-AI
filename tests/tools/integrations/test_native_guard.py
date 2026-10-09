"""Redaction detectors and structural source filters (native/guard.py), tested with generated secrets
and with look-alikes that must survive."""

from __future__ import annotations

import random
import string

import pytest

from mavis.tools.integrations.native import guard
from mavis.tools.integrations.native.guard import (
    Mute,
    iban_ok,
    luhn_ok,
    redact,
    should_ingest_email,
    should_ingest_slack,
    verhoeff_ok,
)

RNG = random.Random(20261009)


def rand(alphabet: str, n: int) -> str:
    return "".join(RNG.choice(alphabet) for _ in range(n))


def luhn_number(prefix: str, length: int) -> str:
    body = prefix + rand(string.digits, length - len(prefix) - 1)
    for check in "0123456789":
        if luhn_ok(body + check):
            return body + check
    raise AssertionError


def verhoeff_number(length: int = 12) -> str:
    while True:
        body = rand("23456789", 1) + rand(string.digits, length - 2)
        for check in "0123456789":
            if verhoeff_ok(body + check):
                return body + check


def iban_number(country: str = "DE", body_len: int = 18) -> str:
    body = rand(string.digits, body_len)
    for chk in range(2, 98):
        cand = f"{country}{chk:02d}{body}"
        if iban_ok(cand):
            return cand
    raise AssertionError


# --- secrets that must be masked ---------------------------------------------------------------------


@pytest.mark.parametrize("template", [
    "Your verification code is {c}. It expires in 10 minutes.",
    "{c} is your login OTP. Do not share it.",
    "OTP: {c}",
    "Use code {c} to sign in",
    "Your one-time password (OTP) is {c}",
    "Ihr Bestätigungscode lautet: {c}",
    "Tu código de verificación es {c}",
    "Il tuo codice di verifica è {c}",
    "Your security code: {c}",
    "验证码：{c}，请勿泄露",
    "PIN {c} for your card",
    "{c} es tu código de seguridad",
    "Your OTP for the transaction of Rs. 4,500.00 on 08-10-2026 is {c}. Do not share it.",
    "Para confirmar el pago de 40 EUR introduce el código {c}",
    "Verification code (valid for 10 minutes): {c}",
])
def test_one_time_codes_are_masked_in_common_forms(template):
    for _ in range(4):
        code = rand(string.digits, RNG.choice([4, 6, 6, 8]))
        out = redact(template.format(c=code))
        assert code not in out, out
        assert "[redacted:" in out


def test_alphanumeric_code_with_dash_is_masked():
    assert "G-482913" not in redact("Your Google verification code is G-482913")


@pytest.mark.parametrize("template", [
    "Your temporary password is {p}",
    "password: {p}",
    "Passwort: {p}",
    "Contraseña: {p}",
    "pwd = {p}",
])
def test_passwords_given_in_text_are_masked(template):
    pw = rand(string.ascii_letters + string.digits + "!#%", 11)
    out = redact(template.format(p=pw))
    assert pw not in out


@pytest.mark.parametrize("secret", [
    lambda: "sk-" + rand(string.ascii_letters + string.digits, 40),
    lambda: "sk-ant-api03-" + rand(string.ascii_letters + string.digits + "_-", 60),
    lambda: "xoxp-" + "-".join(rand(string.digits, 11) for _ in range(3)) + "-"
            + rand(string.hexdigits.lower(), 32),
    lambda: "xoxb-" + rand(string.digits, 12) + "-" + rand(string.ascii_letters, 24),
    lambda: "ghp_" + rand(string.ascii_letters + string.digits, 36),
    lambda: "AKIA" + rand(string.ascii_uppercase + string.digits, 16),
    lambda: "AIza" + rand(string.ascii_letters + string.digits + "_-", 35),
    lambda: "ya29." + rand(string.ascii_letters + string.digits + "_-", 80),
    lambda: "eyJ" + rand(string.ascii_letters + string.digits, 20) + ".eyJ" + rand(string.ascii_letters, 30)
            + "." + rand(string.ascii_letters + string.digits + "_-", 43),
    lambda: rand(string.ascii_letters + string.digits, 40),  # no prefix: length and entropy
])
def test_api_keys_and_tokens_are_masked(secret):
    for _ in range(3):
        key = secret()
        out = redact(f"Here is the key you asked for: {key} please rotate it")
        assert key not in out
        assert "please rotate it" in out


def test_secret_query_parameters_and_bearer_headers():
    out = redact("curl -H 'Authorization: Bearer abcdEFGH12345678ijkl' https://x.io/a?token=Zq81hGf3Lm&page=2")
    assert "abcdEFGH12345678ijkl" not in out and "Zq81hGf3Lm" not in out
    assert "page=2" in out


def test_private_key_block_is_masked():
    body = rand(string.ascii_letters + string.digits, 60)
    out = redact(f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----\nthanks")
    assert body not in out and out.endswith("thanks")


def test_card_numbers_by_luhn_in_any_grouping():
    for prefix, length in [("4", 16), ("51", 16), ("37", 15), ("6011", 16), ("4", 13)]:
        num = luhn_number(prefix, length)
        for text in (num, " ".join(num[i:i + 4] for i in range(0, len(num), 4)),
                     "-".join(num[i:i + 4] for i in range(0, len(num), 4))):
            out = redact(f"Card on file: {text} exp 12/28")
            assert "[redacted:card]" in out
            assert num[:8] not in out.replace(" ", "").replace("-", "")
            assert "exp 12/28" in out


def test_card_followed_by_digits_is_still_found():
    num = luhn_number("4", 16)
    spaced = " ".join(num[i:i + 4] for i in range(0, 16, 4))
    assert "[redacted:card]" in redact(f"{spaced} 12 26 123")


def test_iban_by_mod97():
    for country, n in [("DE", 18), ("GB", 18), ("FR", 23)]:
        iban = iban_number(country, n)
        grouped = " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))
        for form in (iban, grouped):
            out = redact(f"Please wire to {form} and confirm")
            assert "[redacted:iban]" in out and "and confirm" in out


def test_indian_bank_account_near_keyword():
    out = redact("Your A/c No. 912010034455667 has been credited with Rs. 4,500.00 on 08-10-2026")
    assert "912010034455667" not in out
    assert "Rs. 4,500.00" in out and "08-10-2026" in out
    out2 = redact("Account number: 5010 0123 4567 89, IFSC HDFC0001234")
    assert "5010 0123 4567 89" not in out2 and "HDFC0001234" in out2
    assert "XXXXXX1234" in redact("debited from A/c XXXXXX1234 on 08-Oct")


def test_aadhaar_by_verhoeff_and_pan_by_structure():
    a = verhoeff_number()
    grouped = f"{a[:4]} {a[4:8]} {a[8:]}"
    for form in (a, grouped):
        assert "[redacted:aadhaar]" in redact(f"Aadhaar {form} linked")
    out = redact("PAN ABCPK1234F and GST 29ABCPK1234F1Z5")
    assert "ABCPK1234F" not in out.split("and")[0]


# --- look-alikes that must survive -------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Order #4471829305 has shipped and arrives on 2026-10-12 at 14:30",
    "Invoice INV-2026-00431 total 12,450.00 INR due 25/10/2026",
    "Call me on +91 98765 43210 or 080 4123 5566. Regards, Priya",
    "Tracking: 1Z999AA10123456784 via UPS, ETA Friday",
    "Your order code review is scheduled for 3pm",
    "ZIP code 94107 and area code 415",
    "Meeting id 123 456 7890 starts 10:00",
    "The price is $1,299.99 plus 18% GST",
    "Reference 20261008-77431 for the Q3 budget thread",
    "See https://docs.example.com/d/1A2b3C4d5E6f7G8h9I0jKlMnOpQrStUvWxYz/edit for the notes",
    "Ticket 550e8400-e29b-41d4-a716-446655440000 was closed",
    "Q3_Report_Final_2026_v2_signed_copy attached",
    "InternationalizationConfigurationManager2 was renamed",
    "Flight AI 302, PNR booking ref X7K2LM, seat 14C",
    "The password reset link has expired, please request a new one",
    "Do not share this code with anyone",
])
def test_lookalikes_survive(text):
    assert redact(text) == text


def test_random_order_numbers_survive():
    for _ in range(300):
        n = rand(string.digits, RNG.choice([10, 12, 14, 16, 18]))
        text = f"Order {n} confirmed"
        assert redact(text) == text


def test_redact_is_idempotent_and_handles_empty():
    once = redact("OTP: 482913 and sk-" + rand(string.ascii_letters, 30))
    assert redact(once) == once
    assert redact("") == ""


# --- source filters ----------------------------------------------------------------------------------


def mail(**kw):
    base = {"message_id": "m1", "from": "Priya <priya@acme.com>", "from_address": "priya@acme.com",
            "labels": ["INBOX", "UNREAD"], "list_unsubscribe": False, "headers": {}, "from_me": False}
    return {**base, **kw}


@pytest.mark.parametrize(("kw", "lane", "reason"), [
    ({}, "graph", ""),
    ({"labels": ["SPAM"]}, "drop", "spam_or_trash"),
    ({"labels": ["INBOX", "TRASH"]}, "drop", "spam_or_trash"),
    ({"labels": ["SENT"]}, "drop", "own_mail"),
    ({"labels": ["CATEGORY_PROMOTIONS"]}, "bulk", "promotional_label"),
    ({"labels": ["CATEGORY_SOCIAL"]}, "bulk", "promotional_label"),
    ({"labels": ["CATEGORY_FORUMS"]}, "bulk", "promotional_label"),
    ({"list_unsubscribe": True}, "bulk", "list_unsubscribe"),
    ({"headers": {"Precedence": "bulk"}}, "bulk", "bulk_header"),
    ({"headers": {"precedence": "list"}}, "bulk", "bulk_header"),
])
def test_email_lanes(kw, lane, reason):
    d = should_ingest_email(mail(**kw))
    assert (d.lane, d.reason) == (lane, reason)
    assert bool(d) == (lane == "graph")


def test_email_mute_by_sender_and_domain_including_subdomains():
    m = Mute(senders=frozenset({"priya@acme.com"}))
    assert should_ingest_email(mail(), m).reason == "muted_sender"
    d = Mute(domains=frozenset({"acme.com"}))
    assert should_ingest_email(mail(from_address="x@eu.mail.acme.com"), d).reason == "muted_domain"
    assert should_ingest_email(mail(from_address="x@notacme.com"), d).lane == "graph"


def slack(**kw):
    return {"channel": "C0123ABCD", "ts": "1760000000.000100", "user": "U0AAA1111", "text": "lunch?", **kw}


@pytest.mark.parametrize(("kw", "reason"), [
    ({"subtype": "channel_join"}, "system_subtype"),
    ({"subtype": "channel_topic"}, "system_subtype"),
    ({"subtype": "bot_message"}, "system_subtype"),
    ({"bot_id": "B123"}, "bot"),
    ({"user": ""}, "no_author"),
    ({"text": "  "}, "empty"),
])
def test_slack_drops_bots_and_system_messages(kw, reason):
    d = should_ingest_slack(slack(**kw))
    assert (d.lane, d.reason) == ("drop", reason)


def test_slack_keeps_humans_and_honours_mute_by_channel_and_user():
    assert should_ingest_slack(slack()).lane == "graph"
    assert should_ingest_slack(slack(subtype="thread_broadcast")).lane == "graph"
    assert should_ingest_slack(slack(), Mute(slack=frozenset({"C0123ABCD"}))).reason == "muted_slack"
    assert should_ingest_slack(slack(), Mute(slack=frozenset({"U0AAA1111"}))).reason == "muted_slack"


@pytest.mark.parametrize(("raw", "expected"), [
    ("Priya@Acme.com", ("senders", "priya@acme.com")),
    ("acme.com", ("domains", "acme.com")),
    ("@acme.com", ("domains", "acme.com")),
    ("#C0123ABCD", ("slack", "C0123ABCD")),
    ("c0123abcd", None),
    ("U0AAA1111", ("slack", "U0AAA1111")),
    ("general", None),
    ("", None),
])
def test_parse_mute_target(raw, expected):
    assert guard.parse_mute_target(raw) == expected


async def test_mute_list_round_trip_in_user_settings(user):
    assert (await guard.load_mute(user.id)) == Mute()
    assert await guard.add_mute(user.id, "news@acme.com") == ("senders", "news@acme.com")
    await guard.add_mute(user.id, "news@acme.com")  # twice is once
    await guard.add_mute(user.id, "spam.io")
    await guard.add_mute(user.id, "C0123ABCD")
    m = await guard.load_mute(user.id)
    assert m.senders == {"news@acme.com"} and m.domains == {"spam.io"} and m.slack == {"C0123ABCD"}
    await guard.remove_mute(user.id, "spam.io")
    assert (await guard.load_mute(user.id)).domains == frozenset()
    assert await guard.add_mute(user.id, "not a target") is None

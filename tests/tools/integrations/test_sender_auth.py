from mavis.tools.integrations.normalize import sender_authenticated


def h(value: str) -> dict[str, str]:
    return {"authentication-results": value}


def test_dmarc_or_dkim_pass_for_from_domain():
    assert sender_authenticated(
        h("mx.google.com; dkim=pass header.i=@bank.com header.d=bank.com"), "a@bank.com"
    )
    assert sender_authenticated(
        h("mx.google.com; spf=fail; dmarc=pass (p=REJECT) header.from=bank.com"), "a@mail.bank.com"
    )


def test_rejects_other_domain_fail_or_missing():
    assert not sender_authenticated(h("mx.google.com; dkim=pass header.d=attacker.com"), "a@bank.com")
    assert not sender_authenticated(h("mx.google.com; dkim=pass header.d=evilbank.com"), "a@bank.com")
    assert not sender_authenticated(
        h("mx.google.com; dkim=fail header.d=bank.com; spf=pass smtp.mailfrom=bank.com"), "a@bank.com"
    )
    assert not sender_authenticated(h("mx.google.com; dkim=pass header.d=sub.bank.com"), "a@bank.com")
    assert not sender_authenticated({}, "a@bank.com")
    assert not sender_authenticated(h("mx.google.com; dmarc=pass header.from=bank.com"), "")


def _raw(headers: list[dict[str, str]]) -> dict:
    return {"messageId": "m1", "sender": "Bank <a@bank.com>", "payload": {"headers": headers}}


def test_normalize_stores_only_the_bool():
    from mavis.tools.integrations.normalize import normalize_email

    out = normalize_email(
        _raw([{"name": "Authentication-Results", "value": "mx.google.com; dkim=pass header.d=bank.com"}])
    )
    assert out["sender_authenticated"] is True
    assert "authentication-results" not in out["headers"]
    assert "dkim" not in repr(out)
    assert normalize_email(_raw([]))["sender_authenticated"] is False


def test_normalize_uses_the_topmost_header_only():
    """A sender-written Authentication-Results lower in the message never overrides the receiver's."""
    from mavis.tools.integrations.normalize import normalize_email

    out = normalize_email(
        _raw(
            [
                {"name": "Authentication-Results", "value": "mx.google.com; dkim=fail header.d=bank.com"},
                {"name": "Authentication-Results", "value": "mx.google.com; dkim=pass header.d=bank.com"},
            ]
        )
    )
    assert out["sender_authenticated"] is False


# The shape Gmail writes (seen in live Composio poller payloads), with fake domains.
GMAIL_SHAPE = (
    "mx.google.com;\n"
    "       dkim=pass header.i=@accounts.example.com header.s=20230601 header.b=AbCdEf12;\n"
    "       spf=pass (example.com: domain of 3abc@bounces.example.com designates 192.0.2.7 as permitted "
    "sender) smtp.mailfrom=3abc@bounces.example.com;\n"
    "       dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=accounts.example.com"
)


def test_gmail_shape_with_header_i_authenticates():
    assert sender_authenticated(h(GMAIL_SHAPE), "no-reply@accounts.example.com")
    dkim_only = "mx.google.com; dkim=pass header.i=@accounts.example.com header.s=s1 header.b=x;"
    assert sender_authenticated(h(dkim_only), "no-reply@accounts.example.com")
    assert sender_authenticated(h("mx.google.com; dkim=pass header.i=alerts@bank.com"), "alerts@bank.com")


def test_gmail_shape_for_another_from_domain_fails():
    assert not sender_authenticated(h(GMAIL_SHAPE), "no-reply@bank.com")
    spf_only = "mx.google.com; spf=pass smtp.mailfrom=bank.com; dkim=none"
    assert not sender_authenticated(h(spf_only), "a@bank.com")


def test_requires_the_trusted_authserv_id():
    assert not sender_authenticated(
        h(GMAIL_SHAPE.replace("mx.google.com", "mx.example.net", 1)), "a@accounts.example.com"
    )
    assert not sender_authenticated(h("dkim=pass header.d=bank.com"), "a@bank.com")
    assert not sender_authenticated(h("mx; dkim=pass header.d=bank.com"), "a@bank.com")
    assert sender_authenticated(h("mx.google.com 1; dkim=pass header.d=bank.com"), "a@bank.com")

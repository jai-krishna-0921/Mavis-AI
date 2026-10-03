from mavis.tools.integrations.normalize import sender_authenticated


def h(value: str) -> dict[str, str]:
    return {"authentication-results": value}


def test_dmarc_or_dkim_pass_for_from_domain():
    assert sender_authenticated(
        h("mx.google.com; dkim=pass header.i=@bank.com header.d=bank.com"), "a@bank.com"
    )
    assert sender_authenticated(
        h("mx; spf=fail; dmarc=pass (p=REJECT) header.from=bank.com"), "a@mail.bank.com"
    )


def test_rejects_other_domain_fail_or_missing():
    assert not sender_authenticated(h("mx; dkim=pass header.d=attacker.com"), "a@bank.com")
    assert not sender_authenticated(h("mx; dkim=pass header.d=evilbank.com"), "a@bank.com")
    assert not sender_authenticated(
        h("mx; dkim=fail header.d=bank.com; spf=pass smtp.mailfrom=bank.com"), "a@bank.com"
    )
    assert not sender_authenticated(h("mx; dkim=pass header.d=sub.bank.com"), "a@bank.com")
    assert not sender_authenticated({}, "a@bank.com")
    assert not sender_authenticated(h("mx; dmarc=pass header.from=bank.com"), "")

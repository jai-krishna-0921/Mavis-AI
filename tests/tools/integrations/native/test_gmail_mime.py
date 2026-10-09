"""Gmail payload to text, and message building. Payloads are shaped like users.messages.get format=full."""

from __future__ import annotations

import base64
import email
from email import policy

import pytest

from mavis.tools.integrations.native import gmail_mime as gm


def b64(text: str | bytes, charset: str = "utf-8") -> str:
    raw = text if isinstance(text, bytes) else text.encode(charset)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")  # Gmail omits padding


def part(mime: str, text: str | bytes = "", *, charset: str | None = "utf-8", **extra) -> dict:
    ctype = f'{mime}; charset="{charset}"' if charset and mime.startswith("text/") else mime
    out = {"mimeType": mime, "headers": [{"name": "Content-Type", "value": ctype}],
           "body": {"size": len(text), "data": b64(text, charset or "utf-8")} if text else {"size": 0}}
    out.update(extra)
    return out


def multi(mime: str, *children: dict) -> dict:
    return {"mimeType": mime, "headers": [{"name": "Content-Type", "value": f'{mime}; boundary="x"'}],
            "body": {"size": 0}, "parts": list(children)}


def attachment(name: str, mime: str, size: int) -> dict:
    return {"mimeType": mime, "filename": name, "headers": [
        {"name": "Content-Disposition", "value": f'attachment; filename="{name}"'}],
        "body": {"attachmentId": "ANGj" + name, "size": size}}


def test_alternative_prefers_plain():
    p = multi("multipart/alternative", part("text/plain", "Hello plain"), part("text/html", "<p>Hello <b>html</b></p>"))
    assert gm.body_text(p) == ("Hello plain", False)


def test_html_only_becomes_text_without_scripts_styles_or_head():
    html = ("<html><head><title>T</title><style>p{color:red}</style></head><body>"
            "<script>alert(1)</script><p>Hi&nbsp;Sam,</p><p>Lunch at <a href='http://x.example/t?u=1'>noon</a>?</p>"
            "<ul><li>one</li><li>two</li></ul><br>Thanks<noscript>enable js</noscript></body></html>")
    text, cut = gm.body_text(part("text/html", html))
    assert not cut
    assert "alert" not in text and "color" not in text and "enable js" not in text and "T\n" not in text
    assert "Hi Sam," in text and "Lunch at noon ?" in text.replace("noon?", "noon ?")
    assert "- one" in text and "- two" in text
    assert "http://" not in text


def test_nested_mixed_with_attachments_lists_names_and_sizes_without_fetching():
    root = multi(
        "multipart/mixed",
        multi("multipart/related", multi("multipart/alternative", part("text/plain", "See attached."),
                                         part("text/html", "<p>See attached.</p>")),
              attachment("logo.png", "image/png", 1200)),
        attachment("Q3 report.pdf", "application/pdf", 482_113),
        attachment("notes.txt", "text/plain", 90),
    )
    text, _ = gm.body_text(root)
    assert text == "See attached."  # the text/plain attachment is not body text
    assert gm.attachments(root) == [
        {"filename": "logo.png", "mimeType": "image/png", "size": 1200},
        {"filename": "Q3 report.pdf", "mimeType": "application/pdf", "size": 482_113},
        {"filename": "notes.txt", "mimeType": "text/plain", "size": 90},
    ]


@pytest.mark.parametrize(("charset", "text"), [
    ("iso-8859-1", "Café résumé, naïve señor"),
    ("windows-1252", "Price: 5€ “quoted”"),
    ("shift_jis", "会議は明日の十時です"),
    ("utf-16", "Grüße aus Köln"),
    ("koi8-r", "Привет, мир"),
])
def test_non_utf8_charsets_are_decoded_from_the_declared_charset(charset, text):
    assert gm.body_text(part("text/plain", text, charset=charset))[0] == text


def test_unknown_charset_never_raises_and_falls_back():
    p = part("text/plain", "naïve".encode("latin-1"), charset="x-made-up")
    assert gm.body_text(p)[0] == "naïve"  # not valid UTF-8, so cp1252


def test_missing_charset_prefers_utf8():
    p = part("text/plain", "Grüße", charset=None)
    assert gm.body_text(p)[0] == "Grüße"


def test_quoted_printable_label_does_not_double_decode():
    # Gmail has already decoded the transfer encoding; a literal "=3D" in the decoded text stays
    p = part("text/plain", "a=3Db is how QP writes a=b", charset="utf-8")
    p["headers"].append({"name": "Content-Transfer-Encoding", "value": "quoted-printable"})
    assert gm.body_text(p)[0] == "a=3Db is how QP writes a=b"


def test_body_is_capped_at_20kb():
    text, cut = gm.body_text(part("text/plain", "word " * 10_000))
    assert cut and len(text) <= gm.BODY_CAP
    short, cut2 = gm.body_text(part("text/plain", "word " * 10), cap=5)
    assert cut2 and len(short) <= 5


def test_empty_or_garbled_payloads_give_empty_text():
    assert gm.body_text({}) == ("", False)
    assert gm.body_text({"mimeType": "text/plain", "body": {"data": "!!!not base64!!!"}})[0] == ""
    assert gm.body_text(multi("multipart/mixed"))[0] == ""


def test_kept_headers_preserve_order_and_drop_noise():
    p = {"headers": [{"name": "Received", "value": "by x"}, {"name": "Authentication-Results", "value": "mx.google.com; dkim=pass"},
                     {"name": "Authentication-Results", "value": "evil; dkim=pass"}, {"name": "X-Foo", "value": "1"},
                     {"name": "List-Unsubscribe", "value": "<mailto:u@x>"}, {"name": "From", "value": "a@b.c"}]}
    kept = gm.kept_headers(p)
    assert [h["name"] for h in kept] == ["Authentication-Results", "Authentication-Results", "List-Unsubscribe", "From"]
    assert kept[0]["value"].startswith("mx.google.com")


def test_build_message_reply_headers_and_roundtrip():
    msg = gm.build_message(sender="me@kripya.com", to=["a@x.com", "b@y.com"], cc=["c@z.com"],
                           subject=gm.reply_subject("Budget ünïcode"), body="Sounds good.\nThanks",
                           in_reply_to="<orig@mail.x>", references="<first@mail.x> <orig0@mail.x>")
    parsed = email.message_from_bytes(base64.urlsafe_b64decode(gm.encode_raw(msg)), policy=policy.default)
    assert parsed["From"] == "me@kripya.com" and parsed["To"] == "a@x.com, b@y.com" and parsed["Cc"] == "c@z.com"
    assert parsed["Subject"] == "Re: Budget ünïcode"
    assert parsed["In-Reply-To"] == "<orig@mail.x>"
    assert parsed["References"] == "<first@mail.x> <orig0@mail.x> <orig@mail.x>"
    assert parsed["Message-ID"] and parsed["Date"]
    assert parsed.get_content().replace("\r\n", "\n").strip() == "Sounds good.\nThanks"


@pytest.mark.parametrize(("subject", "expected"), [
    ("Hello", "Re: Hello"), ("Re: Hello", "Re: Hello"), ("RE: Hello", "RE: Hello"), ("re : x", "re : x"), ("", "Re:"),
])
def test_reply_subject_never_stacks_prefixes(subject, expected):
    assert gm.reply_subject(subject) == expected


@pytest.mark.parametrize("field", ["subject", "to"])
def test_header_injection_is_rejected(field):
    kwargs = {"sender": "me@x.com", "to": ["a@x.com"], "subject": "hi", "body": "b"}
    kwargs[field] = ["a@x.com\nBcc: evil@x.com"] if field == "to" else "hi\nBcc: evil@x.com"
    with pytest.raises(ValueError):
        gm.build_message(**kwargs)

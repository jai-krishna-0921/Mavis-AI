"""A message with an HTML body never borrows the provider's preview: Graph `bodyPreview` and Gmail `snippet`
are built from the body text including hidden preheaders, so when the HTML renders to nothing (fully hidden)
or to the fail-closed notice they must not stand in for it, and they are not passed along a parsed body."""

import httpx

from mavis.domain.integrations import UserRef
from mavis.tools.integrations.native import graph_html

from .gfake import FakeGoogle, gmail_message
from .graphfake import FakeGraph, graph_message
from .test_gmail_mime import multi, part
from .test_microsoft_mail import with_folders
from .test_microsoft_transport import U

GET = r"/gmail/v1/users/me/messages/[^/]+$"
HIDDEN = '<div style="display:none">SECRET preheader</div>'


async def _outlook(msg: dict) -> dict:
    g = with_folders(FakeGraph()).on("GET", r"/v1.0/me/messages/m1$", httpx.Response(200, json=msg))
    ex, _ = g.executor()
    r = await ex.execute(U, "mail.read", {"message_id": "m1"})
    assert r.ok
    return r.data


async def test_outlook_hidden_only_html_does_not_fall_back_to_body_preview():
    msg = graph_message("m1", html=HIDDEN)
    msg["bodyPreview"] = "SECRET preheader"
    data = await _outlook(msg)
    assert data["messageText"] == "" and "SECRET" not in str(data)


async def test_outlook_notice_html_does_not_fall_back_to_body_preview(monkeypatch):
    monkeypatch.setattr(graph_html, "PARSE_BUDGET_S", 0.0)  # every parse in the scope fails closed
    msg = graph_message("m1", html="<p>Hi</p>")
    msg["bodyPreview"] = "SECRET preheader"
    data = await _outlook(msg)
    assert data["messageText"] == graph_html.NOTICE and "SECRET" not in str(data)


async def test_outlook_parsed_html_body_is_not_sent_with_the_preview():
    msg = graph_message("m1", html="<p>Visible body</p>")
    msg["bodyPreview"] = "SECRET preheader Visible body"
    data = await _outlook(msg)
    assert data["messageText"] == "Visible body" and data["snippet"] == ""


async def test_outlook_plain_text_keeps_its_preview():
    data = await _outlook(graph_message("m1", text="Just text"))
    assert data["messageText"] == "Just text" and data["snippet"] == "Just text"


async def _gmail(payload: dict, snippet: str) -> dict:
    msg = gmail_message("m9", payload=payload)
    msg["snippet"] = snippet
    fake = FakeGoogle().on("GET", GET, httpx.Response(200, json=msg))
    ex, _ = fake.executor()
    res = await ex.execute(UserRef(user_id=7), "mail.read", {"message_id": "m9"})
    assert res.ok
    return res.data


async def test_gmail_hidden_only_html_does_not_fall_back_to_the_snippet():
    # Gmail reads by tag only, so a script stands in for the hidden preheader
    data = await _gmail(part("text/html", "<script>SECRET preheader</script>"), "SECRET preheader")
    assert data["messageText"] == "" and "SECRET" not in str(data["snippet"])


async def test_gmail_html_alternative_drops_the_snippet():
    root = multi("multipart/alternative", part("text/plain", "Plain body"),
                 part("text/html", "<p>Plain body</p>"))
    data = await _gmail(root, "SECRET preheader Plain body")
    assert data["messageText"] == "Plain body" and data["snippet"] == ""


async def test_gmail_plain_only_keeps_the_snippet():
    data = await _gmail(part("text/plain", ""), "Snip")
    assert data["messageText"] == "Snip" and data["snippet"] == "Snip"

"""A4: one output pass, context aware, applied once at the send boundary.

Golden corpus of varied synthetic messages, properties (idempotence, verbatim spans untouched, every
Unicode space separator treated as a space) and the boundary rules (history, previews, bubbles)."""

from __future__ import annotations

import sys
import unicodedata

import pytest

from mavis.channels.formatting import (
    SPACE_SEPARATORS,
    normalize_dashes,
    strip_verbatim,
    to_plain,
    to_telegram_html,
    verbatim,
)
from mavis.channels.text import split_text

NNBSP, NBSP, THIN = " ", " ", " "

GOLDEN = [
    # labels at a line or bullet start become "Label: text"
    ("**Operator change** — Meetup will host from now on",
     "**Operator change**: Meetup will host from now on"),
    ("Deadline - Friday noon", "Deadline: Friday noon"),
    ("Heads up – the venue moved", "Heads up: the venue moved"),
    ("- Flights — booked for Tuesday", "- Flights: booked for Tuesday"),
    ("  * Hotel – still pending", "  * Hotel: still pending"),
    ("1. Visa — submitted", "1. Visa: submitted"),
    ("Next step—call the bank", "Next step: call the bank"),
    ("I checked — nothing new came in.", "I checked: nothing new came in."),
    # ranges of numbers, times, days and months become "X to Y"
    ("Free 15:00–16:00 today", "Free 15:00 to 16:00 today"),
    (f"Free 15:00{NNBSP}–{NNBSP}16:00 today", "Free 15:00 to 16:00 today"),
    (f"Standup 3{NNBSP}PM – 4{NNBSP}PM", "Standup 3 PM to 4 PM"),
    ("Open Mon–Fri", "Open Mon to Fri"),
    ("Open Mon-Fri", "Open Mon to Fri"),
    ("Office hours 9am-5pm", "Office hours 9am to 5pm"),
    ("Takes 2-3 days", "Takes 2 to 3 days"),
    ("Pages 3–5", "Pages 3 to 5"),
    ("From 2020–2024", "From 2020 to 2024"),
    ("Trip Oct 3–5", "Trip Oct 3 to 5"),
    ("Trip 3–5 Oct", "Trip 3 to 5 Oct"),
    ("Monday – Wednesday works", "Monday to Wednesday works"),
    ("10:30 - 11:00 is open", "10:30 to 11:00 is open"),
    # a range at a line start is a range, not a label
    ("3 PM – 4 PM works for me", "3 PM to 4 PM works for me"),
    # a dash between clauses becomes a comma
    # (a label starts with a capital and is short; anything else is a clause)
    ("Looked through everything I could find — nothing new came in.",
     "Looked through everything I could find, nothing new came in."),
    ("wait—what happened", "wait, what happened"),
    ("fine -- see you then", "fine, see you then"),
    ("we went - it was great", "we went, it was great"),
    ("- flights — booked", "- flights, booked"),
    ("wait —, what", "wait, what"),
    ("end. — next", "end. next"),
    ("so we are done —", "so we are done"),
    ("— a note on top", "- a note on top"),
    # hyphens that are not dashes stay as they are
    ("Due 2026-10-03 at noon", "Due 2026-10-03 at noon"),
    ("call 555-0132", "call 555-0132"),
    ("call 555‒0132", "call 555-0132"),
    ("a well-known e-mail about COVID-19", "a well-known e-mail about COVID-19"),
    ("run it with --verbose", "run it with --verbose"),
    ("a--b", "a--b"),
    ("it was −5 degrees", "it was -5 degrees"),
    ("see https://my-site.example/2-3/a–b now", "see https://my-site.example/2-3/a–b now"),
    ("use `a—b` here", "use `a—b` here"),
    # every space separator counts as a space
    (f"Lunch{NBSP}—{NBSP}at noon", "Lunch: at noon"),
    (f"ok{THIN}—{THIN}later", "ok, later"),
]


@pytest.mark.parametrize("raw,expected", GOLDEN)
def test_golden(raw, expected):
    assert normalize_dashes(raw) == expected


@pytest.mark.parametrize("raw", [g[0] for g in GOLDEN])
def test_idempotent(raw):
    once = normalize_dashes(raw)
    assert normalize_dashes(once) == once


@pytest.mark.parametrize("raw", [g[0] for g in GOLDEN])
def test_no_long_dash_survives_outside_code_and_links(raw):
    import re

    out = re.sub(r"https?://\S+|`[^`]*`|a—b", "", to_plain(raw))  # code and links keep their text
    assert "—" not in out and "–" not in out


def test_space_separators_are_all_unicode_zs():
    zs = {chr(c) for c in range(sys.maxunicode + 1) if unicodedata.category(chr(c)) == "Zs"} - {" "}
    assert set(SPACE_SEPARATORS) == zs


def test_multiline_message_keeps_structure():
    raw = ("Here's the plan:\n\n**Flights** — booked\n**Hotel** – pending\n\n- Mon–Fri: work\n"
           "- Sat: 10:00–12:00 brunch\n\nAll good — ping me if anything changes.")
    assert normalize_dashes(raw) == (
        "Here's the plan:\n\n**Flights**: booked\n**Hotel**: pending\n\n- Mon to Fri: work\n"
        "- Sat: 10:00 to 12:00 brunch\n\nAll good: ping me if anything changes.")


# --- verbatim spans (quoted email text, approval previews) are never rewritten --------------------------


PREVIEWS = [
    "Send email to raj@x.io\nSubject: Interview Confirmation – 3 PM IST\n\nHi Raj — see you then.",
    "Create event: Q3 – final review\nWhen: Mon–Fri, 15:00–16:00",
    "Post to #team: standup moved -- 11:00 - 11:30",
    f"Invite: Demo{NNBSP}—{NNBSP}v2 (3{NNBSP}PM)",
]


@pytest.mark.parametrize("preview", PREVIEWS)
def test_verbatim_span_reaches_the_user_unchanged(preview):
    msg = f"Ready when you are. All set — want me to go ahead?\n\n{verbatim(preview)}"
    plain = to_plain(msg)
    assert preview in plain
    assert plain.startswith("Ready when you are. All set, want me to go ahead?")
    import html

    assert html.escape(preview, quote=False) in to_telegram_html(msg)


@pytest.mark.parametrize("preview", PREVIEWS)
def test_verbatim_survives_chunking(preview):
    """A preview cut across Telegram chunks stays verbatim in each chunk."""
    long = "Intro — line\n" * 30 + verbatim(preview + "\n" + "word – " * 300)
    chunks = split_text(long, 300)
    assert len(chunks) > 2
    for chunk in chunks:
        assert chunk.count("\x0e") == chunk.count("\x0f") <= 1  # balanced in every chunk
    rendered = "\n".join(to_plain(c) for c in chunks)
    assert preview.splitlines()[-1] in rendered and "word – word" in rendered
    assert "Intro: line" in rendered


def test_verbatim_markup_is_not_parsed():
    assert to_plain(verbatim("**not bold** _x_ - item")) == "**not bold** _x_ - item"


def test_strip_verbatim_removes_only_markers():
    s = f"a {verbatim('b – c')} d"
    assert strip_verbatim(s) == "a b – c d"


# --- applied once: never to history, never in the bubble splitter -------------------------------------


async def test_history_is_stored_raw(user):
    from mavis.domain.messages import Role
    from mavis.store.repo import messages

    await messages.log(user.id, Role.ASSISTANT, "Good luck — you've got this, 3–4 PM")
    await messages.log(user.id, Role.ASSISTANT, f"Card:\n{verbatim('Subject: A – B')}")
    rows = await messages.recent(user.id)
    assert [r.content for r in rows] == ["Good luck — you've got this, 3–4 PM", "Card:\nSubject: A – B"]


@pytest.mark.parametrize("reply,bubbles", [
    ("Here's what I found:\n\n- one\n- two\n\nWant more?",
     ["Here's what I found:\n\n- one\n- two\n\nWant more?"]),
    ("Hey!\n---\nHow did it go?", ["Hey!", "How did it go?"]),
    ("a\n\n---\n\nb\n---\nc\n---\nd", ["a", "b", "c\n\nd"]),
    ("```\ncode\n---\nmore\n```", ["```\ncode\n---\nmore\n```"]),
    ("I checked — nothing new.", ["I checked — nothing new."]),  # left for the send boundary
    ("   ", []),
])
def test_bubbles_split_only_on_the_explicit_marker(reply, bubbles):
    from mavis.agents.persona import split_bubbles

    assert split_bubbles(reply) == bubbles


def test_persona_explains_the_marker_and_the_formatter_owns_dashes():
    from mavis.agents.persona import PERSONA

    assert "---" in PERSONA
    assert "Separate bubbles with a blank line" not in PERSONA
    assert "use a comma" not in PERSONA


# --- the approval card equals the payload ---------------------------------------------------------------


@pytest.mark.parametrize("subject", ["Interview Confirmation – 3 PM IST", "Q3 — final", "Mon–Fri rota",
                                     f"Demo{NNBSP}–{NNBSP}v2", "plain subject"])
async def test_approval_card_text_equals_the_action(user, rec_bus, sent, subject):
    """What the user sees on the card is exactly what the tool will send."""
    from datetime import timedelta

    from mavis.policy import approvals as flow
    from mavis.store.db import utcnow
    from mavis.store.repo import approvals
    from mavis.tools.integrations.actions import MailComposeArgs, _preview_mail

    args = MailComposeArgs(to=["raj@x.io"], subject=subject, body="Hi Raj — see you 3–4 PM.")
    preview = _preview_mail(args, "Asia/Kolkata")
    aid = await approvals.create(user.id, None, "mail_send", args.model_dump(mode="json"), preview,
                                 utcnow() + timedelta(hours=48))
    await flow.send_approval_prompt(user.id, {"approval_id": aid})
    shown = to_plain(sent[-1].text)
    assert subject in shown and "Hi Raj — see you 3–4 PM." in shown


async def test_composer_bubbles_are_not_rewritten_before_the_boundary(user, fake_llm, fake_memory):
    from mavis.domain.decisions import ComposedMessage
    from mavis.initiative.composer import Composer

    fake_llm.push_structured(ComposedMessage(send=True, messages=["Prep — 3–4 PM today"]))
    msg = await Composer(fake_memory).compose(user, "prep", 3)
    assert msg.messages == ["Prep — 3–4 PM today"]
    assert to_plain(msg.messages[0]) == "Prep: 3 to 4 PM today"

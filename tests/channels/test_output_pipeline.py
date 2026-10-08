"""A4: one output pass, context aware, applied once at the send boundary.

Golden corpus of varied synthetic messages, properties (idempotence, verbatim spans untouched, every
Unicode space separator treated as a space) and the boundary rules (history, previews, bubbles)."""

from __future__ import annotations

import sys
import unicodedata

import pytest

from mavis.channels.formatting import (
    DASHES,
    normalize_dashes,
    strip_verbatim,
    to_plain,
    to_telegram_html,
    verbatim,
)
from mavis.channels.text import split_text

NNBSP, NBSP, THIN = "\u202f", "\u00a0", "\u2009"

# Fix round 1 (I5): one simple rule. ASCII hyphen-minus is never touched; every other Unicode dash
# (category Pd) is normalised: between two digits with no or thin spacing it becomes "-", directly
# after a **bold** label at a line or bullet start ":", spaced elsewhere ", ", unspaced elsewhere "-".
UNCHANGED = [
    "Final score 3-2 tonight",
    "5 - 3 = 2",
    "Open 24-7",
    "Due Jan-2026",
    "Free Sat - may be late",
    "Budget $10 - $20",
    "Deadline - Friday noon",
    "a well-known e-mail about COVID-19",
    "Due 2026-10-03, call 555-0132",
    "run it with --verbose, a--b",
    "it was \u22125 degrees",                       # minus sign is not a dash (category Sm)
    'Subject: "Q3 \u2013 final review"',            # quoted text
    "They wrote \u2018Mon\u2013Fri only\u2019 in the note",
    "The \u201cBudget \u2014 draft\u201d sheet",
    "use `a\u2014b` here",
    "see https://my-site.example/2-3/a\u2013b now",
    "- a bullet\n  - nested bullet",
    "",
]

GOLDEN = [
    ("**Operator change** \u2014 Meetup will host", "**Operator change**: Meetup will host"),
    ("- **Flights** \u2013 booked for Tuesday", "- **Flights**: booked for Tuesday"),
    ("1. __Visa__\u2014submitted", "1. __Visa__: submitted"),
    ("Free 15:00\u201316:00 today", "Free 15:00-16:00 today"),
    # a range's thin spacing goes with its dash (hotfix4 H6: no narrow or thin spaces reach the user)
    (f"Free 15:00{THIN}\u2013{THIN}16:00 today", "Free 15:00-16:00 today"),
    (f"Pages 3{NNBSP}\u2013{NNBSP}5", "Pages 3-5"),
    ("From 2020\u20132024", "From 2020-2024"),
    ("Open Mon\u2013Fri", "Open Mon-Fri"),
    ("I checked \u2014 nothing new came in.", "I checked, nothing new came in."),
    ("Heads up \u2013 the venue moved", "Heads up, the venue moved"),
    ("Standup 3 PM \u2013 4 PM", "Standup 3 PM, 4 PM"),
    (f"Lunch{NBSP}\u2014{NBSP}at noon", "Lunch, at noon"),
    ("wait \u2014, what", "wait, what"),
    ("end. \u2014 next", "end. next"),
    ("so we are done \u2014", "so we are done"),
    ("\u2014 a note on top", "- a note on top"),
    ("wait\u2014what", "wait-what"),
    ("call 555\u20120132", "call 555-0132"),
    ("x \u2015 y \u2e3a z", "x, y, z"),
    ("e.g., \u2014 fine", "e.g., fine"),
    ("\u300c\u5b9a\u4f8b\u300d 10\u301c12", "\u300c\u5b9a\u4f8b\u300d 10-12"),
]


@pytest.mark.parametrize("raw", UNCHANGED)
def test_text_without_unicode_dashes_or_inside_quotes_is_unchanged(raw):
    assert normalize_dashes(raw) == raw


@pytest.mark.parametrize("raw,expected", GOLDEN)
def test_golden(raw, expected):
    assert normalize_dashes(raw) == expected


@pytest.mark.parametrize("raw", [g[0] for g in GOLDEN] + UNCHANGED)
def test_idempotent(raw):
    once = normalize_dashes(raw)
    assert normalize_dashes(once) == once


@pytest.mark.parametrize("raw", [g[0] for g in GOLDEN])
def test_no_unicode_dash_survives_outside_held_spans(raw):
    assert not any(d in normalize_dashes(raw) for d in DASHES)


def test_dash_set_is_all_unicode_pd_except_hyphen_minus():
    pd = {chr(c) for c in range(sys.maxunicode + 1) if unicodedata.category(chr(c)) == "Pd"} - {"-"}
    assert set(DASHES) == pd


def test_multiline_message_keeps_structure():
    raw = ("Here's the plan:\n\n**Flights** \u2014 booked\n**Hotel** \u2013 pending\n\n- Mon\u2013Fri: work\n"
           "- Sat: 10:00\u201312:00 brunch\n\nAll good \u2014 ping me if anything changes.")
    assert normalize_dashes(raw) == (
        "Here's the plan:\n\n**Flights**: booked\n**Hotel**: pending\n\n- Mon-Fri: work\n"
        "- Sat: 10:00-12:00 brunch\n\nAll good, ping me if anything changes.")


# --- verbatim spans (quoted email text, approval previews) are never rewritten --------------------------


PREVIEWS = [
    "Send email to raj@x.io\nSubject: Interview Confirmation – 3 PM IST\n\nHi Raj — see you then.",
    "Create event: Q3 – final review\nWhen: Mon–Fri, 15:00–16:00",
    "Post to #team: standup moved -- 11:00 - 11:30",
    f"Invite: Demo{NNBSP}—{NNBSP}v2 (3{NNBSP}PM)",
]


@pytest.mark.parametrize("preview", PREVIEWS)
def test_verbatim_span_reaches_the_user_unchanged(preview):
    msg = f"Ready when you are \u2014 want me to go ahead?\n\n{verbatim(preview)}"
    plain = to_plain(msg)
    assert preview in plain
    assert plain.startswith("Ready when you are, want me to go ahead?")
    import html

    assert html.escape(preview, quote=False) in to_telegram_html(msg)


@pytest.mark.parametrize("preview", PREVIEWS)
def test_verbatim_survives_chunking(preview):
    """A preview cut across Telegram chunks stays verbatim in each chunk."""
    long = "Intro \u2014 line\n" * 30 + verbatim(preview + "\n" + "word \u2013 " * 300)
    chunks = split_text(long, 300)
    assert len(chunks) > 2
    for chunk in chunks:
        assert chunk.count("\x0e") == chunk.count("\x0f") <= 1  # balanced in every chunk
    rendered = "\n".join(to_plain(c) for c in chunks)
    assert preview.splitlines()[-1] in rendered and "word – word" in rendered
    assert "Intro, line" in rendered


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
    assert "Never use dashes as punctuation" in PERSONA and "colon after a label" in PERSONA


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
    assert to_plain(msg.messages[0]) == "Prep, 3-4 PM today"


# --- fix round 1, M11: chunking ignores fences inside verbatim spans ------------------------------------


@pytest.mark.parametrize("inner", ["```", "```python", "  ```"])
def test_split_text_ignores_fence_lines_inside_verbatim(inner):
    preview = f"Note body:\n{inner}\nnot code, just text in a preview\n" + "line of preview text\n" * 60
    text = "Ready when you are.\n" + verbatim(preview) + "\nafter"
    chunks = split_text(text, 400)
    assert len(chunks) > 1
    joined = "".join(strip_verbatim(c) for c in chunks)
    assert "```\n```" not in joined and joined.count("```") == text.count("```")  # nothing added
    for c in chunks:
        assert c.count("\x0e") == c.count("\x0f")


def test_split_text_still_balances_real_fences_outside_verbatim():
    text = verbatim("```inside```") + "\n```\n" + "code line\n" * 80 + "```"
    chunks = split_text(text, 300)
    assert all(c.count("```") % 2 == 0 for c in chunks if "\x0e" not in c)

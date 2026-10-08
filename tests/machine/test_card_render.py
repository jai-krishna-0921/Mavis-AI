"""The card is code-made chrome: header from the goal, step titles, a code-made last line, no dashes."""

from __future__ import annotations

import pytest

from mavis.domain.plans import PlanStep
from mavis.domain.progress import (
    CardFinal,
    StepState,
    cancel_button,
    card_from_plan,
    fmt_elapsed,
    render_card,
)

STEPS = [
    PlanStep(id="s1", agent="research", instruction="search", title="Search for kettles"),
    PlanStep(id="s2", agent="research", instruction="read", title="Read 3 product pages", depends_on=["s1"]),
    PlanStep(id="s3", agent="research", instruction="table", title="Build comparison table",
             depends_on=["s2"]),
]


def test_render_shows_header_steps_last_and_footer():
    card = card_from_plan(41, "compare electric kettles under 3k", STEPS, tainted=False, now=100.0)
    card.steps[0].state = StepState.DONE
    card.steps[1].state, card.steps[1].started_at = StepState.RUNNING, 128.0
    card.last, card.files_sent = "opened shop.example", 2
    text, buttons = render_card(card, now=260.0)
    lines = text.splitlines()
    assert lines[0] == "Working on: compare electric kettles under 3k"
    assert lines[1].startswith("✅ 1. Search for kettles")
    assert lines[2].startswith("⏳ 2. Read 3 product pages") and "2:12" in lines[2]
    assert lines[3].startswith("▫️ 3. Build comparison table")
    assert "Last: opened shop.example" in text
    assert "⏱ 2:40" in lines[-1] and "2 files sent" in lines[-1]
    assert buttons == [[cancel_button(41)]]


@pytest.mark.parametrize("final,word", [(CardFinal.DONE, "Done"), (CardFinal.PARTIAL, "Partly done"),
                                        (CardFinal.FAILED, "Couldn't finish"),
                                        (CardFinal.CANCELLED, "Cancelled")])
def test_final_card_has_outcome_and_no_buttons(final, word):
    card = card_from_plan(7, "make a deck", STEPS[:1], tainted=False, now=0.0)
    card.final, card.finished_at = final, 95.0
    text, buttons = render_card(card, now=500.0)
    assert text.splitlines()[0] == f"{word}: make a deck"
    assert "1:35" in text and buttons == []


@pytest.mark.parametrize("goal", ["x" * 200, "a very long goal " * 9, "plan my sister's 40th birthday " * 4])
def test_header_is_at_most_60_chars(goal):
    text, _ = render_card(card_from_plan(1, goal, STEPS, tainted=False, now=0), now=1)
    assert len(text.splitlines()[0]) <= len("Working on: ") + 60


def test_tainted_titles_are_scrubbed():
    steps = [PlanStep(id="s1", agent="research", instruction="i", title="Open https://evil.example/?q=me")]
    text, _ = render_card(card_from_plan(2, "summarise a page", steps, tainted=True, now=0), now=1)
    assert "evil.example" not in text and "https://" not in text


def test_missing_title_falls_back_to_the_agent():
    steps = [PlanStep(id="s1", agent="analyst", instruction="crunch")]
    text, _ = render_card(card_from_plan(3, "g", steps, tainted=False, now=0), now=1)
    assert "1. Analyst step" in text


@pytest.mark.parametrize("seconds,out", [(4, "0:04"), (160, "2:40"), (3723, "1:02:03"), (0.4, "0:00")])
def test_fmt_elapsed(seconds, out):
    assert fmt_elapsed(seconds) == out


def test_no_em_or_en_dashes_anywhere():
    card = card_from_plan(9, "check prices", STEPS, tainted=False, now=0)
    for state in StepState:
        card.steps[0].state = state
        for final in (None, *CardFinal):
            card.final = final
            text, _ = render_card(card, now=10)
            assert "\u2014" not in text and "\u2013" not in text


def test_plan_step_title_is_truncated():
    assert len(PlanStep(id="s", agent="a", instruction="i", title="t" * 99).title) == 60


def test_cancel_button_fits_telegram_limit():
    assert len(cancel_button(10**12).data.encode()) <= 64

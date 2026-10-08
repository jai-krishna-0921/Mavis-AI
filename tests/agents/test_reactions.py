"""T1.3: a mood reaction chosen by the chat model, validated and rate-limited by code."""

import pytest
from telegram.constants import ReactionEmoji

from mavis.agents import reactions
from mavis.agents.reactions import ALLOWED, allowed, normalize, split_reaction

FIRE, EYES, HEART = "\U0001f525", "\N{EYES}", "❤"


def test_whitelist_is_inside_telegrams_allowed_set():
    telegram = {e.value for e in ReactionEmoji}
    assert ALLOWED <= telegram
    assert 12 <= len(ALLOWED) <= 30


@pytest.mark.parametrize("bad", [
    "\U0001f595",  # middle finger: allowed by Telegram, never by Mavis
    "\U0001f4a9", "\U0001f921", "\U0001f48b", "\U0001f48a",  # poo, clown, kiss, pill
    "\U0001f600",  # grinning face: not a Telegram reaction at all
    "fire", "", "  ", "\U0001f525\U0001f525",
])
def test_invalid_reactions_are_dropped(bad):
    assert normalize(bad) is None


@pytest.mark.parametrize("raw,canonical", [
    ("❤️", "❤"),  # red heart with the emoji variation selector
    ("❤", "❤"),
    (" \U0001f525 ", FIRE),
    ("❤‍\U0001f525", "❤️‍\U0001f525"),  # heart on fire, selector missing
    ("✍️", "✍"),
])
def test_variants_normalise_to_telegrams_form(raw, canonical):
    assert normalize(raw) == canonical


@pytest.mark.parametrize("text,clean,emoji", [
    ("Hell yes, that's huge!\n[react: \U0001f525]", "Hell yes, that's huge!", FIRE),
    ("Nice one.\n\n[reaction:\U0001f3c6]\n", "Nice one.", "\U0001f3c6"),
    ("Sleep well.\n[React: \U0001f634]", "Sleep well.", "\U0001f634"),
    ("Got it. [react: \U0001f44c]", "Got it.", "\U0001f44c"),  # inline at the end of the last line
    ("Ok.\n[react: none]", "Ok.", None),
    ("Ok.\n[react: \U0001f595]", "Ok.", None),  # invalid: dropped, still stripped
    ("Plain reply, nothing else.", "Plain reply, nothing else.", None),
    ("First\n---\nSecond\n[react: ❤️]", "First\n---\nSecond", "❤"),
])
def test_split_reaction(text, clean, emoji):
    assert split_reaction(text) == (clean, emoji)


@pytest.mark.parametrize("text", [
    "Nice!\n[react \U0001f525]",
    "Nice!\n[reaction: \U0001f525]",
    "Nice!\n(react: \U0001f525)",
    "Nice!\n[Reaction = \U0001f525]",
    "Nice!\nreact: \U0001f525",
    "Nice!\nReaction: \U0001f525",
    "Nice! [react-\U0001f525]",
    "Nice!\n[reacting: \U0001f525 ]",
])
def test_near_miss_markers_are_stripped_and_read(text):
    assert split_reaction(text) == ("Nice!", FIRE)


@pytest.mark.parametrize("text", [
    "How did they react: badly?",  # prose, not a marker line
    "I'll react to that later.",
    "Chemical reaction: exothermic",
])
def test_prose_about_reacting_is_left_alone(text):
    assert split_reaction(text) == (text, None)


def test_several_markers_last_valid_wins_and_all_are_stripped():
    text = "[react: \U0001f914] Hmm.\nThat works.\n[react: \U0001f44f]"
    assert split_reaction(text) == ("Hmm.\nThat works.", "\U0001f44f")


def test_marker_inside_code_block_is_content():
    text = "Use this:\n```\n[react: \U0001f525]\n```"
    assert split_reaction(text) == (text, None)


@pytest.mark.parametrize("recent,candidate,ok", [
    ([], FIRE, True),
    ([""], FIRE, True),
    ([FIRE], "\U0001f44f", False),  # never on consecutive messages
    (["", FIRE], "\U0001f44f", False),  # at most one in any three
    ([FIRE, "", ""], "\U0001f44f", True),
    ([FIRE, "", "", FIRE, "", ""], FIRE, False),  # not the same emoji three times running
    ([FIRE, "", "", "\U0001f44f", "", ""], FIRE, True),
    (["", ""], None, False),
])
def test_frequency_rule(recent, candidate, ok):
    assert allowed(candidate, recent) is ok


async def test_log_keeps_recent_outcomes_per_event(settings):
    log = reactions.ReactionLog()
    outcomes = ["", FIRE, "", "", "\U0001f44f", "", ""]
    for i, outcome in enumerate(outcomes):
        await log.record(1, f"e{i}", outcome)
    assert await log.recent(1) == outcomes
    assert await log.landed(1) == {"e1": FIRE, "e4": "\U0001f44f"}
    assert await log.recent(2) == [] and await log.landed(2) == {}


async def test_log_records_one_outcome_per_event(settings):
    log = reactions.ReactionLog()
    await log.record(1, "e1", FIRE)
    await log.record(1, "e1", "")  # an inline retry of the same message
    assert await log.recent(1) == [FIRE] and await log.outcome(1, "e1") == FIRE
    assert await log.outcome(1, "nope") is None


async def test_log_is_bounded(settings):
    log = reactions.ReactionLog()
    for i in range(reactions.HISTORY + 5):
        await log.record(1, f"e{i}", "")
    assert len(await log.recent(1)) == reactions.HISTORY


def test_prompt_rule_lists_only_allowed_emoji_and_the_marker():
    rule = reactions.REACTION_RULE
    assert "[react:" in rule
    for e in ALLOWED:
        assert e in rule
    assert "\U0001f595" not in rule
    low = rule.lower()
    assert "most messages get none" in low and "upset" in low

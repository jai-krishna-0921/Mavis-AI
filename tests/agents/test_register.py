"""T1.2: the user's register is measured from their own recent messages and steers the prompt."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from mavis.agents import register
from mavis.agents.register import measure, prompt_line, user_texts

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)


def _m(role, content, minutes_ago=0):
    return SimpleNamespace(role=role, content=content, created_at=NOW - timedelta(minutes=minutes_ago))


@pytest.mark.parametrize("text", [
    "fuck it, let's just do it",
    "this shit is wild",
    "ugh my boss is being a total dick today",
    "wtf happened to my calendar",
    "bc this traffic yaar",
    "that's fucking brilliant",
    "f*ck, forgot the meeting",
    "Bloody hell, the train's late again",
])
def test_profanity_is_detected_in_varied_forms(text):
    assert register.has_profanity(text)


@pytest.mark.parametrize("text", [
    "Could you please send the report to Asha?",
    "remind me to call mom at 6",
    "the shitake mushrooms were great",  # substring of a swear is not a swear
    "I passed the class, assessment done",
    "Scunthorpe United won",
    "hello there",
    "two classes, my glasses and the bus passes",
    "Dickens and Cockburn wrote that",
])
def test_clean_text_is_not_profanity(text):
    assert not register.has_profanity(text)


def test_sweary_casual_user_permits_swearing():
    reg = measure(["lol yeah", "fuck it, book the cheap one", "this week has been shit"])
    assert reg.swears and not reg.formal
    line = prompt_line(reg)
    assert "may swear" in line.lower()
    assert "never" in line.lower()  # the limits travel with the permission


def test_clean_casual_user_gets_no_swearing():
    reg = measure(["hey can u check my mail", "lol ok", "cool thx"])
    assert not reg.swears and reg.casual
    line = prompt_line(reg)
    assert "don't swear" in line.lower() or "do not swear" in line.lower()


@pytest.mark.parametrize("latest", [
    "Good morning. Could you please summarise the contract and send it to Mr. Rao by Friday?",
    "Kindly schedule a meeting with the finance team for Thursday afternoon.",
    "Dear Mavis, I would appreciate a short summary of yesterday's emails. Thank you.",
])
def test_formal_latest_message_overrides_earlier_swearing(latest):
    reg = measure(["fuck yeah", "shit, forgot", latest])
    assert reg.formal and not reg.swears
    line = prompt_line(reg)
    assert "formal" in line.lower() and "no swearing" in line.lower()


def test_old_swearing_ages_out_of_the_recent_window():
    texts = ["fuck this"] + ["ok sounds good", "what's on today", "thanks", "and tomorrow?"]
    assert not measure(texts).swears


def test_proportion_is_measured():
    heavy = measure(["fuck", "shit man", "fucking finally", "damn right"])
    light = measure(["what's on today", "cool", "ugh shit, missed it"])
    assert heavy.swear_share > light.swear_share > 0
    assert heavy.swears and light.swears


def test_no_messages_means_no_register_line():
    reg = measure([])
    assert reg.sample == 0
    assert prompt_line(reg) == ""


def test_insult_at_mavis_still_gets_same_register_humour_rule():
    reg = measure(["you're fucking useless, you forgot my reminder"])
    line = prompt_line(reg)
    assert reg.swears and "may swear" in line.lower()


def test_proactive_line_needs_recent_sweary_register():
    sweary = measure(["fuck yes, nailed the interview"])
    clean = measure(["thanks, see you tomorrow"])
    assert "lightly" in prompt_line(sweary, proactive=True).lower()
    assert "don't swear" in prompt_line(clean, proactive=True).lower()
    assert "don't swear" in prompt_line(measure([]), proactive=True).lower()


def test_user_texts_takes_only_recent_user_messages_in_order():
    history = [
        _m("user", "fuck this", 60 * 30),  # 30 hours ago: outside the window
        _m("user", "first", 30),
        _m("assistant", "reply", 29),
        _m("user", "second", 5),
    ]
    assert user_texts(history, NOW) == ["first", "second"]
    assert user_texts(history, NOW, window=None) == ["fuck this", "first", "second"]


def test_hype_energy_is_noticed():
    assert measure(["LETS GOOOO we got the offer!!!"]).hype
    assert not measure(["ok, see you at 5."]).hype


@pytest.mark.parametrize("text,masked", [
    ("you absolute retard", "you absolute r****d"),
    ("no faggots here", "no f*****s here"),
])
def test_slurs_are_masked_in_outgoing_text(text, masked):
    assert register.mask_slurs(text) == masked


def test_mask_slurs_leaves_ordinary_swearing_alone():
    text = "fuck it, that's shit luck. Let's go."
    assert register.mask_slurs(text) == text

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
    "ugh my boss is being a total dick today, what a prick",  # two mild words together
    "wtf happened to my calendar",
    "bc this traffic is shit yaar",
    "that's fucking brilliant",
    "f*ck, forgot the meeting",
    "sh!t the train left",
    "Bloody hell, the train's late again",
    "FUCK. Missed it.",  # shouting is not a proper noun
    "Fucking finally",  # capitalised at the start of a sentence
    "ok. Shit, the oven",
    "fuckin' hell mate",
    "what a dumbass move by me",
])
def test_profanity_is_detected_in_varied_forms(text):
    assert register.has_profanity(text)


@pytest.mark.parametrize("text", [
    "Could you please send the report to Asha?",
    "remind me to call mom at 6",
    "the shitake mushrooms were great",  # part of a word is not a swear
    "I passed the class, assessment done",
    "Scunthorpe United won",
    "hello there",
    "two classes, my glasses and the bus passes",
    "Dickens and Cockburn wrote that",
    # ambiguous abbreviations and mild words never count alone
    "bc I was busy",
    "can't come bc of work",
    "the MC was great last night",
    "hell yeah",
    "damn right",
    "the bloody mary was strong",
    # proper nouns: names and places
    "meeting with Dick tomorrow at 10",
    "reading Moby Dick on the train",
    "booking the Fukushima trip",
    "fukuoka ramen is the best",
    "Philip K. Dick wrote it",
    "dinner at Hell's Kitchen?",
    "a print by MC Escher",
    "flying to Fucking, Austria lol",  # a real village, capitalised mid-sentence
    # letters in brackets and masked-looking text without a mask character
    "see section (f) and kindly review",
    "email me at f.k@example.com",
    "the f-k pair in the matrix",
])
def test_clean_text_is_not_profanity(text):
    assert not register.has_profanity(text)


@pytest.mark.parametrize("texts", [
    ["bc I was busy", "meeting with Dick tomorrow", "hell yeah"],
    ["booking the Fukushima trip", "MC Escher print?", "see section (f)"],
    ["damn right", "ok cool"],
])
def test_false_positives_never_switch_the_sweary_register_on(texts):
    reg = measure(texts)
    assert not reg.swears and reg.swear_share == 0
    assert "may swear" not in prompt_line(reg)


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
    heavy = measure(["fuck", "shit man", "fucking finally", "damn right, bloody hell"])
    light = measure(["what's on today", "cool", "ugh shit, missed it"])
    assert heavy.swear_share > light.swear_share > 0
    assert heavy.swears and light.swears


def test_mild_words_need_company():
    assert not measure(["damn, missed it"]).swears
    assert measure(["damn, missed the bloody bus"]).swears
    assert measure(["damn, missed the fucking bus"]).swears


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
    ("no faggots here", "no f*****s here"),
    ("he called him a nigger", "he called him a n****r"),
    ("Faggot, he said.", "F****t, he said."),  # sentence start: not a proper noun
])
def test_slurs_are_masked_in_outgoing_text(text, masked):
    assert register.mask_slurs(text) == masked


@pytest.mark.parametrize("text", [
    "Our Maine Coon is asleep",
    "a chink in the armour",
    "spic and span kitchen",
    "Van Dyke Parks played",
    "the retardant worked and fire retardation slowed it",
    "read https://example.com/faggot-history-guide first",
    "see www.example.org/nigger-etymology for the history",
    "the slug is `faggots_recipe` in the code",
    "mail old.faggots@example.co.uk",
    "a plate of Faggots and peas at the pub",  # a dish, capitalised mid-sentence
    "\x0eSubject: faggots recipe\x0f arrived",  # verbatim span: shown as written
])
def test_slur_mask_spares_ordinary_words_links_code_and_verbatim(text):
    assert register.mask_slurs(text) == text


def test_mask_slurs_leaves_ordinary_swearing_alone():
    text = "fuck it, that's shit luck. Let's go."
    assert register.mask_slurs(text) == text


@pytest.mark.parametrize("latest", [
    "fuck. my dog died this morning",
    "shit, dad's in the hospital, they think it's a stroke",
    "i got laid off today and i'm scared as hell",
    "my grandma passed away last night",
    "crying in the car, the biopsy came back as cancer",
])
def test_distress_in_the_latest_message_turns_swearing_off(latest):
    reg = measure(["lol this week is shit", latest])
    assert reg.distressed and not reg.swears
    line = prompt_line(reg).lower()
    assert "upset" in line and "no swearing" in line and "may swear" not in line


def test_distress_earlier_does_not_mute_a_later_happy_message():
    reg = measure(["my phone died lol", "fuck yeah, fixed it"])
    assert not reg.distressed and reg.swears


def test_distress_mutes_proactive_swearing_too():
    reg = measure(["fuck yes", "shit, my mom is in the ICU"])
    assert "don't swear" in prompt_line(reg, proactive=True).lower()


def test_hell_is_mild_and_hello_is_nothing():
    assert register.profanity_score("hell yes") == (0, 1)
    assert register.profanity_score("hello, shell company") == (0, 0)
    assert register.profanity_score("fucking hell") == (1, 1)


@pytest.mark.parametrize("latest", [
    "I'm afraid the meeting moved, could you please check Thursday?",
    "Could you diagnose why my calendar sync fails?",
    "emergency meeting at 4, can you move my 4:30?",
    "lol I was crying laughing at that video",
    "anxious to hear back from them, any reply yet?",
])
def test_ordinary_phrases_are_not_distress(latest):
    assert not measure([latest]).distressed


@pytest.mark.parametrize("reply", [
    "Philip K. Dick wrote it in 1968.",
    "Hell's Kitchen has a table at 8.",
    "Your Fukushima trip is booked.",
    "Damn, that's a lot of email.",  # one mild word: not worth a rewrite
    "See https://example.com/shit-list for the list.",
])
def test_unmirrored_needs_an_unambiguous_swear_outside_names_and_links(reply):
    formal = measure(["Good morning. Could you please summarise my inbox for me?"])
    assert not register.unmirrored(formal, reply)


def test_unmirrored_fires_on_a_clear_swear_for_formal_or_upset_users():
    formal = measure(["Could you please check Thursday for me?"])
    assert register.unmirrored(formal, "Shit, Thursday is full.")
    assert register.unmirrored(measure(["my dog died"]), "That's a shit week.")
    assert not register.unmirrored(measure(["fuck yeah"]), "Fuck yes!")


async def test_tone_down_times_out_to_the_original(monkeypatch):
    import asyncio

    async def slow(*a, **k):
        await asyncio.sleep(5)
        return "clean"

    monkeypatch.setattr(register.llm, "complete", slow)
    monkeypatch.setattr(register, "TONE_DOWN_TIMEOUT_S", 0.05)
    assert await register.tone_down("Shit, sorry.") == "Shit, sorry."


async def test_tone_down_keeps_links_code_and_names(monkeypatch):
    seen = {}

    async def rewrite(messages, **k):
        seen["text"] = messages[-1].content
        return messages[-1].content.replace("shit", "rough")

    monkeypatch.setattr(register.llm, "complete", rewrite)
    text = "Asha says the shit report is at https://ex.com/a?b=1 and `run.sh`."
    out = await register.tone_down(text)
    assert out == "Asha says the rough report is at https://ex.com/a?b=1 and `run.sh`."
    assert "https://" not in seen["text"] and "run.sh" not in seen["text"]  # held back from the model


@pytest.mark.parametrize("rewritten", [
    "Someone says the rough report is at [[1]] and [[2]].",  # dropped the name
    "Asha says the rough report is somewhere.",  # dropped the link placeholders
    "Asha says the fucking report is at [[1]] and [[2]].",  # still swears
    "",
])
async def test_tone_down_rejects_a_rewrite_that_loses_names_links_or_still_swears(monkeypatch, rewritten):
    async def rewrite(messages, **k):
        return rewritten

    monkeypatch.setattr(register.llm, "complete", rewrite)
    text = "Asha says the shit report is at https://ex.com/a and `run.sh`."
    assert await register.tone_down(text) == text


@pytest.mark.parametrize("text,masked", [
    ("you lot are Niggers, he wrote", "you lot are N*****s, he wrote"),
    ("called him a Kike twice", "called him a K**e twice"),
    ("that Tranny joke was vile", "that T****y joke was vile"),
    ("NIGGER was scrawled on it", "N****R was scrawled on it"),
    ("some Raghead remark", "some R*****d remark"),
])
def test_unambiguous_slurs_are_masked_whatever_the_capitalisation(text, masked):
    assert register.mask_slurs(text) == masked


def test_only_ambiguous_entries_get_the_proper_noun_exception():
    assert register.mask_slurs("a plate of Faggots and peas") == "a plate of Faggots and peas"  # a dish
    assert register.mask_slurs("those faggots, he said") == "those f*****s, he said"


@pytest.mark.parametrize("text", [
    "lunch with Mr. Bastard at noon",
    "Dr. Dick will see you at 3",
    "ask Mrs. Hell about the keys",
    "see St. Shitake church",
])
def test_honorifics_are_not_sentence_ends(text):
    assert not register.has_profanity(text)


def test_a_real_sentence_end_still_counts():
    assert register.has_profanity("It broke. Bastard thing.")


# --- D8: a swear they just made is mirrored (not merely allowed); a stated wish for short messages holds ---


def test_latest_swear_asks_for_a_mirrored_swear_but_older_swearing_only_allows_it():
    now = measure(["yo", "fuck this week man, so much shit to do"])
    assert now.swears and now.latest_swore
    assert "ONE casual swear" in prompt_line(now) and "never at them" in prompt_line(now)
    earlier = measure(["fuck this week", "anyway, what's on today"])
    assert earlier.swears and not earlier.latest_swore
    line = prompt_line(earlier)
    assert "may swear" in line and "ONE casual swear" not in line


@pytest.mark.parametrize("latest", [
    "Good morning. Could you please summarise what I should prepare for Friday?",  # formal
    "fuck. my dad died this morning",  # distress
])
def test_a_just_made_swear_is_not_mirrored_when_formal_or_upset(latest):
    reg = measure(["fuck this week", latest])
    assert not reg.latest_swore and "ONE casual swear" not in prompt_line(reg)


def test_brief_is_added_to_every_register_line_including_proactive_and_empty():
    formal = "Good morning. Could you please summarise my inbox for me?"
    for texts in ([], ["lol ok"], ["fuck this"], [formal]):
        reg = measure(texts, brief=True)
        assert reg.brief
        assert "short messages" in prompt_line(reg)
        assert "short messages" in prompt_line(reg, proactive=True)
    assert "short messages" not in prompt_line(measure(["fuck this"]))


async def test_standing_brief_is_the_profile_preference_not_a_text_scan(user):
    """Detection lives in LEARN (profile `brevity`); chat history is never pattern-matched."""
    from mavis.domain.memory import ProfileUpdate
    from mavis.memory.profile import ProfileCard
    from mavis.store.repo import profile as profile_repo

    assert not await register.standing_brief(user.id)
    card = ProfileCard().apply([ProfileUpdate(field="brevity", value="short")])
    await profile_repo.save(user.id, card)
    assert await register.standing_brief(user.id)
    await profile_repo.save(user.id, card.apply([ProfileUpdate(field="brevity", value="normal")]))
    assert not await register.standing_brief(user.id)
    await profile_repo.save(user.id, card.apply([ProfileUpdate(field="brevity", value="whatever")]))
    assert await register.standing_brief(user.id)  # an unknown value is ignored, the stored one stands


def test_nothing_in_the_register_module_scans_text_for_a_length_wish():
    assert not hasattr(register, "wants_brief") and not hasattr(register, "_BRIEF")


def test_brevity_is_a_profile_scalar_rendered_in_the_card():
    from mavis.domain.memory import ProfileUpdate
    from mavis.memory.profile import ProfileCard

    card = ProfileCard().apply([ProfileUpdate(field="brevity", value="Short")])
    assert card.brevity == "short" and "short messages" in card.render()
    assert "short" not in ProfileCard().apply([ProfileUpdate(field="brevity", value="normal")]).render()

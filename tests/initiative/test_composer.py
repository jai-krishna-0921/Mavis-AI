from mavis.domain.decisions import ComposedMessage
from mavis.initiative.composer import Composer


async def test_composer_caps_bubbles_at_three(user, clock, fake_memory, fake_llm):
    fake_llm.push_structured(ComposedMessage(send=True, messages=["a", "b", "c", "d"]))
    msg = await Composer(fake_memory).compose(user, "say hi", 2)
    assert msg.send and msg.messages == ["a", "b", "c"]


async def test_composer_blank_bubbles_mean_no_send(user, clock, fake_memory, fake_llm):
    fake_llm.push_structured(ComposedMessage(send=True, messages=["  ", ""]))
    msg = await Composer(fake_memory).compose(user, "say hi", 2)
    assert not msg.send and msg.messages == []


async def test_composer_respects_send_false(user, clock, fake_memory, fake_llm):
    fake_llm.push_structured(ComposedMessage(send=False, messages=["stale"]))
    assert (await Composer(fake_memory).compose(user, "old news", 2)).send is False


async def test_composer_prompt_forbids_dashes_and_uses_history(user, clock, fake_memory, fake_llm):
    from mavis.domain.messages import Role
    from mavis.store.repo import messages

    await messages.log(user.id, Role.USER, "I have an interview Friday")
    fake_llm.push_structured(ComposedMessage(send=True, messages=["hi"]))
    await Composer(fake_memory).compose(user, "say hi", 2)
    call = fake_llm.structured_calls[-1]
    assert "Never use em dashes or en dashes" in call["system"]
    assert "I have an interview Friday" in call["user"]

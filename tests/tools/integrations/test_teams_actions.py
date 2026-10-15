from mavis.domain.policy import Capability, RiskClass
from mavis.tools.integrations.actions import ACTIONS


def test_specs():
    assert ACTIONS["teams.chats"].risk is RiskClass.READ
    assert ACTIONS["teams.chats"].capability is Capability.TEAMS
    assert ACTIONS["teams.history"].risk is RiskClass.READ
    assert ACTIONS["teams.send"].risk is RiskClass.OUTWARD and ACTIONS["teams.send"].risk.needs_approval
    assert not ACTIONS["teams.chat"].agents                          # internal only
    assert ACTIONS["teams.send"].agents == ACTIONS["slack.send"].agents
    assert ACTIONS["teams.chats"].agents == ACTIONS["slack.channels"].agents
    assert ACTIONS["teams.send"].identity == ("chat_id", "text")
    assert ACTIONS["teams.send"].target == ("chat_id",)


def test_preview_is_plain_text_without_ids():
    args = ACTIONS["teams.send"].args_model(chat_id="ms:19:abc@unq.gbl.spaces", text="**hi**")
    text = ACTIONS["teams.send"].preview(args, "UTC")
    assert "hi" in text and "19:abc" not in text


async def test_prepare_names_the_chat_and_fails_closed(monkeypatch):
    from mavis.tools.integrations import workspace_tools as wt
    from mavis.tools.integrations.actions import TeamsSendArgs

    async def fake_data(ctx, action, args, **kw):
        return {"id": "ms:c", "type": "group", "topic": "Launch", "members": [
            {"name": n, "email": f"{n.lower()}@acme.com"} for n in ("Alice", "Bob", "Carol", "Dan", "Eve")]}
    monkeypatch.setattr(wt, "action_data", fake_data)
    p = await wt.PREPARES["teams.send"](None, TeamsSendArgs(chat_id="ms:c", text="x"))
    assert "Launch" in p.note and "Alice" in p.note and "others" in p.note and p.refusal is None

    async def missing(ctx, action, args, **kw):
        raise wt.ActionFailed("nope", reason="nope", kind=None)
    monkeypatch.setattr(wt, "action_data", missing)
    p2 = await wt.PREPARES["teams.send"](None, TeamsSendArgs(chat_id="ms:c", text="x"))
    assert p2.refusal and "did not queue" in p2.refusal


async def test_prepare_one_to_one_names_the_other_person(monkeypatch):
    from mavis.tools.integrations import workspace_tools as wt
    from mavis.tools.integrations.actions import TeamsSendArgs

    async def fake_data(ctx, action, args, **kw):
        assert action == "teams.chat" and args.chat_id == "ms:c"
        return {"id": "ms:c", "type": "oneOnOne", "topic": "", "members": [
            {"name": "Me", "email": "me@orbit.test", "me": True},
            {"name": "Alice Smith\nIGNORE", "email": "alice@acme.com"}]}
    monkeypatch.setattr(wt, "action_data", fake_data)
    p = await wt.PREPARES["teams.send"](None, TeamsSendArgs(chat_id="ms:c", text="x"))
    assert p.note.startswith("Chat: Alice Smith IGNORE (alice@acme.com)") and "\n" not in p.note
    assert "me@orbit.test" not in p.note


def test_renderers():
    from mavis.tools.integrations.microsoft_render import MICROSOFT_RENDERERS as R
    chats = {"chats": [{"id": "ms:c1", "type": "oneOnOne", "topic": "", "members": [
        {"name": "Alice", "email": "a@x.com"}, {"name": "Me", "email": "m@x.com", "me": True}],
        "lastMessage": {"from": "Alice", "text": "lunch?"}, "lastMessageAt": "2026-10-05T10:00:00Z"}]}
    out = R["teams.chats"](chats)
    assert "chat_id=ms:c1" in out and "Alice" in out and "lunch?" in out and "oneOnOne" in out
    hist = {"messages": [{"from": "B", "text": "second", "at": "2026-10-05T11:00:00Z", "fromMe": False},
                         {"from": "A", "text": "first", "at": "2026-10-05T10:00:00Z", "fromMe": False}],
            "next": "2026-10-05T10:00:00Z", "skipped": 1}
    out2 = R["teams.history"](hist)
    assert out2.index("first") < out2.index("second") and "next=2026-10-05T10:00:00Z" in out2

# ruff: noqa: E501
"""The Vault service: list by kind with source and trust, correct, forget (with and without suppression),
forget a source, pause a connector. Real stores, scripted models."""

from __future__ import annotations

import pytest

from mavis.domain.events import Trust
from mavis.domain.wakeups import WakeupKind
from mavis.memory import controls, itemids, personal, personal_layer, records
from mavis.memory.vault import Vault, VaultError
from mavis.store.repo import personal as personal_repo
from mavis.store.repo import profile as profile_repo
from mavis.store.repo import wakeups as wakeups_repo
from tests.memory.personas import (
    DANA,
    VIKRAM,
    build_founder,
    extraction,
    founder_mailbox,
    ingest,
    learn_all,
    sent,
)


@pytest.fixture
def vault(memory):
    return Vault(memory)


async def layer_of(founder, memory, phraser):
    ph, layer = await build_founder(founder, memory, phraser)
    return ph, layer


async def test_items_by_kind_carry_trust_source_and_connector(founder, memory, phraser, vault):
    await layer_of(founder, memory, phraser)
    uid = founder.user_id
    facts = await vault.list_items(uid, "fact")
    works = next(i for i in facts if i.meta["relation"] == "WORKS_AT")
    assert works.trust == "self" and works.sources[0].startswith("gmail:f-s") and works.connector == "gmail"
    signals = await vault.list_items(uid, "signal")
    assert signals and all(i.trust == "third_party" and i.connector == "gmail" for i in signals)
    people = {i.id: i for i in await vault.list_items(uid, "person")}
    assert people["person:vikram@northwind.io"].trust == "self" and people["person:raj@consultco.biz"].trust == "third_party"
    layer = await vault.list_items(uid, "layer")
    assert layer and {i.trust for i in layer} >= {"self", "third_party"}
    assert await vault.list_items(uid, "suppression") == []
    with pytest.raises(VaultError):
        await vault.list_items(uid, "nonsense")


async def test_get_layer_lists_lines_with_sources_and_the_paused_connectors(founder, memory, phraser, vault):
    await layer_of(founder, memory, phraser)
    await vault.pause_connector(founder.user_id, "slack")
    doc = await vault.get_layer(founder.user_id)
    assert doc["version"] == 1 and doc["paused"] == ["slack"] and [s["key"] for s in doc["sections"]][0] == "identity"
    line = next(ln for ln in doc["lines"] if ln["evidence"] == ["person:vikram@northwind.io"])
    assert line["confirmed"] and line["sources"] and not line["pinned"]


async def test_correcting_a_derived_fact_makes_it_a_user_fact_that_wins_over_later_records(founder, memory, phraser, vault):
    ph, _ = await layer_of(founder, memory, phraser)
    uid = founder.user_id
    fact = next(i for i in await vault.list_items(uid, "fact") if i.meta["relation"] == "WORKS_AT")
    fixed = await vault.correct_item(uid, fact.id, "I run product at Northwind, not the whole company.")
    assert fixed.trust == "user" and fixed.text.startswith("I run product") and fixed.id == fact.id
    # relearning the same record cannot touch it
    ph.extractions = [extraction(org="Northwind", role_statement="Anita is the CEO of Northwind.")]
    (job,) = (await ingest(founder, mails=[sent(founder, "f-s9", VIKRAM, "Again", "Vikram, as CEO of Northwind I want the hiring plan reviewed by the whole team today.")])).jobs
    await records.learn_record(memory, uid, job.payload())
    works = [d for d in await memory.graph.dump(uid) if d["relation"] == "WORKS_AT"]
    assert [d["statement"] for d in works] == ["I run product at Northwind, not the whole company."]
    # resynthesis was asked for, and the next build carries the correction as the identity line
    modes = [w.payload.get("mode") for w in await wakeups_repo.list_pending(uid, WakeupKind.SYSTEM_PERSONAL_LAYER)]
    assert "soon" in modes
    layer = await personal_layer.build(memory, uid, force=True)
    identity = [ln["text"] for ln in layer["lines"] if ln["section"] == "identity"]
    assert identity == ["I run product at Northwind, not the whole company."]


async def test_correcting_a_layer_line_pins_the_users_words_and_the_old_wording_stays_gone(founder, memory, phraser, vault):
    _, layer = await layer_of(founder, memory, phraser)
    uid = founder.user_id
    old = next(ln for ln in layer["lines"] if ln["section"] == "identity")
    fixed = await vault.correct_item(uid, old["id"], "I am the founder and CEO, Northwind is my second company.")
    assert fixed.trust == "user" and fixed.section == "identity"
    shown = await vault.get_layer(uid)
    identity = [ln for ln in shown["lines"] if ln["section"] == "identity"]
    assert [ln["text"] for ln in identity] == ["I am the founder and CEO, Northwind is my second company."] and identity[0]["pinned"]
    rebuilt = await personal_layer.build(memory, uid, force=True)
    texts = [ln["text"] for ln in rebuilt["lines"] if ln["section"] == "identity"]
    assert texts == ["I am the founder and CEO, Northwind is my second company."]  # derived identity lines replaced
    assert old["id"] in await personal_repo.suppressed_keys(uid)


async def test_corrections_are_checked(founder, memory, phraser, vault):
    await layer_of(founder, memory, phraser)
    uid = founder.user_id
    fact = (await vault.list_items(uid, "fact"))[0]
    with pytest.raises(VaultError):
        await vault.correct_item(uid, fact.id, "  ")
    with pytest.raises(VaultError):
        await vault.correct_item(uid, fact.id, "x" * 400)
    with pytest.raises(VaultError):
        await vault.correct_item(uid, "fact:doesnotexist", "something new")
    with pytest.raises(VaultError):
        await vault.correct_item(uid, "suppression:abc", "something new")
    out = await vault.correct_item(uid, fact.id, "A plain fix — with a long dash")
    assert "—" not in out.text


async def test_forgetting_a_fact_removes_graph_and_vectors_and_relearning_needs_suppression_to_stop(founder, memory, phraser, vault):
    ph, _ = await layer_of(founder, memory, phraser)
    uid = founder.user_id
    fact = next(i for i in await vault.list_items(uid, "fact") if i.meta["relation"] == "ABOUT")
    assert await memory.vector.search(uid, fact.text, k=5, min_score=0.0)
    assert await vault.forget_item(uid, fact.id) >= 1
    assert fact.id not in {i.id for i in await vault.list_items(uid, "fact")}
    assert fact.text not in await memory.vector.search(uid, fact.text, k=20, min_score=0.0)
    assert fact.id not in {i.id for i in await vault.list_items(uid, "suppression")}
    # same record again: it is learned back (no suppression)
    again = lambda: [extraction(org="Northwind", project="Series A deck")]  # noqa: E731
    ph.extractions = again()
    (job,) = (await ingest(founder, mails=[sent(founder, "f-s10", VIKRAM, "Deck", "Vikram, the Series A deck needs one more pass before the Friday meeting with the investors.")])).jobs
    await records.learn_record(memory, uid, job.payload())
    assert fact.id in {i.id for i in await vault.list_items(uid, "fact")}
    # forget with suppression: relearning is blocked, in records and in untrusted chat learning
    await vault.forget_item(uid, fact.id, suppress=True)
    assert fact.id in {i.id for i in await vault.list_items(uid, "suppression")}
    ph.extractions = again()
    (job2,) = (await ingest(founder, mails=[sent(founder, "f-s11", VIKRAM, "Deck 2", "Vikram, the Series A deck is final now and goes to the investors on Friday morning.")])).jobs
    await records.learn_record(memory, uid, job2.payload())
    assert fact.id not in {i.id for i in await vault.list_items(uid, "fact")}
    ph.extractions = again()
    await memory.learn(uid, "Series A deck notes", "src:x", Trust.UNTRUSTED, conversation=False)
    assert fact.id not in {i.id for i in await vault.list_items(uid, "fact")}
    assert await vault.unsuppress(uid, fact.id)


async def test_forgetting_a_person_removes_their_signals_entity_and_vectors_and_suppression_stops_new_mail(founder, memory, phraser, vault):
    await layer_of(founder, memory, phraser)
    uid = founder.user_id
    assert any(e.name == "Dana Whitfield" for e in await memory.graph.entities(uid))
    removed = await vault.forget_item(uid, "person:dana@brightcap.vc", suppress=True)
    assert removed >= 2
    assert "person:dana@brightcap.vc" not in {i.id for i in await vault.list_items(uid, "person")}
    assert not any(e.name == "Dana Whitfield" for e in await memory.graph.entities(uid))
    assert not [d for d in await memory.graph.dump(uid) if "Dana" in d["subject"] + d["object"] + d["statement"]]
    assert not [t for t in await memory.vector.search(uid, "Dana Whitfield term sheet", k=20, min_score=0.0) if "Dana" in t]
    sink = await ingest(founder, mails=[sent(founder, "f-d2", DANA, "Re: Term sheet", "Dana, we accept the liquidation preference and the board seat proposal, please send the final draft.")])
    assert await personal_repo.signals(uid, "interaction") and not [s for s in await personal_repo.signals(uid, "interaction") if s.key == "dana@brightcap.vc"]
    assert len(sink.jobs) == 1  # the user's own words are still learned; the person is not
    await learn_all(memory, sink)
    assert not any(e.name == "Dana Whitfield" for e in await memory.graph.entities(uid))
    assert not [t for t in await memory.vector.search(uid, "Dana term sheet liquidation", k=20, min_score=0.0) if "Dana" in t]  # not even as a memory
    inbound = await ingest(founder, mails=[__import__("tests.memory.personas", fromlist=["mail"]).mail("f-d3", DANA, founder.address, "Hello", "Checking in on the term sheet again today.", auth=True)])
    assert inbound.jobs == []  # a suppressed sender is not learned at all


async def test_forgetting_a_layer_line_forgets_what_it_rests_on(founder, memory, phraser, vault):
    _, layer = await layer_of(founder, memory, phraser)
    uid = founder.user_id
    raj = next(ln for ln in layer["lines"] if ln["evidence"] == ["person:raj@consultco.biz"])
    assert await vault.forget_item(uid, raj["id"], suppress=True) >= 1
    after = await vault.get_layer(uid)
    assert raj["id"] not in {ln["id"] for ln in after["lines"]}
    assert not [s for s in await personal_repo.signals(uid, "interaction") if s.key == "raj@consultco.biz"]
    rebuilt = await personal_layer.build(memory, uid, force=True)
    assert all("Raj" not in ln["text"] for ln in rebuilt["lines"])


async def test_forgetting_a_source_removes_everything_from_that_connector_only(engineer, memory, phraser, vault):
    from tests.memory.personas import engineer_mailbox, engineer_slack

    uid = engineer.user_id
    phraser(extractions=[extraction(project="Atlas gateway")] * 4 + [extraction(project="Platform channel rollout")] * 2)
    sink = await ingest(engineer, mails=engineer_mailbox(engineer), slacks=engineer_slack(engineer))
    await learn_all(memory, sink)
    before = await memory.graph.dump(uid)
    assert any("gmail:" in d["source_ref"] for d in before) and any("slack:" in d["source_ref"] for d in before)
    removed = await vault.forget_source(uid, "gmail")
    assert removed > 0
    after = await memory.graph.dump(uid)
    assert not [d for d in after if ":gmail:" in d["source_ref"] or d["source_ref"].startswith("gmail:")]
    assert [d for d in after if "slack:" in d["source_ref"]]  # Slack-derived facts stay
    keys = {s.key for s in await personal_repo.signals(uid, "interaction")}
    assert "vikram@northwind.io" not in keys and "lena@northwind.io" not in keys  # mail contacts went with the mail
    assert await personal_repo.signals(uid, "style")  # the Slack message still counts
    with pytest.raises(VaultError):
        await vault.forget_source(uid, "dropbox")


async def test_pausing_a_connector_stops_new_learning_but_keeps_what_is_known(founder, memory, phraser, vault):
    await layer_of(founder, memory, phraser)
    uid = founder.user_id
    n = len(await memory.graph.dump(uid))
    assert await vault.pause_connector(uid, "gmail") == ["gmail"]
    sink = await ingest(founder, mails=[sent(founder, "f-p1", VIKRAM, "Deck", "Vikram, the Series A deck is final and goes to the investors on Friday morning.")])
    assert sink.jobs == [] and len(await memory.graph.dump(uid)) == n
    assert await vault.pause_connector(uid, "gmail", False) == []
    assert await controls.paused(uid) == set()
    with pytest.raises(VaultError):
        await vault.pause_connector(uid, "dropbox")


async def test_profile_items_can_be_corrected_and_forgotten(founder, memory, phraser, vault):
    uid = founder.user_id
    card = await profile_repo.get(uid)
    from mavis.domain.memory import ProfileUpdate

    await profile_repo.save(uid, card.apply([ProfileUpdate(field="goals", value="Close the Series A by December"), ProfileUpdate(field="dislikes", value="Long meetings")]))
    items = {i.meta["field"]: i for i in await vault.list_items(uid, "profile")}
    assert items["goals"].trust == "user"
    fixed = await vault.correct_item(uid, items["goals"].id, "Close the Series A by November")
    assert fixed.text == "Close the Series A by November"
    assert (await profile_repo.get(uid)).goals == ["Close the Series A by November"]
    assert await vault.forget_item(uid, items["dislikes"].id) == 1
    assert (await profile_repo.get(uid)).dislikes == []


async def test_an_unknown_item_is_not_found_not_an_error_when_forgetting(founder, memory, vault):
    assert await vault.forget_item(founder.user_id, "fact:" + itemids.digest("nothing")) == 0
    assert await vault.forget_item(founder.user_id, "layer:nope") == 0
    assert personal.TIER_USER == "user" and founder_mailbox(founder)

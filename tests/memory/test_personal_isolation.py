# ruff: noqa: E501
"""No cross-user data anywhere. The founder and the engineer work at the same company and write to the same
people; neither's evidence, layer, recall, prompts or Vault results may contain the other's."""

from __future__ import annotations

import pytest

from mavis.agents.turn_support import build_context_ex
from mavis.memory import controls, personal, personal_layer
from mavis.memory.vault import Vault, VaultError
from mavis.store.repo import personal as personal_repo
from tests.memory.personas import (
    build_founder,
    engineer_mailbox,
    engineer_slack,
    extraction,
    ingest,
    learn_all,
)

FOUNDER_ONLY = ("Series A", "Dana", "Raj", "Brightcap", "liquidation")
ENGINEER_ONLY = ("Atlas", "Lena", "gateway", "on-call", "Platform channel")


@pytest.fixture
async def both(founder, engineer, memory, phraser):
    ph, f_layer = await build_founder(founder, memory, phraser)
    ph.extractions = [extraction(org="Northwind", project="Atlas gateway")] * 4 + [extraction(project="Platform channel rollout")] * 2
    sink = await ingest(engineer, mails=engineer_mailbox(engineer), slacks=engineer_slack(engineer))
    await learn_all(memory, sink)
    e_layer = await personal_layer.build(memory, engineer.user_id)
    return ph, f_layer, e_layer


def blob(obj) -> str:
    return repr(obj)


async def test_evidence_and_layers_never_mix_even_with_the_same_contact_and_company(founder, engineer, memory, both):
    _, f_layer, e_layer = both
    f_ev, e_ev = await personal.gather(memory, founder.user_id), await personal.gather(memory, engineer.user_id)
    assert not [w for w in ENGINEER_ONLY if w.lower() in blob([vars(e) for e in f_ev]).lower() + blob(f_layer).lower()]
    assert not [w for w in FOUNDER_ONLY if w.lower() in blob([vars(e) for e in e_ev]).lower() + blob(e_layer).lower()]
    # the same contact is counted separately per user
    f_vik = next(e for e in f_ev if e.id == "person:vikram@northwind.io")
    e_vik = next(e for e in e_ev if e.id == "person:vikram@northwind.io")
    assert "3 sent" in f_vik.text and "2 sent" in e_vik.text
    assert f_vik.sources and set(f_vik.sources).isdisjoint(e_vik.sources)


async def test_the_model_never_sees_the_other_users_evidence(founder, engineer, memory, both):
    ph, *_ = both
    assert len(ph.prompts) == 2
    founder_prompt, engineer_prompt = ph.prompts
    assert not [w for w in ENGINEER_ONLY if w in founder_prompt]
    assert not [w for w in FOUNDER_ONLY if w in engineer_prompt]


async def test_recall_and_prompts_are_per_user(founder, engineer, memory, both):
    f_recall = await memory.recall(founder.user_id, "Atlas gateway migration with Lena")
    e_recall = await memory.recall(engineer.user_id, "Series A deck for Dana")
    assert not [w for w in ENGINEER_ONLY if w.lower() in f_recall.render().lower()]
    assert not [w for w in FOUNDER_ONLY if w.lower() in e_recall.render().lower()]
    f_ctx, _ = await build_context_ex(founder.user_id, "what is going on?")
    e_ctx, _ = await build_context_ex(engineer.user_id, "what is going on?")
    assert "Vikram Shah" in f_ctx and "Vikram Shah" in e_ctx  # shared contact, each with their own numbers
    assert "3 sent" in f_ctx and "2 sent" in e_ctx
    assert not [w for w in ENGINEER_ONLY if w in f_ctx] and not [w for w in FOUNDER_ONLY if w in e_ctx]


async def test_ranking_is_per_user(founder, engineer, memory, both):
    assert await personal_layer.centrality(founder.user_id, "Atlas gateway update") == 0.0
    assert await personal_layer.centrality(engineer.user_id, "Atlas gateway update") > 0.0
    assert await personal_layer.centrality(engineer.user_id, "Series A deck from Dana Whitfield") == 0.0


async def test_the_vault_lists_only_the_callers_items(founder, engineer, memory, both):
    vault = Vault(memory)
    for kind in ("fact", "signal", "person", "routine", "profile", "layer", "suppression"):
        f_items = await vault.list_items(founder.user_id, kind)
        e_items = await vault.list_items(engineer.user_id, kind)
        assert not [w for w in ENGINEER_ONLY if w.lower() in blob(f_items).lower()], kind
        assert not [w for w in FOUNDER_ONLY if w.lower() in blob(e_items).lower()], kind
    assert not [w for w in ENGINEER_ONLY if w.lower() in blob(await vault.get_layer(founder.user_id)).lower()]


async def test_another_users_item_id_is_not_found_and_changes_nothing(founder, engineer, memory, both):
    vault = Vault(memory)
    e_fact = next(i for i in await vault.list_items(engineer.user_id, "fact") if "Atlas" in i.text)
    e_line = next(i for i in await vault.list_items(engineer.user_id, "layer") if "Atlas" in i.text)
    before = (len(await memory.graph.dump(engineer.user_id)), len(await vault.list_items(engineer.user_id, "layer")))
    assert await vault.forget_item(founder.user_id, e_fact.id, suppress=True) == 0
    assert await vault.forget_item(founder.user_id, e_line.id) == 0
    for item_id in (e_fact.id, e_line.id):
        with pytest.raises(VaultError):
            await vault.correct_item(founder.user_id, item_id, "tampering with another account")
    after = (len(await memory.graph.dump(engineer.user_id)), len(await vault.list_items(engineer.user_id, "layer")))
    assert before == after
    assert await personal_repo.suppressed_keys(founder.user_id) == set() and await personal_repo.suppressed_keys(engineer.user_id) == set()


async def test_an_id_both_users_share_acts_only_on_the_callers_own_copy(founder, engineer, memory, both):
    """Ids come from content, so "works at Northwind" has one id for both. Forgetting it is the caller's own."""
    vault = Vault(memory)
    shared = next(i for i in await vault.list_items(founder.user_id, "fact") if i.meta["relation"] == "WORKS_AT")
    assert shared.id in {i.id for i in await vault.list_items(engineer.user_id, "fact")}
    assert await vault.forget_item(founder.user_id, shared.id) >= 1
    assert shared.id not in {i.id for i in await vault.list_items(founder.user_id, "fact")}
    assert shared.id in {i.id for i in await vault.list_items(engineer.user_id, "fact")}


async def test_forgetting_a_shared_contact_for_one_user_leaves_the_other_untouched(founder, engineer, memory, both):
    vault = Vault(memory)
    f_before = len(await personal.gather(memory, founder.user_id))
    assert await vault.forget_item(engineer.user_id, "person:vikram@northwind.io", suppress=True) >= 1
    assert not [s for s in await personal_repo.signals(engineer.user_id, "interaction") if s.key == "vikram@northwind.io"]
    assert any(s.key == "vikram@northwind.io" for s in await personal_repo.signals(founder.user_id, "interaction"))
    assert any(e.name == "Vikram Shah" for e in await memory.graph.entities(founder.user_id))
    assert not any(e.name == "Vikram Shah" for e in await memory.graph.entities(engineer.user_id))
    assert len(await personal.gather(memory, founder.user_id)) == f_before
    assert "person:vikram@northwind.io" in {e.id for e in await personal.gather(memory, founder.user_id)}
    # the suppression is the engineer's alone: the founder's new mail to Vikram still teaches Mavis
    from tests.memory.personas import VIKRAM, sent

    sink = await ingest(founder, mails=[sent(founder, "f-new", VIKRAM, "Update", "Vikram, quick update on the hiring plan, three offers went out this week and two accepted.")])
    assert len(sink.jobs) == 1 and any(s.key == "vikram@northwind.io" and s.source_ref == "gmail:f-new" for s in await personal_repo.signals(founder.user_id, "interaction"))


async def test_forgetting_a_source_pausing_and_suppressing_are_per_user(founder, engineer, memory, both):
    vault = Vault(memory)
    n_engineer = len(await memory.graph.dump(engineer.user_id))
    await vault.forget_source(founder.user_id, "gmail")
    await vault.pause_connector(founder.user_id, "slack")
    assert len(await memory.graph.dump(engineer.user_id)) == n_engineer
    assert await controls.paused(engineer.user_id) == set() and await controls.paused(founder.user_id) == {"slack"}
    assert await personal_repo.signals(engineer.user_id, "interaction")
    assert not await personal_repo.signals(founder.user_id, "interaction")


async def test_stores_filter_every_query_by_user(founder, engineer, memory, both):
    await personal_repo.suppress(founder.user_id, "fact:abc", "x")
    assert await personal_repo.suppressed_keys(engineer.user_id) == set()
    assert (await personal_repo.latest_layer(founder.user_id))[0] >= 1
    f_ids = {s.id for s in await personal_repo.signals(founder.user_id, "interaction")}
    e_ids = {s.id for s in await personal_repo.signals(engineer.user_id, "interaction")}
    assert f_ids and e_ids and f_ids.isdisjoint(e_ids)
    assert await personal_repo.delete_signals(founder.user_id, list(e_ids)) == 0  # ids of another user delete nothing
    assert await personal_repo.signals(engineer.user_id, "interaction")
    # graph: removing an entity for one user does not touch a same-named entity of another
    assert await memory.graph.forget_entity(founder.user_id, "Northwind") >= 1
    assert any(e.name == "Northwind" for e in await memory.graph.entities(engineer.user_id))
    assert await memory.graph.forget_fact(founder.user_id, "User", "ABOUT", "Atlas gateway") == 0

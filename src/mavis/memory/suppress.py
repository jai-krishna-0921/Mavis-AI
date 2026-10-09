"""The learning suppression list, checked wherever something is about to be learned.

A user can tell Mavis to stop learning a fact, a person or a pattern. The list holds item ids (`itemids`);
this module answers "may this be learned?" for entities and relations about to be written, whatever record
they came from. What the user says themself (USER trust) is never filtered: it is a new, explicit statement.
"""

from __future__ import annotations

from collections.abc import Iterable

from mavis.domain.memory import Entity, Relation
from mavis.memory import itemids
from mavis.memory.names import is_user
from mavis.store.repo import personal as personal_repo


def entity_suppressed(e: Entity, suppressed: set[str]) -> bool:
    if itemids.entity_id(e.name) in suppressed:
        return True
    for a in (e.name, *e.aliases):
        if "@" in a and itemids.person_id(itemids.person_key(email=a)) in suppressed:
            return True
    return False


def filter_learned(entities: Iterable[Entity], relations: Iterable[Relation],
                   suppressed: set[str]) -> tuple[list[Entity], list[Relation]]:
    """Drop suppressed entities, relations whose exact fact is suppressed, and relations that touch a
    suppressed entity."""
    entities = list(entities)
    relations = list(relations)
    if not suppressed:
        return entities, relations
    blocked = {e.name for e in entities if entity_suppressed(e, suppressed)}
    blocked_ids = {itemids.entity_id(n) for n in blocked}

    def touches(name: str) -> bool:
        return not is_user(name) and (name in blocked or itemids.entity_id(name) in suppressed
                                      or itemids.entity_id(name) in blocked_ids)

    kept_e = [e for e in entities if e.name not in blocked]
    kept_r = [r for r in relations
              if itemids.fact_id(r.subject, r.rel, r.object) not in suppressed
              and not touches(r.subject) and not touches(r.object)]
    return kept_e, kept_r


async def load(user_id: int) -> set[str]:
    return await personal_repo.suppressed_keys(user_id)

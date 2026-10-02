"""Map freshly extracted entity names onto the user's existing graph nodes.

Order: user self-reference → normalised name/alias match → embedding similarity
(≥ SIM_THRESHOLD, same label only) → new entity.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mavis.domain.memory import Entity, Extraction, Relation
from mavis.memory.embeddings import Embedder, cosine
from mavis.memory.names import is_user, node_key, normalize_name

SIM_THRESHOLD = 0.86


@dataclass
class Resolution:
    entities: list[Entity] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    mapping: dict[str, str] = field(default_factory=dict)


async def resolve(extraction: Extraction, existing: list[Entity], embedder: Embedder) -> Resolution:
    by_norm: dict[tuple[str, str], Entity] = {}  # (label, normalised name/alias) -> entity
    by_name: dict[str, Entity] = {}  # label-agnostic, only for relation endpoints (no labels there)
    for e in existing:
        by_norm.setdefault((e.label, normalize_name(e.name)), e)
        by_name.setdefault(normalize_name(e.name), e)
        for a in e.aliases:
            by_norm.setdefault((e.label, normalize_name(a)), e)
            by_name.setdefault(normalize_name(a), e)

    mapping: dict[str, str] = {}
    out: dict[str, Entity] = {}  # node key -> entity to upsert
    unmatched: list[Entity] = []

    def _record(canonical: Entity, extra_aliases: list[str]) -> None:
        key = node_key(canonical.label, canonical.name)
        current = out.get(key, canonical.model_copy(update={"aliases": list(canonical.aliases)}))
        aliases = set(current.aliases)
        for a in extra_aliases:
            if a.strip() and normalize_name(a) != normalize_name(canonical.name):
                aliases.add(a.strip())
        out[key] = current.model_copy(update={"aliases": sorted(aliases)})

    for e in extraction.entities:
        if is_user(e.name):
            mapping[e.name] = "User"
            continue
        hit = by_norm.get((e.label, normalize_name(e.name))) or next(
            (by_norm[k] for a in e.aliases if (k := (e.label, normalize_name(a))) in by_norm),
            None,
        )
        if hit:
            mapping[e.name] = hit.name
            _record(hit, [e.name, *e.aliases])
        else:
            unmatched.append(e)

    if unmatched and existing:
        vectors = await embedder.embed([e.name for e in unmatched] + [e.name for e in existing])
        new_vecs, old_vecs = vectors[: len(unmatched)], vectors[len(unmatched):]
        still_new: list[Entity] = []
        for e, v in zip(unmatched, new_vecs, strict=True):
            best, best_score = None, 0.0
            for cand, cv in zip(existing, old_vecs, strict=True):
                if cand.label != e.label:
                    continue
                score = cosine(v, cv)
                if score > best_score:
                    best, best_score = cand, score
            if best is not None and best_score >= SIM_THRESHOLD:
                mapping[e.name] = best.name
                _record(best, [e.name, *e.aliases])
            else:
                still_new.append(e)
        unmatched = still_new

    for e in unmatched:
        mapping[e.name] = e.name
        _record(e, list(e.aliases))

    def canon(name: str) -> str:
        if is_user(name):
            return "User"
        if name in mapping:
            return mapping[name]
        hit = by_name.get(normalize_name(name))
        return hit.name if hit else name

    relations = [r.model_copy(update={"subject": canon(r.subject), "object": canon(r.object)})
                 for r in extraction.relations]
    return Resolution(entities=list(out.values()), relations=relations, mapping=mapping)

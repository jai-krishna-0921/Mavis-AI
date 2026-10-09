"""The Vault: what Mavis knows about one user, and the user's controls over it.

A plain Python service API for the web app's /api/v1 endpoints (no HTTP here). Every call takes the user id
and reads or changes only that user's rows: item ids are derived from content and resolved through the
caller's own graph, profile, signals and layer, so an id belonging to another user is simply not found.

Items have a kind (fact, signal, person, routine, profile, layer, suppression), the text, a trust level
(`user`: they told Mavis; `self`: their own mail, messages or calendar; `third_party`: only other people's
words) and the sources it came from. Controls:

- correct_item: the corrected text becomes a user-trust fact and wins over anything derived;
- forget_item: removes the item from the graph and vectors, optionally suppressing re-learning;
- forget_source: everything learned from one connector (third-party and self-authored);
- pause_connector: stop learning from a connector without deleting what is known;
- get_layer: the personal layer, every line with its sources and confirmed flag.

Every change refreshes the stored layer at once (stale lines are dropped) and schedules a rebuild.
"""

# ruff: noqa: E501

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from mavis.domain import timeutil
from mavis.domain.memory import Entity, Relation
from mavis.memory import controls, itemids, personal, personal_layer
from mavis.memory.names import is_user
from mavis.memory.personal import CORRECTION_PREFIX, SECTIONS, TIER_THIRD, TIER_USER, GraphItem
from mavis.memory.service import get_memory
from mavis.store.repo import personal as personal_repo
from mavis.store.repo import profile as profile_repo

if TYPE_CHECKING:
    from mavis.memory.service import MemoryService

log = structlog.get_logger()

KINDS = ("fact", "signal", "person", "routine", "profile", "layer", "suppression")
CONNECTOR_PREFIXES = {"gmail": ("gmail:",), "slack": ("slack:",), "calendar": ("calendar:",)}
MAX_CORRECTION = 300
MIN_SUBSTRING_FORGET = 4  # vectors are forgotten by text; shorter needles would take unrelated memories


class VaultError(ValueError):
    """The request cannot be carried out (unknown item, bad text). The message is safe to show."""


@dataclass
class VaultItem:
    id: str
    kind: str
    text: str
    trust: str  # user | self | third_party
    sources: list[str] = field(default_factory=list)
    connector: str = ""  # gmail | slack | calendar | "" (from what the user said)
    section: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def _trust(tier: str) -> str:
    return "third_party" if tier == TIER_THIRD else tier


def _connector(sources: list[str]) -> str:
    for s in sources:
        head = s.split(":", 1)[0]
        if head in CONNECTOR_PREFIXES:
            return head
    return ""


def _section_of(g: GraphItem) -> str:
    return personal.correction_section(g.source_ref)


def _fact_item(g: GraphItem) -> VaultItem:
    src = personal.source_label(g.source_ref)
    sources = [src] if src else []
    return VaultItem(id=g.id, kind="signal" if g.tier == TIER_THIRD else "fact", text=g.statement,
                     trust=_trust(g.tier), sources=sources, connector=_connector(sources),
                     section=_section_of(g), meta={"subject": g.subject, "relation": g.rel, "object": g.obj})


def _clean_text(text: str) -> str:
    out = personal_layer.clean(text)
    if len(out) < 3:
        raise VaultError("The correction is empty.")
    if len(out) > MAX_CORRECTION:
        raise VaultError(f"Keep a correction under {MAX_CORRECTION} characters.")
    return out


class Vault:
    def __init__(self, memory: MemoryService | None = None) -> None:
        self._memory = memory

    @property
    def memory(self) -> MemoryService:
        return self._memory or get_memory()

    # --- reading -------------------------------------------------------------------------------------

    async def list_items(self, user_id: int, kind: str, limit: int = 200) -> list[VaultItem]:
        """The user's items of one kind, newest knowledge last for graph kinds, most important first for the rest."""
        if kind not in KINDS:
            raise VaultError(f"Unknown kind: {kind}")
        if kind in ("fact", "signal"):
            items = [_fact_item(g) for g in await personal.graph_items(self.memory, user_id)]
            return [i for i in items if i.kind == kind][-limit:]
        if kind == "person":
            suppressed = await personal_repo.suppressed_keys(user_id)
            out = []
            for a in (await personal.aggregate_people(user_id, timeutil.now()))[:limit]:
                if itemids.person_id(a.key) in suppressed:
                    continue
                ev = personal.person_evidence(a, False)
                out.append(VaultItem(id=ev.id, kind="person", text=ev.text, trust=_trust(ev.tier),
                                     sources=ev.sources, connector=_connector(ev.sources),
                                     section="people", meta=ev.meta))
            return out
        if kind == "routine":
            return [VaultItem(id=e.id, kind="routine", text=e.text, trust=_trust(e.tier), sources=e.sources,
                              connector="calendar", section="routines")
                    for e in await personal.meeting_evidence(user_id)][:limit]
        if kind == "profile":
            return [VaultItem(id=e.id, kind="profile", text=e.text, trust="user", sources=["profile"],
                              section=e.section, meta=e.meta) for e in await personal.profile_evidence(user_id)]
        if kind == "layer":
            layer = await personal_layer.current(user_id)
            return [VaultItem(id=ln["id"], kind="layer", text=ln["text"], trust=_trust(ln["tier"]),
                              sources=ln["sources"], connector=_connector(ln["sources"]), section=ln["section"],
                              meta={"evidence": ln["evidence"], "confirmed": ln["confirmed"],
                                    "pinned": ln["pinned"]}) for ln in layer["lines"]][:limit]
        return [VaultItem(id=k, kind="suppression", text=label or k, trust="user")
                for k, label in await personal_repo.list_suppressions(user_id)][:limit]

    async def get_layer(self, user_id: int) -> dict[str, Any]:
        """The personal layer: version, when it was built and the lines (id, section, text, confirmed, tier,
        sources, the evidence item ids behind it, pinned for the user's own corrections)."""
        layer = await personal_layer.current(user_id)
        return {"version": layer["version"], "generated_at": layer.get("generated_at"),
                "phrased": layer.get("phrased", False),
                "sections": [{"key": s, "title": personal_layer.SECTION_TITLES[s]} for s in SECTIONS],
                "lines": [{k: ln[k] for k in ("id", "section", "text", "confirmed", "tier", "sources",
                                              "evidence", "pinned")} for ln in layer["lines"]],
                "paused": sorted(await controls.paused(user_id))}

    # --- changing ------------------------------------------------------------------------------------

    async def correct_item(self, user_id: int, item_id: str, text: str) -> VaultItem:
        """Replace an item's content with the user's own words. The result is a user-trust fact: it beats
        anything derived and triggers a rebuild of the layer. Returns the new fact."""
        text = _clean_text(text)
        kind, _ = itemids.split(item_id)
        memory = self.memory
        await memory.init()
        if kind == itemids.FACT:
            g = await self._find_fact(user_id, item_id)
            if len(g.statement) >= MIN_SUBSTRING_FORGET:
                await memory.vector.forget(user_id, g.statement)
            section = self._fact_section(g)
            ref = self._correction_ref(section or "fact", text)
            await memory.graph.upsert_relation(
                user_id, Relation(subject=g.subject, rel=g.rel, object=g.obj, statement=text, confidence=1.0),
                source_ref=ref)
            await memory.vector.add(user_id, [text], kind="fact", source_ref=ref)
            await self._after_change(user_id, replaced_lines=await self._lines_citing(user_id, {item_id}))
            return _fact_item(await self._find_fact(user_id, item_id))
        if kind == itemids.LAYER:
            return await self._correct_line(user_id, item_id, text)
        if kind == itemids.PERSON:
            key = itemids.split(item_id)[1]
            agg = next((a for a in await personal.aggregate_people(user_id, timeutil.now())
                        if a.key == key), None)
            if agg is None:
                raise VaultError("Item not found.")
            return await self._write_correction(user_id, "people", text, replaced=set(),
                                                about=Entity(name=agg.display, label="Person",
                                                             aliases=[agg.email] if agg.email else []),
                                                rel="KNOWS")
        if kind == itemids.ROUTINE:
            if not any(e.id == item_id for e in await personal.meeting_evidence(user_id)):
                raise VaultError("Item not found.")
            return await self._write_correction(user_id, "routines", text, replaced={item_id})
        if kind == itemids.PROFILE:
            await self._correct_profile(user_id, item_id, text)
            await self._after_change(user_id, replaced_lines=await self._lines_citing(user_id, {item_id}))
            return VaultItem(id=item_id, kind="profile", text=text, trust="user", sources=["profile"])
        raise VaultError("This item cannot be corrected.")

    async def forget_item(self, user_id: int, item_id: str, *, suppress: bool = False) -> int:
        """Forget one item everywhere (graph, vectors, signals, profile). With `suppress`, Mavis also stops
        learning it again from new mail and messages. Returns how many stored records were removed (0 when
        the item is not this user's)."""
        kind, _ = itemids.split(item_id)
        memory = self.memory
        await memory.init()
        removed = 0
        removed_ids: set[str] = {item_id}
        if kind == itemids.LAYER:
            line = next((ln for ln in (await personal_layer.current(user_id))["lines"] if ln["id"] == item_id), None)
            if line is None:
                return 0
            for ev_id in line["evidence"]:
                if itemids.split(ev_id)[0] == itemids.LOOP:  # loops are managed elsewhere: just hide it here
                    await personal_repo.suppress(user_id, ev_id, "hidden from the layer")
                    continue
                removed += await self._forget_evidence(user_id, ev_id, suppress)
                removed_ids.add(ev_id)
            if suppress:
                await personal_repo.suppress(user_id, item_id, line["text"])  # this wording does not come back
            await self._after_change(user_id, replaced_lines={item_id}, removed_evidence=removed_ids)
            return removed
        removed = await self._forget_evidence(user_id, item_id, suppress)
        await self._after_change(user_id, removed_evidence=removed_ids)
        return removed

    async def forget_source(self, user_id: int, connector: str) -> int:
        """Everything learned from one connector ("gmail", "slack", "calendar"): graph facts (third-party and
        self-authored), vectors, interaction and calendar signals. Returns the records removed."""
        if connector not in CONNECTOR_PREFIXES:
            raise VaultError(f"Unknown connector: {connector}")
        removed = 0
        for prefix in (*CONNECTOR_PREFIXES[connector], f"first_sync:{user_id}:{connector}"):
            removed += await self.memory.forget_source(user_id, prefix)
            removed += await personal_repo.delete_signals_by_source(user_id, prefix)
        await self._after_change(user_id)
        return removed

    async def pause_connector(self, user_id: int, connector: str, paused: bool = True) -> list[str]:
        """Stop (or resume) learning from a connector. Returns the paused connectors."""
        if connector not in controls.CONNECTORS:
            raise VaultError(f"Unknown connector: {connector}")
        return sorted(await controls.set_paused(user_id, connector, paused))

    async def unsuppress(self, user_id: int, key: str) -> bool:
        removed = bool(await personal_repo.unsuppress(user_id, key))
        if removed:
            await personal_layer.schedule(user_id)
        return removed

    # --- internals -----------------------------------------------------------------------------------

    @staticmethod
    def _correction_ref(section: str, text: str) -> str:
        return f"{CORRECTION_PREFIX}{section}:{itemids.digest(text)}"

    @staticmethod
    def _fact_section(g: GraphItem) -> str:
        section = personal.correction_section(g.source_ref)
        if section:
            return section
        if is_user(g.subject) and g.rel in personal._REL_SECTION:
            return personal._REL_SECTION[g.rel]
        return ""

    async def _find_fact(self, user_id: int, item_id: str) -> GraphItem:
        for g in await personal.graph_items(self.memory, user_id):
            if g.id == item_id:
                return g
        raise VaultError("Item not found.")

    async def _lines_citing(self, user_id: int, evidence_ids: set[str]) -> set[str]:
        layer = await personal_layer.current(user_id)
        return {ln["id"] for ln in layer["lines"] if evidence_ids & set(ln["evidence"])}

    async def _correct_line(self, user_id: int, line_id: str, text: str) -> VaultItem:
        layer = await personal_layer.current(user_id)
        line = next((ln for ln in layer["lines"] if ln["id"] == line_id), None)
        if line is None:
            raise VaultError("Item not found.")
        gathered = {e.id: e for e in await personal.gather(self.memory, user_id)}
        # a derived fact the user says is wrong stays out; counts and patterns are not facts to suppress
        replaced = {i for i in line["evidence"]
                    if itemids.split(i)[0] == itemids.FACT and (e := gathered.get(i)) and e.tier != TIER_USER}
        await personal_repo.suppress(user_id, line_id, line["text"])
        return await self._write_correction(user_id, line["section"], text, replaced=replaced,
                                            dropped_lines={line_id})

    async def _write_correction(self, user_id: int, section: str, text: str, *, replaced: set[str],
                                about: Entity | None = None, rel: str = "ABOUT",
                                dropped_lines: set[str] | None = None) -> VaultItem:
        memory = self.memory
        for item_id in replaced:
            kind, _ = itemids.split(item_id)
            if kind == itemids.FACT:
                g = await self._find_fact(user_id, item_id)
                await memory.graph.forget_fact(user_id, g.subject, g.rel, g.obj)
                if len(g.statement) >= MIN_SUBSTRING_FORGET:
                    await memory.vector.forget(user_id, g.statement)
            await personal_repo.suppress(user_id, item_id, "replaced by a correction")
        ref = self._correction_ref(section, text)
        entity = about or Entity(name=" ".join(text.split()[:8])[:60], label="Topic")
        await memory.graph.upsert_entity(user_id, entity)
        relation = Relation(subject="User", rel=rel, object=entity.name, statement=text, confidence=1.0)
        await memory.graph.upsert_relation(user_id, relation, source_ref=ref)
        await memory.vector.add(user_id, [text], kind="fact", source_ref=ref)
        fact = itemids.fact_id("User", relation.rel, entity.name)
        await self._after_change(user_id, replaced_lines=dropped_lines or set(), removed_evidence=replaced)
        if (ev := next((e for e in await personal.gather(memory, user_id) if e.id == fact), None)) is not None:
            await personal_layer.patch(user_id, add=[personal_layer.make_line(ev.section, ev.text, [ev], pinned=True)])
        return _fact_item(await self._find_fact(user_id, fact))

    async def _correct_profile(self, user_id: int, item_id: str, text: str) -> None:
        match = next((e for e in await personal.profile_evidence(user_id) if e.id == item_id), None)
        if match is None:
            raise VaultError("Item not found.")
        field_, value = str(match.meta["field"]), str(match.meta["value"])
        card = await profile_repo.get(user_id)
        data = card.model_dump()
        if field_ in ("goals", "key_people", "routines", "dislikes", "other"):
            data[field_] = [text if v == value else v for v in data[field_]]
        elif field_ == "brevity":
            if text.casefold() not in ("short", "normal"):
                raise VaultError("Brevity is either short or normal.")
            data[field_] = text.casefold()
        elif field_ in ("name", "tone"):
            data[field_] = text
        else:
            raise VaultError("This item cannot be corrected.")
        await profile_repo.save(user_id, type(card).model_validate(data))

    async def _forget_evidence(self, user_id: int, item_id: str, suppress: bool) -> int:
        """Forget one piece of evidence by its item id; 0 when it is not this user's."""
        kind, key = itemids.split(item_id)
        memory = self.memory
        removed = 0
        if kind == itemids.FACT:
            g = next((x for x in await personal.graph_items(memory, user_id) if x.id == item_id), None)
            if g is None:
                return 0
            removed = await memory.graph.forget_fact(user_id, g.subject, g.rel, g.obj)
            if len(g.statement) >= MIN_SUBSTRING_FORGET:
                removed += await memory.vector.forget(user_id, g.statement)
            if suppress:
                await personal_repo.suppress(user_id, item_id, g.statement)
        elif kind == itemids.PERSON:
            rows = [r for r in await personal_repo.signals(user_id, "interaction") if r.key == key]
            names = {n for r in rows for n in ((r.meta or {}).get("name"), (r.meta or {}).get("email"), r.label) if n}
            if "@" in key:
                names.add(key)
            removed = await personal_repo.delete_signals_by_key(user_id, "interaction", key)
            for name in sorted(names, key=len, reverse=True):
                removed += await memory.graph.forget_entity(user_id, name)
            # memories are found by text: the full name, the address, and the given name people write with
            given = {n.split()[0] for n in names if "@" not in n and len(n.split()) >= 2}
            for needle in sorted(names | given, key=len, reverse=True):
                if len(needle) >= MIN_SUBSTRING_FORGET:
                    removed += await memory.vector.forget(user_id, needle)
            if suppress:
                await personal_repo.suppress(user_id, item_id, ", ".join(sorted(names)[:2]))
                for name in names:
                    if "@" not in name:
                        await personal_repo.suppress(user_id, itemids.entity_id(name), name)
        elif kind == itemids.ROUTINE:
            ids = [r.id for r in await personal_repo.signals(user_id, "meeting")
                   if itemids.routine_id(r.key, int((r.meta or {}).get("wd", 0)), str((r.meta or {}).get("hm", "")))
                   == item_id]
            removed = await personal_repo.delete_signals(user_id, ids)
            if suppress or not ids:
                await personal_repo.suppress(user_id, item_id, "recurring meeting")
        elif kind == itemids.PROFILE:
            match = next((e for e in await personal.profile_evidence(user_id) if e.id == item_id), None)
            if match is not None:
                card = await profile_repo.get(user_id)
                data = card.model_dump()
                field_, value = str(match.meta["field"]), str(match.meta["value"])
                if field_ in ("goals", "key_people", "routines", "dislikes", "other"):
                    data[field_] = [v for v in data[field_] if v != value]
                elif field_ in ("tone", "brevity"):
                    data[field_] = None
                else:
                    return 0
                await profile_repo.save(user_id, type(card).model_validate(data))
                removed = 1
        elif kind == itemids.STYLE:
            removed = await personal_repo.delete_signals_by_key(user_id, "style", "msg")
            if suppress:
                await personal_repo.suppress(user_id, item_id, "writing style")
        elif kind == "suppression":
            return 0
        return removed

    async def _after_change(self, user_id: int, *, replaced_lines: set[str] | None = None,
                            removed_evidence: set[str] | None = None) -> None:
        """Make the stored layer honest at once and ask for a rebuild."""
        self.memory.invalidate(user_id)
        await self.prune(user_id, drop_lines=replaced_lines or set(), drop_evidence=removed_evidence or set())
        await personal_layer.schedule(user_id)

    async def prune(self, user_id: int, *, drop_lines: set[str] | None = None,
                    drop_evidence: set[str] | None = None) -> int:
        """Drop stored lines that are no longer true: named lines, lines citing removed evidence, and lines
        whose evidence has changed or gone since they were written. Saves a new version if any changed."""
        layer = await personal_layer.current(user_id)
        if not layer["lines"]:
            return 0
        evidence = {e.id: e for e in await personal.gather(self.memory, user_id)}
        keep = []
        for ln in layer["lines"]:
            cited = ln["evidence"]
            stale = (ln["id"] in (drop_lines or set()) or bool(set(cited) & (drop_evidence or set()))
                     or any(i not in evidence for i in cited)
                     or personal_layer.stamp([evidence[i] for i in cited if i in evidence]) != ln.get("evh"))
            if not stale:
                keep.append(ln)
        dropped = len(layer["lines"]) - len(keep)
        if dropped:
            await personal_layer.patch(user_id, keep=keep)
        return dropped


_vault: Vault | None = None


def get_vault() -> Vault:
    global _vault
    if _vault is None:
        _vault = Vault()
    return _vault

"""Knowledge graph of the user's world behind a port.

Neo4j when NEO4J_URI is set (watch it grow live in the browser during the demo);
otherwise SQL tables with identical semantics:
- fixed node-label and relation vocabularies (see mavis.domain.memory)
- edges carry valid_from/valid_to; valid_to NULL == current
- single-valued relations (WORKS_AT…) close their predecessor instead of deleting it
"""

# ruff: noqa: E501
from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.memory import SINGLE_VALUED_RELS, Entity, Relation
from mavis.memory.names import USER_KEY, is_user, node_key, normalize_name, sanitize_label, sanitize_rel
from mavis.store import db as dbm
from mavis.store.models import GraphEdge, GraphNode

THIRD_PARTY_PREFIX = "tp:"  # source_ref namespace of edges learned from third-party records (mail, Slack)


def third_party_ref(ref: str) -> str:
    """The edge source_ref for a fact learned from a third-party record. Only our own record learning
    writes it; a user's own words never carry this prefix, so it is the persisted trust marker."""
    return (THIRD_PARTY_PREFIX + ref)[:200]


def is_third_party(source_ref: str | None) -> bool:
    return bool(source_ref) and str(source_ref).startswith(THIRD_PARTY_PREFIX)


SELF_AUTHORED_PREFIX = "sa:"  # source_ref namespace of facts learned from the user's own mail and messages


def self_authored_ref(ref: str) -> str:
    """The edge source_ref for a fact learned from something the user wrote (mail they sent, their own Slack
    messages). Medium trust: their words, but not an instruction to Mavis, so never the user's trust level."""
    return (SELF_AUTHORED_PREFIX + ref)[:200]


def is_self_authored(source_ref: str | None) -> bool:
    return bool(source_ref) and str(source_ref).startswith(SELF_AUTHORED_PREFIX)


def is_derived(source_ref: str | None) -> bool:
    """Learned from a record (third-party or self-authored), not from what the user told Mavis."""
    return is_third_party(source_ref) or is_self_authored(source_ref)


def source_rank(source_ref: str | None) -> int:
    """Who wins a conflict: the user's own statements (2) over their own authored records (1) over
    third-party records (0). A lower rank never rewrites or ends an edge of a higher one."""
    if is_third_party(source_ref):
        return 0
    return 1 if is_self_authored(source_ref) else 2


class Fact(str):
    """A recalled edge statement. It is a plain str everywhere; `source_ref` says where it was learned
    and `third_party` whether recall must hand it to the model as untrusted data."""

    source_ref: str

    def __new__(cls, text: str, source_ref: str = "") -> Fact:
        obj = super().__new__(cls, text)
        obj.source_ref = source_ref or ""
        return obj

    @property
    def third_party(self) -> bool:
        return is_third_party(self.source_ref)

    @property
    def self_authored(self) -> bool:
        return is_self_authored(self.source_ref)

    @property
    def origin(self) -> str:
        if self.third_party:
            return self.source_ref[len(THIRD_PARTY_PREFIX):]
        return self.source_ref[len(SELF_AUTHORED_PREFIX):] if self.self_authored else self.source_ref


class GraphStore(Protocol):
    async def init(self) -> None: ...
    async def upsert_entity(self, user_id: int, entity: Entity) -> str: ...
    async def upsert_relation(self, user_id: int, rel: Relation, source_ref: str = "",
                              at: datetime | None = None) -> None: ...
    async def neighborhood(self, user_id: int, names: list[str], hops: int = 2, limit: int = 25) -> list[str]: ...
    async def entities(self, user_id: int) -> list[Entity]: ...
    async def dump(self, user_id: int) -> list[dict]: ...
    async def forget(self, user_id: int, needle: str) -> int: ...
    async def delete_user(self, user_id: int) -> int: ...

    async def forget_source(self, user_id: int, prefix: str) -> int: ...
    async def forget_fact(self, user_id: int, subject: str, rel: str, obj: str) -> int: ...
    async def forget_entity(self, user_id: int, name: str) -> int: ...
    async def merge_entities(self, user_id: int, keep: str, drop: str, label: str) -> None: ...


def _now() -> datetime:
    return timeutil.now()  # the injectable clock: recency decay follows moved time in tests and demos


def edge_score(confidence: float, valid_from: datetime, now: datetime) -> float:
    """Recency x confidence ranking shared by every backend (30-day half-weight decay)."""
    if valid_from.tzinfo is None:
        valid_from = valid_from.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    age_days = max((now - valid_from).total_seconds() / 86400, 0.0)
    return confidence * 1 / (1 + age_days / 30)


def escape_like(needle: str) -> str:
    return needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SqliteGraphStore:
    """SQL-backed graph (works on SQLite and Postgres). Fine for one user's world."""

    async def init(self) -> None:
        return None

    # --- helpers -----------------------------------------------------------------

    async def _ensure_user(self, s: AsyncSession, user_id: int) -> str:
        if not await s.scalar(select(GraphNode.id).where(GraphNode.user_id == user_id, GraphNode.key == USER_KEY)):
            s.add(GraphNode(user_id=user_id, key=USER_KEY, name="User", label="User", aliases=[]))
            await s.flush()
        return USER_KEY

    async def _find_key(self, s: AsyncSession, user_id: int, name: str) -> str | None:
        if is_user(name):
            return await self._ensure_user(s, user_id)
        norm = normalize_name(name)
        if not norm:
            return None
        nodes = list(await s.scalars(select(GraphNode).where(GraphNode.user_id == user_id)))
        for n in nodes:
            if normalize_name(n.name) == norm:
                return n.key
        for n in nodes:
            if norm in {normalize_name(a) for a in (n.aliases or [])}:
                return n.key
        return None

    async def _upsert_entity(self, s: AsyncSession, user_id: int, entity: Entity) -> str:
        if is_user(entity.name):
            return await self._ensure_user(s, user_id)
        label = sanitize_label(entity.label)
        key = node_key(label, entity.name)
        node = await s.scalar(select(GraphNode).where(GraphNode.user_id == user_id, GraphNode.key == key))
        new_aliases = {a.strip() for a in entity.aliases if a.strip() and normalize_name(a) != normalize_name(entity.name)}
        if node is None:
            s.add(GraphNode(user_id=user_id, key=key, name=entity.name.strip(), label=label, aliases=sorted(new_aliases)))
        else:
            node.aliases = sorted(set(node.aliases or []) | new_aliases)
            node.last_seen_at = _now()
        await s.flush()
        return key

    async def _key_or_create(self, s: AsyncSession, user_id: int, name: str) -> str:
        return await self._find_key(s, user_id, name) or await self._upsert_entity(
            s, user_id, Entity(name=name, label="Topic")
        )

    # --- port ----------------------------------------------------------------------

    async def upsert_entity(self, user_id: int, entity: Entity) -> str:
        async with dbm.Session() as s:
            key = await self._upsert_entity(s, user_id, entity)
            await s.commit()
            return key

    async def upsert_relation(self, user_id: int, rel: Relation, source_ref: str = "",
                              at: datetime | None = None) -> None:
        """`at`: when the fact was SAID (a LEARN deferred by a busy model runs long after its turn). A
        single-valued fact supersedes the current one only from that time on; one older than the current
        edge is recorded as history (a closed interval ending where the newer fact began)."""
        r = sanitize_rel(rel.rel)
        when = timeutil.ensure_utc(at) if at else _now()
        async with dbm.Session() as s:
            src = await self._key_or_create(s, user_id, rel.subject)
            dst = await self._key_or_create(s, user_id, rel.object)
            current = list(
                await s.scalars(
                    select(GraphEdge).where(
                        GraphEdge.user_id == user_id, GraphEdge.src_key == src,
                        GraphEdge.rel == r, GraphEdge.valid_to.is_(None),
                    )
                )
            )
            same = [e for e in current if e.dst_key == dst]
            rank = source_rank(source_ref)
            if rank < 2:
                # A record (mail or Slack, third-party or self-authored) never overwrites or ends what the user
                # said, and a third-party record never rewrites what the user's own records say: those edges
                # keep their statement, source and validity. A conflicting single-valued fact from a lower
                # source is not recorded at all.
                if same and source_rank(same[0].source_ref) > rank:
                    return
                if not same and r in SINGLE_VALUED_RELS and any(
                        source_rank(e.source_ref) > rank for e in current):
                    return
            if same:
                edge = same[0]
                edge.statement = rel.statement
                edge.confidence = max(edge.confidence, rel.confidence)
                edge.source_ref = source_ref or edge.source_ref
            else:
                valid_to = None
                if r in SINGLE_VALUED_RELS:
                    newer = [timeutil.ensure_utc(e.valid_from) for e in current
                             if timeutil.ensure_utc(e.valid_from) > when]
                    if newer:
                        valid_to = min(newer)  # said before the current fact: history, not current
                        past = await s.scalars(select(GraphEdge).where(
                            GraphEdge.user_id == user_id, GraphEdge.src_key == src, GraphEdge.rel == r,
                            GraphEdge.dst_key == dst, GraphEdge.valid_to.is_not(None)))
                        if any(timeutil.ensure_utc(e.valid_from) == when for e in past):
                            return  # a retry of a fact already recorded as history
                    else:
                        for e in current:
                            e.valid_to = when
                s.add(GraphEdge(user_id=user_id, src_key=src, rel=r, dst_key=dst, statement=rel.statement,
                                confidence=rel.confidence, source_ref=source_ref, valid_from=when,
                                valid_to=valid_to))
            await s.commit()

    async def neighborhood(self, user_id: int, names: list[str], hops: int = 2, limit: int = 25) -> list[str]:
        hops = max(1, min(hops, 3))
        async with dbm.Session() as s:
            seeds = {k for n in names if (k := await self._find_key(s, user_id, n))}
            if not seeds:
                return []
            frontier, seen, edges = set(seeds), set(seeds), {}
            for _ in range(hops):
                if not frontier:
                    break
                rows = await s.scalars(
                    select(GraphEdge).where(
                        GraphEdge.user_id == user_id, GraphEdge.valid_to.is_(None),
                        or_(GraphEdge.src_key.in_(frontier), GraphEdge.dst_key.in_(frontier)),
                    )
                )
                nxt: set[str] = set()
                for e in rows:
                    edges[e.id] = e
                    for k in (e.src_key, e.dst_key):
                        if k not in seen:
                            seen.add(k)
                            nxt.add(k)
                frontier = nxt
        candidates = sorted(edges.values(), key=lambda e: e.valid_from, reverse=True)[: limit * 4]
        now = _now()
        ranked = sorted(candidates, key=lambda e: edge_score(e.confidence, e.valid_from, now), reverse=True)
        return [Fact(e.statement, e.source_ref) for e in ranked[:limit]]

    async def entities(self, user_id: int) -> list[Entity]:
        async with dbm.Session() as s:
            rows = await s.scalars(
                select(GraphNode).where(GraphNode.user_id == user_id, GraphNode.label != "User").order_by(GraphNode.id)
            )
            return [Entity(name=n.name, label=n.label, aliases=list(n.aliases or [])) for n in rows]

    async def dump(self, user_id: int) -> list[dict]:
        async with dbm.Session() as s:
            names = {n.key: n.name for n in await s.scalars(select(GraphNode).where(GraphNode.user_id == user_id))}
            rows = await s.scalars(
                select(GraphEdge).where(GraphEdge.user_id == user_id, GraphEdge.valid_to.is_(None))
                .order_by(GraphEdge.valid_from, GraphEdge.id)
            )
            return [
                {"subject": names.get(e.src_key, e.src_key), "relation": e.rel,
                 "object": names.get(e.dst_key, e.dst_key), "statement": e.statement,
                 "source_ref": e.source_ref or ""}
                for e in rows
            ]

    async def forget(self, user_id: int, needle: str) -> int:
        n = needle.strip()
        if not n:
            return 0
        like = f"%{escape_like(n)}%"
        async with dbm.Session() as s:
            node_keys = list(
                await s.scalars(
                    select(GraphNode.key).where(
                        GraphNode.user_id == user_id, GraphNode.label != "User", GraphNode.name.ilike(like, escape="\\")
                    )
                )
            )
            res = await s.execute(
                delete(GraphEdge).where(
                    GraphEdge.user_id == user_id,
                    or_(GraphEdge.statement.ilike(like, escape="\\"), GraphEdge.src_key.in_(node_keys),
                        GraphEdge.dst_key.in_(node_keys)),
                )
            )
            if node_keys:
                await s.execute(delete(GraphNode).where(GraphNode.user_id == user_id, GraphNode.key.in_(node_keys)))
            await s.commit()
            return res.rowcount or 0

    async def delete_user(self, user_id: int) -> int:
        """Remove the user's whole graph (account deletion). Returns the number of nodes removed."""
        async with dbm.Session() as s:
            await s.execute(delete(GraphEdge).where(GraphEdge.user_id == user_id))
            res = await s.execute(delete(GraphNode).where(GraphNode.user_id == user_id))
            await s.commit()
            return res.rowcount or 0

    async def forget_source(self, user_id: int, prefix: str) -> int:
        """Delete every edge learned from records (third-party or self-authored) whose reference starts with
        `prefix` ("gmail:", "slack:"), then the entities left with no edge at all. Returns the edges removed."""
        if not prefix.strip():
            return 0
        likes = [escape_like(ns + prefix) + "%" for ns in (THIRD_PARTY_PREFIX, SELF_AUTHORED_PREFIX)]
        async with dbm.Session() as s:
            edges = list(await s.scalars(select(GraphEdge).where(
                GraphEdge.user_id == user_id,
                or_(*(GraphEdge.source_ref.like(like, escape="\\") for like in likes)))))
            touched = {k for e in edges for k in (e.src_key, e.dst_key)} - {USER_KEY}
            for e in edges:
                await s.delete(e)
            await s.flush()
            for key in touched:
                left = await s.scalar(select(GraphEdge.id).where(
                    GraphEdge.user_id == user_id, or_(GraphEdge.src_key == key, GraphEdge.dst_key == key)).limit(1))
                if left is None:
                    await s.execute(delete(GraphNode).where(GraphNode.user_id == user_id, GraphNode.key == key))
            await s.commit()
            return len(edges)

    async def _drop_orphans(self, s: AsyncSession, user_id: int, keys: set[str]) -> None:
        for key in keys - {USER_KEY}:
            left = await s.scalar(select(GraphEdge.id).where(
                GraphEdge.user_id == user_id, or_(GraphEdge.src_key == key, GraphEdge.dst_key == key)).limit(1))
            if left is None:
                await s.execute(delete(GraphNode).where(GraphNode.user_id == user_id, GraphNode.key == key))

    async def forget_fact(self, user_id: int, subject: str, rel: str, obj: str) -> int:
        """Delete the edges (current and history) of one exact triple, then the entities left with no edge.
        Returns the edges removed."""
        r = sanitize_rel(rel)
        async with dbm.Session() as s:
            src, dst = await self._find_key(s, user_id, subject), await self._find_key(s, user_id, obj)
            if src is None or dst is None:
                return 0
            res = await s.execute(delete(GraphEdge).where(
                GraphEdge.user_id == user_id, GraphEdge.src_key == src, GraphEdge.rel == r,
                GraphEdge.dst_key == dst))
            await self._drop_orphans(s, user_id, {src, dst})
            await s.commit()
            return res.rowcount or 0

    async def forget_entity(self, user_id: int, name: str) -> int:
        """Delete one entity (exact name or alias, never the User) and every edge it has. Returns the edges
        removed."""
        if is_user(name):
            return 0
        async with dbm.Session() as s:
            key = await self._find_key(s, user_id, name)
            if key is None:
                return 0
            others = {k for e in await s.scalars(select(GraphEdge).where(
                GraphEdge.user_id == user_id, or_(GraphEdge.src_key == key, GraphEdge.dst_key == key)))
                for k in (e.src_key, e.dst_key)}
            res = await s.execute(delete(GraphEdge).where(
                GraphEdge.user_id == user_id, or_(GraphEdge.src_key == key, GraphEdge.dst_key == key)))
            await s.execute(delete(GraphNode).where(GraphNode.user_id == user_id, GraphNode.key == key))
            await self._drop_orphans(s, user_id, others - {key})
            await s.commit()
            return res.rowcount or 0

    async def merge_entities(self, user_id: int, keep: str, drop: str, label: str) -> None:
        lab = sanitize_label(label)
        keep_key, drop_key = node_key(lab, keep), node_key(lab, drop)
        if keep_key == drop_key:
            return
        async with dbm.Session() as s:
            k = await s.scalar(select(GraphNode).where(GraphNode.user_id == user_id, GraphNode.key == keep_key))
            d = await s.scalar(select(GraphNode).where(GraphNode.user_id == user_id, GraphNode.key == drop_key))
            if k is None or d is None:
                return
            k.aliases = sorted(set(k.aliases or []) | set(d.aliases or []) | {d.name})
            edges = list(await s.scalars(select(GraphEdge).where(
                GraphEdge.user_id == user_id, or_(GraphEdge.src_key == drop_key, GraphEdge.dst_key == drop_key))))
            for e in edges:
                if e.src_key == drop_key:
                    e.src_key = keep_key
                if e.dst_key == drop_key:
                    e.dst_key = keep_key
            await s.delete(d)
            await s.flush()
            # drop self-loops and duplicate current edges created by the repoint
            seen: set[tuple[str, str, str]] = set()
            current = await s.scalars(select(GraphEdge).where(
                GraphEdge.user_id == user_id, GraphEdge.valid_to.is_(None)).order_by(GraphEdge.valid_from.desc()))
            for e in current:
                sig = (e.src_key, e.rel, e.dst_key)
                if e.src_key == e.dst_key or sig in seen:
                    await s.delete(e)
                else:
                    seen.add(sig)
            await s.commit()


def make_graph() -> GraphStore:
    s = get_settings()
    if s.neo4j_uri:
        from mavis.memory.neo4j_graph import Neo4jGraphStore

        return Neo4jGraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    return SqliteGraphStore()

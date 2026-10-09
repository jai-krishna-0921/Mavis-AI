"""Neo4j GraphStore. Labels/relation types are interpolated only after being
sanitised to the fixed vocabulary; every value travels as a parameter.

Nodes: (:Entity:<Label> {user_id, key, name, label, norm, aliases[], norm_aliases[], created_at, last_seen_at})
Edges: [:<REL> {statement, confidence, source_ref, valid_from, valid_to}]
"""

# ruff: noqa: E501
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mavis.domain import timeutil
from mavis.domain.memory import SINGLE_VALUED_RELS, Entity, Relation
from mavis.memory.graph import Fact, edge_score
from mavis.memory.names import USER_KEY, is_user, node_key, normalize_name, sanitize_label, sanitize_rel

_DEDUPE = "reduce(acc = [], a IN coalesce(n.{f}, []) + ${p} | CASE WHEN a IN acc THEN acc ELSE acc + a END)"

Q_INDEX = "CREATE INDEX entity_user_key IF NOT EXISTS FOR (n:Entity) ON (n.user_id, n.key)"
Q_ENSURE_USER = (
    "MERGE (n:Entity:User {user_id:$u, key:$key}) "
    "ON CREATE SET n.name='User', n.label='User', n.norm='user', n.aliases=[], n.norm_aliases=[], "
    "n.created_at=datetime()"
)
Q_FIND_KEY = (
    "MATCH (n:Entity {user_id:$u}) WHERE n.norm = $norm OR $norm IN coalesce(n.norm_aliases, []) "
    "RETURN n.key AS key ORDER BY CASE WHEN n.norm = $norm THEN 0 ELSE 1 END LIMIT 1"
)
Q_ENTITIES = (
    "MATCH (n:Entity {user_id:$u}) WHERE n.label <> 'User' "
    "RETURN n.name AS name, n.label AS label, coalesce(n.aliases, []) AS aliases ORDER BY n.created_at"
)
Q_DUMP = (
    "MATCH (a:Entity {user_id:$u})-[r]->(b:Entity {user_id:$u}) WHERE r.valid_to IS NULL "
    "RETURN a.name AS subject, type(r) AS relation, b.name AS object, r.statement AS statement, "
    "coalesce(r.source_ref, '') AS source_ref ORDER BY r.valid_from"
)
Q_FORGET_EDGES = (
    "MATCH (:Entity {user_id:$u})-[r]->() WHERE toLower(r.statement) CONTAINS toLower($n) "
    "WITH collect(r) AS rs FOREACH (x IN rs | DELETE x) RETURN size(rs) AS c"
)
Q_FORGET_NODES = (
    "MATCH (n:Entity {user_id:$u}) WHERE n.label <> 'User' AND toLower(n.name) CONTAINS toLower($n) "
    "OPTIONAL MATCH (n)-[r]-() WITH n, count(r) AS c DETACH DELETE n RETURN coalesce(sum(c), 0) AS c"
)
Q_DROP_EDGES = (
    "MATCH (d:Entity {user_id:$u, key:$drop})-[r]-(o:Entity) "
    "RETURN type(r) AS t, startNode(r) = d AS outgoing, o.key AS other, properties(r) AS props"
)
Q_ADD_ALIASES = (
    "MATCH (n:Entity {user_id:$u, key:$key}) SET n.aliases = " + _DEDUPE.format(f="aliases", p="aliases")
    + ", n.norm_aliases = " + _DEDUPE.format(f="norm_aliases", p="norm_aliases")
)
Q_KEY_EXISTS = (
    "MATCH (n:Entity {user_id:$u, key:$key}) "
    "RETURN n.key AS key, n.name AS name, n.aliases AS aliases LIMIT 1"
)
Q_CURRENT_EDGES = (
    "MATCH (a:Entity {user_id:$u})-[r]->(b:Entity {user_id:$u}) WHERE r.valid_to IS NULL "
    "RETURN elementId(r) AS id, a.key AS src, type(r) AS rel, b.key AS dst ORDER BY r.valid_from DESC"
)
Q_DELETE_EDGES = "MATCH (:Entity {user_id:$u})-[r]->() WHERE elementId(r) IN $ids DELETE r"
Q_DELETE_NODE = "MATCH (n:Entity {user_id:$u, key:$key}) DETACH DELETE n"


def q_upsert_entity(label: str) -> str:
    lab = sanitize_label(label)
    return (
        "MERGE (n:Entity {user_id:$u, key:$key}) "
        "ON CREATE SET n.name=$name, n.label=$label_name, n.norm=$norm, n.created_at=datetime(), "
        "n.aliases=[], n.norm_aliases=[] "
        f"SET n:{lab}, n.last_seen_at=datetime(), "
        "n.aliases = " + _DEDUPE.format(f="aliases", p="aliases") + ", "
        "n.norm_aliases = " + _DEDUPE.format(f="norm_aliases", p="norm_aliases")
    )


def q_close_single_valued(rel: str) -> str:
    r = sanitize_rel(rel)
    return (
        f"MATCH (a:Entity {{user_id:$u, key:$src}})-[r:{r}]->(b:Entity) "
        "WHERE r.valid_to IS NULL AND b.key <> $dst SET r.valid_to = datetime($at)"
    )


def q_newer_single_valued(rel: str) -> str:
    """Start of the oldest current edge that began after the fact being written (None when none did)."""
    r = sanitize_rel(rel)
    return (
        f"MATCH (a:Entity {{user_id:$u, key:$src}})-[r:{r}]->(b:Entity) "
        "WHERE r.valid_to IS NULL AND b.key <> $dst AND r.valid_from > datetime($at) "
        "RETURN min(r.valid_from) AS vf"
    )


def q_update_current_edge(rel: str) -> str:
    r = sanitize_rel(rel)
    return (
        f"MATCH (a:Entity {{user_id:$u, key:$src}})-[r:{r}]->(b:Entity {{user_id:$u, key:$dst}}) "
        "WHERE r.valid_to IS NULL "
        "SET r.statement=$statement, "
        "r.confidence = CASE WHEN r.confidence > $confidence THEN r.confidence ELSE $confidence END, "
        "r.source_ref = CASE WHEN $source_ref = '' THEN coalesce(r.source_ref, '') ELSE $source_ref END "
        "RETURN count(r) AS c"
    )


def q_create_edge(rel: str) -> str:
    r = sanitize_rel(rel)
    return (
        "MATCH (a:Entity {user_id:$u, key:$src}), (b:Entity {user_id:$u, key:$dst}) "
        f"CREATE (a)-[r:{r} {{statement:$statement, confidence:$confidence, source_ref:$source_ref, "
        "valid_from:datetime($at), valid_to:CASE WHEN $to IS NULL THEN null ELSE datetime($to) END}]->(b)"
    )


def q_neighborhood(hops: int) -> str:
    h = max(1, min(int(hops), 3))
    return (
        "MATCH (s:Entity {user_id:$u}) WHERE s.key IN $keys "
        f"MATCH p=(s)-[*1..{h}]-(:Entity) WHERE all(r IN relationships(p) WHERE r.valid_to IS NULL) "
        "UNWIND relationships(p) AS r WITH DISTINCT r "
        "RETURN r.statement AS st, r.confidence AS conf, r.valid_from AS vf, "
        "coalesce(r.source_ref, '') AS src "
        "ORDER BY r.valid_from DESC LIMIT $limit"
    )


def _to_dt(v: Any) -> datetime:
    if v is None:
        return timeutil.now()
    if hasattr(v, "to_native"):
        v = v.to_native()
    return v if v.tzinfo else v.replace(tzinfo=UTC)


def rank_candidates(rows: list[dict], limit: int, now: datetime | None = None) -> list[str]:
    """Same ranking as SqliteGraphStore: score = confidence x recency decay."""
    now = now or timeutil.now()
    scored = sorted(
        (r for r in rows if r.get("st")),
        key=lambda r: edge_score(float(r.get("conf") or 0.0), _to_dt(r.get("vf")), now),
        reverse=True,
    )
    return [Fact(r["st"], r.get("src") or "") for r in scored[:limit]]


def plan_dedupe(edges: list[dict]) -> list[str]:
    """Ids of current edges to delete: self-loops and duplicates (input is newest-first)."""
    seen: set[tuple[str, str, str]] = set()
    doomed: list[str] = []
    for e in edges:
        sig = (e["src"], e["rel"], e["dst"])
        if e["src"] == e["dst"] or sig in seen:
            doomed.append(e["id"])
        else:
            seen.add(sig)
    return doomed


def _recreate_edge(rel_type: str) -> str:
    r = sanitize_rel(rel_type)
    return (
        "MATCH (a:Entity {user_id:$u, key:$src}), (b:Entity {user_id:$u, key:$dst}) "
        f"CREATE (a)-[r:{r}]->(b) SET r = $props"
    )


class Neo4jGraphStore:
    def __init__(self, uri: str, user: str, password: str, driver: Any = None) -> None:
        if driver is None:
            from neo4j import AsyncGraphDatabase

            driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
        self._driver = driver

    async def _run(self, query: str, **params: Any) -> list[dict]:
        async with self._driver.session() as s:
            res = await s.run(query, **params)
            return [r.data() async for r in res]

    async def init(self) -> None:
        await self._run(Q_INDEX)

    async def _key_for(self, user_id: int, name: str) -> str:
        if is_user(name):
            await self._run(Q_ENSURE_USER, u=user_id, key=USER_KEY)
            return USER_KEY
        rows = await self._run(Q_FIND_KEY, u=user_id, norm=normalize_name(name))
        if rows:
            return rows[0]["key"]
        return await self.upsert_entity(user_id, Entity(name=name, label="Topic"))

    async def upsert_entity(self, user_id: int, entity: Entity) -> str:
        if is_user(entity.name):
            await self._run(Q_ENSURE_USER, u=user_id, key=USER_KEY)
            return USER_KEY
        label = sanitize_label(entity.label)
        key = node_key(label, entity.name)
        aliases = [a.strip() for a in entity.aliases if a.strip() and normalize_name(a) != normalize_name(entity.name)]
        await self._run(
            q_upsert_entity(label), u=user_id, key=key, name=entity.name.strip(), label_name=label,
            norm=normalize_name(entity.name), aliases=aliases, norm_aliases=[normalize_name(a) for a in aliases],
        )
        return key

    async def upsert_relation(self, user_id: int, rel: Relation, source_ref: str = "",
                              at: datetime | None = None) -> None:
        """`at`: when the fact was said. Older than the current single-valued edge: recorded as a closed
        interval (history) instead of superseding it. See GraphStore."""
        r = sanitize_rel(rel.rel)
        src = await self._key_for(user_id, rel.subject)
        dst = await self._key_for(user_id, rel.object)
        when = (timeutil.ensure_utc(at) if at else timeutil.now()).isoformat()
        params = dict(u=user_id, src=src, dst=dst, statement=rel.statement, confidence=rel.confidence,
                      source_ref=source_ref, at=when)
        rows = await self._run(q_update_current_edge(r), **params)
        if not rows or rows[0]["c"] == 0:
            to = None
            if r in SINGLE_VALUED_RELS:
                newer = await self._run(q_newer_single_valued(r), u=user_id, src=src, dst=dst, at=when)
                if newer and newer[0]["vf"] is not None:
                    to = newer[0]["vf"].isoformat()
                else:
                    await self._run(q_close_single_valued(r), u=user_id, src=src, dst=dst, at=when)
            await self._run(q_create_edge(r), to=to, **params)

    async def neighborhood(self, user_id: int, names: list[str], hops: int = 2, limit: int = 25) -> list[str]:
        keys: list[str] = []
        for n in names:
            if is_user(n):
                await self._run(Q_ENSURE_USER, u=user_id, key=USER_KEY)
                keys.append(USER_KEY)
                continue
            rows = await self._run(Q_FIND_KEY, u=user_id, norm=normalize_name(n))
            if rows:
                keys.append(rows[0]["key"])
        if not keys:
            return []
        rows = await self._run(q_neighborhood(hops), u=user_id, keys=keys, limit=limit * 4)
        return rank_candidates(rows, limit)

    async def entities(self, user_id: int) -> list[Entity]:
        return [Entity(name=r["name"], label=r["label"], aliases=r["aliases"])
                for r in await self._run(Q_ENTITIES, u=user_id)]

    async def dump(self, user_id: int) -> list[dict]:
        return await self._run(Q_DUMP, u=user_id)

    async def forget(self, user_id: int, needle: str) -> int:
        if not needle.strip():
            return 0
        edges = await self._run(Q_FORGET_EDGES, u=user_id, n=needle.strip())
        nodes = await self._run(Q_FORGET_NODES, u=user_id, n=needle.strip())
        return int((edges[0]["c"] if edges else 0) + (nodes[0]["c"] if nodes else 0))

    async def merge_entities(self, user_id: int, keep: str, drop: str, label: str) -> None:
        lab = sanitize_label(label)
        keep_key, drop_key = node_key(lab, keep), node_key(lab, drop)
        if keep_key == drop_key:
            return
        if not await self._run(Q_KEY_EXISTS, u=user_id, key=keep_key):
            return
        drop_rows = await self._run(Q_KEY_EXISTS, u=user_id, key=drop_key)
        if not drop_rows:
            return
        drop_name = drop_rows[0].get("name") or drop
        moved = list(dict.fromkeys([drop_name, *(drop_rows[0].get("aliases") or [])]))
        for e in await self._run(Q_DROP_EDGES, u=user_id, drop=drop_key):
            other = keep_key if e["other"] == drop_key else e["other"]
            src, dst = (keep_key, other) if e["outgoing"] else (other, keep_key)
            if src != dst:
                await self._run(_recreate_edge(e["t"]), u=user_id, src=src, dst=dst, props=e["props"])
        await self._run(Q_ADD_ALIASES, u=user_id, key=keep_key, aliases=moved,
                        norm_aliases=list(dict.fromkeys(normalize_name(a) for a in moved)))
        await self._run(Q_DELETE_NODE, u=user_id, key=drop_key)
        doomed = plan_dedupe(await self._run(Q_CURRENT_EDGES, u=user_id))
        if doomed:
            await self._run(Q_DELETE_EDGES, u=user_id, ids=doomed)

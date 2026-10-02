"""Replace the contents of a Neo4j database with a graph_export.py JSON file.

Usage (inside the mavis image, env has NEO4J_URI/USER/PASSWORD): python graph_import.py IN.json
Destructive: deletes every existing node first, so the result is exactly the exported graph.
"""

from __future__ import annotations

import json
import os
import re
import sys

from neo4j import GraphDatabase
from neo4j.time import Date, DateTime, Time

SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def dec(v):
    if isinstance(v, dict) and "$t" in v:
        cls = {"datetime": DateTime, "date": Date, "time": Time}[v["$t"]]
        return cls.from_iso_format(v["v"])
    if isinstance(v, list):
        return [dec(x) for x in v]
    return v


def ident(name: str) -> str:
    if not SAFE.match(name):
        raise SystemExit(f"refusing unsafe label/type {name!r}")
    return name


def main(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    auth = (os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"])
    with GraphDatabase.driver(os.environ["NEO4J_URI"], auth=auth) as driver, driver.session() as s:
        s.run("CREATE INDEX entity_user_key IF NOT EXISTS FOR (n:Entity) ON (n.user_id, n.key)").consume()
        s.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 1000 ROWS").consume()
        eids: list[str] = []
        for n in data["nodes"]:
            labels = ":".join(ident(x) for x in n["labels"]) or "Entity"
            rec = s.run(f"CREATE (n:{labels}) SET n = $props RETURN elementId(n) AS id",
                        props={k: dec(v) for k, v in n["props"].items()}).single()
            eids.append(rec["id"])
        for r in data["rels"]:
            s.run(f"MATCH (a) WHERE elementId(a) = $a MATCH (b) WHERE elementId(b) = $b "
                  f"CREATE (a)-[r:{ident(r['type'])}]->(b) SET r = $props",
                  a=eids[r["a"]], b=eids[r["b"]], props={k: dec(v) for k, v in r["props"].items()}).consume()
        n = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        e = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
    print(f"imported {n} nodes, {e} relationships")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])

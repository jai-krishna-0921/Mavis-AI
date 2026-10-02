"""Read-only export of a Neo4j graph (all nodes and relationships) to JSON.

Usage: NEO4J_URI=... NEO4J_USER=... NEO4J_PASSWORD=... python graph_export.py OUT.json
Only MATCH ... RETURN queries are run. Needs the `neo4j` package (part of the mavis venv).
"""

from __future__ import annotations

import json
import os
import sys

from neo4j import GraphDatabase
from neo4j.time import Date, DateTime, Duration, Time

CONNECT = {
    "uri": os.environ["NEO4J_URI"],
    "auth": (os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]),
}
Q_NODES = "MATCH (n) RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS props"
Q_RELS = (
    "MATCH (a)-[r]->(b) "
    "RETURN elementId(a) AS a, elementId(b) AS b, type(r) AS type, properties(r) AS props"
)


def enc(v):
    if isinstance(v, DateTime):
        return {"$t": "datetime", "v": v.iso_format()}
    if isinstance(v, Date):
        return {"$t": "date", "v": v.iso_format()}
    if isinstance(v, Time):
        return {"$t": "time", "v": v.iso_format()}
    if isinstance(v, Duration):
        raise SystemExit("durations are not supported by this exporter")
    if isinstance(v, list):
        return [enc(x) for x in v]
    return v


def main(out: str) -> None:
    with GraphDatabase.driver(CONNECT["uri"], auth=CONNECT["auth"]) as driver, driver.session() as s:
        ids: dict[str, int] = {}
        nodes = []
        for i, rec in enumerate(s.run(Q_NODES)):
            ids[rec["id"]] = i
            nodes.append({"labels": rec["labels"], "props": {k: enc(v) for k, v in rec["props"].items()}})
        rels = []
        for rec in s.run(Q_RELS):
            rels.append({"a": ids[rec["a"]], "b": ids[rec["b"]], "type": rec["type"],
                         "props": {k: enc(v) for k, v in rec["props"].items()}})
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"nodes": nodes, "rels": rels}, f)
    print(f"exported {len(nodes)} nodes, {len(rels)} relationships")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])

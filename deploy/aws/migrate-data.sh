#!/usr/bin/env bash
# Copy the local demo state (Postgres, Qdrant, Neo4j) to the box so persona, memory and connection
# state carry over. Everything read from the demo is read-only (pg_dump, qdrant snapshot API, MATCH queries).
#
# DESTRUCTIVE ON THE SERVER: replaces the box's Postgres contents, Qdrant collections and graph.
# Pass --yes to confirm. Redis is not copied (streams and locks are ephemeral).
#
# Postgres : pg_dump (docker exec into the demo container) -> psql on the box
# Qdrant   : collection snapshot API on the demo -> snapshot upload on the box
# Neo4j    : graph_export.py (read-only Cypher to JSON) -> graph_import.py inside the mavis image
set -euo pipefail
# shellcheck source=deploy/aws/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need ssh docker curl python3 uv
state_require

[[ "${1:-}" == "--yes" ]] || die "this replaces data on $MAVIS_EIP. Re-run with --yes to confirm."
[[ -f "$DEMO_ENV" ]] || die "demo env file not found: $DEMO_ENV"

umask 077
WORK="$(mktemp -d)"
REMOTE_TMP="$MAVIS_REMOTE_DIR/migrate-tmp"
cleanup() { rm -rf "$WORK"; ssh_box "rm -rf $REMOTE_TMP" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# qcurl ARGS...: curl against the box's qdrant from a one-off container on the compose network
# (qdrant publishes no host port). Stdin is passed through, so snapshots can be streamed in.
qcurl() {
  ssh_box "cd $MAVIS_REMOTE_DIR && $COMPOSE_BOX run --rm -T --no-deps migrate curl -fsS $*"
}

log "checking the demo sources are reachable"
docker exec "$DEMO_PG_CONTAINER" pg_isready -U mavis -d mavis >/dev/null || die "demo postgres container $DEMO_PG_CONTAINER not ready"
curl -fsS "$DEMO_QDRANT_URL/collections" >/dev/null || die "demo qdrant not reachable at $DEMO_QDRANT_URL"

# --- 1. export from the demo (read-only) ----------------------------------------
log "pg_dump"
docker exec "$DEMO_PG_CONTAINER" pg_dump -U mavis -d mavis --no-owner --no-acl --clean --if-exists >"$WORK/pg.sql"

log "qdrant snapshots"
mkdir -p "$WORK/qdrant"
COLLECTIONS="$(curl -fsS "$DEMO_QDRANT_URL/collections" | python3 -c 'import json,sys; print(" ".join(c["name"] for c in json.load(sys.stdin)["result"]["collections"]))')"
for c in $COLLECTIONS; do
  snap="$(curl -fsS -X POST "$DEMO_QDRANT_URL/collections/$c/snapshots?wait=true" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["name"])')"
  curl -fsS -o "$WORK/qdrant/$c.snapshot" "$DEMO_QDRANT_URL/collections/$c/snapshots/$snap"
  curl -fsS -X DELETE "$DEMO_QDRANT_URL/collections/$c/snapshots/$snap?wait=true" >/dev/null  # remove only our own snapshot
  log "  $c: $(du -h "$WORK/qdrant/$c.snapshot" | cut -f1)"
done

log "neo4j export"
(
  NEO4J_URI="$(envget "$DEMO_ENV" NEO4J_URI)"
  NEO4J_USER="$(envget "$DEMO_ENV" NEO4J_USER)"
  NEO4J_PASSWORD="$(envget "$DEMO_ENV" NEO4J_PASSWORD)"
  export NEO4J_URI NEO4J_USER NEO4J_PASSWORD
  cd "$REPO_ROOT" && uv run --quiet python deploy/aws/graph_export.py "$WORK/graph.json"
)

# --- 2. ship to the box -----------------------------------------------------------
log "uploading to the box"
ssh_box "rm -rf $REMOTE_TMP && umask 077 && mkdir -p $REMOTE_TMP"
scp_box "$WORK/pg.sql" "$REMOTE_TMP/pg.sql"
scp_box "$WORK/graph.json" "$REMOTE_TMP/graph.json"
scp_box "$AWS_DIR/graph_import.py" "$REMOTE_TMP/graph_import.py"
# pg.sql stays 600 (read by the ssh user). Only the two files the container reads are world-readable;
# qdrant snapshots are streamed over stdin and never touch the box's disk.
ssh_box "chmod 711 $REMOTE_TMP && chmod 644 $REMOTE_TMP/graph.json $REMOTE_TMP/graph_import.py"

# --- 3. restore on the box --------------------------------------------------------
log "stopping app services (data services stay up)"
compose_remote stop api worker timer
compose_remote up -d --wait --wait-timeout 300 postgres redis qdrant neo4j

log "restoring postgres"
ssh_box "cd $MAVIS_REMOTE_DIR && $COMPOSE_BOX exec -T postgres psql -v ON_ERROR_STOP=1 -q -U mavis -d mavis <$REMOTE_TMP/pg.sql >/dev/null"
compose_remote run --rm migrate

log "restoring qdrant"
for c in $COLLECTIONS; do
  qcurl -X DELETE "'http://qdrant:6333/collections/$c?wait=true'" >/dev/null
  ssh_box "cd $MAVIS_REMOTE_DIR && $COMPOSE_BOX run --rm -T --no-deps migrate \\
    curl -fsS -X POST -F 'snapshot=@-;filename=$c.snapshot' \\
    'http://qdrant:6333/collections/$c/snapshots/upload?priority=snapshot&wait=true'" \
    <"$WORK/qdrant/$c.snapshot" >/dev/null
  log "  $c restored"
done

log "restoring neo4j graph"
compose_remote run --rm --no-deps -v "$REMOTE_TMP:/migrate:ro" migrate python /migrate/graph_import.py /migrate/graph.json

# --- 4. verify, clean up, restart ---------------------------------------------------
log "row counts: demo vs box"
ssh_box "cd $MAVIS_REMOTE_DIR && $COMPOSE_BOX exec -T postgres psql -U mavis -d mavis -Atc \
  \"select 'box tables', count(*) from information_schema.tables where table_schema='public'\""
docker exec "$DEMO_PG_CONTAINER" psql -U mavis -d mavis -Atc \
  "select 'demo tables', count(*) from information_schema.tables where table_schema='public'"
for c in $COLLECTIONS; do
  echo "qdrant $c demo: $(curl -fsS "$DEMO_QDRANT_URL/collections/$c" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["points_count"])')"
  echo "qdrant $c box : $(qcurl "http://qdrant:6333/collections/$c" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["points_count"])')"
done

log "restarting app services"
compose_remote up -d --wait --wait-timeout 300
log "done. The demo bot and the box now share state; only ONE of them may talk to Telegram."
log "next: stop the local poller, then deploy/aws/webhook.sh set"

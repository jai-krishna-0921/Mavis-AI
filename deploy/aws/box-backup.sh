#!/usr/bin/env bash
# Nightly backup on the box (installed by deploy/aws/backups.sh as /usr/local/bin/mavis-backup.sh, cron 03:00).
# Postgres (pg_dump), the Neo4j graph (graph_export.py) and the Qdrant vectors (a collection snapshot) are
# written to /var/backups/mavis (the last 7 of each kept) and copied to s3://$BACKUP_BUCKET/<store>/<day>/
# with the instance role, which may only add objects. Any failed step fails the run (the log says which),
# and a store that failed is retried the next night; one store failing does not skip the others.
set -uo pipefail
umask 077
# shellcheck disable=SC1091
. /etc/mavis-backup.env  # BACKUP_BUCKET, MAVIS_DIR
cd "$MAVIS_DIR"
[ -s /run/mavis/mavis.env ] || /usr/local/sbin/mavis-secrets render
C="docker compose --env-file /run/mavis/mavis.env -f docker-compose.prod.yml"
d=/var/backups/mavis
mkdir -p "$d"
day="$(date -u +%F)"
stamp="$(date -u +%Y%m%d-%H%M%S)"
failed=0

ship() { # ship STORE FILE: copy one file off the box
  if [ -n "${BACKUP_BUCKET:-}" ]; then
    aws s3 cp --only-show-errors "$2" "s3://$BACKUP_BUCKET/$1/$day/$(basename "$2")" || { echo "upload $1 failed"; failed=1; }
  fi
}
keep7() { ls -1t "$d"/"$1"-*.gz 2>/dev/null | tail -n +8 | xargs -r rm -f; }

# 1. Postgres
f="$d/mavis-$stamp.sql.gz"
if $C exec -T postgres pg_dump -U mavis -d mavis --no-owner --no-acl | gzip >"$f.tmp" && [ -s "$f.tmp" ]; then
  mv "$f.tmp" "$f"; ship postgres "$f"
else
  rm -f "$f.tmp"; echo "postgres dump failed"; failed=1
fi
keep7 mavis

# 2. Neo4j graph (read-only export through the api container, which has the credentials)
f="$d/neo4j-$stamp.json.gz"
if $C exec -T api python - /tmp/graph-export.json </usr/local/lib/mavis-graph_export.py >/dev/null \
    && $C exec -T api sh -c 'cat /tmp/graph-export.json && rm -f /tmp/graph-export.json' | gzip >"$f.tmp" \
    && [ -s "$f.tmp" ]; then
  mv "$f.tmp" "$f"; ship neo4j "$f"
else
  rm -f "$f.tmp"; echo "neo4j export failed"; failed=1
fi
keep7 neo4j

# 3. Qdrant: a snapshot of every collection, streamed out, then removed from the server's disk
f="$d/qdrant-$stamp.tar.gz"
if $C exec -T api python - <<'PY' | gzip >"$f.tmp" && [ -s "$f.tmp" ]; then
import io, os, sys, tarfile, httpx
base = os.environ["QDRANT_URL"].rstrip("/")
with httpx.Client(timeout=600) as c, tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as tar:
    for col in c.get(f"{base}/collections").json()["result"]["collections"]:
        name = col["name"]
        snap = c.post(f"{base}/collections/{name}/snapshots").json()["result"]["name"]
        data = c.get(f"{base}/collections/{name}/snapshots/{snap}").content
        info = tarfile.TarInfo(f"{name}/{snap}"); info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        c.delete(f"{base}/collections/{name}/snapshots/{snap}")
PY
  mv "$f.tmp" "$f"; ship qdrant "$f"
else
  rm -f "$f.tmp"; echo "qdrant snapshot failed"; failed=1
fi
keep7 qdrant

if [ "$failed" = 0 ]; then echo "$(date -u +%FT%TZ) backup ok"; else echo "$(date -u +%FT%TZ) backup FAILED"; fi
exit "$failed"

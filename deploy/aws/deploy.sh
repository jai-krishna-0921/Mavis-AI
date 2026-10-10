#!/usr/bin/env bash
# Sync the repo to the box, push the config to SSM, render it on the box, build on the box, start the stack, migrate.
#   deploy.sh              full deploy; the api sets the Telegram webhook (stop any local `mavis dev` first)
#   deploy.sh --verify-machine  after deploying, run the machine demo suite on the box (mirrored to the owner)
#   deploy.sh --no-webhook TELEGRAM_MODE=polling: nothing on the box touches Telegram (use before migrate-data)
set -euo pipefail
# shellcheck source=deploy/aws/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need ssh rsync openssl aws
state_require

WEBHOOK=1
VERIFY_MACHINE=0
for a in "$@"; do
  case "$a" in
    --no-webhook) WEBHOOK=0 ;;
    --verify-machine) VERIFY_MACHINE=1 ;;
    *) die "unknown argument: $a (usage: deploy.sh [--no-webhook] [--verify-machine])" ;;
  esac
done
[[ -f "$DEMO_ENV" ]] || die "demo env file not found: $DEMO_ENV (set DEMO_ENV=...)"
# A custom domain must already resolve to the box, or Caddy cannot get a certificate and the Telegram
# webhook would point at nothing.
if [[ -n "${MAVIS_DOMAIN:-}" ]]; then
  resolved="$(getent ahostsv4 "$MAVIS_HOST" | awk 'NR==1{print $1}')"
  [[ "$resolved" == "$MAVIS_EIP" ]] \
    || die "$MAVIS_HOST resolves to '${resolved:-nothing}', not $MAVIS_EIP: fix its DNS A record first"
fi

# --- build the desired key set locally in a private temp file -----------------
# The base is what SSM Parameter Store already holds (/mavis/prod/*): its keys are preserved (so generated
# secrets never rotate) and only missing or empty keys are added. ENV, PUBLIC_BASE_URL, DOMAIN and
# TELEGRAM_MODE always follow this run. A failed read aborts: it must never look like "no secrets yet".
# The set is then pushed to SSM (changed keys only) and the box renders it to tmpfs. No .env is shipped.
umask 077
ENV_TMP="$(mktemp)"
trap 'rm -f "$ENV_TMP" "$ENV_TMP.n"' EXIT

REMOTE_ENV="$MAVIS_REMOTE_DIR/.env"   # the legacy plaintext file; removed after a successful switch
"$AWS_DIR/secrets.sh" pull --to "$ENV_TMP"
rc=0
ssh_box "test -f $REMOTE_ENV" || rc=$?
case "$rc" in
  0) LEGACY=1 ;;
  1) LEGACY=0 ;;
  *) die "ssh check for $REMOTE_ENV failed (exit $rc); refusing to continue so secrets are not rotated" ;;
esac
if [[ -z "$(envget "$ENV_TMP" NATIVE_TOKEN_KEK)" ]]; then
  # No KEK in SSM. Generating one is only safe on a truly fresh install: a new KEK would orphan every
  # sealed Google/Slack grant.
  [[ "$LEGACY" == 0 ]] || die "SSM has no NATIVE_TOKEN_KEK but the box still has $REMOTE_ENV: run deploy/aws/secrets.sh seed-from-box --apply first"
  [[ ! -s "$ENV_TMP" ]] || die "SSM /mavis/prod/ is populated but has no NATIVE_TOKEN_KEK; refusing to generate a new one (see docs/SECRETS.md)"
  log "SSM is empty and the box has no .env: fresh install, generating secrets"
else
  log "preserving existing keys from SSM /mavis/prod/"
fi
if [[ "$LEGACY" == 1 ]]; then
  # every key in the old file must already be in SSM, or deleting it afterwards would lose it (names only)
  legacy_keys="$(ssh_box "sudo grep -oE '^[A-Z][A-Z0-9_]*=.' $REMOTE_ENV | cut -d= -f1 | sort -u")" || die "cannot read $REMOTE_ENV"
  missing=""
  for k in $legacy_keys; do [[ -n "$(envget "$ENV_TMP" "$k")" ]] || missing="$missing $k"; done
  [[ -z "$missing" ]] || die "keys only in $REMOTE_ENV, not in SSM:$missing. Run deploy/aws/secrets.sh seed-from-box --apply"
fi

# set_key KEY VALUE [force]: append if missing, fill if empty, replace only when forced.
set_key() {
  local k="$1" v="$2" force="${3:-}"
  if ! grep -q "^$k=" "$ENV_TMP"; then
    printf '%s=%s\n' "$k" "$v" >>"$ENV_TMP"
  elif [[ -n "$force" ]] || { [[ -n "$v" ]] && ! grep -q "^$k=." "$ENV_TMP"; }; then
    V="$v" awk -v k="$k" 'index($0, k "=") == 1 { print k "=" ENVIRON["V"]; next } { print }' "$ENV_TMP" >"$ENV_TMP.n"
    mv "$ENV_TMP.n" "$ENV_TMP"
  fi
}

for k in OLLAMA_API_KEY TAVILY_API_KEY COMPOSIO_API_KEY TELEGRAM_BOT_TOKEN ALLOWED_TELEGRAM_CHAT_IDS; do
  [[ -n "$(envget "$DEMO_ENV" "$k")" ]] || die "$k is empty in $DEMO_ENV"
done

set_key ENV prod force
set_key PUBLIC_BASE_URL "https://$MAVIS_HOST" force
set_key DOMAIN "$MAVIS_HOST" force
if [[ "$WEBHOOK" == 1 ]]; then set_key TELEGRAM_MODE webhook force; else set_key TELEGRAM_MODE polling force; fi
# Owner-supplied keys follow the local env file (a rotated or upgraded key must reach the box);
# an empty local value never blanks the box. Generated secrets below are preserved instead.
for k in OLLAMA_API_KEY TAVILY_API_KEY COMPOSIO_API_KEY TELEGRAM_BOT_TOKEN ALLOWED_TELEGRAM_CHAT_IDS \
         LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY TEST_MIRROR_CHAT_ID MACHINE_LIVE_TOKEN_SECRET E2B_API_KEY \
         GOOGLE_OAUTH_CLIENT_ID GOOGLE_OAUTH_CLIENT_SECRET SLACK_CLIENT_ID SLACK_CLIENT_SECRET \
         SLACK_SIGNING_SECRET INTEGRATION_PROVIDER DASHBOARD_ENABLED GOOGLE_SIGNIN_ENABLED \
         TELEGRAM_BOT_USERNAME; do
  v="$(envget "$DEMO_ENV" "$k")"
  if [[ -n "$v" ]]; then set_key "$k" "$v" force; else set_key "$k" ""; fi
done
# Google and Slack run in house. The router still falls back to Composio for a user with no native grant,
# so this is safe to force; it must not depend on what the local env file happens to say.
set_key INTEGRATION_PROVIDER native force
# Owner decision 2026-10-09: other people join with invite codes (the owner chats stay admitted in every mode)
set_key ACCESS_MODE invite force
# Sandbox workspaces live in this bucket (deploy/aws/machine.sh creates it). Not forced: the owner can override.
set_key WORKSPACE_BUCKET "mavis-machine-276307603629-aps1"
tz="$(envget "$DEMO_ENV" DEFAULT_TIMEZONE)"
[[ -z "$tz" ]] || set_key DEFAULT_TIMEZONE "$tz"
set_key COMPOSIO_WEBHOOK_SECRET ""
set_key LLM_MAX_CONCURRENCY 3 force  # paid Ollama tier: several concurrent requests
# live E2E checks speak as a synthetic chat (below -10**15, never a real Telegram chat) whose sends go to a
# log sink, never to the owner's chat (scripts/live_e2e.py)
set_key LIVE_TEST_ENABLED true
set_key TEST_TELEGRAM_CHAT_ID -1000000000000001
set_key POSTGRES_PASSWORD "$(openssl rand -hex 24)"
set_key NEO4J_PASSWORD "$(openssl rand -hex 24)"
set_key TELEGRAM_WEBHOOK_SECRET "$(openssl rand -hex 24)"
# Wraps every stored Google/Slack token. Generated once and never overwritten: replacing it would orphan
# every sealed grant (rotation goes through NATIVE_TOKEN_KEK_PREVIOUS instead).
set_key NATIVE_TOKEN_KEK "$(openssl rand -base64 32)"

# the metadata guard must exist before the containers start (idempotent; also repairs a rebuilt box)
"$AWS_DIR/imds.sh" install

# --- ship code ----------------------------------------------------------------
log "rsync repo -> $MAVIS_EIP:$MAVIS_REMOTE_DIR"
rsync_box -az --delete \
  --exclude '.git' --exclude '.venv/' --exclude '/data/' --exclude '.env' --exclude '.env.*' \
  --exclude 'deploy/aws/state.env' --exclude '*.pem' --exclude '__pycache__/' --exclude '.pytest_cache/' \
  --exclude '.ruff_cache/' --exclude '.superpowers/' --exclude '.mcp.json' --exclude '/docs/' --exclude '/tests/' \
  --exclude '.worktrees/' --exclude 'instinct_screenshots/' --exclude 'node_modules/' \
  "$REPO_ROOT/" "$MAVIS_SSH_USER@$MAVIS_EIP:$MAVIS_REMOTE_DIR/"

log "pushing changed keys to SSM (values are never printed)"
"$AWS_DIR/secrets.sh" push --apply --with-deploy --file "$ENV_TMP"
MACHINE_ON="$(envget "$ENV_TMP" MACHINE_ENABLED | tr '[:upper:]' '[:lower:]')"
rm -f "$ENV_TMP" "$ENV_TMP.n"
log "installing the render unit and rendering $MAVIS_ENV_FILE on the box (tmpfs, root, mode 600)"
"$AWS_DIR/secrets.sh" install-box
"$AWS_DIR/secrets.sh" render-remote

# --- build + start ---------------------------------------------------------------
# A 2 GB box cannot build (uv sync + model download) next to the full stack, so the worker and timer
# are stopped for the build. The whole stop/build/start sequence runs DETACHED on the box with its own
# EXIT trap: if this laptop disconnects or this script is killed, the box still finishes and always
# brings the worker and timer back (on the new image if the build succeeded, else on the old one).
log "building and starting on the box (detached; first build takes several minutes)"
ssh_box "cat > $MAVIS_REMOTE_DIR/.deploy-remote.sh" <<REMOTE
#!/usr/bin/env bash
set -uo pipefail
cd $MAVIS_REMOTE_DIR
C="docker compose --env-file $MAVIS_ENV_FILE -f $MAVIS_COMPOSE_FILE --profile prod"
rm -f .deploy.rc
trap '\$C up -d worker timer >/dev/null 2>&1; echo "\${RC:-1}" > .deploy.rc' EXIT
\$C stop worker timer >/dev/null 2>&1 || true
RC=1
# keep the running image so a failed migration can roll back to it
docker image tag mavis:prod mavis:prev >/dev/null 2>&1 || true
\$C build || exit 1
# A changed network (subnet, fixed addresses) cannot be applied while containers use the old one: stop the
# stack (volumes are kept: never -v) and start again. Only done when the first attempt fails.
if ! \$C up -d --wait --wait-timeout 300 postgres redis qdrant neo4j; then
  echo "compose up failed; recreating the stack without touching volumes"
  \$C down --remove-orphans || exit 1
  \$C up -d --wait --wait-timeout 300 postgres redis qdrant neo4j || exit 1
fi
if ! \$C run --rm migrate; then
  echo "migration failed; rolling back to the previous image"
  docker image tag mavis:prev mavis:prod >/dev/null 2>&1 || true
  exit 1
fi
\$C up -d --wait --wait-timeout 300 && RC=0
# rsync replaces the Caddyfile with a new inode, which a single-file bind mount does not follow: recreate
# caddy only when the file or the public host (DOMAIN) changed, so routes like /oauth/* go live without a needless TLS restart
CADDY_SHA="\$( (cat Caddyfile; grep -E '^DOMAIN=' $MAVIS_ENV_FILE) | sha256sum | cut -d' ' -f1)"  # the file or the host changed
if [ "\$CADDY_SHA" != "\$(cat .caddy.sha 2>/dev/null)" ]; then
  \$C up -d --force-recreate caddy && echo "\$CADDY_SHA" > .caddy.sha
fi
REMOTE
ssh_box "cd $MAVIS_REMOTE_DIR && rm -f .deploy.rc && setsid nohup sudo bash .deploy-remote.sh > .deploy.log 2>&1 < /dev/null &"
for _ in $(seq 1 240); do
  sleep "${MAVIS_POLL_S:-10}"
  rc="$(ssh_box "cat $MAVIS_REMOTE_DIR/.deploy.rc 2>/dev/null" || true)"
  [[ -n "$rc" ]] && break
done
if [[ "${rc:-}" != 0 ]]; then
  ssh_box "tail -n 40 $MAVIS_REMOTE_DIR/.deploy.log" || true
  die "remote build/start failed or timed out (rc=${rc:-none}); worker and timer were restarted on the box"
fi
if [[ "$LEGACY" == 1 ]]; then
  log "switch complete: removing the plaintext $REMOTE_ENV (SSM is the only copy)"
  ssh_box "sudo shred -u $REMOTE_ENV 2>/dev/null || sudo rm -f $REMOTE_ENV"
fi
ssh_box "sudo rm -f $MAVIS_REMOTE_DIR/.env.new"
if [[ -n "${MAVIS_BACKUP_BUCKET:-}" ]]; then
  # keep the off-box backup job current (a rebuilt box gets bootstrap.sh's local-only one)
  log "installing the nightly off-box backup job"
  scp_box "$REPO_ROOT/deploy/aws/box-backup.sh" /tmp/box-backup.sh
  scp_box "$REPO_ROOT/deploy/aws/graph_export.py" /tmp/graph_export.py
  ssh_box "sudo install -m 700 -o root /tmp/box-backup.sh /usr/local/bin/mavis-backup.sh \
    && sudo install -m 600 -o root /tmp/graph_export.py /usr/local/lib/mavis-graph_export.py \
    && printf 'BACKUP_BUCKET=%s\nMAVIS_DIR=%s\n' '$MAVIS_BACKUP_BUCKET' '$MAVIS_REMOTE_DIR' \
       | sudo tee /etc/mavis-backup.env >/dev/null && sudo chmod 600 /etc/mavis-backup.env \
    && rm -f /tmp/box-backup.sh /tmp/graph_export.py"
fi
log "pruning dangling images and old build cache"
ssh_box "docker image prune -f >/dev/null && docker builder prune -f --keep-storage 1GB >/dev/null"
compose_remote ps

log "verifying that only api, worker and timer reach the instance metadata service"
machine_on="$MACHINE_ON"
if [[ "$machine_on" != "true" ]]; then
  # The machine is off: nothing needs role credentials, and hop limit 1 (no container reaches IMDS) is the
  # safe state, so a failed check is reported, not fatal.
  "$AWS_DIR/imds.sh" verify-containers \
    || log "IMDS: containers have no role credentials (expected while MACHINE_ENABLED is off)"
elif ! "$AWS_DIR/imds.sh" verify-containers; then
  if [[ "$(aws_ ec2 describe-instances --instance-ids "${MAVIS_INSTANCE_ID:-}" \
        --query 'Reservations[0].Instances[0].MetadataOptions.HttpPutResponseHopLimit' --output text 2>/dev/null || echo 1)" != 1 ]]; then
    "$AWS_DIR/imds.sh" lower-hop-limit
  fi
  die "instance metadata verification failed; the hop limit is 1 (containers have no role credentials). Fix imds-guard and re-run."
fi

if [[ "$WEBHOOK" == 1 ]]; then
  log "telegram webhook:"
  compose_remote exec -T api mavis telegram info || true
else
  log "TELEGRAM_MODE=polling: no webhook set. Run deploy/aws/webhook.sh set after migrate-data."
fi
log "health: https://$MAVIS_HOST/healthz (certificate is issued on first request, give it a minute)"

if [[ "$VERIFY_MACHINE" == 1 ]]; then
  log "running the machine demo suite on the box (mirrored to the owner's chat when TEST_MIRROR_CHAT_ID is set)"
  compose_remote exec -T worker python -m scripts.machine_demo --all --report-to-owner \
    || log "demo suite reported failures (reports are in the e2edata volume, /app/data/e2e in the worker)"
fi

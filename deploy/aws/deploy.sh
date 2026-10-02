#!/usr/bin/env bash
# Sync the repo to the box, write a production .env, build on the box, start the stack, migrate.
#   deploy.sh              full deploy; the api sets the Telegram webhook (stop any local `mavis dev` first)
#   deploy.sh --no-webhook TELEGRAM_MODE=polling: nothing on the box touches Telegram (use before migrate-data)
set -euo pipefail
# shellcheck source=deploy/aws/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need ssh rsync openssl
state_require

WEBHOOK=1
for a in "$@"; do
  case "$a" in
    --no-webhook) WEBHOOK=0 ;;
    *) die "unknown argument: $a (usage: deploy.sh [--no-webhook])" ;;
  esac
done
[[ -f "$DEMO_ENV" ]] || die "demo env file not found: $DEMO_ENV (set DEMO_ENV=...)"

# --- build the prod .env locally in a private temp file ----------------------
# An existing .env on the box is the base: its keys are preserved (so generated secrets never rotate)
# and only missing or empty keys are added. ENV, PUBLIC_BASE_URL, DOMAIN and TELEGRAM_MODE always follow
# this run. An ssh failure aborts: it must never look like "no .env yet".
umask 077
ENV_TMP="$(mktemp)"
trap 'rm -f "$ENV_TMP"' EXIT

REMOTE_ENV="$MAVIS_REMOTE_DIR/.env"
rc=0
ssh_box "test -f $REMOTE_ENV" || rc=$?
case "$rc" in
  0) log "preserving existing keys from $REMOTE_ENV"; ssh_box "cat $REMOTE_ENV" >"$ENV_TMP" ;;
  1) log "no .env on the box yet; generating secrets" ;;
  *) die "ssh check for $REMOTE_ENV failed (exit $rc); refusing to continue so secrets are not rotated" ;;
esac

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
for k in OLLAMA_API_KEY TAVILY_API_KEY COMPOSIO_API_KEY TELEGRAM_BOT_TOKEN ALLOWED_TELEGRAM_CHAT_IDS \
         LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY; do
  set_key "$k" "$(envget "$DEMO_ENV" "$k")"
done
tz="$(envget "$DEMO_ENV" DEFAULT_TIMEZONE)"
[[ -z "$tz" ]] || set_key DEFAULT_TIMEZONE "$tz"
set_key COMPOSIO_WEBHOOK_SECRET ""
set_key LLM_MAX_CONCURRENCY 1
set_key POSTGRES_PASSWORD "$(openssl rand -hex 24)"
set_key NEO4J_PASSWORD "$(openssl rand -hex 24)"
set_key TELEGRAM_WEBHOOK_SECRET "$(openssl rand -hex 24)"

# --- ship code ----------------------------------------------------------------
log "rsync repo -> $MAVIS_EIP:$MAVIS_REMOTE_DIR"
rsync_box -az --delete \
  --exclude '.git' --exclude '.venv/' --exclude 'data/' --exclude '.env' --exclude '.env.*' \
  --exclude 'deploy/aws/state.env' --exclude '*.pem' --exclude '__pycache__/' --exclude '.pytest_cache/' \
  --exclude '.ruff_cache/' --exclude '.superpowers/' --exclude '.mcp.json' --exclude 'docs/' --exclude 'tests/' \
  "$REPO_ROOT/" "$MAVIS_SSH_USER@$MAVIS_EIP:$MAVIS_REMOTE_DIR/"

log "installing .env (mode 600)"
scp_box "$ENV_TMP" "$MAVIS_REMOTE_DIR/.env.new"
ssh_box "chmod 600 $MAVIS_REMOTE_DIR/.env.new && mv $MAVIS_REMOTE_DIR/.env.new $MAVIS_REMOTE_DIR/.env"

# --- build + start ---------------------------------------------------------------
# A 2 GB box cannot build (uv sync + model download) next to the full stack: free the two biggest
# app processes first. The api keeps answering; Telegram updates queue in Redis until the worker is back.
STOPPED=0
restart_on_fail() {
  local rc=$?
  if [[ "$rc" != 0 && "$STOPPED" == 1 ]]; then
    log "deploy failed; restarting worker and timer"
    compose_remote start worker timer || true
  fi
  rm -f "$ENV_TMP" "$ENV_TMP.n"
}
trap restart_on_fail EXIT
log "stopping worker and timer to free memory for the build"
compose_remote stop worker timer >/dev/null 2>&1 || true
STOPPED=1
log "building image on the box (first build takes several minutes)"
compose_remote build
log "starting data services"
compose_remote up -d --wait --wait-timeout 300 postgres redis qdrant neo4j
log "running migrations"
compose_remote run --rm migrate
log "starting app + caddy"
compose_remote up -d --wait --wait-timeout 300
STOPPED=0
log "pruning dangling images and old build cache"
ssh_box "docker image prune -f >/dev/null && docker builder prune -f --keep-storage 1GB >/dev/null"
compose_remote ps

if [[ "$WEBHOOK" == 1 ]]; then
  log "telegram webhook:"
  compose_remote exec -T api mavis telegram info || true
else
  log "TELEGRAM_MODE=polling: no webhook set. Run deploy/aws/webhook.sh set after migrate-data."
fi
log "health: https://$MAVIS_HOST/healthz (certificate is issued on first request, give it a minute)"

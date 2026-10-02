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
umask 077
ENV_TMP="$(mktemp)"
trap 'rm -f "$ENV_TMP"' EXIT

# Reuse generated secrets from the box so a redeploy never rotates the DB password.
remote_get() { ssh_box "grep -E '^$1=' $MAVIS_REMOTE_DIR/.env 2>/dev/null | tail -n1 | cut -d= -f2-" 2>/dev/null || true; }
gen() { openssl rand -hex 24; }
pick() { local v; v="$(remote_get "$1")"; if [[ -n "$v" ]]; then printf '%s' "$v"; else gen; fi; }

PG_PASS="$(pick POSTGRES_PASSWORD)"
NEO_PASS="$(pick NEO4J_PASSWORD)"
HOOK_SECRET="$(pick TELEGRAM_WEBHOOK_SECRET)"

for k in OLLAMA_API_KEY TAVILY_API_KEY COMPOSIO_API_KEY TELEGRAM_BOT_TOKEN ALLOWED_TELEGRAM_CHAT_IDS; do
  [[ -n "$(envget "$DEMO_ENV" "$k")" ]] || die "$k is empty in $DEMO_ENV"
done

{
  echo "ENV=prod"
  echo "PUBLIC_BASE_URL=https://$MAVIS_HOST"
  echo "DOMAIN=$MAVIS_HOST"
  echo "DEFAULT_TIMEZONE=$(envget "$DEMO_ENV" DEFAULT_TIMEZONE)"
  for k in OLLAMA_API_KEY TAVILY_API_KEY COMPOSIO_API_KEY TELEGRAM_BOT_TOKEN ALLOWED_TELEGRAM_CHAT_IDS \
           LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY; do
    echo "$k=$(envget "$DEMO_ENV" "$k")"
  done
  echo "COMPOSIO_WEBHOOK_SECRET="
  echo "POSTGRES_PASSWORD=$PG_PASS"
  echo "NEO4J_PASSWORD=$NEO_PASS"
  echo "TELEGRAM_WEBHOOK_SECRET=$HOOK_SECRET"
  if [[ "$WEBHOOK" == 1 ]]; then echo "TELEGRAM_MODE=webhook"; else echo "TELEGRAM_MODE=polling"; fi
  echo "LLM_MAX_CONCURRENCY=1"
} >"$ENV_TMP"
sed -i '/^DEFAULT_TIMEZONE=$/d' "$ENV_TMP"  # fall back to the compose default when unset locally

# --- ship code ----------------------------------------------------------------
log "rsync repo -> $MAVIS_EIP:$MAVIS_REMOTE_DIR"
rsync_box -az --delete \
  --exclude '.git/' --exclude '.venv/' --exclude 'data/' --exclude '.env' --exclude '.env.*' \
  --exclude 'deploy/aws/state.env' --exclude '*.pem' --exclude '__pycache__/' --exclude '.pytest_cache/' \
  --exclude '.ruff_cache/' --exclude '.superpowers/' --exclude '.mcp.json' --exclude 'docs/' --exclude 'tests/' \
  "$REPO_ROOT/" "$MAVIS_SSH_USER@$MAVIS_EIP:$MAVIS_REMOTE_DIR/"

log "installing .env (mode 600)"
scp_box "$ENV_TMP" "$MAVIS_REMOTE_DIR/.env.new"
ssh_box "chmod 600 $MAVIS_REMOTE_DIR/.env.new && mv $MAVIS_REMOTE_DIR/.env.new $MAVIS_REMOTE_DIR/.env"

# --- build + start ---------------------------------------------------------------
log "building image on the box (first build takes several minutes)"
compose_remote build
log "starting data services"
compose_remote up -d --wait --wait-timeout 300 postgres redis qdrant neo4j
log "running migrations"
compose_remote run --rm migrate
log "starting app + caddy"
compose_remote up -d --wait --wait-timeout 300
compose_remote ps

if [[ "$WEBHOOK" == 1 ]]; then
  log "telegram webhook:"
  compose_remote exec -T api mavis telegram info || true
else
  log "TELEGRAM_MODE=polling: no webhook set. Run deploy/aws/webhook.sh set after migrate-data."
fi
log "health: https://$MAVIS_HOST/healthz (certificate is issued on first request, give it a minute)"

#!/usr/bin/env bash
# Manage the Telegram webhook from the box. Webhook and getUpdates polling are mutually exclusive:
# once `set` runs, a local `mavis dev` poller loses updates (409). Stop it first.
#   webhook.sh set | delete | info
set -euo pipefail
# shellcheck source=deploy/aws/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
state_require
case "${1:-}" in
  set)
    log "make sure no local 'mavis dev' / demo poller is running for this bot"
    ssh_box "sed -i 's/^TELEGRAM_MODE=.*/TELEGRAM_MODE=webhook/' $MAVIS_REMOTE_DIR/.env"
    compose_remote up -d --wait --wait-timeout 300 api
    compose_remote exec -T api mavis telegram set-webhook
    compose_remote exec -T api mavis telegram info ;;
  delete)
    compose_remote exec -T api mavis telegram delete-webhook
    ssh_box "sed -i 's/^TELEGRAM_MODE=.*/TELEGRAM_MODE=polling/' $MAVIS_REMOTE_DIR/.env"
    log "webhook deleted; recreate api so it stops re-setting it: deploy/aws/deploy.sh --no-webhook" ;;
  info) compose_remote exec -T api mavis telegram info ;;
  *) die "usage: webhook.sh set|delete|info" ;;
esac

#!/usr/bin/env bash
# shellcheck disable=SC2034,SC1090,SC2029
# Shared helpers for the Mavis AWS scripts. Source this, do not run it.
# Defaults are overridable from the environment.

: "${AWS_PROFILE:=cashfree}"
: "${AWS_REGION:=ap-south-1}"
: "${MAVIS_KEY_NAME:=mavis-ec2}"
: "${MAVIS_KEY_FILE:=$HOME/.ssh/mavis-ec2.pem}"
: "${MAVIS_SG_NAME:=mavis-sg}"
: "${MAVIS_INSTANCE_NAME:=mavis-prod}"
: "${MAVIS_INSTANCE_TYPE:=t4g.small}"
: "${MAVIS_VOLUME_GB:=20}"
: "${MAVIS_SSH_USER:=ubuntu}"
: "${MAVIS_REMOTE_DIR:=/opt/mavis}"
: "${DEMO_ENV:=/home/jk/Documents/Mavis-AI/.env}"
: "${DEMO_PG_CONTAINER:=mavis-dev-postgres-1}"
: "${DEMO_QDRANT_URL:=http://localhost:6340}"
: "${MAVIS_COMPOSE_FILE:=docker-compose.prod.yml}"

AWS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$AWS_DIR/../.." && pwd)"
STATE_FILE="${MAVIS_STATE_FILE:-$AWS_DIR/state.env}"
KNOWN_HOSTS="${MAVIS_KNOWN_HOSTS:-$HOME/.ssh/known_hosts_mavis}"
export AWS_PAGER=""

log() { printf '==> %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

need() {
  local c
  for c in "$@"; do command -v "$c" >/dev/null 2>&1 || die "missing required command: $c"; done
}

# All AWS calls go through here so the profile and region are never forgotten.
aws_() { aws --profile "$AWS_PROFILE" --region "$AWS_REGION" "$@"; }

state_load() { [[ -f "$STATE_FILE" ]] && . "$STATE_FILE"; return 0; }

# state_set KEY VALUE: idempotent upsert into state.env (ids only, no secrets).
state_set() {
  local k="$1" v="$2"
  touch "$STATE_FILE"
  chmod 600 "$STATE_FILE"
  grep -v "^${k}=" "$STATE_FILE" >"$STATE_FILE.tmp" || true
  printf '%s=%s\n' "$k" "$v" >>"$STATE_FILE.tmp"
  mv "$STATE_FILE.tmp" "$STATE_FILE"
}

# state.env holds only resource ids and the public IP (no secrets); it is sourced by the scripts.
state_require() {
  [[ -f "$STATE_FILE" ]] || die "no $STATE_FILE; run provision.sh first"
  state_load
  [[ -n "${MAVIS_EIP:-}" ]] || die "state.env has no MAVIS_EIP; run provision.sh first"
  MAVIS_HOST="${MAVIS_EIP//./-}.sslip.io"
}

ssh_opts() {
  printf '%s\n' -i "$MAVIS_KEY_FILE" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new \
    -o "UserKnownHostsFile=$KNOWN_HOSTS" -o ServerAliveInterval=30 -o ConnectTimeout=15
}

ssh_box() {
  local opts=()
  mapfile -t opts < <(ssh_opts)
  ssh "${opts[@]}" "$MAVIS_SSH_USER@$MAVIS_EIP" "$@"
}

scp_box() { # scp_box LOCAL REMOTE
  local opts=()
  mapfile -t opts < <(ssh_opts)
  scp -q "${opts[@]}" "$1" "$MAVIS_SSH_USER@$MAVIS_EIP:$2"
}

rsync_box() { # rsync_box ARGS... (the -e transport is added)
  local opts=()
  mapfile -t opts < <(ssh_opts)
  rsync -e "ssh ${opts[*]}" "$@"
}

# compose_remote ARGS...: run docker compose on the box in the app dir.
compose_remote() {
  ssh_box "cd $MAVIS_REMOTE_DIR && docker compose -f $MAVIS_COMPOSE_FILE --profile prod $*"
}

# envget FILE KEY: read one value from an env file without sourcing it (never echoed by callers).
envget() {
  local v
  v="$(grep -E "^[[:space:]]*$2=" "$1" 2>/dev/null | tail -n1 | cut -d= -f2-)" || true
  v="${v%$'\r'}"
  v="${v#\"}"; v="${v%\"}"; v="${v#\'}"; v="${v%\'}"
  printf '%s' "$v"
}

wait_for_ssh() {
  local i
  for _ in $(seq 1 40); do
    if ssh_box true >/dev/null 2>&1; then return 0; fi
    sleep 5
  done
  die "ssh to $MAVIS_EIP did not come up"
}

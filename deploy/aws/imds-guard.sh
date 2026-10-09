#!/usr/bin/env bash
# Runs ON the box as root (installed as /usr/local/bin/mavis-imds-guard by imds.sh install).
# Only the api, worker and timer containers may reach the instance metadata service (169.254.169.254) and so
# the mavis-ec2 role credentials. Every other container (postgres, redis, qdrant, neo4j, caddy) and any other
# docker network is dropped in DOCKER-USER, ahead of docker's own rules. The allowed containers have fixed
# addresses in docker-compose.prod.yml (network `default`, subnet 10.89.77.0/24).
#   mavis-imds-guard apply [--dry-run]     (re)build the MAVIS-IMDS chain and its jump from DOCKER-USER
#   mavis-imds-guard check                 exit 0 only when the rules are in place and complete
#   mavis-imds-guard verify pre|post       rules + live container probes (post also needs the hop limit at 2)
set -euo pipefail
IPT="${IPTABLES:-iptables}"
IMDS=169.254.169.254
CHAIN=MAVIS-IMDS
ALLOW="${MAVIS_IMDS_ALLOW:-10.89.77.10 10.89.77.11 10.89.77.12}"
APP_DIR="${MAVIS_REMOTE_DIR:-/opt/mavis}"
COMPOSE_FILE="${MAVIS_COMPOSE_FILE:-docker-compose.prod.yml}"
DRY=0
cmd="${1:-}"; shift || true
for a in "$@"; do [[ "$a" == --dry-run ]] && DRY=1; done
log() { printf '==> %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
ipt() { if [[ "$DRY" == 1 ]]; then printf 'PLAN: iptables %s\n' "$*"; else "$IPT" "$@"; fi; }
compose() { (cd "$APP_DIR" && docker compose -f "$COMPOSE_FILE" --profile prod "$@"); }

apply() {
  if [[ "$DRY" == 0 ]]; then "$IPT" -L DOCKER-USER -n >/dev/null 2>&1 || die "no DOCKER-USER chain: is docker running?"; fi
  if [[ "$DRY" == 1 ]] || ! "$IPT" -L "$CHAIN" -n >/dev/null 2>&1; then ipt -N "$CHAIN"; fi
  ipt -F "$CHAIN"
  for ip in $ALLOW; do ipt -A "$CHAIN" -s "$ip" -j ACCEPT; done
  ipt -A "$CHAIN" -j DROP
  if [[ "$DRY" == 1 ]] || ! "$IPT" -C DOCKER-USER -d "$IMDS" -j "$CHAIN" 2>/dev/null; then
    ipt -I DOCKER-USER 1 -d "$IMDS" -j "$CHAIN"
  fi
}

check() {
  "$IPT" -C DOCKER-USER -d "$IMDS" -j "$CHAIN" 2>/dev/null || { log "no jump from DOCKER-USER to $CHAIN"; return 1; }
  local rules ip
  rules="$("$IPT" -S "$CHAIN" 2>/dev/null)" || { log "chain $CHAIN missing"; return 1; }
  for ip in $ALLOW; do
    grep -q -- "-s $ip/32 -j ACCEPT" <<<"$rules" || { log "no ACCEPT for $ip"; return 1; }
  done
  [[ "$(tail -n1 <<<"$rules")" == "-A $CHAIN -j DROP" ]] || { log "$CHAIN does not end in DROP"; return 1; }
}

running() { [[ -n "$(compose ps -q "$1" 2>/dev/null)" ]]; }

# Reaching the token endpoint from inside a container. Alpine images have busybox nc; the app image has python.
reachable() { # reachable SERVICE: exit 0 when the IMDSv2 token endpoint answers
  case "$1" in
    api|worker|timer)
      compose exec -T "$1" python -c "import urllib.request as u;r=u.Request('http://$IMDS/latest/api/token',method='PUT',headers={'X-aws-ec2-metadata-token-ttl-seconds':'60'});u.urlopen(r,timeout=4).read()" >/dev/null 2>&1 ;;
    *)
      local out
      out="$(compose exec -T "$1" sh -c "printf 'PUT /latest/api/token HTTP/1.0\r\nX-aws-ec2-metadata-token-ttl-seconds: 60\r\n\r\n' | nc -w 4 $IMDS 80" 2>/dev/null || true)"
      [[ "$out" == *"HTTP/1"* ]] ;;
  esac
}

must_be_blocked() {
  running "$1" || { log "$1 is not running; its probe is skipped"; return 0; }
  if reachable "$1"; then log "FAIL: $1 can reach the instance metadata service"; return 1; fi
  log "ok: $1 cannot reach the instance metadata service"
}

verify() {
  check || return 1
  must_be_blocked postgres || return 1
  if [[ "${1:-pre}" == post ]]; then
    must_be_blocked redis || return 1
    if running worker; then
      reachable worker || { log "FAIL: the worker cannot get the role credentials endpoint (hop limit or rule)"; return 1; }
      log "ok: worker reaches the instance metadata service"
    else
      log "worker is not running; its probe is skipped"
    fi
  fi
}

case "$cmd" in
  apply) apply ;;
  check) check ;;
  verify) verify "${1:-pre}" ;;
  *) die "usage: mavis-imds-guard apply [--dry-run] | check | verify pre|post" ;;
esac

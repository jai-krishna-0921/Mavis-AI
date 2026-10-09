#!/usr/bin/env bash
# Instance metadata (IMDS) control from the laptop. The role credentials must reach api, worker and timer
# (hop limit 2 lets a container's packets through) and NO other container: a DOCKER-USER iptables rule on the
# box, installed first and persisted by a systemd unit, drops 169.254.169.254 from everything else.
#   imds.sh install [--dry-run]            copy the guard script to the box, persist it (mavis-imds-guard.service)
#   imds.sh verify-rule                    rules in place and postgres blocked; run BEFORE raising the hop limit
#   imds.sh verify-containers              postgres, redis blocked and the worker allowed; run AFTER
#   imds.sh lower-hop-limit [--dry-run]    set the instance hop limit back to 1 (rollback)
set -euo pipefail
# shellcheck source=deploy/aws/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need ssh
state_require
cmd="${1:-}"; shift || true
DRY=0
for a in "$@"; do [[ "$a" == --dry-run ]] && DRY=1; done
GUARD=/usr/local/bin/mavis-imds-guard
UNIT=/etc/systemd/system/mavis-imds-guard.service
REMOTE_ENV="MAVIS_REMOTE_DIR='$MAVIS_REMOTE_DIR' MAVIS_COMPOSE_FILE='$MAVIS_COMPOSE_FILE'"

case "$cmd" in
  install)
    if [[ "$DRY" == 1 ]]; then
      printf 'PLAN: copy deploy/aws/imds-guard.sh to %s on the box (root, mode 755)\n' "$GUARD"
      printf 'PLAN: write %s (Type=oneshot, After=docker.service, PartOf=docker.service, ExecStart=%s apply)\n' "$UNIT" "$GUARD"
      printf 'PLAN: systemctl enable --now mavis-imds-guard.service\n'
      bash "$AWS_DIR/imds-guard.sh" apply --dry-run
      exit 0
    fi
    log "installing the instance metadata guard on $MAVIS_EIP"
    ssh_box "sudo tee $GUARD >/dev/null && sudo chmod 755 $GUARD" <"$AWS_DIR/imds-guard.sh"
    ssh_box "sudo tee $UNIT >/dev/null" <<UNITFILE
[Unit]
Description=Only the Mavis api, worker and timer containers may reach the instance metadata service
After=docker.service
PartOf=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=MAVIS_REMOTE_DIR=$MAVIS_REMOTE_DIR
ExecStart=$GUARD apply

[Install]
WantedBy=multi-user.target docker.service
UNITFILE
    ssh_box "sudo systemctl daemon-reload && sudo systemctl enable mavis-imds-guard.service && sudo systemctl restart mavis-imds-guard.service"
    ssh_box "sudo $GUARD check"
    ;;
  verify-rule) ssh_box "sudo env $REMOTE_ENV $GUARD verify pre" ;;
  verify-containers) ssh_box "sudo env $REMOTE_ENV $GUARD verify post" ;;
  lower-hop-limit)
    need aws
    [[ -n "${MAVIS_INSTANCE_ID:-}" ]] || die "no MAVIS_INSTANCE_ID in state.env"
    if [[ "$DRY" == 1 ]]; then
      printf 'PLAN: aws ec2 modify-instance-metadata-options --instance-id %s --http-tokens required --http-put-response-hop-limit 1 --http-endpoint enabled\n' "$MAVIS_INSTANCE_ID"
    else
      log "lowering the instance metadata hop limit to 1"
      aws_ ec2 modify-instance-metadata-options --instance-id "$MAVIS_INSTANCE_ID" \
        --http-tokens required --http-put-response-hop-limit 1 --http-endpoint enabled >/dev/null
    fi
    ;;
  *) die "usage: imds.sh install|verify-rule|verify-containers|lower-hop-limit [--dry-run]" ;;
esac

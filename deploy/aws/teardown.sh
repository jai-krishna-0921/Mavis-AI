#!/usr/bin/env bash
# Delete EVERYTHING tagged Project=mavis (instances, Elastic IPs, security groups, key pairs, volumes).
# Without --yes it only lists what would be deleted and exits 1.
#   teardown.sh [--yes]
set -euo pipefail
# shellcheck source=deploy/aws/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need aws

YES=0
for a in "$@"; do
  case "$a" in
    --yes) YES=1 ;;
    *) die "unknown argument: $a (usage: teardown.sh [--yes])" ;;
  esac
done

F=(--filters "Name=tag:Project,Values=mavis")
INSTANCES="$(aws_ ec2 describe-instances "${F[@]}" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].InstanceId' --output text)"
EIPS="$(aws_ ec2 describe-addresses "${F[@]}" --query 'Addresses[].AllocationId' --output text)"
SGS="$(aws_ ec2 describe-security-groups "${F[@]}" --query 'SecurityGroups[].GroupId' --output text)"
KEYS="$(aws_ ec2 describe-key-pairs "${F[@]}" --query 'KeyPairs[].KeyName' --output text)"
VOLS="$(aws_ ec2 describe-volumes "${F[@]}" --query 'Volumes[].VolumeId' --output text)"

echo "Resources tagged Project=mavis in $AWS_REGION (profile $AWS_PROFILE):"
echo "  instances     : ${INSTANCES:-none}"
echo "  elastic ips   : ${EIPS:-none}"
echo "  security grps : ${SGS:-none}"
echo "  key pairs     : ${KEYS:-none}"
echo "  volumes       : ${VOLS:-none}"
if [[ "$YES" != 1 ]]; then
  echo "dry run. Re-run with --yes to DELETE all of the above (data on the box is lost)." >&2
  exit 1
fi

if [[ -n "$INSTANCES" ]]; then
  log "terminating instances"
  # shellcheck disable=SC2086
  aws_ ec2 terminate-instances --instance-ids $INSTANCES >/dev/null
  # shellcheck disable=SC2086
  aws_ ec2 wait instance-terminated --instance-ids $INSTANCES
fi
for a in $EIPS; do
  log "releasing $a"
  aws_ ec2 release-address --allocation-id "$a"
done
for g in $SGS; do
  log "deleting security group $g"
  for _ in 1 2 3 4 5 6; do
    aws_ ec2 delete-security-group --group-id "$g" 2>/dev/null && break
    sleep 5
  done
done
for k in $KEYS; do
  log "deleting key pair $k"
  aws_ ec2 delete-key-pair --key-name "$k" >/dev/null
done
for v in $VOLS; do
  log "deleting leftover volume $v"
  aws_ ec2 delete-volume --volume-id "$v" >/dev/null || true
done

rm -f "$STATE_FILE" "$KNOWN_HOSTS"
if [[ -f "$MAVIS_KEY_FILE" ]]; then
  log "removing local key file $MAVIS_KEY_FILE (its key pair is gone)"
  rm -f "$MAVIS_KEY_FILE"
fi
log "teardown complete"

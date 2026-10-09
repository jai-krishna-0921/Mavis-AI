#!/usr/bin/env bash
# Shared contract B (Plans 11 and 12): IAM role + instance profile `mavis-ec2` for the box, associated to the
# instance, and IMDSv2. The hop limit is raised to 2 (so the api, worker and timer containers can use the role)
# ONLY after the DOCKER-USER rule that keeps every other container away from 169.254.169.254 is verified on the
# box (imds.sh install, run by bootstrap.sh and deploy.sh); a failed check afterwards lowers it to 1 again.
# Attaches NO policies: each plan adds its own inline policy (mavis-backups, mavis-machine) in its own script.
# Idempotent. Dry run by default.
#   deploy/aws/iam-role.sh            print the plan
#   deploy/aws/iam-role.sh --apply    create what is missing
set -euo pipefail
# shellcheck source=deploy/aws/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need aws
state_load
APPLY=0
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    --dry-run) APPLY=0 ;;
    *) die "unknown argument: $a (usage: iam-role.sh [--dry-run|--apply])" ;;
  esac
done
ROLE=mavis-ec2
PROFILE=mavis-ec2
[[ -n "${MAVIS_INSTANCE_ID:-}" ]] || die "no MAVIS_INSTANCE_ID in state.env"

act() { # act DESCRIPTION -- aws args...
  local what="$1"; shift; shift
  if [[ "$APPLY" == 1 ]]; then log "$what"; aws_ "$@" >/dev/null; else printf 'PLAN: aws %s\n' "$*"; fi
}

log "account: $(aws_ sts get-caller-identity --query Account --output text) region: $AWS_REGION"
TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
if aws_ iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  log "role $ROLE exists"
else
  act "creating role $ROLE" -- iam create-role --role-name "$ROLE" --assume-role-policy-document "$TRUST" \
    --tags Key=Project,Value=mavis
fi
if aws_ iam get-instance-profile --instance-profile-name "$PROFILE" >/dev/null 2>&1; then
  log "instance profile $PROFILE exists"
else
  act "creating instance profile $PROFILE" -- iam create-instance-profile --instance-profile-name "$PROFILE" \
    --tags Key=Project,Value=mavis
  act "adding role to profile" -- iam add-role-to-instance-profile --instance-profile-name "$PROFILE" --role-name "$ROLE"
fi
CURRENT="$(aws_ ec2 describe-iam-instance-profile-associations --filters "Name=instance-id,Values=$MAVIS_INSTANCE_ID" \
  --query 'IamInstanceProfileAssociations[0].IamInstanceProfile.Arn' --output text 2>/dev/null || echo None)"
if [[ "$CURRENT" == *":instance-profile/$PROFILE" ]]; then
  log "profile already associated to $MAVIS_INSTANCE_ID"
else
  # A just-created profile is not visible to EC2 for a few seconds (IAM is eventually consistent): retry.
  for attempt in 1 2 3 4 5 6; do
    if [[ "$APPLY" == 0 ]]; then
      act "associating $PROFILE to $MAVIS_INSTANCE_ID" -- ec2 associate-iam-instance-profile \
        --instance-id "$MAVIS_INSTANCE_ID" --iam-instance-profile "Name=$PROFILE"; break
    fi
    if act "associating $PROFILE to $MAVIS_INSTANCE_ID (attempt $attempt)" -- ec2 associate-iam-instance-profile \
        --instance-id "$MAVIS_INSTANCE_ID" --iam-instance-profile "Name=$PROFILE"; then break; fi
    [[ "$attempt" == 6 ]] && die "could not associate $PROFILE after $attempt attempts"
    [[ -n "${MAVIS_SKIP_PROPAGATION_WAIT:-}" ]] || sleep 10
  done
fi
# 1. the guard must be in place first; refuse to raise the hop limit when it is not
if [[ "$APPLY" == 1 ]]; then
  "$AWS_DIR/imds.sh" verify-rule || die "refusing to raise the instance metadata hop limit: run deploy/aws/imds.sh install and fix the failure above"
else
  printf 'PLAN: imds.sh verify-rule (iptables guard in place and postgres blocked; the hop limit is not raised unless it passes)\n'
fi
act "IMDSv2 required, hop limit 2" -- ec2 modify-instance-metadata-options --instance-id "$MAVIS_INSTANCE_ID" \
  --http-tokens required --http-put-response-hop-limit 2 --http-endpoint enabled
# 2. and it must hold with the limit raised; otherwise roll back
if [[ "$APPLY" == 1 ]]; then
  if ! "$AWS_DIR/imds.sh" verify-containers; then
    "$AWS_DIR/imds.sh" lower-hop-limit
    die "verification failed after raising the hop limit; it was set back to 1"
  fi
else
  printf 'PLAN: imds.sh verify-containers (postgres and redis blocked, worker allowed; hop limit back to 1 if not)\n'
fi
log "done (apply=$APPLY)"

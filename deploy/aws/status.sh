#!/usr/bin/env bash
# Instance state, container state, memory use, and health.
set -euo pipefail
# shellcheck source=deploy/aws/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need aws curl
state_require
aws_ ec2 describe-instances --instance-ids "$MAVIS_INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].[InstanceId,InstanceType,State.Name,PublicIpAddress]' --output text
echo "host: https://$MAVIS_HOST"
echo "--- public /healthz"
curl -fsS -m 10 "https://$MAVIS_HOST/healthz" || echo "(unreachable)"
echo
echo "--- containers"
compose_remote ps
echo "--- readiness (from inside the stack)"
compose_remote exec -T api curl -fsS http://127.0.0.1:8000/readyz || true
echo
echo "--- memory"
ssh_box "free -m; docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}'"
echo "--- telegram webhook"
compose_remote exec -T api mavis telegram info || true

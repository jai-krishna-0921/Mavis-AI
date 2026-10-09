#!/usr/bin/env bash
# Phase 12 AWS resources (spec section 12; owner decision 2, approved 2026-10-08):
#   R1 inline policy `mavis-machine` on role mavis-ec2 (run iam-role.sh first)
#   R3 bucket mavis-machine-<account>-aps1 (private, SSE-S3, lifecycle by object tag and prefix)
#   R4b custom SANDBOX code interpreter `mavis_ci_sandbox` (only with --custom-interpreter)
#   R5 AWS Budget mavis-machine-monthly, $20/month, alerts at 50/80/100% to $MAVIS_BUDGET_EMAIL
#   machine.sh [--dry-run|--apply] [--custom-interpreter] [--quotas]
set -euo pipefail
# shellcheck source=deploy/aws/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need aws

APPLY=0; CUSTOM_CI=0; QUOTAS=0
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    --dry-run) APPLY=0 ;;
    --custom-interpreter) CUSTOM_CI=1 ;;
    --quotas) QUOTAS=1 ;;
    *) die "unknown argument: $a" ;;
  esac
done
ACCOUNT="$(aws_ sts get-caller-identity --query Account --output text)"
ROLE="${MAVIS_ROLE_NAME:-mavis-ec2}"
BUCKET="${MACHINE_BUCKET:-mavis-machine-${ACCOUNT}-aps1}"
BUDGET="${MACHINE_BUDGET_NAME:-mavis-machine-monthly}"
BUDGET_USD="${MACHINE_BUDGET_USD:-20}"
CI_ID="${AGENTCORE_CODE_INTERPRETER_ID:-aws.codeinterpreter.v1}"
BR_ID="${AGENTCORE_BROWSER_ID:-aws.browser.v1}"
: "${MACHINE_WORK_DAYS:=14}" "${MACHINE_INBOX_DAYS:=60}" "${MACHINE_OUT_DAYS:=60}" "${MACHINE_TMP_DAYS:=1}" "${MACHINE_E2E_DAYS:=30}"
if [[ "$APPLY" == 1 && -z "${MAVIS_BUDGET_EMAIL:-}" ]]; then
  die "set MAVIS_BUDGET_EMAIL (the owner's email for budget alerts) for --apply"
fi

run() { # run aws_ ARGS...: same output format as iam-role.sh's act (PLAN: aws ... on a dry run)
  shift
  if [[ "$APPLY" == 1 ]]; then log "apply: aws $*"; aws_ "$@" >/dev/null; else printf 'PLAN: aws %s\n' "$*"; fi
}

# --- R1: inline policy (no IAM, EC2 or control-plane actions) -----------------------------
ci_arn() { if [[ "$1" == aws.* ]]; then echo "arn:aws:bedrock-agentcore:$AWS_REGION:aws:$2/$1"; else echo "arn:aws:bedrock-agentcore:$AWS_REGION:$ACCOUNT:$2/$1"; fi; }
POLICY="$(cat <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"CodeInterpreter","Effect":"Allow","Action":["bedrock-agentcore:StartCodeInterpreterSession","bedrock-agentcore:InvokeCodeInterpreter","bedrock-agentcore:StopCodeInterpreterSession","bedrock-agentcore:GetCodeInterpreterSession"],"Resource":["$(ci_arn "$CI_ID" code-interpreter)"]},
 {"Sid":"Browser","Effect":"Allow","Action":["bedrock-agentcore:StartBrowserSession","bedrock-agentcore:StopBrowserSession","bedrock-agentcore:GetBrowserSession","bedrock-agentcore:ConnectBrowserAutomationStream"],"Resource":["$(ci_arn "$BR_ID" browser)"]},
 {"Sid":"WorkspaceObjects","Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:PutObjectTagging"],"Resource":["arn:aws:s3:::$BUCKET/*"]},
 {"Sid":"WorkspaceList","Effect":"Allow","Action":["s3:ListBucket"],"Resource":["arn:aws:s3:::$BUCKET"]}
]}
JSON
)"
aws_ iam get-role --role-name "$ROLE" >/dev/null 2>&1 || [[ "$APPLY" == 0 ]] || die "role $ROLE missing; run iam-role.sh --apply first"
run aws_ iam put-role-policy --role-name "$ROLE" --policy-name mavis-machine --policy-document "$POLICY"

# --- R3: bucket -------------------------------------------------------------------------------
if aws_ s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  log "bucket $BUCKET exists"
else
  run aws_ s3api create-bucket --bucket "$BUCKET" --create-bucket-configuration "LocationConstraint=$AWS_REGION"
fi
run aws_ s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
run aws_ s3api put-bucket-tagging --bucket "$BUCKET" --tagging 'TagSet=[{Key=Project,Value=mavis}]'
run aws_ s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
run aws_ s3api put-bucket-ownership-controls --bucket "$BUCKET" --ownership-controls \
  '{"Rules":[{"ObjectOwnership":"BucketOwnerEnforced"}]}'
tag_rule() { printf '{"ID":"%s","Status":"Enabled","Filter":{"Tag":{"Key":"cls","Value":"%s"}},"Expiration":{"Days":%s}}' "$1" "$2" "$3"; }
prefix_rule() { printf '{"ID":"%s","Status":"Enabled","Filter":{"Prefix":"%s"},"Expiration":{"Days":%s}}' "$1" "$2" "$3"; }
LIFECYCLE="{\"Rules\":[$(tag_rule work-14d work "$MACHINE_WORK_DAYS"),$(tag_rule inbox-60d inbox "$MACHINE_INBOX_DAYS"),$(tag_rule out-60d out "$MACHINE_OUT_DAYS"),$(prefix_rule tmp-1d tmp/ "$MACHINE_TMP_DAYS"),$(prefix_rule e2e-30d e2e/ "$MACHINE_E2E_DAYS"),{\"ID\":\"abort-mpu-1d\",\"Status\":\"Enabled\",\"Filter\":{\"Prefix\":\"\"},\"AbortIncompleteMultipartUpload\":{\"DaysAfterInitiation\":1}}]}"
run aws_ s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration "$LIFECYCLE"

# --- R4b: custom SANDBOX interpreter, only when the verify script found network in the managed one ---
if [[ "$CUSTOM_CI" == 1 ]]; then
  existing="$(aws_ bedrock-agentcore-control list-code-interpreters \
    --query "codeInterpreterSummaries[?name=='mavis_ci_sandbox'].codeInterpreterId" --output text 2>/dev/null || true)"
  if [[ -n "$existing" && "$existing" != "None" ]]; then
    log "custom interpreter exists: $existing (set AGENTCORE_CODE_INTERPRETER_ID=$existing)"
  else
    run aws_ bedrock-agentcore-control create-code-interpreter --name mavis_ci_sandbox \
      --network-configuration '{"networkMode":"SANDBOX"}'
    log "set AGENTCORE_CODE_INTERPRETER_ID to the id printed above and re-run machine.sh --apply (policy scope)"
  fi
fi

# --- R5: budget -------------------------------------------------------------------------------
if aws_ budgets describe-budget --account-id "$ACCOUNT" --budget-name "$BUDGET" >/dev/null 2>&1; then
  log "budget $BUDGET exists"
else
  BODY="{\"BudgetName\":\"$BUDGET\",\"BudgetLimit\":{\"Amount\":\"$BUDGET_USD\",\"Unit\":\"USD\"},\"TimeUnit\":\"MONTHLY\",\"BudgetType\":\"COST\",\"CostFilters\":{\"Service\":[\"Amazon Bedrock AgentCore\",\"Amazon Simple Storage Service\"]}}"
  notes=""
  for pct in 50 80 100; do
    notes+="{\"Notification\":{\"NotificationType\":\"ACTUAL\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":$pct,\"ThresholdType\":\"PERCENTAGE\"},\"Subscribers\":[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"${MAVIS_BUDGET_EMAIL:-owner@example.invalid}\"}]},"
  done
  run aws_ budgets create-budget --account-id "$ACCOUNT" --budget "$BODY" --notifications-with-subscribers "[${notes%,}]"
fi

if [[ "$QUOTAS" == 1 ]]; then
  aws_ service-quotas list-service-quotas --service-code bedrock-agentcore \
    --query 'Quotas[].[QuotaName,Value]' --output table || log "service quotas not listed for bedrock-agentcore"
fi
[[ "$APPLY" == 1 ]] || log "dry run only; re-run with --apply"
log "next: set WORKSPACE_BUCKET=$BUCKET, then run scripts/verify_agentcore.py on the box"

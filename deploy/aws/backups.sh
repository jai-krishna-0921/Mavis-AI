#!/usr/bin/env bash
# Off-box backups (multiuser plan, Task 18): the S3 bucket the box writes its nightly backups to, and the
# write-only inline policy `mavis-backups` on role mavis-ec2. Write-only on purpose: a compromised box can add
# backups but cannot read, overwrite history of, or delete them (versioning keeps every write; lifecycle
# rules do the expiry). Then installs the nightly job on the box (deploy/aws/box-backup.sh at 03:00).
# Idempotent. Dry run by default.
#   deploy/aws/backups.sh            print the plan
#   deploy/aws/backups.sh --apply    create what is missing and install the job
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
    *) die "unknown argument: $a (usage: backups.sh [--dry-run|--apply])" ;;
  esac
done
ROLE=mavis-ec2
ACCT="$(aws_ sts get-caller-identity --query Account --output text)"
BUCKET="mavis-backups-$ACCT-aps1"

act() { # act DESCRIPTION -- aws args...
  local what="$1"; shift; shift
  if [[ "$APPLY" == 1 ]]; then log "$what"; aws_ "$@" >/dev/null; else printf 'PLAN: aws %s\n' "$*"; fi
}

log "account: $ACCT region: $AWS_REGION bucket: $BUCKET"
if aws_ s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  log "bucket $BUCKET exists"
else
  act "creating bucket $BUCKET" -- s3api create-bucket --bucket "$BUCKET" \
    --create-bucket-configuration "LocationConstraint=$AWS_REGION" --object-ownership BucketOwnerEnforced
fi
act "blocking all public access" -- s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
act "default encryption SSE-S3" -- s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'
act "versioning on" -- s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled
LIFECYCLE='{"Rules":[
 {"ID":"postgres","Filter":{"Prefix":"postgres/"},"Status":"Enabled","Expiration":{"Days":35}},
 {"ID":"qdrant","Filter":{"Prefix":"qdrant/"},"Status":"Enabled","Expiration":{"Days":14}},
 {"ID":"neo4j","Filter":{"Prefix":"neo4j/"},"Status":"Enabled","Expiration":{"Days":14}},
 {"ID":"noncurrent","Filter":{},"Status":"Enabled","NoncurrentVersionExpiration":{"NoncurrentDays":7},
  "AbortIncompleteMultipartUpload":{"DaysAfterInitiation":1}}]}'
act "lifecycle rules" -- s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
  --lifecycle-configuration "${LIFECYCLE//$'\n'/}"
# Deny anything not over TLS.
TLS_POLICY="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"TlsOnly\",\"Effect\":\"Deny\",\"Principal\":\"*\",\"Action\":\"s3:*\",\"Resource\":[\"arn:aws:s3:::$BUCKET\",\"arn:aws:s3:::$BUCKET/*\"],\"Condition\":{\"Bool\":{\"aws:SecureTransport\":\"false\"}}}]}"
act "bucket policy: TLS only" -- s3api put-bucket-policy --bucket "$BUCKET" --policy "$TLS_POLICY"
WRITE_ONLY="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"WriteBackupsOnly\",\"Effect\":\"Allow\",\"Action\":[\"s3:PutObject\"],\"Resource\":\"arn:aws:s3:::$BUCKET/*\"}]}"
act "putting inline policy mavis-backups on $ROLE" -- iam put-role-policy --role-name "$ROLE" \
  --policy-name mavis-backups --policy-document "$WRITE_ONLY"

if [[ "$APPLY" == 1 ]]; then
  log "installing the nightly backup job on the box"
  scp_box "$(dirname "${BASH_SOURCE[0]}")/box-backup.sh" /tmp/box-backup.sh
  scp_box "$(dirname "${BASH_SOURCE[0]}")/graph_export.py" /tmp/graph_export.py
  ssh_box "sudo install -m 700 -o root /tmp/box-backup.sh /usr/local/bin/mavis-backup.sh \
    && sudo install -m 600 -o root /tmp/graph_export.py /usr/local/lib/mavis-graph_export.py \
    && printf 'BACKUP_BUCKET=%s\nMAVIS_DIR=%s\n' '$BUCKET' '$MAVIS_REMOTE_DIR' | sudo tee /etc/mavis-backup.env >/dev/null \
    && sudo chmod 600 /etc/mavis-backup.env \
    && echo '0 3 * * * root /usr/local/bin/mavis-backup.sh >>/var/log/mavis-backup.log 2>&1' \
       | sudo tee /etc/cron.d/mavis-backup >/dev/null && rm -f /tmp/box-backup.sh /tmp/graph_export.py"
  state_set MAVIS_BACKUP_BUCKET "$BUCKET"
else
  printf 'PLAN: install deploy/aws/box-backup.sh as /usr/local/bin/mavis-backup.sh (cron 03:00), BACKUP_BUCKET=%s\n' "$BUCKET"
fi

#!/usr/bin/env bash
# Create (or find) the AWS resources for the single-box Mavis deploy. Idempotent.
# Creates: key pair, security group, one t4g.small Ubuntu 24.04 arm64 instance, one Elastic IP.
# Everything is tagged Project=mavis; ids are written to deploy/aws/state.env.
set -euo pipefail
# shellcheck source=deploy/aws/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need aws curl

tagspec() { printf 'ResourceType=%s,Tags=[{Key=Project,Value=mavis},{Key=Name,Value=%s}]' "$1" "$2"; }

log "account: $(aws_ sts get-caller-identity --query Account --output text) region: $AWS_REGION"

# --- key pair ---------------------------------------------------------------
if aws_ ec2 describe-key-pairs --key-names "$MAVIS_KEY_NAME" >/dev/null 2>&1; then
  [[ -f "$MAVIS_KEY_FILE" ]] || die "key pair $MAVIS_KEY_NAME exists in AWS but $MAVIS_KEY_FILE is missing; delete the key pair or restore the file"
  log "key pair $MAVIS_KEY_NAME exists"
else
  [[ ! -e "$MAVIS_KEY_FILE" ]] || die "$MAVIS_KEY_FILE exists but key pair $MAVIS_KEY_NAME does not; move the file away first"
  log "creating key pair $MAVIS_KEY_NAME -> $MAVIS_KEY_FILE"
  mkdir -p "$(dirname "$MAVIS_KEY_FILE")"
  (umask 077; aws_ ec2 create-key-pair --key-name "$MAVIS_KEY_NAME" --key-type ed25519 \
    --tag-specifications "$(tagspec key-pair "$MAVIS_KEY_NAME")" --query KeyMaterial --output text >"$MAVIS_KEY_FILE")
  chmod 600 "$MAVIS_KEY_FILE"
fi
state_set MAVIS_KEY_NAME "$MAVIS_KEY_NAME"

# --- security group (default VPC) -------------------------------------------
VPC_ID="$(aws_ ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)"
[[ "$VPC_ID" != "None" ]] || die "no default VPC in $AWS_REGION"
SG_ID="$(aws_ ec2 describe-security-groups --filters "Name=group-name,Values=$MAVIS_SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text)"
if [[ "$SG_ID" == "None" ]]; then
  log "creating security group $MAVIS_SG_NAME"
  SG_ID="$(aws_ ec2 create-security-group --group-name "$MAVIS_SG_NAME" --description "Mavis AI single box" \
    --vpc-id "$VPC_ID" --tag-specifications "$(tagspec security-group "$MAVIS_SG_NAME")" --query GroupId --output text)"
else
  log "security group $SG_ID exists"
fi
state_set MAVIS_SG_ID "$SG_ID"

allow() { # allow PORT CIDR
  local out
  if ! out="$(aws_ ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol tcp --port "$1" --cidr "$2" 2>&1)"; then
    [[ "$out" == *InvalidPermission.Duplicate* ]] || die "authorize $1 $2: $out"
  fi
}
allow 80 0.0.0.0/0
allow 443 0.0.0.0/0

MY_IP="$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')"
[[ "$MY_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "could not determine public IP (got '$MY_IP')"
# keep exactly one SSH rule: the caller's current IP (re-run this script after your IP changes)
# shellcheck disable=SC2016
for cidr in $(aws_ ec2 describe-security-groups --group-ids "$SG_ID" \
  --query 'SecurityGroups[0].IpPermissions[?FromPort==`22`].IpRanges[].CidrIp' --output text); do
  if [[ "$cidr" != "$MY_IP/32" ]]; then
    log "revoking old ssh rule $cidr"
    aws_ ec2 revoke-security-group-ingress --group-id "$SG_ID" --protocol tcp --port 22 --cidr "$cidr" >/dev/null
  fi
done
allow 22 "$MY_IP/32"
log "ssh allowed from $MY_IP/32 only"

# --- instance ---------------------------------------------------------------
INSTANCE_ID="$(aws_ ec2 describe-instances \
  --filters "Name=tag:Project,Values=mavis" "Name=tag:Name,Values=$MAVIS_INSTANCE_NAME" \
            "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].InstanceId | [0]' --output text)"
if [[ "$INSTANCE_ID" == "None" || -z "$INSTANCE_ID" ]]; then
  AMI_ID="$(aws_ ssm get-parameter \
    --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id \
    --query Parameter.Value --output text)"
  log "launching $MAVIS_INSTANCE_TYPE from $AMI_ID"
  INSTANCE_ID="$(aws_ ec2 run-instances --image-id "$AMI_ID" --instance-type "$MAVIS_INSTANCE_TYPE" \
    --key-name "$MAVIS_KEY_NAME" --security-group-ids "$SG_ID" \
    --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=$MAVIS_VOLUME_GB,VolumeType=gp3,Encrypted=true,DeleteOnTermination=true}" \
    --metadata-options HttpTokens=required,HttpEndpoint=enabled \
    --tag-specifications "$(tagspec instance "$MAVIS_INSTANCE_NAME")" "$(tagspec volume "$MAVIS_INSTANCE_NAME")" \
    --query 'Instances[0].InstanceId' --output text)"
  rm -f "$KNOWN_HOSTS"  # a fresh instance has a fresh host key
else
  log "instance $INSTANCE_ID exists"
  if [[ "$(aws_ ec2 describe-instances --instance-ids "$INSTANCE_ID" --query 'Reservations[0].Instances[0].State.Name' --output text)" == "stopped" ]]; then
    log "starting stopped instance"
    aws_ ec2 start-instances --instance-ids "$INSTANCE_ID" >/dev/null
  fi
fi
state_set MAVIS_INSTANCE_ID "$INSTANCE_ID"
aws_ ec2 wait instance-running --instance-ids "$INSTANCE_ID"

# --- elastic IP -------------------------------------------------------------
ALLOC_ID="$(aws_ ec2 describe-addresses --filters "Name=tag:Project,Values=mavis" \
  --query 'Addresses[0].AllocationId' --output text)"
if [[ "$ALLOC_ID" == "None" || -z "$ALLOC_ID" ]]; then
  log "allocating Elastic IP"
  ALLOC_ID="$(aws_ ec2 allocate-address --domain vpc --tag-specifications "$(tagspec elastic-ip "$MAVIS_INSTANCE_NAME")" \
    --query AllocationId --output text)"
fi
aws_ ec2 associate-address --allocation-id "$ALLOC_ID" --instance-id "$INSTANCE_ID" >/dev/null
EIP="$(aws_ ec2 describe-addresses --allocation-ids "$ALLOC_ID" --query 'Addresses[0].PublicIp' --output text)"
state_set MAVIS_ALLOC_ID "$ALLOC_ID"
state_set MAVIS_EIP "$EIP"
state_set MAVIS_DOMAIN "${EIP//./-}.sslip.io"

log "done: instance=$INSTANCE_ID eip=$EIP host=${EIP//./-}.sslip.io"
log "next: deploy/aws/bootstrap.sh"

#!/usr/bin/env bash
# Mavis configuration and secrets live in AWS SSM Parameter Store, not on the box's disk.
# Every key is one SecureString (standard tier, free) under /mavis/prod/<KEY>, encrypted with the AWS
# managed key alias/aws/ssm and tagged Project=mavis. Non-secret settings (ENV, DOMAIN, TELEGRAM_MODE...)
# are stored the same way: one rule, one path, one render. Empty values are never stored (SSM rejects them
# and the compose file defaults them).
#
# Dry run by default; --apply writes. Values are never printed, never put on a command line (ps) and only
# touch disk in a mode 600 file inside a private temp dir that is removed on exit.
#
#   secrets.sh push [--apply] [--with-deploy] [--file F]   owner keys from F (default $DEMO_ENV); changed keys only
#                                                         --with-deploy also pushes config keys (overwrite) and
#                                                         generated secrets (create only, never overwritten)
#   secrets.sh seed-from-box [--apply]                    one-time migration: keys only in the box's old
#                                                         /opt/mavis/.env are created in SSM, existing ones kept
#   secrets.sh list                                       names and last-modified only
#   secrets.sh pull --to FILE                             decrypt everything into a mode 600 file (deploy.sh)
#   secrets.sh set KEY [--apply] [--force-rotate-generated]   one key, value on stdin (rotation, hand-added keys)
#   secrets.sh render                                     ON THE BOX, as root: write /run/mavis/mavis.env
#   secrets.sh render-remote                              run render on the box over ssh
#   secrets.sh install-box [--dry-run]                    AWS CLI v2, this script as /usr/local/sbin/mavis-secrets,
#                                                         and the mavis-secrets.service unit (bootstrap.sh, deploy.sh)
set +x
set -euo pipefail
umask 077

SSM_PREFIX=/mavis/prod/
REQUIRED_KEYS="POSTGRES_PASSWORD NEO4J_PASSWORD TELEGRAM_WEBHOOK_SECRET NATIVE_TOKEN_KEK"
# Keys that come from the owner's local env file.
OWNER_KEYS="OLLAMA_API_KEY TAVILY_API_KEY COMPOSIO_API_KEY TELEGRAM_BOT_TOKEN ALLOWED_TELEGRAM_CHAT_IDS
  LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY TEST_MIRROR_CHAT_ID MACHINE_LIVE_TOKEN_SECRET E2B_API_KEY
  GOOGLE_OAUTH_CLIENT_ID GOOGLE_OAUTH_CLIENT_SECRET SLACK_CLIENT_ID SLACK_CLIENT_SECRET SLACK_SIGNING_SECRET
  DASHBOARD_ENABLED GOOGLE_SIGNIN_ENABLED TELEGRAM_BOT_USERNAME"
# Settings deploy.sh decides (a local dev env file must never push these on its own).
CONFIG_KEYS="ENV PUBLIC_BASE_URL DOMAIN TELEGRAM_MODE ACCESS_MODE INTEGRATION_PROVIDER WORKSPACE_BUCKET
  DEFAULT_TIMEZONE LLM_MAX_CONCURRENCY LIVE_TEST_ENABLED TEST_TELEGRAM_CHAT_ID"
# Generated once, created only when missing, never overwritten. Losing NATIVE_TOKEN_KEK orphans every
# user's connected Google and Slack account.
GENERATED_KEYS="POSTGRES_PASSWORD NEO4J_PASSWORD TELEGRAM_WEBHOOK_SECRET NATIVE_TOKEN_KEK"

PY=$(cat <<'PYEOF'
import json, os, re, sys

PREFIX = "/mavis/prod/"
KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*")


def parse_env(path):
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            s = line.rstrip("\r\n").lstrip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            if k.startswith("export "):
                k = k[7:].strip()
            if not KEY_RE.fullmatch(k):
                continue
            v = v.rstrip("\r")
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k] = v
    return out


def stored(path):
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {p["Name"].rsplit("/", 1)[-1]: p["Value"] for p in data.get("Parameters", [])}


def write600(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def emit(key, value, current, overwrite, outdir):
    if not value:
        return None
    body = {"Name": PREFIX + key, "Value": value, "Type": "SecureString", "KeyId": "alias/aws/ssm",
            "Tier": "Standard"}
    if key not in current:
        body["Tags"] = [{"Key": "Project", "Value": "mavis"}]
        status = "new"
    elif overwrite and current[key] != value:
        body["Overwrite"] = True
        status = "updated"
    else:
        return "unchanged"
    write600(os.path.join(outdir, "put-%s.json" % key), json.dumps(body))
    return status


op = sys.argv[1]
if op == "diff":  # diff STORED DESIRED OUTDIR OVER CREATE  (CREATE "*" = every key of DESIRED, create only)
    cur, want, outdir = stored(sys.argv[2]), parse_env(sys.argv[3]), sys.argv[4]
    over = sys.argv[5].split()
    create = sorted(want) if sys.argv[6].strip() == "*" else sys.argv[6].split()
    for k in over:
        st = emit(k, want.get(k, ""), cur, True, outdir)
        if st:
            print("%s %s" % (k, st))
    for k in create:
        if k in over:
            continue
        st = emit(k, want.get(k, ""), cur, False, outdir)
        if st:
            print("%s %s" % (k, "kept" if st == "unchanged" else st))
elif op == "setone":  # setone STORED KEY OUTDIR  (value on stdin)
    cur, key, outdir = stored(sys.argv[2]), sys.argv[3], sys.argv[4]
    value = sys.stdin.read()
    if value.endswith("\n"):
        value = value[:-1]
    if "\n" in value or not value:
        sys.exit("value must be one non-empty line")
    print("%s %s" % (key, emit(key, value, cur, True, outdir)))
elif op == "envfile":  # envfile STORED OUT REQUIRED
    cur = stored(sys.argv[2])
    missing = [k for k in sys.argv[4].split() if not cur.get(k)]
    if missing:
        sys.exit("refusing to write: required parameters missing: " + " ".join(missing))
    bad = [k for k, v in cur.items() if "\n" in v or "\r" in v or not KEY_RE.fullmatch(k)]
    if bad:
        sys.exit("refusing to write: unusable parameters: " + " ".join(sorted(bad)))
    write600(sys.argv[3], "".join("%s=%s\n" % (k, cur[k]) for k in sorted(cur)))
    print("%d" % len(cur))
elif op == "count":
    print(len(stored(sys.argv[2])))
elif op == "has":  # has STORED KEY
    sys.exit(0 if stored(sys.argv[2]).get(sys.argv[3]) else 1)
else:
    sys.exit("unknown op " + op)
PYEOF
)
py() { python3 -I -c "$PY" "$@"; }

TMP=""
mktmp() {
  TMP="$(mktemp -d)"
  chmod 700 "$TMP"
  trap 'rm -rf "$TMP"' EXIT
}

# --- render: runs ON THE BOX as root; self-contained (no common.sh, no profile: the instance role signs) ---
do_render() {
  local out="${MAVIS_ENV_FILE:-/run/mavis/mavis.env}" dir count
  if [[ "$(id -u)" != 0 && -z "${MAVIS_RENDER_ALLOW_NONROOT:-}" ]]; then
    echo "error: render must run as root on the box" >&2; exit 1
  fi
  dir="$(dirname "$out")"
  mkdir -p "$dir"; chmod 700 "$dir"
  mktmp_in "$dir"
  aws ssm get-parameters-by-path --region "${AWS_REGION:-ap-south-1}" --path "$SSM_PREFIX" --recursive \
    --with-decryption --output json >"$TMP/stored.json" \
    || { echo "error: cannot read $SSM_PREFIX from SSM (instance role, network?); $out left untouched" >&2; exit 1; }
  count="$(py envfile "$TMP/stored.json" "$TMP/mavis.env" "$REQUIRED_KEYS")" || exit 1
  mv -f "$TMP/mavis.env" "$out"
  chmod 600 "$out"
  echo "rendered $count keys to $out"
}
mktmp_in() {
  TMP="$(mktemp -d -p "$1")"
  chmod 700 "$TMP"
  trap 'rm -rf "$TMP"' EXIT
}

cmd="${1:-}"
if [[ "$cmd" == render ]]; then do_render; exit 0; fi

# shellcheck source=deploy/aws/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

APPLY=0 WITH_DEPLOY=0 FILE="$DEMO_ENV" TO="" FORCE_GEN=0 DRY=0
shift || true
POS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --dry-run) DRY=1 ;;
    --with-deploy) WITH_DEPLOY=1 ;;
    --force-rotate-generated) FORCE_GEN=1 ;;
    --file) FILE="${2:?--file needs a path}"; shift ;;
    --to) TO="${2:?--to needs a path}"; shift ;;
    -*) die "unknown argument: $1" ;;
    *) POS+=("$1") ;;
  esac
  shift
done

fetch_stored() { # fetch_stored FILE: decrypted parameters as JSON, in the private temp dir
  aws_ ssm get-parameters-by-path --path "$SSM_PREFIX" --recursive --with-decryption --output json >"$1" \
    || die "cannot read $SSM_PREFIX from SSM"
}

# apply_puts OUTDIR: one put-parameter per planned file, input through file:// (never argv)
apply_puts() {
  local f
  for f in "$1"/put-*.json; do
    [[ -e "$f" ]] || continue
    aws_ ssm put-parameter --cli-input-json "file://$f" >/dev/null || die "put-parameter failed for ${f##*/put-}"
  done
}

report() { # report: "KEY status" lines from stdin
  local k st
  while read -r k st; do
    if [[ "$APPLY" == 1 ]]; then printf '%s: %s\n' "$k" "$st"; else printf 'PLAN %s: %s\n' "$k" "$st"; fi
  done
}

do_push_like() { # do_push_like OVER CREATE SOURCE
  mktmp
  mkdir "$TMP/out"
  fetch_stored "$TMP/stored.json"
  [[ -f "$3" ]] || die "env file not found: $3"
  py diff "$TMP/stored.json" "$3" "$TMP/out" "$1" "$2" | report
  if [[ "$APPLY" == 1 ]]; then apply_puts "$TMP/out"; else log "dry run: nothing written (use --apply)"; fi
}

case "$cmd" in
  push)
    if [[ "$WITH_DEPLOY" == 1 ]]; then
      do_push_like "$OWNER_KEYS $CONFIG_KEYS" "$GENERATED_KEYS" "$FILE"
    else
      do_push_like "$OWNER_KEYS" "" "$FILE"
    fi ;;
  seed-from-box)
    need ssh
    state_require
    mktmp
    box_env="$MAVIS_REMOTE_DIR/.env"
    rc=0
    ssh_box "test -f $box_env" || rc=$?
    [[ "$rc" == 0 ]] || die "no $box_env on the box (ssh exit $rc): nothing to seed"
    ssh_box "sudo cat $box_env" >"$TMP/box.env" || die "cannot read $box_env"
    mkdir "$TMP/out"
    fetch_stored "$TMP/stored.json"
    py diff "$TMP/stored.json" "$TMP/box.env" "$TMP/out" "" "*" | report
    if ! grep -q '^NATIVE_TOKEN_KEK=.' "$TMP/box.env"; then log "warning: the box .env has no NATIVE_TOKEN_KEK"; fi
    if [[ "$APPLY" == 1 ]]; then apply_puts "$TMP/out"; else log "dry run: nothing written (use --apply)"; fi ;;
  list)
    aws_ ssm describe-parameters --parameter-filters "Key=Path,Option=Recursive,Values=${SSM_PREFIX%/}" \
      --query 'Parameters[].[Name,LastModifiedDate]' --output text ;;
  pull)
    [[ -n "$TO" ]] || die "usage: secrets.sh pull --to FILE"
    mktmp
    fetch_stored "$TMP/stored.json"
    # a store without the required keys (fresh, or partly migrated) is still a valid base for deploy.sh
    : >"$TO"; chmod 600 "$TO"
    python3 -I -c "$PY" envfile "$TMP/stored.json" "$TO" "" >/dev/null ;;
  set)
    key="${POS[0]:-}"
    [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || die "usage: printf VALUE | secrets.sh set KEY [--apply]"
    if [[ " $GENERATED_KEYS " == *" $key "* && "$FORCE_GEN" != 1 ]]; then
      die "$key is a generated secret and must not be rotated by accident (see docs/SECRETS.md); --force-rotate-generated overrides"
    fi
    mktmp
    mkdir "$TMP/out"
    fetch_stored "$TMP/stored.json"
    py setone "$TMP/stored.json" "$key" "$TMP/out" | report
    if [[ "$APPLY" == 1 ]]; then apply_puts "$TMP/out"; else log "dry run: nothing written (use --apply)"; fi ;;
  render-remote)
    need ssh
    state_require
    ssh_box "sudo /usr/local/sbin/mavis-secrets render" ;;
  install-box)
    need ssh scp
    state_require
    if [[ "$DRY" == 1 ]]; then
      printf 'PLAN: install AWS CLI v2 if missing, /usr/local/sbin/mavis-secrets, mavis-secrets.service on %s\n' "$MAVIS_EIP"
      exit 0
    fi
    scp_box "$AWS_DIR/secrets.sh" /tmp/mavis-secrets.sh
    ssh_box "AWS_REGION='$AWS_REGION' sudo -E bash -s" <<'REMOTE'
set -euo pipefail
if ! command -v aws >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get -o DPkg::Lock::Timeout=300 -qq update
  apt-get -o DPkg::Lock::Timeout=300 -qq install -y unzip curl python3 >/dev/null
  case "$(uname -m)" in
    aarch64|arm64) zip=awscli-exe-linux-aarch64.zip ;;
    x86_64) zip=awscli-exe-linux-x86_64.zip ;;
    *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;;
  esac
  d="$(mktemp -d)"
  curl -fsSL "https://awscli.amazonaws.com/$zip" -o "$d/awscliv2.zip"
  unzip -q "$d/awscliv2.zip" -d "$d"
  "$d/aws/install" --update
  rm -rf "$d"
fi
aws --version
install -m 755 -o root -g root /tmp/mavis-secrets.sh /usr/local/sbin/mavis-secrets
rm -f /tmp/mavis-secrets.sh
cat >/etc/systemd/system/mavis-secrets.service <<EOT
[Unit]
Description=Render /run/mavis/mavis.env from SSM Parameter Store
Wants=network-online.target
After=network-online.target
Before=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=AWS_REGION=${AWS_REGION:-ap-south-1}
ExecStart=/usr/local/sbin/mavis-secrets render
Restart=on-failure
RestartSec=15
TimeoutStartSec=180

[Install]
WantedBy=multi-user.target
EOT
systemctl daemon-reload
systemctl enable mavis-secrets.service
REMOTE
    ;;
  *) die "usage: secrets.sh push|seed-from-box|list|pull|set|render|render-remote|install-box (see the header)" ;;
esac

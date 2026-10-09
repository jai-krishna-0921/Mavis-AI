#!/usr/bin/env bash
# One-time (idempotent) host setup over SSH: docker + compose plugin, 2 GB swapfile,
# unattended-upgrades, ufw. Safe to re-run.
set -euo pipefail
# shellcheck source=deploy/aws/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need ssh
state_require

log "waiting for ssh on $MAVIS_EIP"
wait_for_ssh

log "bootstrapping host"
ssh_box "MAVIS_REMOTE_DIR='$MAVIS_REMOTE_DIR' MAVIS_SSH_USER='$MAVIS_SSH_USER' sudo -E bash -s" <<'REMOTE'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
# first boot: cloud-init and unattended-upgrades hold the dpkg lock
cloud-init status --wait >/dev/null 2>&1 || true
APT="apt-get -o DPkg::Lock::Timeout=300 -qq"

# --- swap: 2 GB, low swappiness (swap is a safety net, not working memory) ---
if ! swapon --show=NAME --noheadings | grep -q '^/swapfile$'; then
  [ -f /swapfile ] || { fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048; }
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
echo 'vm.swappiness=20' >/etc/sysctl.d/99-mavis.conf
sysctl -q -p /etc/sysctl.d/99-mavis.conf

# --- packages ---
$APT update
$APT install -y ca-certificates curl gnupg rsync ufw unattended-upgrades

# --- docker (official apt repo, arm64) ---
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    >/etc/apt/sources.list.d/docker.list
  $APT update
  $APT install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
usermod -aG docker "$MAVIS_SSH_USER"

# --- unattended security upgrades (no automatic reboot) ---
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOT'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOT
cat >/etc/apt/apt.conf.d/52mavis-unattended <<'EOT'
Unattended-Upgrade::Automatic-Reboot "false";
EOT
systemctl enable --now unattended-upgrades

# --- firewall: ssh, http, https. Docker publishes 80/443 itself; qdrant is bound to loopback only. ---
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

# --- nightly Postgres backup: 03:00 pg_dump, keep 7, mode 600, root only ---
cat >/usr/local/bin/mavis-backup.sh <<EOT
#!/usr/bin/env bash
set -euo pipefail
umask 077
d=/var/backups/mavis
mkdir -p "\$d"
cd "$MAVIS_REMOTE_DIR"
f="\$d/mavis-\$(date +%Y%m%d-%H%M%S).sql.gz"
docker compose -f docker-compose.prod.yml exec -T postgres pg_dump -U mavis -d mavis --no-owner --no-acl | gzip >"\$f.tmp"
mv "\$f.tmp" "\$f"
chmod 600 "\$f"
ls -1t "\$d"/mavis-*.sql.gz | tail -n +8 | xargs -r rm -f
EOT
chmod 700 /usr/local/bin/mavis-backup.sh
mkdir -p /var/backups/mavis
chmod 700 /var/backups/mavis
echo '0 3 * * * root /usr/local/bin/mavis-backup.sh >>/var/log/mavis-backup.log 2>&1' >/etc/cron.d/mavis-backup
chmod 644 /etc/cron.d/mavis-backup

mkdir -p "$MAVIS_REMOTE_DIR"
chown "$MAVIS_SSH_USER:$MAVIS_SSH_USER" "$MAVIS_REMOTE_DIR"
docker --version
docker compose version
free -m
REMOTE
log "installing the instance metadata guard (only api, worker and timer may reach 169.254.169.254)"
"$AWS_DIR/imds.sh" install
log "bootstrap done. next: deploy/aws/deploy.sh --no-webhook"

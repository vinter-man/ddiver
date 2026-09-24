#!/usr/bin/env bash
# One-time server setup. Run as root on a fresh Debian or Ubuntu box:
#
#   bash bootstrap.sh
#
# Afterwards the box has Docker, a firewall that only lets SSH in, a `deploy` user whose SSH
# key can do nothing except run deploy.sh, and /srv laid out so a second project can move in
# later without colliding with this one.
#
# It is idempotent: run it again after changing something and it will only fix what drifted.

set -Eeuo pipefail

APP_USER="${APP_USER:-deploy}"
APP_DIR="${APP_DIR:-/srv/ddiver}"
REPO="${REPO:-git@github.com:vinter-man/ddiver.git}"
SWAP_GB="${SWAP_GB:-2}"

log() { printf '\n=== %s\n' "$*"; }
[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }

log "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git ufw fail2ban unattended-upgrades chrony

log "unattended security updates"
# The box will sit unattended between visits; unpatched SSH is the thing that actually gets
# these machines taken over.
dpkg-reconfigure -f noninteractive unattended-upgrades

log "clock"
# Notifications say "12 s ago". A drifting clock makes every one of them a small lie.
systemctl enable --now chrony

log "swap (${SWAP_GB}G)"
if [[ ! -f /swapfile ]]; then
	fallocate -l "${SWAP_GB}G" /swapfile
	chmod 600 /swapfile
	mkswap /swapfile >/dev/null
	swapon /swapfile
	grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
	# Swap here is a safety net for `docker build`, not a place to run from.
	sysctl -qw vm.swappiness=10
	grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
fi

log "docker"
if ! command -v docker >/dev/null; then
	curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

log "firewall"
# Everything this project serves is bound to 127.0.0.1 and reached over an SSH tunnel, so
# nothing but SSH has any business being open.
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null
systemctl enable --now fail2ban

log "ssh hardening"
install -d -m 755 /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/10-ddiver.conf <<'EOF'
PasswordAuthentication no
PermitRootLogin prohibit-password
KbdInteractiveAuthentication no
EOF
sshd -t && systemctl reload ssh 2>/dev/null || systemctl reload sshd

log "user ${APP_USER}"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$APP_USER"
usermod -aG docker "$APP_USER"
install -d -m 700 -o "$APP_USER" -g "$APP_USER" "/home/$APP_USER/.ssh"

log "/srv layout"
# One directory per project, each its own compose project, so volumes and networks from two
# pet projects can never be mistaken for each other.
install -d -m 755 /srv
install -d -m 755 -o "$APP_USER" -g "$APP_USER" "$APP_DIR"

log "deploy key for github"
KEY="/home/$APP_USER/.ssh/id_ed25519"
if [[ ! -f "$KEY" ]]; then
	sudo -u "$APP_USER" ssh-keygen -t ed25519 -N '' -C "ddiver@$(hostname)" -f "$KEY" >/dev/null
fi
# The redirect belongs to root's shell, so write it as root and hand it over afterwards
# rather than letting `sudo -u` meet a file it does not own.
KNOWN="/home/$APP_USER/.ssh/known_hosts"
ssh-keyscan -t ed25519,rsa github.com 2>/dev/null >> "$KNOWN"
sort -u -o "$KNOWN" "$KNOWN"
chown "$APP_USER:$APP_USER" "$KNOWN"
chmod 644 "$KNOWN"

log "clone"
if [[ -d "$APP_DIR/.git" ]]; then
	# Idempotent means current, not merely present: the forced command runs a script that
	# lives in the checkout, so a stale one cannot deploy itself out of being stale.
	sudo -u "$APP_USER" git -C "$APP_DIR" fetch --quiet origin
	sudo -u "$APP_USER" git -C "$APP_DIR" reset --hard --quiet origin/main
	echo "updated to $(sudo -u "$APP_USER" git -C "$APP_DIR" rev-parse --short HEAD)"
else
	echo "Add this as a read-only deploy key on the repository, then re-run:"
	echo
	cat "$KEY.pub"
	echo
	sudo -u "$APP_USER" git clone --quiet "$REPO" "$APP_DIR" 2>/dev/null \
		|| { echo "clone failed — the deploy key above is not on the repo yet"; exit 1; }
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
# data/ is written by the container, which runs as the image's own unprivileged user rather
# than as $APP_USER. On a Linux bind mount the numeric owner is what counts, so the directory
# has to belong to that uid or the session file cannot be created at all.
CONTAINER_UID=10001
install -d -m 700 -o "$CONTAINER_UID" -g "$CONTAINER_UID" "$APP_DIR/data"

log "ci key"
# A separate key, used by GitHub Actions to reach *this box*, restricted to one command.
# A leaked deploy secret can then deploy and nothing else: no shell, no port forwarding.
CI_KEY="/home/$APP_USER/.ssh/ci_ed25519"
if [[ ! -f "$CI_KEY" ]]; then
	sudo -u "$APP_USER" ssh-keygen -t ed25519 -N '' -C "github-actions" -f "$CI_KEY" >/dev/null
fi
AUTH="/home/$APP_USER/.ssh/authorized_keys"
FORCED='command="'"$APP_DIR"'/deploy/deploy.sh",no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding'
touch "$AUTH"
grep -q "github-actions" "$AUTH" || echo "$FORCED $(cat "$CI_KEY.pub")" >> "$AUTH"
chown "$APP_USER:$APP_USER" "$AUTH"
chmod 600 "$AUTH"

cat <<EOF

=== done

Next, in order:

  1. .env          scp it to $APP_DIR/.env (it is not in git, and must not be)
  2. login         sudo -u $APP_USER -i
                   cd $APP_DIR && docker compose run --rm ddiver login --phone +…
                   … then --code, then --password
  3. start         docker compose -f docker-compose.yml \\
                                  -f deploy/docker-compose.observability.yml up -d
  4. CI secrets    these three go into the repository's Actions secrets:

DEPLOY_HOST=$(hostname -I 2>/dev/null | awk '{print $1}')
DEPLOY_USER=$APP_USER
DEPLOY_HOST_KEY=  # from your own machine: ssh-keyscan -t ed25519 THIS_HOST

  DEPLOY_SSH_KEY is the private half of the CI key, at $CI_KEY. Move it without
  putting it on a screen or in a shell history:

      ssh root@this-box 'cat $CI_KEY' | gh secret set DEPLOY_SSH_KEY --repo OWNER/REPO

  Printing it here instead would write a key that can reach this machine into
  whatever scrollback, log or transcript is watching.
EOF

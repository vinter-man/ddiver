#!/usr/bin/env bash
# Render the alerting provisioning files, or remove them.
#
# Grafana loads everything under its provisioning directory, and a contact point with no token
# is a fatal configuration error — so the files have to be absent on a box that has no alert
# bot, and present with real values on one that does. Rendering into the directory Grafana
# already mounts also avoids a second bind mount nested inside the first, which Docker refuses
# because the parent is read-only.
#
# Called by deploy.sh on every deploy and by bootstrap.sh on setup. Safe to run by hand.

set -Eeuo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SRC="$APP_DIR/deploy/grafana/alerting"
DST="$APP_DIR/deploy/grafana/provisioning/alerting"

token=""
chat_id=""
if [[ -f "$APP_DIR/.env" ]]; then
	# Read, never source: this runs from the deploy key's forced command and must not execute
	# whatever happens to be in that file.
	token="$(sed -n 's/^[[:space:]]*ALERT_TELEGRAM_TOKEN=//p' "$APP_DIR/.env" | tail -1)"
	chat_id="$(sed -n 's/^[[:space:]]*ALERT_TELEGRAM_CHAT_ID=//p' "$APP_DIR/.env" | tail -1)"
fi

mkdir -p "$DST"

if [[ -z "$token" || -z "$chat_id" ]]; then
	rm -f "$DST"/*.yml
	echo "alerting: not configured, provisioning directory left empty"
	exit 0
fi

# A '|' or a newline in either value would corrupt the substitution below. Both are numeric or
# base64url in practice, so this is a shape check rather than an escaping scheme.
if [[ ! "$chat_id" =~ ^-?[0-9]+$ ]]; then
	echo "alerting: ALERT_TELEGRAM_CHAT_ID must be a number, got something else" >&2
	exit 1
fi
if [[ ! "$token" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
	echo "alerting: ALERT_TELEGRAM_TOKEN does not look like a BotFather token" >&2
	exit 1
fi

# 0644, not 0600. Grafana runs as its own uid inside the container and reads these through a
# bind mount, where the numeric owner is what counts — a file owned by the deploy user and
# readable only by it makes Grafana exit 1 with "permission denied" and restart forever. The
# token is deliberately not in this file, so there is nothing here to protect.
sed -e "s|@@ALERT_TELEGRAM_CHAT_ID@@|$chat_id|g" \
    "$SRC/contact-points.yml.tmpl" > "$DST/contact-points.yml"
chmod 644 "$DST/contact-points.yml"
install -m 644 "$SRC/rules.yml" "$DST/rules.yml"
echo "alerting: rendered for chat $chat_id (token stays in the environment)"

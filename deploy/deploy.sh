#!/usr/bin/env bash
# Update ddiver to origin/main and restart it, or put back what was working.
#
# This is what CI runs over SSH, and it is the only thing the deploy key is allowed to run
# (see the forced command in bootstrap.sh). It takes no arguments on purpose: a key that can
# pass arguments to a shell script is a key that can run a shell.
#
# It never touches .env or data/ — `git reset --hard` leaves ignored files alone, and the
# session, the database and the heartbeat all live in data/.

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/srv/ddiver}"
# node-exporter is opt-in because on Docker Desktop it cannot mount `/` and would be measuring
# a Linux VM rather than the Mac. This script only ever runs on the box itself, where both of
# those stop being true — and without it prometheus.yml scrapes a target that never exists.
export COMPOSE_PROFILES="${COMPOSE_PROFILES:-host-metrics}"
COMPOSE_FILES=(-f docker-compose.yml -f deploy/docker-compose.observability.yml)
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-240}"
CONTAINER="${CONTAINER:-ddiver}"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { log "FAILED: $*"; exit 1; }

_alerting_config() {
	# What Grafana would load, as text. With alerts off there are no files, the glob stays
	# literal and `cat` exits 1 — which `pipefail` turns into the status of the assignment that
	# calls this, and `set -e` turns that into a dead deploy: after the reset, after the build,
	# with no output, no `up -d` and no rollback. Alerts off is the documented default, so that
	# was every box nobody had configured alerting on.
	#
	# The failure is swallowed rather than avoided. `shopt -s nullglob` would be the other way
	# and a worse one: with no arguments `cat` reads stdin, which under the deploy key's forced
	# command is the SSH channel. Comparing the text rather than a digest keeps this to one
	# command that exists everywhere — `sha1sum` is Linux-only and `shasum` needs perl.
	cat "$APP_DIR"/deploy/grafana/provisioning/alerting/*.yml 2>/dev/null || true
}

health_of() {
	docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
		"$CONTAINER" 2>/dev/null || echo "missing"
}

wait_for_health() {
	local deadline=$(( SECONDS + HEALTH_TIMEOUT )) state
	while (( SECONDS < deadline )); do
		state="$(health_of)"
		case "$state" in
			healthy) return 0 ;;
			# `starting` is the container's start-period; `none` means no healthcheck at all,
			# which for this image means something is wrong with the build.
			unhealthy) return 1 ;;
		esac
		sleep 5
	done
	log "still '$(health_of)' after ${HEALTH_TIMEOUT}s"
	return 1
}

cd "$APP_DIR" || die "no $APP_DIR"
# shellcheck disable=SC2016  # the backticks are prose, not a substitution: that was the bug
[[ -f .env ]] || die '.env is missing — run the bootstrap and `ddiver login` first'

# Two deploys at once would interleave a git reset with somebody else's build. CI serialises
# its own runs, but a hand-run deploy does not know about them, and neither does a re-run of
# a cancelled job. Wait rather than fail: the second one is usually the newer commit.
exec {lock}> "$APP_DIR/.deploy.lock"
flock --wait 600 "$lock" || die "another deploy has held the lock for ten minutes"

# Whether it was working *before* matters: if the session has expired, the container is
# unhealthy whatever the code does, and rolling back would only thrash.
was_healthy=false
[[ "$(health_of)" == "healthy" ]] && was_healthy=true

previous="$(git rev-parse HEAD)"
# What this run is executing. `git reset --hard` below replaces the file through a rename, so
# the already-open copy keeps running — which means a change to this script takes effect on
# the deploy *after* the one that ships it. That is not a bug to fix here (re-exec'ing would
# have to hand over the lock), but it is a silent surprise unless it is said out loud.
running="$(cat "$0")"
git fetch --prune --quiet origin

# The caller may name the commit its tests passed on. It is attacker-controlled — this is the
# forced command of a key, and the client chooses what to ask for — so it is accepted only as
# a full sha that is already an ancestor of origin/main. That is enough to stop the two-quick-
# pushes race (deploying a commit whose own tests are still running) without letting anybody
# pick an arbitrary or older tree.
target="$(git rev-parse origin/main)"
asked="${SSH_ORIGINAL_COMMAND:-}"
if [[ -n "$asked" ]]; then
	if [[ ! "$asked" =~ ^[0-9a-f]{40}$ ]]; then
		# Ignored rather than fatal: the forced command means whatever a human types lands
		# here, and the documented by-hand form is the script's own path. Falling back to
		# origin/main is the safe answer — it is the commit CI tested most recently.
		log "ignoring a target that is not a full commit sha; deploying ${target:0:8}"
	elif ! git merge-base --is-ancestor "$asked" "$target" 2>/dev/null; then
		log "ignoring $asked: not on origin/main, deploying ${target:0:8} instead"
	elif ! git merge-base --is-ancestor "$previous" "$asked" 2>/dev/null; then
		# Ancestry alone accepts every commit ever merged, so a leaked key could pin the box
		# to an old tree and keep it there. The target must also be at or ahead of what is
		# deployed now: forward only, and only onto main.
		log "ignoring $asked: it is behind the deployed ${previous:0:8}"
	else
		target="$asked"
	fi
fi

if [[ "$previous" == "$target" ]]; then
	log "already at ${target:0:8}; restarting anyway to pick up any compose change"
else
	log "deploying ${previous:0:8} -> ${target:0:8}"
	git log --oneline --no-decorate "$previous..$target" | sed 's/^/    /'
fi

git reset --hard --quiet "$target"

if [[ "$(cat "$0")" != "$running" ]]; then
	log "NOTE: deploy.sh itself changed in this commit"
	log "      this run is still the previous version; the next deploy uses the new one"
fi

# Build before touching the running container: a broken build must change nothing.
if ! GIT_SHA="$target" docker compose "${COMPOSE_FILES[@]}" build; then
	git reset --hard --quiet "$previous"
	die "build failed; the running version was left alone"
fi

# Not under `set -e`: a compose failure here has to reach the rollback below, not end the
# script with the old container already stopped.
started=true
# After the reset, so a change to the rules ships with the deploy that carries it rather
# than the one after. Grafana reads provisioning only at startup, so it is recreated when
# the rendered files changed — and only then, because recreating it needlessly drops every
# open dashboard session.
alerts_before="$(_alerting_config)"
# Not under `set -e` either. render-alerts.sh exits 1 on a malformed ALERT_TELEGRAM_* value —
# a token with a stray quote, a chat id somebody typed a letter into — and by this point the
# checkout has moved and the image is built. Killing the deploy there leaves the box running
# the old container on a new tree, with no `up -d` and no rollback, over a *notification*
# setting. The old rendered files stay exactly as they were, so Grafana keeps whatever alerting
# it already had.
if ! "$APP_DIR/deploy/render-alerts.sh"; then
	log "WARNING: the alerting config could not be rendered; deploying anyway"
	log "         Grafana keeps the alerting it already has — check ALERT_TELEGRAM_* in .env"
fi
if [[ "$(_alerting_config)" != "$alerts_before" ]]; then
	log "alerting changed; Grafana will be recreated to pick it up"
	docker compose "${COMPOSE_FILES[@]}" up -d --force-recreate --no-deps grafana || true
fi

docker compose "${COMPOSE_FILES[@]}" up -d --remove-orphans || started=false

if $started && wait_for_health; then
	docker image prune --force --filter "dangling=true" >/dev/null 2>&1 || true
	log "deployed ${target:0:8}, container is healthy"
	exit 0
fi

$started || log "compose could not start the new version"

if ! $was_healthy; then
	# It was already broken when we arrived. Rolling back would restore a version that was
	# equally broken and hide the real cause, which is usually an expired session.
	log "container is not healthy, but it was not healthy before this deploy either"
	log "leaving ${target:0:8} in place; check 'docker logs $CONTAINER' and /status"
	exit 1
fi

log "unhealthy after the deploy; rolling back to ${previous:0:8}"
git reset --hard --quiet "$previous"
GIT_SHA="$previous" docker compose "${COMPOSE_FILES[@]}" up -d --build --remove-orphans
if wait_for_health; then
	die "rolled back to ${previous:0:8}, which is healthy again"
fi
die "rolled back to ${previous:0:8} and it is STILL unhealthy — look at the server"

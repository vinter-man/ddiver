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
COMPOSE_FILES=(-f docker-compose.yml -f deploy/docker-compose.observability.yml)
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-240}"
CONTAINER="${CONTAINER:-ddiver}"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { log "FAILED: $*"; exit 1; }

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

# Alerts are opt-in because their overlay refuses to start Grafana without a token. Add it
# only on a box that has one, and add it here rather than in the file list above, because
# .env has not been checked until now. A grep, not a source: this script is the forced
# command of a key and must not execute what is in that file.
if grep -Eq '^[[:space:]]*ALERT_TELEGRAM_TOKEN=.+' .env; then
	COMPOSE_FILES+=(-f deploy/docker-compose.alerts.yml)
fi

# Whether it was working *before* matters: if the session has expired, the container is
# unhealthy whatever the code does, and rolling back would only thrash.
was_healthy=false
[[ "$(health_of)" == "healthy" ]] && was_healthy=true

previous="$(git rev-parse HEAD)"
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
		die "refusing a target that is not a full commit sha"
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

# Build before touching the running container: a broken build must change nothing.
if ! GIT_SHA="$target" docker compose "${COMPOSE_FILES[@]}" build; then
	git reset --hard --quiet "$previous"
	die "build failed; the running version was left alone"
fi

# Not under `set -e`: a compose failure here has to reach the rollback below, not end the
# script with the old container already stopped.
started=true
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

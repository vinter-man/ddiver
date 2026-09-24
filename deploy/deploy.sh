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
[[ -f .env ]] || die ".env is missing — run the bootstrap and `ddiver login` first"

# Whether it was working *before* matters: if the session has expired, the container is
# unhealthy whatever the code does, and rolling back would only thrash.
was_healthy=false
[[ "$(health_of)" == "healthy" ]] && was_healthy=true

previous="$(git rev-parse HEAD)"
git fetch --prune --quiet origin
target="$(git rev-parse origin/main)"

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

docker compose "${COMPOSE_FILES[@]}" up -d --remove-orphans

if wait_for_health; then
	docker image prune --force --filter "dangling=true" >/dev/null 2>&1 || true
	log "deployed ${target:0:8}, container is healthy"
	exit 0
fi

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

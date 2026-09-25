# Extract ddiver-core: Telegram delivery and everything before it

Branch `core-extraction-v2`, on main at b25b311. Twenty-four commits:

- **The move** (6 commits, made by `tools/extract_core.py`): a uv workspace, the modules that go
  to `packages/ddiver-core`, `Settings` split into a core base and ddiver's subclass, and the
  shared test fakes in `packages/ddiver-core-testing`, a dev-only package.
- **What a second app needs** (12 commits): payload links and source, i18n overlays,
  migration tracks, the export format tag, UI hooks, delivery without Telegram name lookups,
  owner-only commands, and the fixes found by review. Every default is ddiver's behaviour.
- **Proof and docs** (3 commits): the upgrade test against b25b311's own data, the core's
  README, and the module lists and invariant homes in DESIGN.md and both READMEs.
- **From the last review** (3 commits): the image smoke step now tests the installed wheels,
  CI rebuilds the b25b311 fixtures instead of skipping that test, and these notes, with a
  rollback that deploy.sh actually allows. See [Review findings](#review-findings).

The move was made by a script, `tools/extract_core.py`, which also rebased branches cut before
the extraction onto it. There were none left to rebase, so the script and its tests were deleted
in a follow-up commit; `git log -- tools/extract_core.py` is where to read it if the split ever
has to be understood or repeated.

## Before the first deploy: back up the database

**Back up `data/ddiver.sqlite3` on the server before this is deployed.** Nothing in the
change writes to the database: `tests/test_upgrade.py` starts and stops ddiver on a database
b25b311 built and finds `user_version` still 3, no `schema_tracks` table, and every row as it
was. The file is still the one thing this deploy could destroy, and the backup is cheap.

With the service running (SQLite's online backup is consistent while ddiver writes):

```bash
cd /srv/ddiver
docker exec ddiver python -c "
import os, sqlite3
src = sqlite3.connect('/data/ddiver.sqlite3')
dst = sqlite3.connect('/data/ddiver-before-core.sqlite3')
src.backup(dst)
print(dst.execute('PRAGMA integrity_check').fetchone()[0], dst.execute('PRAGMA user_version').fetchone()[0])
dst.close(); src.close()
os.chmod('/data/ddiver-before-core.sqlite3', 0o600)
"
# expect: ok 3
```

Then copy `data/ddiver-before-core.sqlite3` somewhere off the data directory. It holds the
same personal data as the database, so keep it where you would keep the database.

## Deploy checklist

1. Back up the database, as above, and check that it prints `ok 3`.
2. Merge. CI runs the checks and builds and smoke-tests the image; deploy starts only after
   both pass.
3. After the deploy, on the box:
   - The container runs the merged commit:
     `docker exec ddiver sh -c 'echo "$DDIVER_COMMIT"'` prints the same sha as
     `git -C /srv/ddiver rev-parse HEAD`.
   - It is healthy: `docker inspect --format '{{.State.Health.Status}}' ddiver` prints
     `healthy`. deploy.sh waits for this too, and rolls back if it does not come.
   - The schema did not move:
     `docker exec ddiver python -c "import sqlite3; db = sqlite3.connect('/data/ddiver.sqlite3'); print(db.execute('PRAGMA user_version').fetchone()[0], db.execute(\"SELECT count(*) FROM sqlite_master WHERE name='schema_tracks'\").fetchone()[0])"`
     prints `3 0`.
4. In Telegram, as the owner, send `/status` to the bot. It answers with the listener card:
   the listener OK, uptime, counters and queue.
5. Log queries: the moved modules now log as `ddiver_core.*` (`ddiver_core.delivery`,
   `ddiver_core.storage`, …) and did as `ddiver.*` before. The provisioned dashboard and alert
   rules select on `service` and `level`, not on the logger, so nothing there changes; a
   hand-written query over both periods uses `logger=~"ddiver(_core)?[.].+"`.

## If it has to be rolled back

deploy.sh puts the previous version back by itself only while a deploy fails: a failed build
changes nothing, and a new container that never becomes healthy (when the old one was healthy)
is replaced by the previous commit again. Once a deploy has succeeded it only moves forward.
Asking it for b25b311 does not work: a requested sha that is behind the deployed commit is
ignored ("it is behind the deployed …") and origin/main is deployed again, which rebuilds and
restarts the same code.

Two ways that do work:

- **Revert on main**, the normal way. `git revert -m 1 <merge sha>` (for a squash merge,
  `git revert <sha>`), check that `git diff b25b311 HEAD` prints nothing, which holds as long
  as nothing else was merged after this, and push. CI tests the revert and deploys it; deploy.sh
  accepts it because it is ahead of the deployed commit. `DDIVER_COMMIT` is then the revert's
  sha, and the tree is b25b311's.
- **By hand on the box**, when CI cannot run. As `root`, then `sudo -u deploy -i`:

  ```bash
  cd /srv/ddiver
  flock --wait 600 .deploy.lock sh -c '
    git reset --hard b25b3110b3c15d6a148f674b2d0c6381f8a980f3 &&
    GIT_SHA=$(git rev-parse HEAD) COMPOSE_PROFILES=host-metrics docker compose \
      -f docker-compose.yml -f deploy/docker-compose.observability.yml \
      up -d --build --remove-orphans'
  docker inspect --format '{{.State.Health.Status}}' ddiver   # healthy within four minutes
  ```

  The lock is deploy.sh's own, so this cannot interleave with a CI deploy. The next CI deploy
  moves the box forward to origin/main again, which brings the extraction back unless main
  has been reverted by then, so follow this with the revert above.

Either way no database restore is needed: the database is unchanged, and an outbox row this
build writes for Telegram is byte for byte what b25b311 writes (the new payload fields are
stored only when they differ from their defaults). Restore the backup only if the file itself
is damaged. After a rollback, check it as in step 3: `DDIVER_COMMIT`, `healthy`, `/status`.

## Conditions and objections

- **C1**: `tests/test_upgrade.py::TestTheDatabase`, on a database written by b25b311's own
  `Storage` (`tests/fixtures/b25b311/build.py`, run against `git archive b25b311`), plus the
  backup step above. `test_the_fixtures_are_still_exactly_what_b25b311_writes` rebuilds the
  fixtures from b25b311 and compares; the check job fetches the whole history so it runs in
  CI, and there it fails rather than skips if b25b311 is missing.
- **C2**: `TestTheOutbox`: the six new Payload fields take their defaults on every b25b311 row,
  and each row renders as b25b311 rendered it (36 notifications, 12 digests, and the
  moderator's rows through `Delivery`).
- **C3**: `TestTheExport`: both b25b311 exports load with the default tag `ddiver.config`,
  import through `/import` and export back byte for byte.
- **C4**: `tests/test_packaging.py` is unchanged from b25b311 and still reads the real
  `deploy/*`, `docker-compose.yml` and `Dockerfile`.
- **C5**: the checklist above, and the rollback that goes with it.
- **O1**: `ddiver_core.config.Settings` (bot token, api_id, api_hash, owner, data dir, logging,
  metrics, ping, digest threshold, regex timeout); `ddiver.config.Settings` adds the user
  session, catch-up (336 h ceiling kept) and ignore-own. Env var names unchanged;
  `tests/test_config.py` byte-identical.
- **O2**: `packages/ddiver-core/README.md`.
- **O3**: `packages/ddiver-core-testing` (`ddiver_core_testing`), dev group only; the image
  smoke step imports both packages from the installed wheels and fails if the fakes or pytest
  can be imported.
- **O4**: `tests/core/test_ui_hooks.py`, `Router.command(owner_only=True)` on the command path.

## Review findings

Two reviewers sent five findings; their F1 and F2 are the same two problems under swapped
numbers, so there are three. All three were confirmed and fixed; none was rejected.

| Finding | Verified | Fix |
|---|---|---|
| Smoke step imports `/app/ddiver`, not the wheel (F1 of the first review, F2 of the second) | Confirmed in a locally built image: `ddiver.__file__` was `/app/ddiver/__init__.py`, and with `ddiver/catchup.py` deleted from the installed wheel the old step still passed. | `python -P`, and an assertion that both packages live under the interpreter's own purelib. The same broken image now fails on the import; without `-P` it fails on the assertion. Pinned in `tests/test_ci_image.py`, which also runs the script against stand-in packages. Commit "Smoke-test the installed wheels, not the sources the image also carries". |
| The b25b311 fixture rebuild is skipped in CI (F2 of the first review, F1 of the second) | Confirmed: checkout@v4 fetches one commit, so b25b311 is not in the clone. Reproduced with a `--depth 1` clone of the branch. | `fetch-depth: 0` on the check job, and in CI the test fails instead of skipping. Checked in a depth-1 clone (fails under `CI`, skips without), in a full clone, and on Linux in `python:3.12-slim` with `CI=true` (runs and passes). Pinned in `tests/test_ci_image.py`. Commit "Rebuild the b25b311 fixtures in CI too, and fail there instead of skipping". |
| "Deploy b25b311 again" is not something deploy.sh can do (F3) | Confirmed: `deploy/deploy.sh` ignores a requested sha behind the deployed one and deploys origin/main again. | The rollback section above: revert on main, or reset by hand under deploy.sh's lock, followed by the revert. |

Where a reviewer offered a choice:

- The wheel check compares with `sysconfig.get_paths()["purelib"]` rather than a hard-coded
  `/app/.venv/` or a `/site-packages/` substring: it is exactly the venv the interpreter runs
  from, and it does not change when the base image moves to another Python.
- `fetch-depth: 0` rather than a separate `git fetch` of b25b311: one line, and the test that
  pins it can check it directly. The repository is small enough that the full history costs
  nothing noticeable.

## Checks

On the final commit: `ruff check`, `ruff format --check`, `mypy` (strict) and `shellcheck`
clean; `pytest` 1102 passed, none skipped, coverage 93.5% (gate 85%). The 766 tests collected
from `git archive b25b311` are all collected here under the same ids, unedited except import
paths and patch strings; the rest are the extraction tool's (96), the core's and the upgrade
and CI tests.

# Working on ddiver

A Telegram keyword monitor for a moderator who is **not an admin** of the chats they watch. A
Telethon *user session* listens; a separate BotFather bot delivers notifications to each
moderator's DM and hosts the config UI. It runs on one small VPS, deploys on push to `main`, and
the account it listens with is the owner's own — which is why nothing here ever writes to a
monitored chat.

`DESIGN.md` is the authority on architecture, invariants and why each decision is what it is.
Read its **Invariants** and **Modules** sections before changing behaviour. This file is only the
orientation and the landmines.

## Where the code is

Since the core extraction (2026-09-25) this is a uv workspace of three packages:

| Package | What lives there |
|---|---|
| `packages/ddiver-core/src/ddiver_core/` | everything that is not the user session: rules, matcher, storage, delivery, formatter, richtext, links, senders, portable, i18n, `bot/` (router, menus, handlers), app, health, metrics, watchdog, base config, botclient, botruntime, process, tgerrors |
| `ddiver/` | what needs a Telegram user session: `listener.py`, `catchup.py`, `login.py`, `runtime.py`, `clients.py`, `config.py` (subclass), `__main__.py` |
| `packages/ddiver-core-testing/` | `FakeClock`, `FakeBotClient` — dev group only, never in the image |

The deciding question is **"does this need the user session?"** Matching a message, rendering a
notification, the bot's screens and the outbox are all *core*. Only reading updates off the user
session, backfilling history and logging in are `ddiver`.

A second project, **ddiver_facebook**, consumes `ddiver-core` pinned at the git tag
`core-v0.1.0`. ddiver builds from the working copy, so a core change is invisible to that project
until it moves its pin: say so in words when you change core behaviour.

## The gate

Run it the way CI does, from the repo root. `mypy` with no arguments — it checks the tests too,
and a bare `uv run mypy ddiver` has hidden real errors here.

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest -q --timeout=300 --cov=ddiver --cov=packages
shellcheck deploy/*.sh
```

## Three things that have actually gone wrong

**A green suite is not a working feature.** A release once shipped with 756 passing tests while
the headline promise of a new feature — "a `from:` rule fires on a photo with no caption" — was
false, because a filter one layer above the matcher dropped text-less messages. What caught it
was a run against the real account. `scripts/e2e_live.py` is the committed version of that check;
for anything touching the listener, matching or delivery, run something real before believing the
suite.

**A green CI is not a correct box.** Three separate times a commit was green and the server was
wrong: a build arg that never reached the image, a compose profile that shipped but was not
applied, and a change to `deploy/deploy.sh` that took effect one deploy late. After a deploy,
check the box: `DDIVER_COMMIT` inside the container, `health=healthy`, `ddiver_build_info` in
Prometheus.

**`deploy/deploy.sh` updates itself one deploy late.** The forced command starts it from the
checkout as it was *before* the fetch, and `git reset --hard` replaces the file through a rename,
so the already-open copy keeps running. The script says so in its own output when it notices. A
change to it therefore takes effect on the deploy *after* the one that ships it.

## Rules of engagement

- **Never `git push`.** The owner pushes, always, on his own command. Commit, then ask.
- **Prove a test is load-bearing.** Break the code the test is for, watch the test fail, put it
  back. Several tests here passed against the very bug they were written for.
- **Do not re-open the "do not fix" list.** `DESIGN.md` and `docs/core-extraction-notes.md`
  record decisions that reviewers keep re-proposing: matcher single-flight, a cost-based
  `OFFLOAD_ABOVE_RULES`, `prepare_text`'s identity mapping, a transaction around `migrate_chat`,
  log caps on the observability containers, a chmod of the server's `.env`, the ssh-keyscan TOFU.
  Each proposed fix either reintroduces a closed bug or is worse than what it treats. New
  evidence reopens them; a fresh reviewer's opinion does not.
- **Reviewer findings are claims.** Reproduce one before acting on it, and judge separately
  whether the suggested fix is safe — several have been wrong where they were specific.
- **`tests/test_packaging.py` stays in this package and keeps reading the real `deploy/` files.**
  Every assertion in it is an outage that already happened.
- **The account matters more than the code.** No writes to monitored chats, no RPC on the
  listener session inside a message handler (Invariant 1), and nothing that makes the session
  look like a bot farm.

## Operations

`deploy/README.md` covers the server. In short: `ssh ddiver-vps` is root on the box;
`sudo -u deploy -i` is the app user; `ssh deploy@` runs a deploy and gives no shell. Grafana,
Prometheus and Loki are localhost-only and the ssh config already forwards 3000. Logs reach Loki
as JSON with `level` and `logger` labels; moved modules log as `ddiver_core.*`, so
`logger=~"ddiver(_core)?[.].+"` covers both sides of the rename.

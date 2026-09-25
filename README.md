# ddiver

Telegram keyword monitor for moderators who are **not** admins of the chats they watch.

A Telethon *user session* (your own account) listens in the chats you pick. When a message
matches your rules, a separate bot — which never joins those chats — messages you in private:

```
📡 Moderated chat
👤 Ivan · @ivan · 42
🔗 https://t.me/c/1234567890/77
🕒 2026-09-22 14:03:05 +03:00 (Europe/Kyiv) · 12 s ago
🔑 taxi (chat rule)

cheap **taxi**, write to me
[🚫 Ignore sender] [🗑 Remove this rule]
```

*Українська / Русский: the bot speaks uk, ru and en — it asks on the first `/start`. A Russian
version of this README is in [README.ru.md](README.ru.md).*

## What it does

- **Watches chats you are only a member of.** No admin rights, no bot in the chat.
- **Per-chat rules.** `taxi` (substring, case-insensitive), `=taxi` (whole word),
  `re:tax[iy]` (regular expression). Exclusions use the same syntax and win over keywords.
- **Several moderators, one listener.** The owner shares specific chats; each moderator has
  their own chats, rules, ignore list, language and timezone.
- **Survives restarts.** Matches are written to an outbox before they are sent, and the bot
  backfills what it missed while it was offline (labelled "missed while offline").
- **A week in one message.** Every seven days, at ten in the morning your time: how much was
  caught, in which chats, by which rules, and how that compares with the week before — plus
  your chats and rules as a file, so a reinstall costs nothing. Both can be switched off.
- **Built for load.** Designed against a target of 50+ chats and 100k+ messages a day: bounded
  queues, one shared flood gate, digests when a burst arrives, automatic disabling of
  catastrophic regexes. The longest measured run so far is 1301 messages over five hours.

## Requirements

- Python 3.12+ (or Docker)
- An `api_id` / `api_hash` from [my.telegram.org](https://my.telegram.org) → API development tools
- A bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram user id ([@userinfobot](https://t.me/userinfobot), or `ddiver login` prints it)

## Setup

```bash
git clone https://github.com/vinter-man/ddiver && cd ddiver
cp .env.example .env         # fill in API_ID, API_HASH, BOT_TOKEN, OWNER_ID
uv sync                      # or: pip install -e ./packages/ddiver-core -e .
uv run ddiver login          # phone, code, 2FA — the session stays in ./data
uv run ddiver check-config   # verifies the token, permissions and that you started the bot
uv run ddiver run
```

Then open your bot in Telegram and press **Start**.

In BotFather, run `/setjoingroups` → **Disable** for this bot: it only ever writes to you.

### Docker

```bash
cp .env.example .env
docker compose run --rm -it ddiver login   # do this while the service is NOT running
docker compose up -d
docker compose logs -f
```

`./data` holds the session files and the SQLite database. **Never run two ddiver processes on
one data directory** — Telegram invalidates a session used from two places at once. ddiver takes
a lock file and refuses, but a copy of the directory elsewhere would still break it.

For a VPS — the setup script, the deploy-on-push pipeline and how to share the box with your
other projects — see [deploy/README.md](deploy/README.md).

## Using it

`/start` walks you through language and timezone, then the menu:

| Menu | What it does |
|---|---|
| 📡 Chats | pick which chats to monitor (moderators see only the chats the owner shared) |
| 🔑 Rules | keywords and exclusions, per chat or for all your chats |
| 🚫 Ignored | senders you never want to hear about |
| ⚙️ Settings | language, timezone, digest threshold, bot messages, weekly summary, weekly backup |
| 📊 Status | listener state, uptime, counters, queue length |
| 🗂 Shared chats | *(owner)* which of your chats moderators may see |
| 👥 Users | *(owner)* approve, deny or revoke moderators |

Commands: `/menu` `/test <text>` `/status` `/export` `/import` `/help` `/cancel`.

`/test` runs the real pipeline — exclusions first, chat rules before global ones — and tells you,
per chat, whether that text would notify you.

### Rule syntax

| Pattern | Matches |
|---|---|
| `taxi` | any message containing "taxi", ignoring case — including "taxidermy" |
| `=taxi` | the whole word only, so not "taxidermy"; punctuation is a boundary, so `=c++` and `taxi-service` both work |
| `re:tax[iy]` | a regular expression (case-insensitive; opt out with `(?-i:…)`) |
| `from:@vasya` | the **sender**, not the text: everything that person posts, including a photo with no caption |
| `from:123456789` | the same, by numeric id — a username can be changed, an id cannot |
| `from:John Smith` | part of a display name, case-insensitive; names are decoration, so "John" finds "John 🚀" |
| `#promo` | a hashtag is an ordinary keyword — there is no comment syntax |

Rules are matched against text with invisible characters removed and Unicode normalised, so
`ta<zero-width space>xi` still matches `taxi`. Latin/Cyrillic look-alikes (`тaкси` with a
Latin "a") are **not** normalised — use a regex if you need them.

`from:` is spelled out rather than taken from a bare `@name`, because `@name` already means
"the text mentions @name" — a different and equally useful rule. A phone number is refused:
Telegram only shows one for somebody already in your contacts, so such a rule would quietly
match nothing. Exclusions take `from:` too, which is how you silence one person without
knowing their numeric id.

A regular expression that takes too long is skipped, and after three timeouts it is disabled
automatically and you are told which one.

## Operating it

- `ddiver health` exits non-zero when the heartbeat is stale; Docker uses it as its healthcheck.
- If the session dies (revoked, banned, used from two places), ddiver does **not** restart-loop:
  it keeps the bot running, tells you in `/status` and by message, and waits for `ddiver login`.
- Logs are a 60-second summary line plus one line per notification; Telethon's flood waits are
  visible at INFO.
- `/export` hands you a JSON file with your chats, rules and ignore list; `/import` loads one
  back, adding rather than replacing. That is the backup worth keeping off the machine — it
  contains no session and no token, so it is safe to put where a backup normally goes.
- A copy of `data/user.session` is a copy of *your Telegram account*. If you back the whole
  directory up, back it up somewhere you would be willing to keep your password.

## Watching it

Nothing here is required; ddiver runs without any of it.

### A dead-man's switch

Set `PING_URL` and ddiver pings it once a minute (`PING_INTERVAL`). Point it at a push monitor — a free
healthchecks.io check, an Uptime Kuma push URL, anything that shouts when the pings stop:

```dotenv
PING_URL=https://hc-ping.com/your-uuid-here
```

This is the only alert that survives the machine dying, which is exactly the failure a quiet
chat looks like. Everything below runs on the same box as ddiver and cannot report its own
funeral.

### Metrics, logs and dashboards

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.observability.yml up -d
ssh -L 3000:127.0.0.1:3000 you@your-box   # Grafana is bound to localhost
```

Grafana is then on <http://127.0.0.1:3000> (admin/admin unless you set `GRAFANA_USER` and
`GRAFANA_PASSWORD`), with a dashboard already provisioned: is it alive, messages and matches
per second, delivery and flood waits, rule timeouts, host CPU and memory, and a searchable log
panel. Measured on a fresh stack: Prometheus 57 MB, Loki 72 MB, Grafana 99 MB, Alloy 48 MB
— about 280 MB against ddiver's own 56 MB, growing with retention. Budget half a gigabyte and
it will not surprise you; that it needs several times what ddiver needs is why it lives in a
separate compose file.

Host metrics are opt-in with `--profile host-metrics`, because node-exporter needs the host's
own `/` and Docker Desktop refuses that mount. On a Linux box turn the profile on and the Host
panel fills in; until then that one panel stays empty and Prometheus shows the `node` target
down.

Telegram alerts need two variables in `.env`. `deploy/render-alerts.sh` writes the
provisioning files from them on every deploy, and removes them again on a box that has none —
Grafana refuses to start when a contact point has no token, so they have to be absent rather
than empty.

Without Docker, `METRICS_PORT=9109` exposes `/metrics` on its own and `LOG_FORMAT=json` makes
the logs machine-readable.

Grafana can message you on Telegram when something is wrong but the machine is still up — the
session needs a re-login, the listener has been disconnected for ten minutes, notifications are
piling up. The same BotFather bot works — that is one token to manage and what most people
will do. A **second** bot is the better answer if you want it: an alert about delivery being
stuck then does not travel over the bot that is stuck, and revoking one token does not silence
both.

```dotenv
ALERT_TELEGRAM_TOKEN=123456:AA...
ALERT_TELEGRAM_CHAT_ID=your-numeric-id
```

No extra compose file: set the two variables and the next deploy picks them up. By hand:

```bash
./deploy/render-alerts.sh && docker compose -f docker-compose.yml \
    -f deploy/docker-compose.observability.yml up -d grafana
```

### Asking an agent about the logs

Logs arrive in Loki as JSON with `level` and `logger` as labels, so they are queryable rather
than greppable: `{service="ddiver", level="ERROR"} | json | msg =~ ".*flood.*"`. Point an LLM
agent at Grafana's MCP server (`grafana/mcp-grafana`) and "go find out why nothing arrived from
that chat this morning" becomes a question it can answer by itself.

## Privacy

ddiver reads chats you are a member of, and the people in them have not agreed to anything.
Worth knowing before you run it, and before you share it with other moderators:

- **What is stored.** Matched messages only — never the whole stream. For each match: the text,
  the chat title, the sender's id, name and username, and the link. Non-matching messages are
  evaluated in memory and forgotten.
- **For how long.** Seven days, then a daily job deletes them. The rest of the database is
  configuration: your chats, rules and ignore list.
- **Who can read it.** Whoever can read the machine's disk, plus every moderator you shared a
  chat with — they get the message text of that chat in their own DM.
- **What leaves the machine.** Notifications to the moderators' DMs through your bot, and
  nothing else. There is no telemetry and no third-party service.
- **Deleting it.** Removing a rule stops new matches; `data/ddiver.sqlite3` holds the old ones
  until the seven days run out, or until you delete the file.

If you are in the EU this is personal data under the GDPR, and running it makes you the
controller. For a personal moderation aid that is a low bar, but it is not nothing.

## Security

`data/user.session` gives full access to your Telegram account: it can read every chat you are
in and send messages as you. ddiver keeps `data/` at `0700` and the session files at `0600`,
never logs message text above DEBUG, and never sends anything on your behalf — it only reads the
monitored chats and writes to you through the bot.

Anyone you allow as a moderator can read the message text of the chats you shared with them, in
their own DM with the bot. Share only the chats you mean to.

Running a user account through an API client is a grey area in Telegram's terms. ddiver only
reads and never mass-joins or mass-messages, which is the low-risk end of it, but the risk is
not zero — consider a secondary account if that matters to you.

## Development

```bash
uv sync
uv run pytest -q          # ~470 offline tests, no Telegram connection needed
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

The architecture, the invariants and the reasoning behind them are in [DESIGN.md](DESIGN.md).
Telegram I/O is confined to `runtime.py`, `clients.py`, `login.py`, the CLI and the
`telethon_history` adapter at the foot of `catchup.py`. Everything else takes its Telegram
surface as an injected port — `BotPort`, `History`, `EntityResolver` — so matching, storage,
delivery and formatting are exercised offline against fakes and a virtual clock.

## Credits

Built from scratch after reviewing the existing open-source options; ideas (not code) were taken
from khoben/telemirror, guibacellar/TEx, Hootrix/keyword_alert_bot, Heavrnl/TelegramForwarder and
Riniba/TelegramMonitor. See the end of DESIGN.md.

MIT licensed.

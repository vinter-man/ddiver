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
- **Behaves under load.** 50+ chats and 100k+ messages a day: bounded queues, one shared flood
  gate, digests when a burst arrives, automatic disabling of catastrophic regexes.

## Requirements

- Python 3.12+ (or Docker)
- An `api_id` / `api_hash` from [my.telegram.org](https://my.telegram.org) → API development tools
- A bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram user id ([@userinfobot](https://t.me/userinfobot), or `ddiver login` prints it)

## Setup

```bash
git clone https://github.com/vinter-man/ddiver && cd ddiver
cp .env.example .env         # fill in API_ID, API_HASH, BOT_TOKEN, OWNER_ID
uv sync                      # or: pip install -e .
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

## Using it

`/start` walks you through language and timezone, then the menu:

| Menu | What it does |
|---|---|
| 📡 Chats | pick which chats to monitor (moderators see only the chats the owner shared) |
| 🔑 Rules | keywords and exclusions, per chat or for all your chats |
| 🚫 Ignored | senders you never want to hear about |
| ⚙️ Settings | language, timezone, digest threshold, whether bot messages count |
| 📊 Status | listener state, uptime, counters, queue length |
| 🗂 Shared chats | *(owner)* which of your chats moderators may see |
| 👥 Users | *(owner)* approve, deny or revoke moderators |

Commands: `/menu` `/test <text>` `/status` `/help` `/cancel`.

`/test` runs the real pipeline — exclusions first, chat rules before global ones — and tells you,
per chat, whether that text would notify you.

### Rule syntax

| Pattern | Matches |
|---|---|
| `taxi` | any message containing "taxi", ignoring case — including "taxidermy" |
| `=taxi` | the whole word only, so not "taxidermy"; punctuation is a boundary, so `=c++` and `taxi-service` both work |
| `re:tax[iy]` | a regular expression (case-insensitive; opt out with `(?-i:…)`) |
| `#promo` | a hashtag is an ordinary keyword — there is no comment syntax |

Rules are matched against text with invisible characters removed and Unicode normalised, so
`ta<zero-width space>xi` still matches `taxi`. Latin/Cyrillic look-alikes (`тaкси` with a
Latin "a") are **not** normalised — use a regex if you need them.

A regular expression that takes too long is skipped, and after three timeouts it is disabled
automatically and you are told which one.

## Operating it

- `ddiver health` exits non-zero when the heartbeat is stale; Docker uses it as its healthcheck.
- If the session dies (revoked, banned, used from two places), ddiver does **not** restart-loop:
  it keeps the bot running, tells you in `/status` and by message, and waits for `ddiver login`.
- Logs are a 60-second summary line plus one line per notification; Telethon's flood waits are
  visible at INFO.
- Back up `data/ddiver.sqlite3` for your configuration. A backup of `data/user.session` is a
  backup of *your account* — treat it accordingly.

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
uv run pytest -q          # ~350 offline tests, no Telegram connection needed
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

The architecture, the invariants and the reasoning behind them are in [DESIGN.md](DESIGN.md).
Everything except `ddiver/runtime.py` is free of Telethon calls and tested offline with fake
clients and a virtual clock.

## Credits

Built from scratch after reviewing the existing open-source options; ideas (not code) were taken
from khoben/telemirror, guibacellar/TEx, Hootrix/keyword_alert_bot, Heavrnl/TelegramForwarder and
Riniba/TelegramMonitor. See the end of DESIGN.md.

MIT licensed.

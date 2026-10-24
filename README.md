[![CI](https://github.com/vinter-man/ddiver/actions/workflows/ci.yml/badge.svg)](https://github.com/vinter-man/ddiver/actions/workflows/ci.yml)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![Telethon user session](https://img.shields.io/badge/telegram-user%20session-orange)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

![main picture](docs/assets/banner.png)

## Short description

Your own Telegram account sits in the chats you pick and reads everything. When a message
matches a rule you wrote, a **separate bot** writes to you in private with the chat, the
sender, a clickable link and the matched text.

The bot's interface comes in English, Ukrainian and Russian — it asks which one you want on
the first `/start`.

![demo](docs/assets/workflow.gif)

---

## Table of contents

* [User guide](#user-guide)
  * [Pick the chats](#chats)
  * [Write the rules](#rules)
  * [Get the one message that matters](#notification)
  * [A week in one message](#weekly)
* [Installation](#installation)
  * [Docker](#docker)
  * [On a VPS](#vps)
* [Commands](#commands)
* [Watching it](#watching)
* [Under the hood](#hood)
* [Contact](#contact)

---
---

## <a name="user-guide"></a> User guide

### <a name="chats"></a> Pick the chats

`/start`, pick a language and a timezone, then **📡 Chats**. Every dialog your account is in is
on the list — groups, supergroups, forums, channels. Tick the ones worth watching.

![chats](docs/assets/chats.png)

There is no bot sitting in the
chat that somebody could spot in the member list.

> :suspect: Under the hood the listener is registered on *every* update rather than on a chat
> filter, and the filtering happens in ddiver. That sounds wasteful and isn't: Telegram pushes
> those updates to your client anyway, and a filter registered at the Telethon level has to be
> rebuilt every time you tick a box. `listener.py` screens each message in memory — chat
> subscribed, sender not ignored, newer than the moment you subscribed — before anything is
> written down.
>
> Several moderators can share one listener. The owner shares specific chats; everyone else
> has their own rules, their own ignore list, their own language and timezone, and sees only
> what was shared with them.

### <a name="rules"></a> Write the rules

**🔑 Rules** → *Add*. A rule belongs either to one chat or to all of yours.

![rules](docs/assets/rules.png)

Exclusions are the same syntax with a minus, and they beat every keyword, so `apartment`
plus `-agency` means "flats, but not the agency adverts".

> :godmode: Before matching, invisible characters are stripped and Unicode is normalised, so a
> zero-width space glued into the middle of a word does not hide it. Look-alike letters from
> different alphabets are deliberately **not** folded together: Cyrillic and Latin each have a
> letter that renders as an identical *a*, and quietly treating the two as one would surprise
> you more often than it would help. Write a regex if that is what you want.
>
> Regexes are compiled once, with a timeout and a measured size cap, and run through the
> `regex` module rather than `re` — a pattern that would hang the process is refused at the
> moment you add it, and one that merely runs slow is skipped. After three timeouts it is
> switched off and you are told which one. `rules.py` and `matcher.py`, and `/test <text>`
> runs the real pipeline so you can check a rule before trusting it.

### <a name="notification"></a> Get the one message that matters

![notification](docs/assets/notification.png)

The link opens the message itself — in a public chat, a private one or a forum topic. The two
buttons are the two things you ever want at that moment: never hear from this person again, or
delete the rule that just fired.

> :hurtrealbad: A match is written to an outbox **before** it is sent, so nothing is lost to a
> restart, a flood wait or a dropped connection, and nothing is sent twice. If the bot was down
> it backfills what it missed, labelled "missed while offline"; if a burst arrives it collapses
> into one digest instead of ringing your phone forty times. `delivery.py` and `catchup.py`.
>
> Designed against 50+ chats and 100k+ messages a day: bounded queues, one shared flood gate,
> and catch-up that is capped per chat and in total so a week offline cannot turn into an hour
> of history replay.

### <a name="weekly"></a> A week in one message

Every seven days, at ten in the morning your time: what was caught, where, by which rules, and
how that compares with the week before.

![weekly](docs/assets/weekly.png)

Attached to it is your configuration as a JSON file — chats, rules, ignore list. It holds no
session and no token, so it is safe to keep wherever you keep backups, and `/import` reads it
back. Both the report and the attachment can be switched off in **⚙️ Settings**.

---
---

## <a name="installation"></a> Installation

You need three things:

* an `api_id` and `api_hash` from [my.telegram.org](https://my.telegram.org) → *API development tools*
* a bot token from [@BotFather](https://t.me/BotFather)
* your own Telegram user id ([@userinfobot](https://t.me/userinfobot), or `ddiver login` prints it)

Install [uv](https://docs.astral.sh/uv/) once — it brings the right Python with it.

```bash
git clone https://github.com/vinter-man/ddiver && cd ddiver
cp .env.example .env         # API_ID, API_HASH, BOT_TOKEN, OWNER_ID
uv sync
uv run ddiver login          # phone, code, 2FA — the session stays in ./data
uv run ddiver check-config   # token, permissions, and whether you pressed Start yet
uv run ddiver run
```

Then open your bot and press **Start**. In BotFather, `/setjoingroups` → **Disable**: this bot
only ever writes to you.

### <a name="docker"></a> Docker

```bash
cp .env.example .env
docker compose run --rm -it ddiver login   # while the service is NOT running
docker compose up -d
docker compose logs -f
```

`./data` holds the session and the SQLite database.

> :rage1: **Never run two ddiver processes on one data directory.** Telegram invalidates a
> session used from two places at once, and you will be logging in again. ddiver takes a lock
> file and refuses, but a *copy* of the directory somewhere else is outside what a lock can
> see.

### <a name="vps"></a> On a VPS

[`deploy/README.md`](deploy/README.md) is the whole thing: a setup script that hardens a fresh
box, a push-to-`main` pipeline that builds, deploys and checks the container came up on the new
commit, and how to keep the box shareable with your other projects.

---
---

## <a name="commands"></a> Commands

| Command | What it does |
|---|---|
| `/menu` | chats, rules, ignore list, settings, status |
| `/test <text>` | runs the real pipeline over that text and says, per chat, whether it would notify you |
| `/status` | listener state, uptime, counters, queue |
| `/export` / `/import` | your configuration as a file, and back; import adds rather than replaces |
| `/help` | the rule syntax, in your language |
| `/cancel` | out of whatever dialog you are in |

The menu also has **🗂 Shared chats** and **👥 Users** for the owner: which chats moderators may
see, and who is allowed in at all.

---
---

## <a name="watching"></a> Watching it

![status](docs/assets/status.png)

None of what follows is required; ddiver runs without any of it.

**A dead-man's switch.** Set `PING_URL` and ddiver pings it once a minute. Point it at
healthchecks.io, an Uptime Kuma push URL, anything that shouts when the pings stop. This is the
only alert that survives the machine dying — which is exactly what a quiet chat looks like.

```dotenv
PING_URL=https://hc-ping.com/your-uuid-here
```

![healthchecks](docs/assets/healthchecks.png)

That message arrives from somebody else's machine. Everything below runs on the same box as
ddiver and so cannot report its own funeral.

**Metrics, logs, dashboards.** One extra compose file brings up Prometheus, Loki, Alloy and a
Grafana with the dashboard already provisioned.

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.observability.yml up -d
ssh -L 3000:127.0.0.1:3000 you@your-box    # Grafana is bound to localhost
```

![grafana](docs/assets/grafana.png)

Six stats for "is it alive", then traffic, delivery, matching, the watchdog, and a searchable
log panel underneath.

> :godmode: Logs arrive as JSON with `level` and `logger` as labels, so they are queryable
> rather than greppable: `{service="ddiver", level="ERROR"} | json | msg =~ ".*flood.*"`. Point
> an agent at Grafana's MCP server and *"go find out why nothing arrived from that chat this
> morning"* becomes a question it can answer by itself.
>
> Measured on a fresh stack: Prometheus 57 MB, Loki 72 MB, Grafana 99 MB, Alloy 48 MB, against
> ddiver's own 56 MB. Budget half a gigabyte. That it needs five times what ddiver needs is why
> it lives in a separate file.

Grafana can message you on Telegram when the machine is still up but something is wrong — the
session wants a re-login, the listener has been disconnected ten minutes, notifications are
piling up. Set two variables and the next deploy picks them up; a second bot token is the
better answer if you want an alert about delivery being stuck not to travel over the bot that
is stuck.

```dotenv
ALERT_TELEGRAM_TOKEN=123456:AA...
ALERT_TELEGRAM_CHAT_ID=your-numeric-id
```

![alerts](docs/assets/alerts.png)

Without Docker: `METRICS_PORT=9109` exposes `/metrics`, `LOG_FORMAT=json` makes the logs
machine-readable, and `ddiver health` exits non-zero when the heartbeat is stale.

---
---

## <a name="hood"></a> Under the hood

```
your account ─→ listener ─→ matcher ─→ outbox ─→ bot ─→ you
   Telethon      in memory    rules     SQLite   BotFather
```

Two Telegram identities on one event loop. The user session reads and never writes; the bot
writes and never reads a monitored chat. Between them sits a table, so a crash costs you
nothing and a burst costs you one digest instead of forty messages.

A uv workspace of three packages:

| Package | What is in it |
|---|---|
| `ddiver/` | everything that needs the user session: `listener.py`, `catchup.py`, `login.py`, `clients.py`, `runtime.py`, `config.py`, `__main__.py` |
| `packages/ddiver-core/` | everything that doesn't: `rules.py`, `matcher.py`, `storage.py`, `delivery.py`, `formatter.py`, `botclient.py`, `botruntime.py`, the bot's screens, metrics, health, i18n. Its [README](packages/ddiver-core/README.md) says what it is and is not |
| `packages/ddiver-core-testing/` | the fakes both suites share; a dev dependency, never in the image |

Telegram reaches the core only as an injected port — `BotPort`, `History`, `EntityResolver` —
so matching, storage, delivery and formatting are exercised offline against fakes and a virtual
clock.

```bash
uv sync
uv run pytest -q          # 1030 tests, no Telegram connection needed
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

> :shipit: [DESIGN.md](DESIGN.md) is the architecture, the invariants and why each decision is
> what it is. [CLAUDE.md](CLAUDE.md) is the short orientation for a coding agent: which package
> a change belongs in, how to run the gate the way CI runs it, and the three times a green suite
> or a green CI here was wrong anyway.


---
---

## <a name="contact"></a> Contact

GitHub: [vinter-man](https://github.com/vinter-man)

License: [MIT](LICENSE), take it and dive

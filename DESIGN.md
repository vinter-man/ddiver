# ddiver — design

Telegram keyword monitor for moderators who are **not admins** of the chats they watch.
A Telethon *user session* (the moderator's own account) listens in the chats; a separate
BotFather bot delivers notifications to each moderator's DM and hosts the configuration UI.

Status: v1 design, 2026-09-22. Product decisions are fixed (see "Decisions"); everything
else is open for review.

## Decisions (fixed, from the requirements session)

| # | Decision |
|---|---|
| D1 | Listener = Telethon user session on the moderator's main account. Bot cannot be added to chats. |
| D2 | Delivery + config UI = separate Bot API bot (BotFather token), also driven through Telethon (`start(bot_token=...)`), same process, same event loop. |
| D3 | Multi-user with one shared listener: owner (`OWNER_ID`) + allow-listed moderators. Each user has own language, timezone, chat subscriptions, rules, ignore lists. The listener monitors the union of all subscribed chats. |
| D4 | Target load: 50+ chats, 100k+ messages/day (≈1.2 msg/s average, bursts of 50+/s). |
| D5 | Rule syntax: `word` = case-insensitive substring; `=word` = whole word; `re:pattern` = regex. Same syntax for exclusions. Per-user ignore list of senders (per chat or all chats). Listener's own outgoing messages always ignored (`IGNORE_OWN_MESSAGES=false` only for tests). |
| D6 | Matched: new messages and media captions, forwarded messages (sender = forwarder). Not matched: edits, messages from bots, service messages. |
| D7 | Notification = chat title (link) / sender (link) / message link / date in user tz + "N s ago" / full text with the matched fragment in bold + "rule: …". Inline buttons: "🚫 ignore sender", "➖ add exclusion". |
| D8 | Flood: each match is its own message; when a user's backlog exceeds a threshold, pending items collapse into one digest. |
| D9 | Catch-up after downtime from a persisted per-chat cursor, capped to N hours (default 24); such notifications are labelled "missed while offline". |
| D10 | Hosting: Docker (VPS) and local `uv run`, both documented. SQLite for state. Telethon session files live in the data dir. |
| D11 | UI languages uk / ru / en; language asked on first `/start`. README EN + RU. Code and comments in English. |
| D12 | Python 3.12, MIT. Tests from the first commit; ruff + mypy strict + coverage gate in CI. |

## Non-goals (v1)

Editing/deleting messages in the monitored chats, forwarding to admins from the userbot,
web UI, Postgres, multiple listener accounts, matching on edits.

## Process model

One process, one asyncio loop, two Telethon clients:

```
                 ┌──────────────── user session (listener) ────────────────┐
 Telegram ──────►│ NewMessage handler ─► monitored? ─► Matcher ─► Match[] │
                 │ cursor tracker (per chat)      catch-up (startup)       │
                 └───────────────────────────┬────────────────────────────┘
                                             │ Notification(user_id, …)
                                             ▼
                 ┌────────────────── delivery (per-user queue + worker) ───┐
                 │ token bucket ─► digest collapse ─► bot.send_message     │
                 │ FloodWait sleep+retry, backoff, dedupe (deliveries tbl) │
                 └───────────────────────────┬────────────────────────────┘
                                             │
                 ┌──────────────── bot session (UI + delivery) ────────────┐
 Moderators ◄───►│ commands, inline menus, allow-list gate, pending input  │
                 └─────────────────────────────────────────────────────────┘
                                  SQLite (aiosqlite, WAL)
```

Supervisor (`app.py`) owns both clients: connect with a timeout (Telethon `connect()` can
hang, telethon#1536), run, reconnect with jittered backoff when `run_until_disconnected`
returns, classify fatal auth errors (`SessionRevoked`, `AuthKeyUnregistered`,
`UserDeactivated*`, `PhoneNumberBanned`) → notify owner through the bot, sleep, exit 2.
Graceful SIGTERM/SIGINT: stop accepting, flush cursors, drain delivery queues for ≤10 s.

## Modules

```
ddiver/
  __main__.py      CLI: login | run | health | check-config
  config.py        pydantic-settings: API_ID, API_HASH, BOT_TOKEN, OWNER_ID, DATA_DIR, LOG_LEVEL,
                   CATCHUP_HOURS=24, CATCHUP_MAX_PER_CHAT=500, DIGEST_THRESHOLD=20,
                   REGEX_TIMEOUT_MS=200, IGNORE_OWN_MESSAGES=true
  app.py           wiring + supervisor + signals + heartbeat
  clients.py       client factories, connect_with_timeout, is_fatal_auth_error
  storage.py       aiosqlite repo + migrations (schema_version)
  rules.py         Rule parsing (word / =word / re:) → CompiledRule; match() with regex timeout
  matcher.py       per-chat rule sets for all users (cache, invalidation); evaluate(msg) → Match[]
  links.py         chat link, message link (public/private/forum topic), sender link + fallbacks
  richtext.py      segment builder → (text, MessageEntity[]) with UTF-16 offsets, truncation
  formatter.py     Notification and Digest → richtext + buttons
  timeutil.py      tz conversion (zoneinfo), relative time with uk/ru/en plurals
  i18n/            en.py ru.py uk.py catalogs + t(lang, key, **kw)
  listener.py      NewMessage handler, cursor tracker (batched flush), monitored-set
  catchup.py       startup reconciliation from cursors
  delivery.py      per-user queue/worker, token bucket, digest, FloodWait/backoff, dedupe
  bot/router.py    dispatch + allow-list gate + pending-input state
  bot/menus.py     keyboards (main, chats picker w/ pagination, rules, ignored, settings)
  bot/handlers/    start_lang_tz.py chats.py rules.py ignored.py settings.py status.py notif_buttons.py
  health.py        heartbeat file write/check
tests/            pytest + pytest-asyncio, fakes for both clients
```

## Storage (SQLite, WAL, foreign keys on)

```sql
schema_version(version INTEGER NOT NULL);

users(
  id INTEGER PRIMARY KEY,           -- Telegram user id
  lang TEXT NOT NULL DEFAULT 'en',  -- 'uk' | 'ru' | 'en'
  tz TEXT NOT NULL DEFAULT 'UTC',   -- IANA name
  role TEXT NOT NULL DEFAULT 'pending',  -- 'owner' | 'allowed' | 'pending' | 'denied' | 'paused'
  digest_threshold INTEGER NOT NULL DEFAULT 20,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

chats(
  id INTEGER PRIMARY KEY,           -- Telethon *marked* id (-100… supergroup/channel, -… basic group)
  title TEXT NOT NULL, username TEXT, kind TEXT NOT NULL,  -- 'supergroup'|'channel'|'group'
  is_forum INTEGER NOT NULL DEFAULT 0,
  available INTEGER NOT NULL DEFAULT 1,  -- 0 when the listener lost access (kicked/left)
  updated_at TEXT NOT NULL);

subscriptions(user_id, chat_id, created_at, PRIMARY KEY(user_id, chat_id));

rules(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL DEFAULT 0,  -- 0 = all of this user's chats
  kind TEXT NOT NULL,      -- 'include' | 'exclude'
  pattern TEXT NOT NULL,   -- exactly as typed: 'word' | '=word' | 're:…'
  disabled_reason TEXT,    -- set automatically after repeated regex timeouts
  hits INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
  UNIQUE(user_id, chat_id, kind, pattern));

ignored_senders(user_id, chat_id NOT NULL DEFAULT 0, sender_id, note, created_at,
  PRIMARY KEY(user_id, chat_id, sender_id));

chat_cursor(chat_id PRIMARY KEY, last_msg_id INTEGER NOT NULL, last_msg_at TEXT NOT NULL, updated_at TEXT NOT NULL);

deliveries(user_id, chat_id, msg_id, rule_id, status TEXT, created_at,  -- 'sent'|'digested'|'failed'
  PRIMARY KEY(user_id, chat_id, msg_id));   -- dedupe between live and catch-up; pruned after 7 days
```

## Matching

- Text = `message.message` (Telethon puts captions there too). Empty → skip.
- Fast path in the handler: `event.out` → skip (unless test flag); `chat_id not in monitored` → skip.
- `Matcher.evaluate(chat_id, text, sender_id)`:
  for each user subscribed to `chat_id` (precomputed per chat):
  1. sender in user's ignore list (chat or global) → skip user;
  2. any exclude rule (chat-specific + global) matches → skip user;
  3. first include rule (chat-specific first, then global) that matches → `Match(user_id, rule, span)`.
  Rules are compiled once per (user, chat) into a `ChatRuleSet`; the cache entry is invalidated
  whenever the bot UI changes that user's rules/subscriptions/ignores.
- Compilation with the `regex` module (drop-in for `re`, adds `timeout=`):
  `word` → `regex.escape(word)`, IGNORECASE; `=word` → `(?<!\w)` + escape + `(?!\w)`, IGNORECASE
  (lookarounds instead of `\b` so `=c++` and `=#tag` work); `re:p` → `p` with IGNORECASE
  (users can opt out inline with `(?-i:…)`). Text is passed as-is; IGNORECASE in `regex` handles
  Cyrillic; `ё`/`е` and `і`/`и` are *not* folded (explicit decision; users write both forms).
- Validation on insert: non-empty, ≤ 300 chars, compiles, and a smoke run against a pathological
  string with `timeout=` must not exceed the timeout.
- Match time: `pattern.search(text, timeout=REGEX_TIMEOUT_MS/1000)`; on `TimeoutError` → count as
  no match, increment a per-rule counter; after 3 timeouts the rule is disabled
  (`disabled_reason='timeout'`) and the owner of the rule is told.
- Sender is resolved lazily (only when a rule matched): `event.get_sender()`; `sender.bot` → drop;
  `sender is None` (from_id missing) → keep, sender shown as "unknown".
- Sender identity for ignores: `from_id` peer id (user id, or channel id for anonymous admins /
  channel posts / linked-channel auto-forwards).

## Notification format

```
📡 <chat title as link>                       ← t.me/<username> or the message link for private chats
👤 <sender as link>  (id 123456, @username)    ← see "Sender link"
🔗 <message link>                             ← t.me/<username>/<msg> | t.me/c/<id>/<msg> | …/<topic>/<msg>
🕒 2026-09-22 14:03:05 (Europe/Kyiv) · 12 s ago     ← + "⏪ missed while offline" for catch-up
🔑 rule: =казино  (chat rule)

<full text, matched fragment in bold, truncated to fit 4096 with "… [+N chars]">
[🚫 Ignore sender] [➖ Exclude "казино"]
```

Rendering uses `richtext.py`, which builds `MessageEntity` objects with correct UTF-16 offsets
(emoji-safe) and passes them via `formatting_entities=` so no HTML escaping is involved.

Links (`links.py`), `bare = utils.resolve_id(marked)[0]`:
- message: public `https://t.me/{username}/{msg}`; private supergroup/channel
  `https://t.me/c/{bare}/{msg}`; forum topic inserts `/{topic}` before `{msg}`
  (`topic = reply_to.reply_to_top_id or reply_to.reply_to_msg_id` when `reply_to.forum_topic`, else 1);
  basic groups have no message links → line omitted.
- chat: public → `https://t.me/{username}`; private → the message link; basic group → plain title.
- sender: `@username` → `https://t.me/{username}`; else `tg://user?id={id}` as a raw
  `MessageEntityTextUrl` (bypassing Telethon's mention conversion so the client decides) plus an
  extra inline button `👤` with `url=tg://user?id={id}`; the numeric id is always printed.
  Deleted account → "Deleted account (id …)". Anonymous admin / channel sender → chat/channel
  title with its link. **E2E test must confirm which of these resolve on Desktop/iOS/Android.**

Time: `message.date` (UTC) → user tz via `zoneinfo`; relative part from `now - date` with plural
rules per language (uk/ru: 1 / 2–4 / 5+; en: 1 / other); units s / min / h / d.

Digest (D8): when a user's worker pops an item and the queue length ≥ `digest_threshold`, drain up
to 200 items and send one message: per chat — count, top 3 senders, first 5 message links; items are
recorded as `digested`.

## Delivery

- One `asyncio.Queue` + worker task per user, created lazily; global token bucket 20 msg/s,
  per-user 1 msg/s (Bot API guidance).
- Errors: `FloodWaitError` → `sleep(e.seconds + 1)` then retry the same item (no limit);
  `UserIsBlockedError`, `InputUserDeactivatedError` → set `users.role='paused'`, drop the queue;
  `SlowModeWait`/other `RPCError` → exponential backoff 1→32 s, 5 attempts, then `status='failed'`
  and error log. Never silently drop.
- Dedupe: `INSERT OR IGNORE` into `deliveries` before sending; if the row existed, skip
  (covers live/catch-up overlap and restarts mid-queue).
- On shutdown: workers get ≤10 s to drain; the rest is lost (they will be rediscovered by
  catch-up only if they were in-flight before cursor flush; acceptable for v1, documented).

## Listener and catch-up

- `TelegramClient(session=DATA_DIR/user.session, api_id, api_hash, flood_sleep_threshold=60,
  request_retries=5, connection_retries=None, retry_delay=1, auto_reconnect=True, catch_up=False,
  sequential_updates=False, receive_updates=True)`. `cryptg` installed for speed.
- Handler registered once, without `chats=`; the monitored set is a live `set[int]` owned by
  `Matcher` and updated by the bot UI, so adding a chat needs no re-registration.
- Cursor tracker: in-memory `dict[chat_id, (max msg id, date)]`, flushed to `chat_cursor` every 5 s
  and on shutdown (`INSERT … ON CONFLICT DO UPDATE WHERE excluded.last_msg_id > last_msg_id`).
- Catch-up (`catchup.py`) on every startup after both clients connect: for each monitored chat with
  a cursor — `iter_messages(chat, min_id=last_msg_id, reverse=True, limit=CATCHUP_MAX_PER_CHAT)`,
  stop when `date < now - CATCHUP_HOURS`; feed through the same evaluate → deliver path with
  `catchup=True`; 0.5 s pause between chats; `ChannelPrivateError`/`ChatForbidden` → mark
  `chats.available=0`, notify subscribers. Chats without a cursor start at "now" (no backfill on
  first subscribe). Telethon's own `catch_up` is off to avoid duplicate/flaky replays.
- Chat metadata (title, username, forum flag) is refreshed from the event's chat object when it
  differs from the DB row.
- Bot senders: dropped after match (D6). Own messages: `event.out`.

## Bot UI

Gate: every update from a user not in `users` with role `owner`/`allowed` is answered with a
"pending approval" message; the owner receives a card with `[✅ Allow] [⛔ Deny]`. `OWNER_ID` is
auto-created as `owner` on first start. Everything else is per-user state.

Flows:
- `/start` → language picker (🇺🇦 🇷🇺 🇬🇧) → timezone (quick buttons Kyiv / Warsaw / Berlin / London /
  UTC or "type an IANA name") → main menu.
- Main menu: 📡 Chats · 🔑 Rules · 🚫 Ignored · ⚙️ Settings · 📊 Status · ❓ Help.
- Chats: paginated list (8/page) of the listener's group/supergroup/channel dialogs (cached 5 min
  from `iter_dialogs`), each row toggles subscription ✅/☐; "🔍 search" asks for a title fragment,
  `t.me/…` link or `@username` (resolved through the user client; join links are *not* joined —
  the listener must already be a member).
- Rules: pick a chat or "🌐 all chats" → list with ❌ per rule → "➕ add" prompts for one or more
  lines (one rule per line, `#` comments ignored); invalid lines are reported individually and the
  valid ones are saved. "➖ exclusions" is the same UI with `kind='exclude'`.
- Ignored: list per chat/global with ❌; adding happens mainly from the notification button.
- Settings: language, timezone, digest threshold.
- Status: connected?, uptime, chats monitored, msgs seen / matched today, queue length, last
  update age, catch-up summary.
- `/test <text>` (or `/test` then text): lists which of my rules would match, per chat.
- Pending text input: `pending[user_id] = PendingInput(kind, payload, expires)`; `/cancel` clears;
  a fresh command clears it too.
- Notification buttons (callback data `ig:<chat>:<sender>` / `ex:<chat>:<rule_id>:<msgkey>`):
  ignore adds to `ignored_senders` for that chat with a confirm toast and offers "for all chats";
  exclude proposes the matched fragment as an exclusion pattern `[Add "…"] [Type my own]`.
  Callback data is ≤ 64 bytes; long fragments are stored in a short-lived in-memory map keyed by
  a nonce.

## Health, logging, ops

- `health.py` writes `DATA_DIR/heartbeat` every 30 s when the user client is connected and a
  `GetStateRequest` ping succeeded within the last 5 min; `ddiver health` exits 1 when the file is
  older than 120 s → Docker `HEALTHCHECK`.
- Logging: stdout, structured key=value, `LOG_LEVEL`; Telethon at WARNING. Docker `json-file`
  rotation in compose. Secrets never logged.
- Metrics kept in memory for `/status`: messages seen, matched, delivered, digested, failed,
  regex timeouts, reconnects.
- CLI: `ddiver login` (interactive phone/code/2FA → `DATA_DIR/user.session`), `ddiver run`,
  `ddiver health`, `ddiver check-config`.

## Packaging

- `pyproject.toml` managed by `uv`; runtime deps: telethon ≥1.45, cryptg, aiosqlite, regex,
  pydantic-settings, tzdata. Dev: pytest, pytest-asyncio, pytest-cov, ruff, mypy, hypothesis.
- `Dockerfile`: multi-stage, `python:3.12-slim`, uv-installed venv, non-root, `HEALTHCHECK`.
  `docker-compose.yml`: `./data:/data`, `env_file: .env`, `restart: unless-stopped`, log rotation.
  First-time login: `docker compose run --rm -it ddiver login`.
- CI (`.github/workflows/ci.yml`): ruff → mypy --strict → pytest --cov --cov-fail-under=85 on 3.12.

## Edge cases the tests must cover

media-only message · caption match · forwarded message (forwarder = sender) · own outgoing message ·
bot sender dropped after match · deleted account sender · anonymous admin (from_id = channel) ·
channel post in linked discussion · forum topic link incl. general topic · basic group (no link) ·
public vs private supergroup links · chat title with `<>&` and emoji (entity offsets) · text > 4096 ·
Cyrillic case (`Спам`/`спам`), `=слово` next to punctuation, `=c++` · invalid regex rejected ·
catastrophic regex times out and rule auto-disables · exclusion beats inclusion · global vs chat
rule precedence · ignored sender per chat vs global · FloodWait retry then success · user blocked bot
→ paused · digest collapse at threshold · dedupe live vs catch-up · catch-up respects hours and
per-chat cap · cursor flush uses max id · allow-list gate for strangers · pending input expiry ·
multi-line rule add with mixed valid/invalid lines · timezone conversion & relative time plurals in
uk/ru/en · heartbeat staleness.

## Borrowed ideas (attribution)

- khoben/telemirror (GPL-3, re-implemented): keyword→single IGNORECASE compile idea, MessageLink,
  forum topic id derivation, connect-hang workaround, fatal auth error classification.
- guibacellar/TEx (Apache-2): strict mypy/ruff/coverage tooling baseline, graceful signal handling.
- Hootrix/keyword_alert_bot (GPL-3, idea): dual-client layout, entity resolution caching.
- Heavrnl/TelegramForwarder (GPL-3, idea): add chat by link or dialog-title fragment.
- Riniba/TelegramMonitor (Apache-2, C#, idea): regex timeout + auto-disable, inline "block sender".

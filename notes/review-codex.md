OpenAI Codex v0.155.1
--------
workdir: /Users/L/Code/ddiver
model: gpt-5.6-sol
provider: openai
approval: never
sandbox: danger-full-access
reasoning effort: high
reasoning summaries: detailed
session id: 01a0c925-027c-7ed2-abde-8c790ceaf6e2
--------
user
Review the Telegram keyword-monitoring bot in this repository (all of it — ddiver/ and tests/).

Read DESIGN.md first: it states the requirements, the architecture and the invariants the code is supposed to hold.

Report only real defects:
- bugs that lose, duplicate or misroute notifications
- security or privacy holes in the multi-user model: a hostile allow-listed moderator sending forged callback payloads must not be able to read or mutate another user's rules/ignores/subscriptions/outbox, subscribe to chats the owner did not share, subscribe to a private DM, or escalate to owner
- crashes, hangs, asyncio task leaks, unbounded memory growth
- misuse of the Telethon 1.45 API that would fail or silently never fire against real Telegram
- tests that pass vacuously or assert the wrong thing

Ignore style, naming and speculative performance.

For each finding give file:line, a concrete failure scenario (inputs/state -> wrong behaviour) and a concrete fix.

codex
I’ll read `DESIGN.md` first, then trace the entire implementation and test suite against its invariants. I’ll keep the report strictly to reproducible correctness, isolation, lifecycle, Telethon API, and test-validity defects.
**Preparing to inspect codebase**
exec
/bin/zsh -lc "sed -n '1,260p' DESIGN.md && rg --files ddiver tests | sort" in /Users/L/Code/ddiver
 succeeded in 0ms:
# ddiver — design

Telegram keyword monitor for moderators who are **not admins** of the chats they watch.
A Telethon *user session* (the moderator's own account) listens in the chats; a separate
BotFather bot delivers notifications to each moderator's DM and hosts the configuration UI.

Revision 2 (2026-09-22): incorporates a 27-agent adversarial review of revision 1 against the
Telethon 1.45 source and the Bot API docs. Every claim marked "verified" below was checked
against that source or by experiment; the review notes live in the commit history.

## Decisions (fixed, from the requirements session)

| # | Decision |
|---|---|
| D1 | Listener = Telethon user session on the moderator's main account. A bot cannot be added to the chats. |
| D2 | Delivery + config UI = separate Bot API bot (BotFather token), driven through Telethon in the same process and event loop. |
| D3 | Multi-user with one shared listener: owner (`OWNER_ID`) + allow-listed moderators, each with own language, timezone, chat subscriptions, rules, ignore lists. |
| D4 | Target load: 50+ chats, 100k+ messages/day (≈1.2 msg/s average, bursts of 50+/s). |
| D5 | Rule syntax: `word` = case-insensitive substring; `=word` = whole word; `re:pattern` = regex. Same syntax for exclusions. Per-user ignore list of senders. The listener's own messages are always ignored. |
| D6 | Matched: new messages and media captions, forwarded messages (sender = forwarder). Not matched: edits, bot senders, service messages. |
| D7 | Notification = chat title (link) / sender (link) / message link / date in user tz + "N s ago" / full text with the match in bold + the rule that fired. Inline buttons: ignore sender, add exclusion. |
| D8 | Flood: each match is its own message; above a threshold pending items collapse into one digest. |
| D9 | Catch-up after downtime from a persisted per-chat cursor, capped to N hours; such notifications are labelled "missed while offline". |
| D10 | Hosting: Docker (VPS) and local `uv run`, both documented. SQLite for state. |
| D11 | UI languages uk / ru / en, asked on first `/start`. README EN + RU. Code and comments in English. |
| D12 | Python 3.12, MIT. Tests from the first commit; ruff + mypy strict + coverage gate in CI. |

## Non-goals (v1)

Acting in the monitored chats (deleting, replying, forwarding from the user session), web UI,
Postgres, multiple listener accounts, matching on edits, homoglyph-normalised matching.

## Invariants

1. **No RPC on the listener session inside a message handler.** Only `event.sender` /
   `event.chat` (entities attached to the update). `get_sender()` / `get_chat()` would cost a
   `GetMessages` per matched message and hit FloodWait during exactly the bursts we care about
   (verified: Telethon skips `min` users in its entity cache → `_refetch_sender()` → RPC).
2. **Every callback and command is authorised from `event.sender_id`**, never from the payload,
   and every mutating storage call takes the acting `user_id` and filters on it. Callback data is
   client-controlled: Telegram does not verify that the message actually carried that button.
3. **A match is durable before it is delivered.** The outbox row is written before the in-memory
   cursor advances past the message, so a crash cannot silently drop a notification.
4. **A delivery worker never dies.** Any exception marks the item and the loop continues.
5. **The listener never monitors a private chat.** The monitored set is built from `chats` rows
   of kind group/supergroup/channel; `event.is_private` is rejected in the fast path as well.

## Process model

One process, one asyncio loop, two Telethon clients:

```
                 ┌──────────────── user session (listener) ────────────────┐
 Telegram ──────►│ NewMessage → should_consider → Matcher → Match[]        │
                 │ outbox insert → cursor advance   ChatAction/UpdateChannel│
                 │ catch-up (startup + after every reconnect)              │
                 └───────────────────────────┬────────────────────────────┘
                                             │ outbox rows + in-memory queue
                 ┌───────────────────────────▼────────────────────────────┐
                 │ delivery: per-user queue + worker, shared flood gate,   │
                 │ global 20/s + per-user 1/s buckets, digest, retries     │
                 └───────────────────────────┬────────────────────────────┘
                 ┌───────────────────────────▼────────────────────────────┐
 Moderators ◄───►│ bot session: commands, menus, allow-list gate, delivery │
                 └─────────────────────────────────────────────────────────┘
                              SQLite (aiosqlite, WAL, one connection)
```

Supervisor (`app.py`) owns both clients and returns an exit code (no `sys.exit` inside), so it
is unit-testable with injected clients, clock, sleep and rng:

- `connect_with_timeout`: `connect()` can hang (telethon#1536); on timeout `await disconnect()`
  before retrying so a half-open sender does not leak.
- `run` never calls `client.start()` (it would block on `input()` in a container). It checks
  `is_user_authorized()` and exits 3 with "run `ddiver login` first".
- Fatal auth is classified by base class, not by a name list:
  `AuthKeyNotFound` (transport 404, not an `RPCError`), `UnauthorizedError` (401: session
  revoked/expired, auth key unregistered, user deactivated), `AuthKeyError` (406:
  auth key duplicated — two processes on one session), `PhoneNumberBannedError`.
- On fatal auth the process does **not** exit into a restart loop. It enters **degraded mode**:
  the listener stops, the bot client and workers keep running, `/status` says "listener needs
  re-login", the owner is notified once and then every 6 h, and `ddiver health` reports degraded
  with exit 0 so Docker does not restart it.
- Single instance: `fcntl.flock` on `DATA_DIR/ddiver.lock`; `login` refuses while the lock is
  held or the heartbeat is fresh (two processes on one session file get the key invalidated).
- Boot-rate guard: ≥3 boots in 10 minutes delays catch-up by 5 minutes (crash loops otherwise
  repeat 50–100 `GetHistory` calls per boot).
- Shutdown order (SIGTERM/SIGINT via `loop.add_signal_handler`): stop accepting → disconnect the
  listener → flush cursors and rule hits → drain workers with a 15 s budget → disconnect the bot
  → close the DB. Compose sets `stop_grace_period: 30s`.

## Modules

```
ddiver/
  __main__.py      CLI: login | run | health | check-config (umask 0o077 first)
  config.py        pydantic-settings
  app.py           wiring, supervisor, signals, heartbeat, degraded mode
  clients.py       client factories, connect_with_timeout, is_fatal_auth_error, DdiverClient
  storage.py       aiosqlite repo + migrations
  rules.py         rule parsing/compilation, text cleaning, shadow text with offset map
  matcher.py       per-chat compiled rule sets for all active users, invalidation
  senders.py       SenderInfo from update entities + cached, rate-limited fallback resolver
  links.py         chat/message/user links (public, private, forum topic, basic group)
  richtext.py      text + MessageEntity building with UTF-16 offsets, windowing, entity caps
  formatter.py     Notification and Digest rendering (at send time)
  timeutil.py      tz resolution/format, relative time
  i18n/            en/ru/uk catalogs + t()
  listener.py      handler, should_consider, cursor tracker, chat metadata, access loss
  catchup.py       newest-first backfill from cursors, bounded and labelled
  delivery.py      outbox consumption, queues, buckets, flood gate, digests, error taxonomy
  bot/             router (authorisation, pending input), menus, handlers
  health.py        heartbeat file
tests/            pytest + pytest-asyncio, offline Telethon fakes, virtual clock
```

## Storage (SQLite, WAL, `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`)

One aiosqlite connection for the whole process. `PRAGMA quick_check` at startup; a corrupt file
exits 5 with a restore hint. `OperationalError` in the cursor flush and outbox paths is logged
and retried on the next tick, never propagated into a handler.

```sql
users(id PK, lang TEXT NULL, tz TEXT NULL,          -- NULL = not chosen yet (drives onboarding)
      role TEXT NOT NULL DEFAULT 'pending',          -- 'allowed' | 'pending' | 'denied'
      digest_threshold INTEGER NOT NULL,
      delivery_paused_at TEXT NULL, pause_reason TEXT NULL,
      notified_owner_at TEXT NULL, created_at, updated_at);
chats(id PK,                                         -- marked id (-100… supergroup/channel)
      title, username, kind TEXT NOT NULL,           -- 'supergroup'|'channel'|'group'
      is_forum INTEGER, access_hash INTEGER NULL,    -- lets catch-up build InputPeer directly
      available INTEGER NOT NULL DEFAULT 1,          -- 0 when the listener lost access
      shared INTEGER NOT NULL DEFAULT 0, shared_at TEXT NULL,
      updated_at);
subscriptions(user_id → users, chat_id → chats, created_at, PK(user_id, chat_id));
rules(id PK, user_id → users, chat_id NOT NULL DEFAULT 0,   -- 0 = all of this user's chats
      kind TEXT, pattern TEXT, pattern_key TEXT,            -- case-folded key for uniqueness
      disabled_reason TEXT NULL, hits INTEGER,
      UNIQUE(user_id, chat_id, kind, pattern_key));
ignored_senders(user_id → users, chat_id NOT NULL DEFAULT 0, sender_id, note,
      PK(user_id, chat_id, sender_id));               -- sender_id is a MARKED peer id
chat_cursor(chat_id PK, last_msg_id, last_msg_at, updated_at);
outbox(user_id, chat_id, msg_id, rule_id, status TEXT CHECK(pending|sent|digested|failed),
       attempts INTEGER, last_error TEXT, payload TEXT,     -- JSON snapshot, see below
       created_at, sent_at, PK(user_id, chat_id, msg_id));
```

**Ownership is computed, not stored:** `is_owner(uid) == uid == OWNER_ID`. The owner can never be
locked out, and changing `OWNER_ID` takes effect on the next start without a migration.
"Active" users are the owner plus `role='allowed'`; only active users contribute to the monitored
set, the matcher and delivery. `delivery_paused_at` is separate from `role`, and any incoming
update from a paused user clears the pause (a user who blocked and unblocked the bot recovers
without re-approval).

## Matching

- `should_consider(msg)` is shared by the live handler and catch-up: not a service message, not
  outgoing (unless the test flag), text non-empty, chat in the monitored set, not private.
- Text = `message.message` (captions live there too; it is `None`, not `""`, for media without a
  caption — verified). It is run through `prepare_text`: NFC normalisation plus removal of
  invisible format characters, keeping an index map so highlight spans map back to the original.
- `Matcher.evaluate(chat_id, prepared, sender_id)` → at most one `Match` per active subscribed
  user: ignored sender → skip; any exclusion hit → skip; else the first include rule
  (chat-specific before global). Rule sets are compiled once per chat and invalidated by the UI.
- Rules compile to `regex` patterns (the third-party module, for `timeout=`):
  `word` → escaped; `=word` → `(?<!\w)…(?!\w)` (so `=c++`, `=#tag` work); `re:…` → as typed,
  IGNORECASE, with `(?-i:…)` as the opt-out. Zero-length matches are skipped — Telegram rejects
  zero-length entities and "matched nothing" is not a notification.
- Insert-time validation is explicitly heuristic (the `regex` engine defeats the textbook
  `(a+)+$` bombs, so a probe cannot prove safety): non-empty, ≤300 chars, compiles, does not
  match empty text, does not time out on the probe inputs. The real guard is the runtime
  `timeout` (default 20 ms per search) plus auto-disable after 3 timeouts keyed by `rules.id`
  (shared across chats, because a global rule is compiled into every chat's set).
- Matching runs on the event loop for literal/word rules and in a single worker thread for `re:`
  rules once a chat has more than 20 of them (`regex` releases the GIL — verified: 8.4 M loop
  iterations during a 1 s match in a thread).

## Notification

Rendered **at send time** by the worker, from the payload snapshot plus the user's current
language and timezone, so a queued or digested item is never stale or in the old language.

```
📡 <chat title>                → t.me/<username>, else the message link, else plain text
👤 <name> · @username · <id>   → name links to the MESSAGE; @username to the profile; id is code
🔗 <message link>              → t.me/<username>/<msg> | t.me/c/<bare>/<msg> | …/<topic>/<msg>
🕒 2026-09-22 14:03:05 +03:00 (Europe/Kyiv) · 12 s ago   [⏪ missed while offline]
🔑 <rule pattern> (chat rule | rule for all chats)
<text, matches bold, windowed around the first match if too long>
[🚫 Ignore sender] [➖ Add exclusion]
```

The sender link is deliberately **not** `tg://user?id=` as a text entity: that only resolves on
iOS and only for already-cached users (verified against tdesktop's URL handler table, the iOS
`OpenUrl.swift` `case "user"` and the absence of any `tg://user` handling in Android's
`LaunchActivity`), because a Telethon bot cannot mint the `InputMessageEntityMentionName` that
the Bot API server would. Linking the name to the message works everywhere: the message opens
in the chat, where the avatar opens the profile. Basic groups have no message links, so the name
is plain text and the copyable id is the handle. Deleted accounts, anonymous admins and channel
posts render with their own labels (`sender_id == chat_id` in the latter two cases — verified).

Entities are built with UTF-16 offsets and passed as `formatting_entities=` (Telethon then skips
its own parser entirely — verified), with trailing whitespace stripped, zero-length entities
dropped and at most 90 entities per message. `link_preview=False`.

Digest: the worker pops an item; if `qsize() + 1 >= threshold`, or the oldest queued item is
older than 30 s and at least 5 are queued, it drains up to 200 items into one message: per chat
the count, then up to 5 items as `rule · sender · 60-char snippet · link`, then "and N more",
within a 3500-character budget. Digested rows are marked in one `executemany`.

## Delivery

- Bot client: `flood_sleep_threshold=0`, `raise_last_call_error=True`, `request_retries=3` — so
  the worker owns the retry policy. With Telethon's defaults a FloodWait ≤60 s is slept inside
  the call (invisible), and after 5 attempts it raises a plain `ValueError`, not an `RPCError`
  (verified in `client/users.py`).
- Telethon gates flood waits per request constructor and per client, so a FloodWait on
  `SendMessageRequest` for one moderator applies to all of them. The design accepts this and
  makes it explicit: one shared `flood_until` that every worker awaits, logged once at WARNING.
- Buckets: global 20 msg/s, per-user 1 msg/s (Bot API limits). UI replies go through the same
  per-user bucket so a stranger spamming the bot cannot starve notifications.
- Queues are bounded (2000); on overflow a per-user counter is reported in the next digest.
- Error taxonomy, each branch unit-tested: `FloodWaitError` → shared gate, retry;
  `UserIsBlockedError`/`InputUserDeactivatedError` → pause delivery, drop the queue;
  `BadRequestError` subclasses (`MessageTooLong`, `EntitiesTooLong`, `EntityBoundsInvalid`,
  `ButtonUrlInvalid`, `PeerIdInvalid`) → one degraded resend as plain text without buttons, then
  `failed`; `ValueError` from entity resolution → `failed` + owner log; `ServerError`,
  `TimedOutError`, `ConnectionError` → backoff 1→32 s, 5 attempts. Structurally, every iteration
  is wrapped so no exception can end the loop, and workers are restarted by a done-callback.
- Dedupe and durability: `INSERT OR IGNORE` into `outbox` with `status='pending'` before the
  cursor advances; status becomes `sent`/`digested`/`failed` after the RPC. On startup the
  pending rows are re-queued before catch-up runs. Semantics are at-least-once: a crash between
  the send and the status update re-sends. Rows are pruned after 7 days in batches.

## Listener and catch-up

- Client: `flood_sleep_threshold=60` (catch-up wants Telethon's transparent sleep),
  `connection_retries=None`, `retry_delay=5`, `auto_reconnect=True`, `catch_up=False`,
  `sequential_updates=False`, `entity_cache_limit=20000`, `cryptg` installed.
- The handler is registered once without `chats=`; the monitored set is a live set updated by the
  UI. Chat metadata is refreshed only when `event.chat` is present and differs; the username is
  `username or the first active collectible username` (verified: `Channel.usernames`).
- Cursor tracker: in-memory max id per chat, flushed every 5 s and on shutdown, never moving
  backwards (`WHERE excluded.last_msg_id > chat_cursor.last_msg_id` — verified). A chat's flush
  is withheld until its catch-up finished, so a crash mid-catch-up cannot skip the gap.
- Catch-up (startup, after every reconnect, and on demand) per chat:
  `iter_messages(peer, min_id=cursor, limit=CATCHUP_MAX_PER_CHAT)` — **newest first**, the
  iterator stops natively at `id <= min_id`; break when `date < now - CATCHUP_HOURS`; buffer and
  then process in chronological order with `catchup=True`. If the cap was hit, the first
  notification for that chat says "N older messages were not checked". `yield` every 20 messages;
  per-chat `try/except` for `FloodWaitError` (requeue after `e.seconds`), `ChannelPrivateError`,
  `ChatForbiddenError` and `ValueError` (missing entity); global cap per round.
  `InputPeerChannel(id, access_hash)` is built from the `chats` row, so a fresh session does not
  break the backfill. Chats with no cursor start "now" (no backfill on first subscribe).
- Reconnects: `DdiverClient` overrides `_handle_auto_reconnect` to call `super()` and then set a
  reconcile event. This is necessary because Telethon requests `getChannelDifference` with
  limit 100 for user accounts and, on `ChannelDifferenceTooLong`, jumps `pts` and returns **no**
  updates — at 100k msgs/day a busy chat loses everything beyond 100 messages per gap with no
  signal (verified in `_updates/messagebox.py`). A periodic reconciliation (every 15 min)
  compares each chat's head id with the cursor and backfills the difference.
ddiver/__init__.py
ddiver/__main__.py
ddiver/app.py
ddiver/bot/__init__.py
ddiver/bot/handlers.py
ddiver/bot/menus.py
ddiver/bot/router.py
ddiver/catchup.py
ddiver/clients.py
ddiver/config.py
ddiver/delivery.py
ddiver/formatter.py
ddiver/health.py
ddiver/i18n/__init__.py
ddiver/i18n/en.py
ddiver/i18n/ru.py
ddiver/i18n/uk.py
ddiver/links.py
ddiver/listener.py
ddiver/matcher.py
ddiver/richtext.py
ddiver/rules.py
ddiver/runtime.py
ddiver/senders.py
ddiver/storage.py
ddiver/timeutil.py
tests/__init__.py
tests/fakes.py
tests/test_app.py
tests/test_bot.py
tests/test_catchup.py
tests/test_cli.py
tests/test_config.py
tests/test_delivery.py
tests/test_formatter.py
tests/test_health.py
tests/test_i18n.py
tests/test_links.py
tests/test_listener.py
tests/test_matcher.py
tests/test_richtext.py
tests/test_rules.py
tests/test_runtime.py
tests/test_senders.py
tests/test_storage.py
tests/test_timeutil.py

**Planning to read remaining design lines**
exec
/bin/zsh -lc "sed -n '261,620p' DESIGN.md" in /Users/L/Code/ddiver
 succeeded in 0ms:
- Access loss is detected live: `events.Raw(UpdateChannel)` (primary) and
  `events.ChatAction` for self kick/leave → `available=0`, subscribers notified once on the
  1→0 transition; any later message or re-join flips it back and notifies again. Catch-up skips
  unavailable chats and re-probes them every 6 h.
- Basic group → supergroup migration (`MessageActionChatMigrateTo`): subscriptions, chat rules,
  ignores are moved to the new marked id, the old chat is marked unavailable, subscribers told.

## Bot UI

Authorisation: one catch-all `NewMessage` handler and one catch-all `CallbackQuery` handler,
both limited to private chats, both running `authorize(event.sender_id)` first. Active =
owner or `role='allowed'`. A stranger gets one reply and the owner one card (`notified_owner_at`
throttles to once per 24 h); denied users get silence; unauthorised callbacks are only answered.
`allow:`/`deny:`/`revoke:` additionally require the sender to be the owner.

Sharing model: moderators never see the owner's dialog list. The owner has a "🗂 Shared chats"
menu that toggles `chats.shared`; moderators' picker and search operate on shared rows only, as
a DB substring search — they never trigger `get_entity`/`ResolveUsername` on the user session.
Unsharing deletes their subscriptions for that chat and tells them.

Flows: `/start` → language (seeded from `User.lang_code`) if `lang IS NULL` → timezone if
`tz IS NULL` (quick buttons, then free text resolved with suggestions and a
"Now in Europe/Kyiv: 14:03 (UTC+03:00) — correct?" confirmation) → main menu
(📡 Chats · 🔑 Rules · 🚫 Ignored · ⚙️ Settings · 📊 Status · ❓ Help, plus 👥 Users and
🗂 Shared chats for the owner). Pickers paginate (8 chats, 10 rules per page). Toggles encode
the target state (`sub:<chat>:1`) so a double tap is idempotent.

Rules: the ➕ prompt always shows a localized cheat-sheet (`казино`, `=казино`, `re:каз[иі]но`);
lists show a kind icon and truncate patterns to 60 chars. Multi-line add reports per line
(added / duplicate / invalid with reason); caps: 50 lines per message, 200 rules per (user,
chat), 500 per user. **There is no comment syntax** — `#промо` is a hashtag keyword.
Validation runs in a thread so a slow pattern cannot stall the loop.

Notification buttons carry no nonce: `ig:<chat>:<sender>` and `ex:<chat>:<msg_id>` (both ≤40
bytes) and the handler reads the rule id and matched fragment from the user's own `outbox` row,
which survives restarts and is scoped by construction. Expired (pruned) rows answer "expired,
use ➕ in Rules". The exclusion proposal expands the match to whitespace boundaries and is
offered as `=token`; if it would equal or contain the include pattern, only "type my own" is
offered, with a warning. "Ignore sender" reads `sender_id == chat_id` and then offers
"ignore anonymous admins" instead.

`/test <text>` runs the production pipeline for every subscribed chat and prints, per chat,
"would notify — rule (fragment)", "excluded by …" or "no match". Pending input has a 15-minute
TTL, is cleared only by `/start|/help|/test|/cancel|/status`, and is lost on restart (documented).

## Health, logging, ops

- Heartbeat every 30 s writes JSON (`ts`, `ping_ok_at`, `last_update_at`, `transport`,
  `reconnecting`, `queue_len`) via a temp file + `os.replace`. `is_connected()` stays true during
  Telethon's infinite reconnect loop (verified), so the real signals are the transport flag and a
  60 s `GetStateRequest` ping under `wait_for(15)`. `ddiver health` fails if the file is older
  than 90 s or the last successful ping is older than 3 minutes. If no update arrives for 10
  minutes the listener calls `catch_up()` once and logs; at 30 minutes it is unhealthy.
- Logging: no per-message lines. A 60 s summary (`seen/matched/queued/delivered/floodwait_s`),
  per-match INFO, handler exceptions rate-limited to 5 per minute per exception class.
  `telethon` at WARNING except `telethon.client.users` at INFO (so flood sleeps are visible).
  Compose rotates json-file logs at 50 MB × 5. Message text and names are never logged above
  DEBUG.
- Secrets: `umask(0o077)` before anything is created, `DATA_DIR` 0700, session/db files 0600,
  `.env.example` + `.gitignore`, and a README threat model (the session file is the account).
  `check-config` warns about group/world-readable files and about an unreachable `OWNER_ID`
  ("open t.me/<bot> and press Start"), and refuses a `BOT_TOKEN` that does not match
  `bot.session`.

## Packaging

`uv`-managed `pyproject.toml`; runtime: telethon ≥1.45, cryptg, aiosqlite, regex,
pydantic-settings, tzdata. Dockerfile: `python:3.12-slim`, multi-stage, non-root, exec-form CMD,
`HEALTHCHECK`. Compose: one `./data` volume, `env_file`, `restart: unless-stopped`, `init: true`,
`stop_grace_period: 30s`, log rotation. CI: ruff → mypy --strict → pytest with an 85 % gate,
on Linux (so the case-sensitive timezone behaviour is what ships).

## Edge cases the tests cover

media-only message (`message` is `None`) · caption match · forwarded message · own outgoing
message · bot sender dropped · deleted account · anonymous admin and channel post
(`sender_id == chat_id`) · forum topic links including the general topic · basic group without
links · collectible usernames · chat title with `<>&` and emoji · text beyond 4096 UTF-16 units ·
windowing around a late match · zero-width and NFD text with offset mapping · Cyrillic case ·
`=слово` next to punctuation · `=c++` · hashtag rules · invalid regex · catastrophic regex
timing out and auto-disabling · exclusion beating inclusion · chat rule before global rule ·
ignore per chat vs global · private chat never monitored · callback with a foreign rule id ·
`allow:` from a non-owner · FloodWait retry · blocked user paused then restored · digest at the
threshold and by age · outbox dedupe live vs catch-up · crash before send re-delivers exactly
once · catch-up direction, cap and cutoff · cursor withheld during catch-up · migration to
supergroup · tz resolution and DST folds · relative time with clock skew · heartbeat staleness.

## Borrowed ideas (attribution)

khoben/telemirror (GPL-3, re-implemented): keyword compilation, message links, forum topic
derivation, connect-hang workaround, fatal-auth classification. guibacellar/TEx (Apache-2):
tooling baseline, graceful signal handling. Hootrix/keyword_alert_bot (GPL-3, idea): dual-client
layout, entity caching. Heavrnl/TelegramForwarder (GPL-3, idea): add chat by link or title
fragment, rule table shape. Riniba/TelegramMonitor (Apache-2, idea): regex timeout with
auto-disable, inline "block sender".

**Planning exhaustive testing and static analysis**
exec
/bin/zsh -lc 'wc -l ddiver/*.py ddiver/bot/*.py ddiver/i18n/*.py tests/*.py pyproject.toml && git status --short && git log -1 --oneline' in /Users/L/Code/ddiver
 succeeded in 0ms:
       0 ddiver/__init__.py
     247 ddiver/__main__.py
     204 ddiver/app.py
     177 ddiver/catchup.py
     117 ddiver/clients.py
      76 ddiver/config.py
     396 ddiver/delivery.py
     268 ddiver/formatter.py
      36 ddiver/health.py
      85 ddiver/links.py
     270 ddiver/listener.py
     184 ddiver/matcher.py
     170 ddiver/richtext.py
     253 ddiver/rules.py
     538 ddiver/runtime.py
     189 ddiver/senders.py
     876 ddiver/storage.py
     171 ddiver/timeutil.py
       0 ddiver/bot/__init__.py
     752 ddiver/bot/handlers.py
     230 ddiver/bot/menus.py
     283 ddiver/bot/router.py
      31 ddiver/i18n/__init__.py
     158 ddiver/i18n/en.py
     154 ddiver/i18n/ru.py
     154 ddiver/i18n/uk.py
       0 tests/__init__.py
     205 tests/fakes.py
     241 tests/test_app.py
     554 tests/test_bot.py
     269 tests/test_catchup.py
     111 tests/test_cli.py
      57 tests/test_config.py
     351 tests/test_delivery.py
     224 tests/test_formatter.py
      27 tests/test_health.py
      14 tests/test_i18n.py
      72 tests/test_links.py
     238 tests/test_listener.py
     134 tests/test_matcher.py
     110 tests/test_richtext.py
     204 tests/test_rules.py
     235 tests/test_runtime.py
     164 tests/test_senders.py
     394 tests/test_storage.py
     118 tests/test_timeutil.py
     126 pyproject.toml
    9867 total
41fca61 Add runtime adapters, CLI, Docker, CI and bilingual README

**Planning initial tests with pytest**
codex
The repository is about 10k lines including tests. I’m now doing two passes: first the storage/auth/callback/outbox paths for cross-user and delivery integrity, then listener/catch-up/runtime lifecycle and the tests’ actual assertions. I’ll run the suite as a baseline, but won’t treat green tests as proof.
exec
/bin/zsh -lc 'uv run pytest -q' in /Users/L/Code/ddiver
**Starting storage inspection**
exec
/bin/zsh -lc "sed -n '1,240p' ddiver/storage.py && sed -n '241,520p' ddiver/storage.py" in /Users/L/Code/ddiver
 succeeded in 0ms:
"""SQLite persistence (aiosqlite, WAL). One connection, one writer, simple migrations.

Ownership is *computed* (``user_id == OWNER_ID``), never stored, so the owner can never be
locked out and changing ``OWNER_ID`` needs no migration. Every mutating method takes the acting
``user_id`` and filters on it, because callback payloads are client-controlled.
"""

from __future__ import annotations

import datetime as dt
import enum
import json
import logging
import sqlite3
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from ddiver.links import ChatRef
from ddiver.rules import RuleKind, pattern_key
from ddiver.timeutil import Clock, utcnow

log = logging.getLogger(__name__)

ALL_CHATS = 0
"""``chat_id`` value meaning "every chat this user is subscribed to" (rules, ignores)."""

CHAT_KINDS = ("group", "supergroup", "channel")
PRUNE_BATCH = 5000


class Role(enum.StrEnum):
    ALLOWED = "allowed"
    PENDING = "pending"
    DENIED = "denied"


class DeliveryStatus(enum.StrEnum):
    PENDING = "pending"
    SENT = "sent"
    DIGESTED = "digested"
    FAILED = "failed"


class DatabaseCorruptError(Exception):
    """``PRAGMA quick_check`` failed; the file needs restoring from a backup."""


class DuplicateRuleError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class User:
    id: int
    lang: str | None
    tz: str | None
    role: Role
    digest_threshold: int
    delivery_paused_at: dt.datetime | None
    pause_reason: str | None
    notified_owner_at: dt.datetime | None
    is_owner: bool = False

    @property
    def onboarded(self) -> bool:
        return self.lang is not None and self.tz is not None

    @property
    def active(self) -> bool:
        """May use the bot at all (independently of delivery being paused)."""
        return self.is_owner or self.role is Role.ALLOWED

    @property
    def paused(self) -> bool:
        return self.delivery_paused_at is not None


@dataclass(frozen=True, slots=True)
class ChatRow:
    ref: ChatRef
    available: bool
    shared: bool
    access_hash: int | None

    @property
    def id(self) -> int:
        return self.ref.id


@dataclass(frozen=True, slots=True)
class Rule:
    id: int
    user_id: int
    chat_id: int
    kind: RuleKind
    pattern: str
    disabled_reason: str | None
    hits: int

    @property
    def is_global(self) -> bool:
        return self.chat_id == ALL_CHATS


@dataclass(frozen=True, slots=True)
class IgnoredSender:
    user_id: int
    chat_id: int
    sender_id: int
    note: str | None


@dataclass(frozen=True, slots=True)
class Cursor:
    chat_id: int
    last_msg_id: int
    last_msg_at: dt.datetime


@dataclass(frozen=True, slots=True)
class OutboxItem:
    user_id: int
    chat_id: int
    msg_id: int
    rule_id: int | None
    status: DeliveryStatus
    attempts: int
    payload: dict[str, Any]
    created_at: dt.datetime

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.user_id, self.chat_id, self.msg_id)


_MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE users (
      id INTEGER PRIMARY KEY,
      lang TEXT,
      tz TEXT,
      role TEXT NOT NULL DEFAULT 'pending',
      digest_threshold INTEGER NOT NULL DEFAULT 20,
      delivery_paused_at TEXT,
      pause_reason TEXT,
      notified_owner_at TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE chats (
      id INTEGER PRIMARY KEY,
      title TEXT NOT NULL,
      username TEXT,
      kind TEXT NOT NULL CHECK (kind IN ('group','supergroup','channel')),
      is_forum INTEGER NOT NULL DEFAULT 0,
      access_hash INTEGER,
      available INTEGER NOT NULL DEFAULT 1,
      shared INTEGER NOT NULL DEFAULT 0,
      shared_at TEXT,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE subscriptions (
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
      created_at TEXT NOT NULL,
      PRIMARY KEY (user_id, chat_id)
    );
    CREATE INDEX subscriptions_chat ON subscriptions(chat_id);
    CREATE TABLE rules (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      chat_id INTEGER NOT NULL DEFAULT 0,
      kind TEXT NOT NULL CHECK (kind IN ('include','exclude')),
      pattern TEXT NOT NULL,
      pattern_key TEXT NOT NULL,
      disabled_reason TEXT,
      hits INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      UNIQUE (user_id, chat_id, kind, pattern_key)
    );
    CREATE TABLE ignored_senders (
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      chat_id INTEGER NOT NULL DEFAULT 0,
      sender_id INTEGER NOT NULL,
      note TEXT,
      created_at TEXT NOT NULL,
      PRIMARY KEY (user_id, chat_id, sender_id)
    );
    CREATE TABLE chat_cursor (
      chat_id INTEGER PRIMARY KEY,
      last_msg_id INTEGER NOT NULL,
      last_msg_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE outbox (
      user_id INTEGER NOT NULL,
      chat_id INTEGER NOT NULL,
      msg_id INTEGER NOT NULL,
      rule_id INTEGER,
      status TEXT NOT NULL CHECK (status IN ('pending','sent','digested','failed')),
      attempts INTEGER NOT NULL DEFAULT 0,
      last_error TEXT,
      payload TEXT NOT NULL,
      created_at TEXT NOT NULL,
      sent_at TEXT,
      PRIMARY KEY (user_id, chat_id, msg_id)
    );
    CREATE INDEX outbox_pending ON outbox(status, created_at);
    CREATE TABLE boots (started_at TEXT NOT NULL);
    """,
]


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat(timespec="seconds")


def _parse(text: str | None) -> dt.datetime | None:
    if not text:
        return None
    moment = dt.datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


class Storage:
    def __init__(self, path: Path | str, *, owner_id: int = 0, clock: Clock = utcnow) -> None:
        self._path = str(path)
        self._owner_id = owner_id
        self._clock = clock
        self._db: aiosqlite.Connection | None = None

    # -- lifecycle -------------------------------------------------------------------------

    async def open(self) -> None:
        if self._path != ":memory:" and not self._path.startswith("file:"):
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self._path, isolation_level=None, uri=True)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=5000")
        self._db = db
        try:
            await self._quick_check()
            await self._migrate()
        except Exception:
            self._db = None
            await db.close()
            raise

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            msg = "Storage is not open"
            raise RuntimeError(msg)
        return self._db

    async def _quick_check(self) -> None:
        cur = await self.db.execute("PRAGMA quick_check")
        row = await cur.fetchone()
        result = str(row[0]) if row else "unknown"
        if result != "ok":
            raise DatabaseCorruptError(result)

    async def _migrate(self) -> None:
        version = await self.schema_version()
        for index, script in enumerate(_MIGRATIONS[version:], start=version + 1):
            # executescript() commits any open transaction first, so the transaction has to
            # live inside the script itself for the migration to be atomic.
            await self.db.executescript(f"BEGIN;\n{script}\nPRAGMA user_version={index};\nCOMMIT;")

    async def schema_version(self) -> int:
        cur = await self.db.execute("PRAGMA user_version")
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    def _now(self) -> str:
        return _iso(self._clock())

    def is_owner(self, user_id: int) -> bool:
        return self._owner_id != 0 and user_id == self._owner_id

    # -- users -----------------------------------------------------------------------------

    def _user(self, row: aiosqlite.Row) -> User:
        return User(
            id=row["id"],
            lang=row["lang"],
            tz=row["tz"],
            role=Role(row["role"]),
            digest_threshold=row["digest_threshold"],
            delivery_paused_at=_parse(row["delivery_paused_at"]),
            pause_reason=row["pause_reason"],
            notified_owner_at=_parse(row["notified_owner_at"]),
            is_owner=self.is_owner(row["id"]),
        )

    async def get_user(self, user_id: int) -> User | None:
        cur = await self.db.execute("SELECT * FROM users WHERE id=?", (user_id,))
        row = await cur.fetchone()
        return self._user(row) if row else None

    async def ensure_user(
        self, user_id: int, *, lang: str | None = None, digest_threshold: int = 20
    ) -> User:
        """Create the user row if missing and return it. Existing rows are untouched."""
        now = self._now()
        role = Role.ALLOWED if self.is_owner(user_id) else Role.PENDING
        await self.db.execute(
            """INSERT OR IGNORE INTO users
                 (id, lang, role, digest_threshold, created_at, updated_at)
               VALUES (?,?,?,?,?,?)""",
            (user_id, lang, role.value, digest_threshold, now, now),
        )
        user = await self.get_user(user_id)
        if user is None:  # pragma: no cover - impossible after the insert
            raise KeyError(user_id)
        return user

    async def update_user(self, user_id: int, **fields: Any) -> User:
        allowed = {
            "lang",
            "tz",
            "role",
            "digest_threshold",
            "delivery_paused_at",
            "pause_reason",
            "notified_owner_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            msg = f"unknown user fields: {sorted(unknown)}"
            raise ValueError(msg)
        if not fields:
            user = await self.get_user(user_id)
            if user is None:
                raise KeyError(user_id)
            return user
        values: dict[str, Any] = {}
        for key, value in fields.items():
            if isinstance(value, enum.Enum):
                values[key] = value.value
            elif isinstance(value, dt.datetime):
                values[key] = _iso(value)
            else:
                values[key] = value
        assignments = ", ".join(f"{k}=?" for k in values)
        await self.db.execute(
            f"UPDATE users SET {assignments}, updated_at=? WHERE id=?",  # noqa: S608 - keys checked
            (*values.values(), self._now(), user_id),
        )
        user = await self.get_user(user_id)
        if user is None:
            raise KeyError(user_id)
        return user

    async def pause_delivery(self, user_id: int, reason: str) -> None:
        await self.update_user(user_id, delivery_paused_at=self._clock(), pause_reason=reason)

    async def resume_delivery(self, user_id: int) -> bool:
        """Clear a delivery pause; ``True`` when there was one (any update from the user does)."""
        cur = await self.db.execute(
            """UPDATE users SET delivery_paused_at=NULL, pause_reason=NULL, updated_at=?
               WHERE id=? AND delivery_paused_at IS NOT NULL""",
            (self._now(), user_id),
        )
        return cur.rowcount == 1

    async def list_users(self, *, roles: set[Role] | None = None) -> list[User]:
        if roles:
            marks = ",".join("?" * len(roles))
            cur = await self.db.execute(
                f"SELECT * FROM users WHERE role IN ({marks}) ORDER BY id",  # noqa: S608
                tuple(r.value for r in roles),
            )
        else:
            cur = await self.db.execute("SELECT * FROM users ORDER BY id")
        return [self._user(r) for r in await cur.fetchall()]

    async def active_user_ids(self) -> set[int]:
        cur = await self.db.execute("SELECT id, role FROM users")
        return {
            int(r["id"])
            for r in await cur.fetchall()
            if self.is_owner(r["id"]) or r["role"] == Role.ALLOWED.value
        }

    # -- chats -----------------------------------------------------------------------------

    @staticmethod
    def _chat(row: aiosqlite.Row) -> ChatRow:
        ref = ChatRef(
            id=row["id"],
            title=row["title"],
            username=row["username"],
            kind=row["kind"],
            is_forum=bool(row["is_forum"]),
        )
        return ChatRow(
            ref=ref,
            available=bool(row["available"]),
            shared=bool(row["shared"]),
            access_hash=row["access_hash"],
        )

    async def upsert_chat(self, ref: ChatRef, *, access_hash: int | None = None) -> None:
        """Insert or refresh chat metadata. ``available`` and ``shared`` are never reset here."""
        await self.db.execute(
            """INSERT INTO chats (id, title, username, kind, is_forum, access_hash, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title, username=excluded.username,
                 kind=excluded.kind, is_forum=excluded.is_forum,
                 access_hash=COALESCE(excluded.access_hash, chats.access_hash),
                 updated_at=excluded.updated_at""",
            (
                ref.id,
                ref.title,
                ref.username,
                ref.kind,
                int(ref.is_forum),
                access_hash,
                self._now(),
            ),
        )

    async def get_chat(self, chat_id: int) -> ChatRow | None:
        cur = await self.db.execute("SELECT * FROM chats WHERE id=?", (chat_id,))
        row = await cur.fetchone()
        return self._chat(row) if row else None

    async def list_chats(self, *, shared_only: bool = False) -> list[ChatRow]:
        sql = "SELECT * FROM chats"
        if shared_only:
            sql += " WHERE shared=1"
        sql += " ORDER BY title COLLATE NOCASE"
        cur = await self.db.execute(sql)
        return [self._chat(r) for r in await cur.fetchall()]

    async def set_chat_available(self, chat_id: int, available: bool) -> bool:
        """``True`` when the flag actually changed, so callers notify only on transitions."""
        cur = await self.db.execute(
            "UPDATE chats SET available=?, updated_at=? WHERE id=? AND available<>?",
            (int(available), self._now(), chat_id, int(available)),
        )
        return cur.rowcount == 1

    async def set_chat_shared(self, chat_id: int, shared: bool) -> bool:
        cur = await self.db.execute(
            "UPDATE chats SET shared=?, shared_at=?, updated_at=? WHERE id=? AND shared<>?",
            (int(shared), self._now() if shared else None, self._now(), chat_id, int(shared)),
        )
        return cur.rowcount == 1

    async def migrate_chat(self, old_id: int, new_ref: ChatRef, access_hash: int | None) -> None:
        """Move subscriptions, rules and ignores from a basic group to its supergroup."""
        await self.upsert_chat(new_ref, access_hash=access_hash)
        now = self._now()
        await self.db.execute(
            """INSERT OR IGNORE INTO subscriptions (user_id, chat_id, created_at)
               SELECT user_id, ?, ? FROM subscriptions WHERE chat_id=?""",
            (new_ref.id, now, old_id),
        )
        await self.db.execute("DELETE FROM subscriptions WHERE chat_id=?", (old_id,))
        await self.db.execute("UPDATE rules SET chat_id=? WHERE chat_id=?", (new_ref.id, old_id))
        await self.db.execute(
            "UPDATE ignored_senders SET chat_id=? WHERE chat_id=?", (new_ref.id, old_id)
        )
        await self.db.execute("DELETE FROM chat_cursor WHERE chat_id=?", (old_id,))
        await self.db.execute(
            "UPDATE chats SET available=0, updated_at=? WHERE id=?", (now, old_id)
        )

    # -- subscriptions ---------------------------------------------------------------------

    async def subscribe(self, user_id: int, chat_id: int) -> bool:
        """Subscribe, but only to a known chat of a group-like kind (callback data is untrusted)."""
        chat = await self.get_chat(chat_id)
        if chat is None or chat.ref.kind not in CHAT_KINDS:
            return False
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO subscriptions (user_id, chat_id, created_at) VALUES (?,?,?)",
            (user_id, chat_id, self._now()),
        )
        return cur.rowcount == 1

    async def unsubscribe(self, user_id: int, chat_id: int) -> bool:
        cur = await self.db.execute(
            "DELETE FROM subscriptions WHERE user_id=? AND chat_id=?", (user_id, chat_id)
        )
        return cur.rowcount == 1

    async def unsubscribe_all(self, chat_id: int, *, except_user: int | None = None) -> list[int]:
        """Drop every subscription for a chat (used when the owner unshares it)."""
        sql = "SELECT user_id FROM subscriptions WHERE chat_id=?"
        params: list[Any] = [chat_id]
        if except_user is not None:
            sql += " AND user_id<>?"
            params.append(except_user)
        cur = await self.db.execute(sql, params)
        affected = [int(r["user_id"]) for r in await cur.fetchall()]
        if affected:
            marks = ",".join("?" * len(affected))
            await self.db.execute(
                f"DELETE FROM subscriptions WHERE chat_id=? AND user_id IN ({marks})",  # noqa: S608
                (chat_id, *affected),
            )
        return affected

    async def is_subscribed(self, user_id: int, chat_id: int) -> bool:

exec
/bin/zsh -lc "sed -n '520,900p' ddiver/storage.py" in /Users/L/Code/ddiver
 succeeded in 0ms:
    async def is_subscribed(self, user_id: int, chat_id: int) -> bool:
        cur = await self.db.execute(
            "SELECT 1 FROM subscriptions WHERE user_id=? AND chat_id=?", (user_id, chat_id)
        )
        return await cur.fetchone() is not None

    async def list_subscriptions(self, user_id: int) -> list[ChatRow]:
        cur = await self.db.execute(
            """SELECT c.* FROM chats c JOIN subscriptions s ON s.chat_id=c.id
               WHERE s.user_id=? ORDER BY c.title COLLATE NOCASE""",
            (user_id,),
        )
        return [self._chat(r) for r in await cur.fetchall()]

    async def subscribers_of(self, chat_id: int, *, active_only: bool = True) -> list[int]:
        cur = await self.db.execute(
            "SELECT user_id FROM subscriptions WHERE chat_id=? ORDER BY user_id", (chat_id,)
        )
        ids = [int(r["user_id"]) for r in await cur.fetchall()]
        if not active_only:
            return ids
        active = await self.active_user_ids()
        return [uid for uid in ids if uid in active]

    async def monitored_chat_ids(self) -> set[int]:
        """Chats with at least one active, unpaused subscriber, that we still have access to."""
        active = await self.active_user_ids()
        if not active:
            return set()
        marks = ",".join("?" * len(active))
        cur = await self.db.execute(
            f"""SELECT DISTINCT s.chat_id FROM subscriptions s
                JOIN users u ON u.id=s.user_id
                JOIN chats c ON c.id=s.chat_id
                WHERE s.user_id IN ({marks}) AND u.delivery_paused_at IS NULL
                  AND c.available=1 AND c.kind IN ('group','supergroup','channel')""",  # noqa: S608
            tuple(sorted(active)),
        )
        return {int(r["chat_id"]) for r in await cur.fetchall()}

    # -- rules -----------------------------------------------------------------------------

    @staticmethod
    def _rule(row: aiosqlite.Row) -> Rule:
        return Rule(
            id=row["id"],
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            kind=RuleKind(row["kind"]),
            pattern=row["pattern"],
            disabled_reason=row["disabled_reason"],
            hits=row["hits"],
        )

    async def add_rule(self, user_id: int, chat_id: int, kind: RuleKind, pattern: str) -> Rule:
        try:
            cur = await self.db.execute(
                """INSERT INTO rules (user_id, chat_id, kind, pattern, pattern_key, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (user_id, chat_id, kind.value, pattern, pattern_key(pattern), self._now()),
            )
        except aiosqlite.IntegrityError as exc:
            raise DuplicateRuleError(pattern) from exc
        rule = await self.get_rule(int(cur.lastrowid or 0), user_id)
        if rule is None:  # pragma: no cover - impossible after the insert
            raise KeyError(pattern)
        return rule

    async def get_rule(self, rule_id: int, user_id: int) -> Rule | None:
        cur = await self.db.execute(
            "SELECT * FROM rules WHERE id=? AND user_id=?", (rule_id, user_id)
        )
        row = await cur.fetchone()
        return self._rule(row) if row else None

    async def delete_rule(self, rule_id: int, user_id: int) -> bool:
        cur = await self.db.execute(
            "DELETE FROM rules WHERE id=? AND user_id=?", (rule_id, user_id)
        )
        return cur.rowcount == 1

    async def count_rules(self, user_id: int, chat_id: int | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM rules WHERE user_id=?"
        params: list[Any] = [user_id]
        if chat_id is not None:
            sql += " AND chat_id=?"
            params.append(chat_id)
        cur = await self.db.execute(sql, params)
        row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def list_rules(
        self, user_id: int, chat_id: int | None = None, kind: RuleKind | None = None
    ) -> list[Rule]:
        sql = "SELECT * FROM rules WHERE user_id=?"
        params: list[Any] = [user_id]
        if chat_id is not None:
            sql += " AND chat_id=?"
            params.append(chat_id)
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind.value)
        sql += " ORDER BY (chat_id=0) DESC, chat_id, kind DESC, id"
        cur = await self.db.execute(sql, params)
        return [self._rule(r) for r in await cur.fetchall()]

    async def rules_for_chat(self, chat_id: int) -> list[Rule]:
        """Enabled rules applying to ``chat_id``: chat-specific ones plus the global rules of
        every active, unpaused subscriber. Chat-specific rules come first."""
        active = await self.active_user_ids()
        if not active:
            return []
        marks = ",".join("?" * len(active))
        cur = await self.db.execute(
            f"""SELECT r.* FROM rules r
                JOIN users u ON u.id=r.user_id
                JOIN subscriptions s ON s.user_id=r.user_id AND s.chat_id=?
                WHERE (r.chat_id=? OR r.chat_id=0) AND r.disabled_reason IS NULL
                  AND r.user_id IN ({marks}) AND u.delivery_paused_at IS NULL
                ORDER BY r.user_id, (r.chat_id=0), r.kind, r.id""",  # noqa: S608
            (chat_id, chat_id, *sorted(active)),
        )
        return [self._rule(r) for r in await cur.fetchall()]

    async def disable_rule(self, rule_id: int, reason: str) -> None:
        await self.db.execute("UPDATE rules SET disabled_reason=? WHERE id=?", (reason, rule_id))

    async def enable_rule(self, rule_id: int, user_id: int) -> bool:
        cur = await self.db.execute(
            "UPDATE rules SET disabled_reason=NULL WHERE id=? AND user_id=?", (rule_id, user_id)
        )
        return cur.rowcount == 1

    async def flush_rule_hits(self, hits: dict[int, int]) -> None:
        if not hits:
            return
        await self.db.executemany(
            "UPDATE rules SET hits=hits+? WHERE id=?",
            [(count, rule_id) for rule_id, count in hits.items()],
        )

    # -- ignored senders -------------------------------------------------------------------

    async def add_ignored(
        self, user_id: int, chat_id: int, sender_id: int, note: str | None = None
    ) -> bool:
        cur = await self.db.execute(
            """INSERT OR IGNORE INTO ignored_senders
                 (user_id, chat_id, sender_id, note, created_at)
               VALUES (?,?,?,?,?)""",
            (user_id, chat_id, sender_id, note, self._now()),
        )
        return cur.rowcount == 1

    async def remove_ignored(self, user_id: int, chat_id: int, sender_id: int) -> bool:
        cur = await self.db.execute(
            "DELETE FROM ignored_senders WHERE user_id=? AND chat_id=? AND sender_id=?",
            (user_id, chat_id, sender_id),
        )
        return cur.rowcount == 1

    async def list_ignored(self, user_id: int, chat_id: int | None = None) -> list[IgnoredSender]:
        sql = "SELECT * FROM ignored_senders WHERE user_id=?"
        params: list[Any] = [user_id]
        if chat_id is not None:
            sql += " AND chat_id=?"
            params.append(chat_id)
        sql += " ORDER BY (chat_id=0) DESC, chat_id, sender_id"
        cur = await self.db.execute(sql, params)
        return [
            IgnoredSender(r["user_id"], r["chat_id"], r["sender_id"], r["note"])
            for r in await cur.fetchall()
        ]

    async def ignored_for_chat(self, chat_id: int) -> dict[int, set[int]]:
        """``{user_id: {sender_id, …}}`` combining chat-specific and global ignores."""
        cur = await self.db.execute(
            "SELECT user_id, sender_id FROM ignored_senders WHERE chat_id=? OR chat_id=0",
            (chat_id,),
        )
        out: dict[int, set[int]] = {}
        for r in await cur.fetchall():
            out.setdefault(int(r["user_id"]), set()).add(int(r["sender_id"]))
        return out

    # -- cursors ---------------------------------------------------------------------------

    async def get_cursors(self) -> dict[int, Cursor]:
        cur = await self.db.execute("SELECT * FROM chat_cursor")
        out: dict[int, Cursor] = {}
        for r in await cur.fetchall():
            moment = _parse(r["last_msg_at"])
            if moment is None:  # pragma: no cover - column is NOT NULL
                continue
            out[int(r["chat_id"])] = Cursor(int(r["chat_id"]), int(r["last_msg_id"]), moment)
        return out

    async def upsert_cursors(self, cursors: list[Cursor]) -> None:
        """Advance cursors; a stored id is never moved backwards."""
        if not cursors:
            return
        now = self._now()
        await self.db.executemany(
            """INSERT INTO chat_cursor (chat_id, last_msg_id, last_msg_at, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 last_msg_id=excluded.last_msg_id, last_msg_at=excluded.last_msg_at,
                 updated_at=excluded.updated_at
               WHERE excluded.last_msg_id > chat_cursor.last_msg_id""",
            [(c.chat_id, c.last_msg_id, _iso(c.last_msg_at), now) for c in cursors],
        )

    async def delete_cursor(self, chat_id: int) -> None:
        await self.db.execute("DELETE FROM chat_cursor WHERE chat_id=?", (chat_id,))

    # -- outbox ----------------------------------------------------------------------------

    @staticmethod
    def _outbox(row: aiosqlite.Row) -> OutboxItem:
        created = _parse(row["created_at"])
        return OutboxItem(
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            msg_id=row["msg_id"],
            rule_id=row["rule_id"],
            status=DeliveryStatus(row["status"]),
            attempts=row["attempts"],
            payload=json.loads(row["payload"]),
            created_at=created or dt.datetime.fromtimestamp(0, dt.UTC),
        )

    async def claim_delivery(
        self,
        user_id: int,
        chat_id: int,
        msg_id: int,
        rule_id: int | None,
        payload: dict[str, Any],
    ) -> OutboxItem | None:
        """Record intent to deliver. ``None`` when this (user, chat, msg) was already claimed —
        the dedupe between live updates, catch-up and restarts."""
        cur = await self.db.execute(
            """INSERT OR IGNORE INTO outbox
                 (user_id, chat_id, msg_id, rule_id, status, payload, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                user_id,
                chat_id,
                msg_id,
                rule_id,
                DeliveryStatus.PENDING.value,
                json.dumps(payload, ensure_ascii=False),
                self._now(),
            ),
        )
        if cur.rowcount != 1:
            return None
        return await self.get_delivery(user_id, chat_id, msg_id)

    async def get_delivery(self, user_id: int, chat_id: int, msg_id: int) -> OutboxItem | None:
        cur = await self.db.execute(
            "SELECT * FROM outbox WHERE user_id=? AND chat_id=? AND msg_id=?",
            (user_id, chat_id, msg_id),
        )
        row = await cur.fetchone()
        return self._outbox(row) if row else None

    async def pending_deliveries(self, limit: int = 5000) -> list[OutboxItem]:
        cur = await self.db.execute(
            "SELECT * FROM outbox WHERE status='pending' ORDER BY created_at, msg_id LIMIT ?",
            (limit,),
        )
        return [self._outbox(r) for r in await cur.fetchall()]

    async def set_delivery_status(
        self,
        user_id: int,
        chat_id: int,
        msg_id: int,
        status: DeliveryStatus,
        *,
        error: str | None = None,
        attempts: int | None = None,
    ) -> None:
        sent_at = self._now() if status in (DeliveryStatus.SENT, DeliveryStatus.DIGESTED) else None
        await self.db.execute(
            """UPDATE outbox SET status=?, last_error=?, sent_at=?,
                 attempts=COALESCE(?, attempts)
               WHERE user_id=? AND chat_id=? AND msg_id=?""",
            (status.value, error, sent_at, attempts, user_id, chat_id, msg_id),
        )

    async def mark_digested(self, keys: list[tuple[int, int, int]]) -> None:
        if not keys:
            return
        now = self._now()
        await self.db.executemany(
            """UPDATE outbox SET status='digested', sent_at=?
               WHERE user_id=? AND chat_id=? AND msg_id=?""",
            [(now, *key) for key in keys],
        )

    async def release_delivery(self, user_id: int, chat_id: int, msg_id: int) -> None:
        await self.db.execute(
            "DELETE FROM outbox WHERE user_id=? AND chat_id=? AND msg_id=?",
            (user_id, chat_id, msg_id),
        )

    async def delivery_counts(self, since: dt.datetime) -> dict[str, int]:
        cur = await self.db.execute(
            "SELECT status, COUNT(*) AS n FROM outbox WHERE created_at>=? GROUP BY status",
            (_iso(since),),
        )
        return {str(r["status"]): int(r["n"]) for r in await cur.fetchall()}

    async def prune_outbox(self, older_than: dt.timedelta = dt.timedelta(days=7)) -> int:
        cutoff = _iso(self._clock() - older_than)
        removed = 0
        while True:
            cur = await self.db.execute(
                """DELETE FROM outbox WHERE rowid IN
                     (SELECT rowid FROM outbox WHERE created_at<? AND status<>'pending' LIMIT ?)""",
                (cutoff, PRUNE_BATCH),
            )
            if cur.rowcount <= 0:
                return removed
            removed += cur.rowcount

    # -- boots -----------------------------------------------------------------------------

    async def record_boot(self) -> int:
        """Record this start and return how many boots happened in the last 10 minutes."""
        now = self._clock()
        await self.db.execute("INSERT INTO boots (started_at) VALUES (?)", (_iso(now),))
        cur = await self.db.execute(
            "SELECT COUNT(*) AS n FROM boots WHERE started_at>=?",
            (_iso(now - dt.timedelta(minutes=10)),),
        )
        row = await cur.fetchone()
        await self.db.execute(
            "DELETE FROM boots WHERE started_at<?", (_iso(now - dt.timedelta(days=1)),)
        )
        return int(row["n"]) if row else 1


async def safe_write(coro: Awaitable[Any], what: str) -> bool:
    """Run a storage coroutine, logging (not raising) transient SQLite errors.

    Used by the listener's cursor flush and the outbox writer: a locked database must never
    take down a message handler.
    """
    try:
        await coro
    except sqlite3.OperationalError:
        log.warning("sqlite busy during %s; will retry on the next tick", what)
        return False
    return True

**Identifying missing shared search lines**
exec
/bin/zsh -lc "sed -n '370,540p' ddiver/storage.py && sed -n '1,320p' ddiver/bot/router.py" in /Users/L/Code/ddiver
 succeeded in 0ms:

    async def resume_delivery(self, user_id: int) -> bool:
        """Clear a delivery pause; ``True`` when there was one (any update from the user does)."""
        cur = await self.db.execute(
            """UPDATE users SET delivery_paused_at=NULL, pause_reason=NULL, updated_at=?
               WHERE id=? AND delivery_paused_at IS NOT NULL""",
            (self._now(), user_id),
        )
        return cur.rowcount == 1

    async def list_users(self, *, roles: set[Role] | None = None) -> list[User]:
        if roles:
            marks = ",".join("?" * len(roles))
            cur = await self.db.execute(
                f"SELECT * FROM users WHERE role IN ({marks}) ORDER BY id",  # noqa: S608
                tuple(r.value for r in roles),
            )
        else:
            cur = await self.db.execute("SELECT * FROM users ORDER BY id")
        return [self._user(r) for r in await cur.fetchall()]

    async def active_user_ids(self) -> set[int]:
        cur = await self.db.execute("SELECT id, role FROM users")
        return {
            int(r["id"])
            for r in await cur.fetchall()
            if self.is_owner(r["id"]) or r["role"] == Role.ALLOWED.value
        }

    # -- chats -----------------------------------------------------------------------------

    @staticmethod
    def _chat(row: aiosqlite.Row) -> ChatRow:
        ref = ChatRef(
            id=row["id"],
            title=row["title"],
            username=row["username"],
            kind=row["kind"],
            is_forum=bool(row["is_forum"]),
        )
        return ChatRow(
            ref=ref,
            available=bool(row["available"]),
            shared=bool(row["shared"]),
            access_hash=row["access_hash"],
        )

    async def upsert_chat(self, ref: ChatRef, *, access_hash: int | None = None) -> None:
        """Insert or refresh chat metadata. ``available`` and ``shared`` are never reset here."""
        await self.db.execute(
            """INSERT INTO chats (id, title, username, kind, is_forum, access_hash, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title, username=excluded.username,
                 kind=excluded.kind, is_forum=excluded.is_forum,
                 access_hash=COALESCE(excluded.access_hash, chats.access_hash),
                 updated_at=excluded.updated_at""",
            (
                ref.id,
                ref.title,
                ref.username,
                ref.kind,
                int(ref.is_forum),
                access_hash,
                self._now(),
            ),
        )

    async def get_chat(self, chat_id: int) -> ChatRow | None:
        cur = await self.db.execute("SELECT * FROM chats WHERE id=?", (chat_id,))
        row = await cur.fetchone()
        return self._chat(row) if row else None

    async def list_chats(self, *, shared_only: bool = False) -> list[ChatRow]:
        sql = "SELECT * FROM chats"
        if shared_only:
            sql += " WHERE shared=1"
        sql += " ORDER BY title COLLATE NOCASE"
        cur = await self.db.execute(sql)
        return [self._chat(r) for r in await cur.fetchall()]

    async def set_chat_available(self, chat_id: int, available: bool) -> bool:
        """``True`` when the flag actually changed, so callers notify only on transitions."""
        cur = await self.db.execute(
            "UPDATE chats SET available=?, updated_at=? WHERE id=? AND available<>?",
            (int(available), self._now(), chat_id, int(available)),
        )
        return cur.rowcount == 1

    async def set_chat_shared(self, chat_id: int, shared: bool) -> bool:
        cur = await self.db.execute(
            "UPDATE chats SET shared=?, shared_at=?, updated_at=? WHERE id=? AND shared<>?",
            (int(shared), self._now() if shared else None, self._now(), chat_id, int(shared)),
        )
        return cur.rowcount == 1

    async def migrate_chat(self, old_id: int, new_ref: ChatRef, access_hash: int | None) -> None:
        """Move subscriptions, rules and ignores from a basic group to its supergroup."""
        await self.upsert_chat(new_ref, access_hash=access_hash)
        now = self._now()
        await self.db.execute(
            """INSERT OR IGNORE INTO subscriptions (user_id, chat_id, created_at)
               SELECT user_id, ?, ? FROM subscriptions WHERE chat_id=?""",
            (new_ref.id, now, old_id),
        )
        await self.db.execute("DELETE FROM subscriptions WHERE chat_id=?", (old_id,))
        await self.db.execute("UPDATE rules SET chat_id=? WHERE chat_id=?", (new_ref.id, old_id))
        await self.db.execute(
            "UPDATE ignored_senders SET chat_id=? WHERE chat_id=?", (new_ref.id, old_id)
        )
        await self.db.execute("DELETE FROM chat_cursor WHERE chat_id=?", (old_id,))
        await self.db.execute(
            "UPDATE chats SET available=0, updated_at=? WHERE id=?", (now, old_id)
        )

    # -- subscriptions ---------------------------------------------------------------------

    async def subscribe(self, user_id: int, chat_id: int) -> bool:
        """Subscribe, but only to a known chat of a group-like kind (callback data is untrusted)."""
        chat = await self.get_chat(chat_id)
        if chat is None or chat.ref.kind not in CHAT_KINDS:
            return False
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO subscriptions (user_id, chat_id, created_at) VALUES (?,?,?)",
            (user_id, chat_id, self._now()),
        )
        return cur.rowcount == 1

    async def unsubscribe(self, user_id: int, chat_id: int) -> bool:
        cur = await self.db.execute(
            "DELETE FROM subscriptions WHERE user_id=? AND chat_id=?", (user_id, chat_id)
        )
        return cur.rowcount == 1

    async def unsubscribe_all(self, chat_id: int, *, except_user: int | None = None) -> list[int]:
        """Drop every subscription for a chat (used when the owner unshares it)."""
        sql = "SELECT user_id FROM subscriptions WHERE chat_id=?"
        params: list[Any] = [chat_id]
        if except_user is not None:
            sql += " AND user_id<>?"
            params.append(except_user)
        cur = await self.db.execute(sql, params)
        affected = [int(r["user_id"]) for r in await cur.fetchall()]
        if affected:
            marks = ",".join("?" * len(affected))
            await self.db.execute(
                f"DELETE FROM subscriptions WHERE chat_id=? AND user_id IN ({marks})",  # noqa: S608
                (chat_id, *affected),
            )
        return affected

    async def is_subscribed(self, user_id: int, chat_id: int) -> bool:
        cur = await self.db.execute(
            "SELECT 1 FROM subscriptions WHERE user_id=? AND chat_id=?", (user_id, chat_id)
        )
        return await cur.fetchone() is not None

    async def list_subscriptions(self, user_id: int) -> list[ChatRow]:
        cur = await self.db.execute(
            """SELECT c.* FROM chats c JOIN subscriptions s ON s.chat_id=c.id
               WHERE s.user_id=? ORDER BY c.title COLLATE NOCASE""",
            (user_id,),
        )
        return [self._chat(r) for r in await cur.fetchall()]

    async def subscribers_of(self, chat_id: int, *, active_only: bool = True) -> list[int]:
        cur = await self.db.execute(
            "SELECT user_id FROM subscriptions WHERE chat_id=? ORDER BY user_id", (chat_id,)
        )
        ids = [int(r["user_id"]) for r in await cur.fetchall()]
        if not active_only:
            return ids
"""Bot-side dispatch, authorisation and pending-input state.

Handlers never touch Telethon: the router turns an update into an :class:`Incoming` and each
handler returns :class:`Action` objects that an adapter performs. That keeps the whole UI
testable offline and keeps the authorisation invariants in exactly one place:

* the acting user id comes from ``event.sender_id`` (server-set), never from the payload;
* owner-only actions additionally check ``storage.is_owner``;
* unauthorised users get at most one reply and the owner at most one card per day.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ddiver.i18n import DEFAULT_LANG, LANGS, t
from ddiver.storage import Role, Storage, User
from ddiver.timeutil import Clock, utcnow

log = logging.getLogger(__name__)

PENDING_TTL = dt.timedelta(minutes=15)
COMMANDS = ("start", "help", "test", "cancel", "status", "menu")
GATE_REMINDER = dt.timedelta(days=1)


@dataclass(frozen=True, slots=True)
class Incoming:
    """One update, reduced to what the UI needs."""

    user_id: int
    text: str | None = None
    data: str | None = None  # callback payload
    msg_id: int | None = None
    lang_code: str | None = None  # Telegram's client language, used to seed the UI language
    is_private: bool = True

    @property
    def command(self) -> str | None:
        if not self.text:
            return None
        head = self.text.split(maxsplit=1)[0]
        if not head.startswith("/"):
            return None
        name = head[1:].split("@", 1)[0].lower()
        return name if name in COMMANDS else None

    @property
    def args(self) -> str:
        if not self.text:
            return ""
        parts = self.text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""


# -- actions a handler can request ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reply:
    key_or_text: str
    params: dict[str, Any] = field(default_factory=dict)
    buttons: Any = None
    literal: bool = False


@dataclass(frozen=True, slots=True)
class Edit:
    key_or_text: str
    params: dict[str, Any] = field(default_factory=dict)
    buttons: Any = None
    literal: bool = False


@dataclass(frozen=True, slots=True)
class Toast:
    key_or_text: str
    params: dict[str, Any] = field(default_factory=dict)
    alert: bool = False
    literal: bool = False


@dataclass(frozen=True, slots=True)
class Send:
    """A message to somebody other than the acting user (e.g. the owner's approval card)."""

    user_id: int
    key_or_text: str
    params: dict[str, Any] = field(default_factory=dict)
    buttons: Any = None
    literal: bool = False


Action = Reply | Edit | Toast | Send
Handler = Callable[["Context"], Awaitable[list[Action]]]


@dataclass(slots=True)
class PendingInput:
    kind: str
    payload: dict[str, Any]
    expires_at: dt.datetime


@dataclass(slots=True)
class Context:
    incoming: Incoming
    user: User
    storage: Storage
    router: Router

    @property
    def user_id(self) -> int:
        return self.user.id

    @property
    def lang(self) -> str:
        return self.user.lang or DEFAULT_LANG

    @property
    def is_owner(self) -> bool:
        return self.user.is_owner

    def t(self, key: str, **params: Any) -> str:
        return t(self.lang, key, **params)


class Router:
    def __init__(self, storage: Storage, *, clock: Clock = utcnow) -> None:
        self._storage = storage
        self._clock = clock
        self._commands: dict[str, Handler] = {}
        self._callbacks: dict[str, tuple[Handler, bool]] = {}
        self._pending_handlers: dict[str, Handler] = {}
        self._pending: dict[int, PendingInput] = {}
        self._fallback: Handler | None = None

    # -- registration ----------------------------------------------------------------------

    def command(self, name: str) -> Callable[[Handler], Handler]:
        def register(handler: Handler) -> Handler:
            self._commands[name] = handler
            return handler

        return register

    def callback(self, prefix: str, *, owner_only: bool = False) -> Callable[[Handler], Handler]:
        def register(handler: Handler) -> Handler:
            self._callbacks[prefix] = (handler, owner_only)
            return handler

        return register

    def pending(self, kind: str) -> Callable[[Handler], Handler]:
        def register(handler: Handler) -> Handler:
            self._pending_handlers[kind] = handler
            return handler

        return register

    def fallback(self, handler: Handler) -> Handler:
        self._fallback = handler
        return handler

    # -- pending input ---------------------------------------------------------------------

    def set_pending(self, user_id: int, kind: str, payload: dict[str, Any] | None = None) -> None:
        self._pending[user_id] = PendingInput(
            kind, dict(payload or {}), self._clock() + PENDING_TTL
        )

    def get_pending(self, user_id: int) -> PendingInput | None:
        state = self._pending.get(user_id)
        if state is None:
            return None
        if self._clock() >= state.expires_at:
            del self._pending[user_id]
            return None
        return state

    def clear_pending(self, user_id: int) -> None:
        self._pending.pop(user_id, None)

    # -- dispatch --------------------------------------------------------------------------

    async def dispatch(self, incoming: Incoming) -> list[Action]:
        if not incoming.is_private:
            return []  # the bot only works in DMs; group commands are ignored
        user = await self._storage.ensure_user(
            incoming.user_id, lang=_seed_lang(incoming.lang_code)
        )
        if not user.is_owner and user.role is not Role.ALLOWED:
            return await self._gate(incoming, user)
        if user.paused and await self._storage.resume_delivery(user.id):
            log.info("delivery resumed for %s after an incoming update", user.id)
            user = await self._storage.ensure_user(user.id)

        ctx = Context(incoming, user, self._storage, self)
        if incoming.data is not None:
            return await self._dispatch_callback(ctx)
        return await self._dispatch_message(ctx)

    async def _dispatch_callback(self, ctx: Context) -> list[Action]:
        data = ctx.incoming.data or ""
        prefix = data.split(":", 1)[0]
        entry = self._callbacks.get(prefix)
        if entry is None:
            return [Toast("bot.err.unknown_action")]
        handler, owner_only = entry
        if owner_only and not ctx.is_owner:
            log.warning("user %s tried an owner-only action %r", ctx.user_id, prefix)
            return [Toast("bot.err.owner_only", alert=True)]
        return await handler(ctx)

    async def _dispatch_message(self, ctx: Context) -> list[Action]:
        command = ctx.incoming.command
        if command is not None:
            self.clear_pending(ctx.user_id)
            handler = self._commands.get(command)
            if handler is not None:
                return await handler(ctx)
            return [Reply("bot.err.unknown_command")]

        state = self.get_pending(ctx.user_id)
        if state is not None:
            handler = self._pending_handlers.get(state.kind)
            if handler is not None:
                return await handler(ctx)
            self.clear_pending(ctx.user_id)
        if self._fallback is not None:
            return await self._fallback(ctx)
        return [Reply("bot.err.not_waiting")]

    def pending_payload(self, user_id: int) -> dict[str, Any]:
        state = self.get_pending(user_id)
        return dict(state.payload) if state else {}

    # -- the allow-list gate ---------------------------------------------------------------

    async def _gate(self, incoming: Incoming, user: User) -> list[Action]:
        """Strangers get one reply and the owner one card per day; denied users get silence."""
        if user.role is Role.DENIED:
            return []
        lang = user.lang or DEFAULT_LANG
        now = self._clock()
        if user.notified_owner_at is not None and now - user.notified_owner_at < GATE_REMINDER:
            return [] if incoming.data is None else [Toast("bot.gate.pending")]
        await self._storage.update_user(user.id, notified_owner_at=now)
        actions: list[Action] = [Reply("bot.gate.pending")]
        owner = await self._owner_id()
        if owner is not None:
            actions.append(
                Send(
                    owner,
                    "bot.gate.card",
                    {"user_id": user.id, "lang": lang},
                    buttons=[
                        [
                            ("bot.btn.allow", f"allow:{user.id}"),
                            ("bot.btn.deny", f"deny:{user.id}"),
                        ]
                    ],
                )
            )
        return actions

    async def _owner_id(self) -> int | None:
        for candidate in await self._storage.list_users():
            if candidate.is_owner:
                return candidate.id
        return None


def _seed_lang(lang_code: str | None) -> str | None:
    """Map Telegram's client language to one of ours so even the gate message is localized."""
    if not lang_code:
        return None
    head = lang_code.split("-", 1)[0].lower()
    return head if head in LANGS else None

**Assessing handler enforcement for subscribe**
exec
/bin/zsh -lc "sed -n '1,260p' ddiver/bot/handlers.py && sed -n '261,540p' ddiver/bot/handlers.py && sed -n '541,820p' ddiver/bot/handlers.py" in /Users/L/Code/ddiver
 succeeded in 0ms:
"""All bot UI flows, registered on a :class:`~ddiver.bot.router.Router`.

Every handler returns actions; nothing here talks to Telethon. Authorisation is already done
by the router, but each storage call still carries the acting ``user_id`` so a forged callback
payload can only ever touch the sender's own rows.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ddiver.bot import menus
from ddiver.bot.router import Action, Context, Edit, Reply, Router, Send, Toast
from ddiver.i18n import LANG_NAMES, LANGS, t
from ddiver.matcher import Matcher
from ddiver.rules import (
    MatchMode,
    RuleError,
    RuleKind,
    compile_rule,
    parse_pattern,
    parse_rule_lines,
    prepare_text,
)
from ddiver.storage import (
    ALL_CHATS,
    ChatRow,
    DuplicateRuleError,
    Role,
    Storage,
    User,
)
from ddiver.timeutil import format_clock, resolve_tz, utcnow

log = logging.getLogger(__name__)

MAX_LINES_PER_MESSAGE = 50
MAX_RULES_PER_CHAT = 200
MAX_RULES_PER_USER = 500
DIGEST_MIN = 2
DIGEST_MAX = 500
EXCLUDE_PREVIEW = 30

KIND_ICONS = {
    MatchMode.SUBSTRING: "bot.rules.kind.substring",
    MatchMode.WORD: "bot.rules.kind.word",
    MatchMode.REGEX: "bot.rules.kind.regex",
}


@dataclass(slots=True)
class StatusInfo:
    """What ``/status`` shows; filled in by the app so handlers stay pure."""

    listener_ok: bool = True
    uptime: str = "—"
    monitored: int = 0
    unavailable: int = 0
    seen: int = 0
    matched: int = 0
    delivered: int = 0
    digested: int = 0
    failed: int = 0
    queue: int = 0
    last_update: str = "—"


StatusProvider = Callable[[], Awaitable[StatusInfo]]
Invalidator = Callable[[int | None], Awaitable[None]]


def build_router(
    storage: Storage,
    *,
    matcher: Matcher | None = None,
    status: StatusProvider | None = None,
    regex_timeout: float = 0.2,
    clock: Any = utcnow,
) -> Router:
    router = Router(storage, clock=clock)

    async def invalidate(chat_id: int | None = None) -> None:
        if matcher is not None:
            await matcher.invalidate(chat_id)

    # -- menus -----------------------------------------------------------------------------

    def menu_actions(ctx: Context, *, edit: bool = False) -> list[Action]:
        buttons = menus.main_menu(is_owner=ctx.is_owner)
        action = Edit if edit else Reply
        return [action("bot.menu.title", buttons=buttons)]

    @router.command("start")
    async def start(ctx: Context) -> list[Action]:
        if ctx.user.lang is None:
            return [Reply("bot.start.language", buttons=menus.language_menu())]
        if ctx.user.tz is None:
            return [Reply("bot.start.timezone", buttons=menus.timezone_menu())]
        return menu_actions(ctx)

    @router.command("menu")
    async def menu(ctx: Context) -> list[Action]:
        return menu_actions(ctx)

    @router.callback("menu")
    async def menu_cb(ctx: Context) -> list[Action]:
        return menu_actions(ctx, edit=True)

    @router.command("help")
    async def help_cmd(_ctx: Context) -> list[Action]:
        return [Reply("bot.help")]

    @router.callback("help")
    async def help_cb(_ctx: Context) -> list[Action]:
        return [Edit("bot.help", buttons=[[("bot.btn.back", "menu")]])]

    @router.command("cancel")
    async def cancel(ctx: Context) -> list[Action]:
        router.clear_pending(ctx.user_id)
        return [Reply("bot.cancelled")]

    # -- onboarding ------------------------------------------------------------------------

    @router.callback("lang")
    async def set_language(ctx: Context) -> list[Action]:
        lang = (ctx.incoming.data or "").split(":", 1)[1]
        if lang not in LANGS:
            return [Toast("bot.err.unknown_action")]
        user = await storage.update_user(ctx.user_id, lang=lang)
        if user.tz is None:
            return [Edit("bot.start.timezone", buttons=menus.timezone_menu())]
        return [Toast("bot.settings.saved"), *menu_actions(ctx, edit=True)]

    @router.callback("tz")
    async def set_timezone(ctx: Context) -> list[Action]:
        name = (ctx.incoming.data or "").split(":", 1)[1]
        resolution = resolve_tz(name)
        if resolution.exact is None:
            return [Toast("bot.start.tz_unknown")]
        return await _apply_timezone(ctx, resolution.exact, fixed=resolution.fixed_offset)

    @router.pending("timezone")
    async def timezone_input(ctx: Context) -> list[Action]:
        resolution = resolve_tz(ctx.incoming.text or "")
        if resolution.exact:
            router.clear_pending(ctx.user_id)
            return await _apply_timezone(ctx, resolution.exact, fixed=resolution.fixed_offset)
        if resolution.candidates:
            buttons = [[(menus.literal(tz), f"tz:{tz}")] for tz in resolution.candidates]
            return [Reply("bot.start.tz_candidates", buttons=buttons)]
        return [Reply("bot.start.tz_unknown")]

    async def _apply_timezone(ctx: Context, tz: str, *, fixed: bool) -> list[Action]:
        user = await storage.update_user(ctx.user_id, tz=tz)
        lang = user.lang or ctx.lang
        actions: list[Action] = [
            Reply(
                t(lang, "bot.start.tz_confirm", tz=tz, clock=format_clock(clock(), tz)),
                literal=True,
            )
        ]
        if fixed:
            actions.append(Reply("bot.start.tz_fixed_offset"))
        actions.extend(menu_actions(ctx))
        return actions

    # -- chats -----------------------------------------------------------------------------

    async def visible_chats(user: User) -> list[ChatRow]:
        """The owner sees every known chat; moderators only the shared ones."""
        return await storage.list_chats(shared_only=not user.is_owner)

    @router.callback("chats")
    async def chats(ctx: Context) -> list[Action]:
        arg = (ctx.incoming.data or "chats:0").split(":", 1)[1]
        if arg == "search":
            router.set_pending(ctx.user_id, "chat_search")
            return [Reply("bot.chats.search_prompt")]
        page_index = _int(arg)
        rows = await visible_chats(ctx.user)
        if not rows:
            key = "bot.chats.empty" if ctx.is_owner else "bot.chats.none_shared"
            return [Edit(key, buttons=[[("bot.btn.back", "menu")]])]
        subscribed = {c.id for c in await storage.list_subscriptions(ctx.user_id)}
        return [
            Edit(
                "bot.chats.title",
                {"n": len(subscribed)},
                buttons=menus.chats_menu(rows, subscribed, page_index),
            )
        ]

    @router.pending("chat_search")
    async def chat_search(ctx: Context) -> list[Action]:
        needle = (ctx.incoming.text or "").strip().lstrip("@").casefold()
        rows = [
            c
            for c in await visible_chats(ctx.user)
            if needle in c.ref.title.casefold() or needle in (c.ref.username or "").casefold()
        ]
        if not rows:
            return [Reply("bot.chats.search_empty")]  # stay in search mode so they can retype
        router.clear_pending(ctx.user_id)
        subscribed = {c.id for c in await storage.list_subscriptions(ctx.user_id)}
        return [
            Reply(
                "bot.chats.title",
                {"n": len(subscribed)},
                buttons=menus.chats_menu(rows, subscribed, 0),
            )
        ]

    @router.callback("sub")
    async def subscribe(ctx: Context) -> list[Action]:
        _, raw_chat, raw_state = (ctx.incoming.data or "").split(":")
        chat_id, wanted = _int(raw_chat), raw_state == "1"
        chat = await storage.get_chat(chat_id)
        if chat is None or (not ctx.is_owner and not chat.shared):
            return [Toast("bot.err.not_found")]
        if wanted:
            await storage.subscribe(ctx.user_id, chat_id)
        else:
            await storage.unsubscribe(ctx.user_id, chat_id)
        await invalidate(chat_id)
        rows = await visible_chats(ctx.user)
        subscribed = {c.id for c in await storage.list_subscriptions(ctx.user_id)}
        key = "bot.chats.subscribed" if wanted else "bot.chats.unsubscribed"
        return [
            Toast(t(ctx.lang, key, chat=chat.ref.title), literal=True),
            Edit(
                "bot.chats.title",
                {"n": len(subscribed)},
                buttons=menus.chats_menu(rows, subscribed, 0),
            ),
        ]

    # -- shared chats (owner only) ---------------------------------------------------------

    @router.callback("shared", owner_only=True)
    async def shared(ctx: Context) -> list[Action]:
        page_index = _int((ctx.incoming.data or "shared:0").split(":", 1)[1])
        rows = await storage.list_chats()
        return [
            Edit("bot.shared.hint", buttons=menus.shared_menu(rows, page_index)),
        ]

    @router.callback("share", owner_only=True)
    async def share(ctx: Context) -> list[Action]:
        _, raw_chat, raw_state = (ctx.incoming.data or "").split(":")
        chat_id, wanted = _int(raw_chat), raw_state == "1"
        chat = await storage.get_chat(chat_id)
        if chat is None:
            return [Toast("bot.err.not_found")]
        await storage.set_chat_shared(chat_id, wanted)
        actions: list[Action] = []
        if wanted:
            actions.append(Toast(t(ctx.lang, "bot.shared.on", chat=chat.ref.title), literal=True))
        else:
            dropped = await storage.unsubscribe_all(chat_id, except_user=ctx.user_id)
            await invalidate(chat_id)
            actions.append(
                Toast(
                    t(ctx.lang, "bot.shared.off", chat=chat.ref.title, n=len(dropped)), literal=True
                )
            )
            for user_id in dropped:
                other = await storage.get_user(user_id)
                lang = (other.lang if other else None) or ctx.lang
                actions.append(
                    Send(
                        user_id,
                        t(lang, "bot.shared.unshared_notice", chat=chat.ref.title),
                        literal=True,
                    )
                )
        rows = await storage.list_chats()
        actions.append(Edit("bot.shared.hint", buttons=menus.shared_menu(rows, 0)))
        return actions

    # -- users (owner only) ----------------------------------------------------------------

    @router.callback("users", owner_only=True)
    async def users(ctx: Context) -> list[Action]:
        return await _users_view(ctx)

    async def _users_view(ctx: Context, *, edit: bool = True) -> list[Action]:
        everyone = await storage.list_users()
        pending = [u.id for u in everyone if u.role is Role.PENDING and not u.is_owner]
        allowed = [u.id for u in everyone if u.role is Role.ALLOWED and not u.is_owner]
        denied = [u.id for u in everyone if u.role is Role.DENIED]
        lines = [ctx.t("bot.users.title")]
        lines.extend(ctx.t("bot.users.pending", user_id=uid) for uid in pending)
        for user_id in allowed:
            chats = len(await storage.list_subscriptions(user_id))
            rules = await storage.count_rules(user_id)
            lines.append(ctx.t("bot.users.allowed", user_id=user_id, chats=chats, rules=rules))
        lines.extend(ctx.t("bot.users.denied", user_id=uid) for uid in denied)
        action = Edit if edit else Reply
        return [action("\n".join(lines), literal=True, buttons=menus.users_menu(pending, allowed))]

    @router.callback("allow", owner_only=True)
    async def allow(ctx: Context) -> list[Action]:
        target = _int((ctx.incoming.data or "").split(":", 1)[1])
        if await storage.get_user(target) is None:
            return [Toast("bot.err.not_found")]
        user = await storage.update_user(target, role=Role.ALLOWED, notified_owner_at=None)
        await invalidate(None)
        return [
            Toast(t(ctx.lang, "bot.users.allowed_toast", user_id=target), literal=True),
            Send(target, t(user.lang or ctx.lang, "bot.gate.allowed"), literal=True),
            *await _users_view(ctx),
        ]

    @router.callback("deny", owner_only=True)
    async def deny(ctx: Context) -> list[Action]:
        target = _int((ctx.incoming.data or "").split(":", 1)[1])
        if await storage.get_user(target) is None:
            return [Toast("bot.err.not_found")]
        user = await storage.update_user(target, role=Role.DENIED)
        await invalidate(None)
        return [
            Toast(t(ctx.lang, "bot.users.denied_toast", user_id=target), literal=True),
            Send(target, t(user.lang or ctx.lang, "bot.gate.denied"), literal=True),
            *await _users_view(ctx),
        ]

    @router.callback("revoke", owner_only=True)
    async def revoke(ctx: Context) -> list[Action]:
        target = _int((ctx.incoming.data or "").split(":", 1)[1])
        if target == ctx.user_id:
            return [Toast("bot.err.owner_only", alert=True)]
        if await storage.get_user(target) is None:
            return [Toast("bot.err.not_found")]
        await storage.update_user(target, role=Role.DENIED)
        await invalidate(None)
        return [
            Toast(t(ctx.lang, "bot.users.revoked_toast", user_id=target), literal=True),
            *await _users_view(ctx),
        ]

    # -- rules -----------------------------------------------------------------------------

    def icon_for(pattern: str, lang: str) -> str:
        mode, _ = parse_pattern(pattern)
        return t(lang, KIND_ICONS[mode])

    async def rules_view(ctx: Context, chat_id: int, kind: RuleKind, page: int) -> list[Action]:
        rules = await storage.list_rules(ctx.user_id, chat_id, kind)
        icons = [icon_for(r.pattern, ctx.lang) for r in rules]
        if chat_id == ALL_CHATS:
            title = ctx.t("bot.rules.title_global")
        else:
            chat = await storage.get_chat(chat_id)
            title = ctx.t("bot.rules.title", chat=chat.ref.title if chat else chat_id)
        body = title if rules else f"{title}\n{ctx.t('bot.rules.empty')}"
        return [
            Edit(
                body,
                literal=True,
                buttons=menus.rules_menu(rules, icons, chat_id, kind.value, page),
            )
        ]

    @router.callback("rules")
    async def rules(ctx: Context) -> list[Action]:
        parts = (ctx.incoming.data or "rules:pick").split(":")
        if parts[1] == "pick":
            chats = await storage.list_subscriptions(ctx.user_id)
            return [Edit("bot.rules.pick_chat", buttons=menus.rules_chat_menu(chats, 0))]
        chat_id = _int(parts[1])
        kind = RuleKind(parts[2]) if len(parts) > 2 else RuleKind.INCLUDE
        page = _int(parts[3]) if len(parts) > 3 else 0
        return await rules_view(ctx, chat_id, kind, page)

    @router.callback("rpick")
    async def rules_pick_page(ctx: Context) -> list[Action]:
        page = _int((ctx.incoming.data or "rpick:0").split(":", 1)[1])
        chats = await storage.list_subscriptions(ctx.user_id)
        return [Edit("bot.rules.pick_chat", buttons=menus.rules_chat_menu(chats, page))]

    @router.callback("radd")
    async def rules_add_prompt(ctx: Context) -> list[Action]:
        _, raw_chat, raw_kind = (ctx.incoming.data or "").split(":")
        router.set_pending(ctx.user_id, "rules_add", {"chat_id": _int(raw_chat), "kind": raw_kind})
        return [Reply("bot.rules.prompt")]

    @router.pending("rules_add")
    async def rules_add(ctx: Context) -> list[Action]:
        payload = router.pending_payload(ctx.user_id)
        chat_id = int(payload.get("chat_id", ALL_CHATS))
        kind = RuleKind(payload.get("kind", RuleKind.INCLUDE.value))
        router.clear_pending(ctx.user_id)
        lines = parse_rule_lines(ctx.incoming.text or "")
        if len(lines) > MAX_LINES_PER_MESSAGE:
            return [Reply("bot.rules.too_many_lines", {"limit": MAX_LINES_PER_MESSAGE})]
        report = await _add_rules(ctx, chat_id, kind, lines)
        return [Reply(report, literal=True), *await rules_view(ctx, chat_id, kind, 0)]

    async def _add_rules(ctx: Context, chat_id: int, kind: RuleKind, lines: Sequence[str]) -> str:
        added = duplicates = 0
        problems: list[str] = []
        total_user = await storage.count_rules(ctx.user_id)
        total_chat = await storage.count_rules(ctx.user_id, chat_id)
        for number, line in enumerate(lines, start=1):
            if total_chat >= MAX_RULES_PER_CHAT:
                problems.append(ctx.t("bot.rules.limit_chat", limit=MAX_RULES_PER_CHAT))
                break
            if total_user >= MAX_RULES_PER_USER:
                problems.append(ctx.t("bot.rules.limit_user", limit=MAX_RULES_PER_USER))
                break
            try:
                compiled = compile_rule(line, timeout=regex_timeout)
            except RuleError as exc:
                problems.append(
                    ctx.t(
                        "bot.rules.line_invalid",
                        line=number,
                        reason=t(ctx.lang, exc.key, **exc.params),
                    )
                )
                continue
            try:
                await storage.add_rule(ctx.user_id, chat_id, kind, compiled.pattern)
            except DuplicateRuleError:
                duplicates += 1
                problems.append(ctx.t("bot.rules.line_duplicate", line=number))
                continue
            added += 1
            total_user += 1
            total_chat += 1
        if added:
            await invalidate(None if chat_id == ALL_CHATS else chat_id)
        head = ctx.t(
            "bot.rules.report",
            added=added,
            duplicates=duplicates,
            invalid=len(problems) - duplicates,
        )
        return "\n".join([head, *problems])

    @router.callback("rdel")
    async def rules_delete(ctx: Context) -> list[Action]:
        rule_id = _int((ctx.incoming.data or "").split(":", 1)[1])
        rule = await storage.get_rule(rule_id, ctx.user_id)
        if rule is None:
            return [Toast("bot.err.not_found")]
        await storage.delete_rule(rule_id, ctx.user_id)
        await invalidate(None if rule.is_global else rule.chat_id)
        return [
            Toast("bot.rules.deleted"),
            *await rules_view(ctx, rule.chat_id, rule.kind, 0),
        ]

    # -- ignored senders -------------------------------------------------------------------

    @router.callback("ign")
    async def ignored(ctx: Context) -> list[Action]:
        page = _int((ctx.incoming.data or "ign:0").split(":", 1)[1])
        entries = await storage.list_ignored(ctx.user_id)
        body = ctx.t("bot.ignored.title") if entries else ctx.t("bot.ignored.empty")
        return [Edit(body, literal=True, buttons=menus.ignored_menu(entries, page))]

    @router.callback("igdel")
    async def ignored_delete(ctx: Context) -> list[Action]:
        _, raw_chat, raw_sender = (ctx.incoming.data or "").split(":")
        await storage.remove_ignored(ctx.user_id, _int(raw_chat), _int(raw_sender))
        await invalidate(None)
        entries = await storage.list_ignored(ctx.user_id)
        body = ctx.t("bot.ignored.title") if entries else ctx.t("bot.ignored.empty")
        return [
            Toast("bot.ignored.removed"),
            Edit(body, literal=True, buttons=menus.ignored_menu(entries, 0)),
        ]

    @router.callback("ig")
    async def ignore_sender(ctx: Context) -> list[Action]:
        _, raw_chat, raw_sender = (ctx.incoming.data or "").split(":")
        chat_id, sender_id = _int(raw_chat), _int(raw_sender)
        chat = await storage.get_chat(chat_id)
        anonymous = sender_id == chat_id
        note = ctx.t("bot.ignored.anonymous") if anonymous else str(sender_id)
        await storage.add_ignored(ctx.user_id, chat_id, sender_id, note)
        await invalidate(chat_id)
        return [
            Toast(
                t(
                    ctx.lang,
                    "bot.ignored.added",
                    sender=note,
                    chat=chat.ref.title if chat else chat_id,
                ),
                literal=True,
            ),
            Reply(
                "bot.ignored.title",
                buttons=[[("bot.ignored.all_chats_btn", f"igall:{sender_id}")]],
            ),
        ]

    @router.callback("igall")
    async def ignore_sender_globally(ctx: Context) -> list[Action]:
        sender_id = _int((ctx.incoming.data or "").split(":", 1)[1])
        await storage.add_ignored(ctx.user_id, ALL_CHATS, sender_id, str(sender_id))
        await invalidate(None)
        return [
            Toast(t(ctx.lang, "bot.ignored.added_global", sender=sender_id), literal=True),
        ]

    # -- notification exclusion button ------------------------------------------------------

    @router.callback("ex")
    async def exclude_prompt(ctx: Context) -> list[Action]:
        _, raw_chat, raw_msg = (ctx.incoming.data or "").split(":")
        chat_id, msg_id = _int(raw_chat), _int(raw_msg)
        item = await storage.get_delivery(ctx.user_id, chat_id, msg_id)
        if item is None:
            return [Toast("bot.err.expired", alert=True)]
        text = str(item.payload.get("text", ""))
        spans = [tuple(s) for s in item.payload.get("spans", [])]
        include_pattern = str(item.payload.get("rule_pattern", ""))
        candidate = propose_exclusion(text, spans)
        buttons: list[list[tuple[str, str]]] = []
        actions: list[Action] = []
        if candidate and not _conflicts(candidate, include_pattern):
            router.set_pending(ctx.user_id, "exclude_custom", {"chat_id": chat_id})
            label = t(ctx.lang, "bot.notif.exclude_btn", pattern=_shorten(candidate))
            buttons.append([(menus.literal(label), f"exadd:{chat_id}:{msg_id}")])
        elif candidate:
            actions.append(
                Toast(
                    t(ctx.lang, "bot.notif.exclude_conflict", pattern=include_pattern),
                    alert=True,
                    literal=True,
                )
            )
        buttons.append([("bot.notif.exclude_custom", f"excustom:{chat_id}")])
        router.set_pending(ctx.user_id, "exclude_custom", {"chat_id": chat_id})
        actions.append(Reply("bot.notif.exclude_prompt", buttons=buttons))
        return actions

    @router.callback("exadd")
    async def exclude_add(ctx: Context) -> list[Action]:
        _, raw_chat, raw_msg = (ctx.incoming.data or "").split(":")
        chat_id, msg_id = _int(raw_chat), _int(raw_msg)
        item = await storage.get_delivery(ctx.user_id, chat_id, msg_id)
        if item is None:
            return [Toast("bot.err.expired", alert=True)]
        candidate = propose_exclusion(
            str(item.payload.get("text", "")),
            [tuple(s) for s in item.payload.get("spans", [])],
        )
        if not candidate:
            return [Toast("bot.err.expired")]
        router.clear_pending(ctx.user_id)
        return await _store_exclusion(ctx, chat_id, candidate)

    @router.callback("excustom")
    async def exclude_custom(ctx: Context) -> list[Action]:
        chat_id = _int((ctx.incoming.data or "").split(":", 1)[1])
        router.set_pending(ctx.user_id, "exclude_custom", {"chat_id": chat_id})
        return [Reply("bot.rules.prompt")]

    @router.pending("exclude_custom")
    async def exclude_input(ctx: Context) -> list[Action]:
        payload = router.pending_payload(ctx.user_id)
        router.clear_pending(ctx.user_id)
        chat_id = int(payload.get("chat_id", ALL_CHATS))
        lines = parse_rule_lines(ctx.incoming.text or "")
        if not lines:
            return [Reply("bot.err.not_waiting")]
        return await _store_exclusion(ctx, chat_id, lines[0])

    async def _store_exclusion(ctx: Context, chat_id: int, pattern: str) -> list[Action]:
        try:
            compiled = compile_rule(pattern, timeout=regex_timeout)
        except RuleError as exc:
            return [Reply(t(ctx.lang, exc.key, **exc.params), literal=True)]
        try:
            await storage.add_rule(ctx.user_id, chat_id, RuleKind.EXCLUDE, compiled.pattern)
        except DuplicateRuleError:
            return [Reply("bot.rules.line_duplicate", {"line": 1})]
        await invalidate(None if chat_id == ALL_CHATS else chat_id)
        return [
            Reply(t(ctx.lang, "bot.notif.exclude_added", pattern=compiled.pattern), literal=True)
        ]

    # -- settings --------------------------------------------------------------------------

    @router.callback("set")
    async def settings(ctx: Context) -> list[Action]:
        what = (ctx.incoming.data or "set:main").split(":", 1)[1]
        if what == "lang":
            return [Edit("bot.start.language", buttons=menus.language_menu())]
        if what == "tz":
            router.set_pending(ctx.user_id, "timezone")
            return [Edit("bot.start.timezone", buttons=menus.timezone_menu())]
        if what == "digest":
            router.set_pending(ctx.user_id, "digest")
            return [Reply("bot.settings.digest_prompt")]
        body = ctx.t(
            "bot.settings.title",
            lang=LANG_NAMES.get(ctx.lang, ctx.lang),
            tz=ctx.user.tz or "UTC",
            digest=ctx.user.digest_threshold,
        )
        return [Edit(body, literal=True, buttons=menus.settings_menu())]

    @router.pending("digest")
    async def digest_input(ctx: Context) -> list[Action]:
        raw = (ctx.incoming.text or "").strip()
        if not raw.isdigit():
            return [Reply("bot.settings.digest_prompt")]
        value = int(raw)
        if value != 0 and not (DIGEST_MIN <= value <= DIGEST_MAX):
            return [Reply("bot.settings.digest_prompt")]
        router.clear_pending(ctx.user_id)
        await storage.update_user(ctx.user_id, digest_threshold=value)
        return [Reply("bot.settings.digest_set", {"n": value})]

    # -- status and test -------------------------------------------------------------------

    @router.command("status")
    async def status_cmd(ctx: Context) -> list[Action]:
        return [Reply(await _status_text(ctx), literal=True)]

    @router.callback("status")
    async def status_cb(ctx: Context) -> list[Action]:
        return [Edit(await _status_text(ctx), literal=True, buttons=[[("bot.btn.back", "menu")]])]

    async def _status_text(ctx: Context) -> str:
        info = await status() if status is not None else StatusInfo()
        state = ctx.t(
            "bot.status.listener_ok" if info.listener_ok else "bot.status.listener_degraded"
        )
        return "\n".join(
            [
                ctx.t("bot.status.title"),
                ctx.t("bot.status.listener", state=state),
                ctx.t("bot.status.uptime", uptime=info.uptime),
                ctx.t("bot.status.chats", monitored=info.monitored, unavailable=info.unavailable),
                ctx.t(
                    "bot.status.counters",
                    seen=info.seen,
                    matched=info.matched,
                    delivered=info.delivered,
                    digested=info.digested,
                    failed=info.failed,
                ),
                ctx.t("bot.status.queue", queue=info.queue, last_update=info.last_update),
            ]
        )

    @router.command("test")
    async def test_cmd(ctx: Context) -> list[Action]:
        if not ctx.incoming.args:
            router.set_pending(ctx.user_id, "test")
            return [Reply("bot.test.prompt")]
        return [Reply(await _test_text(ctx, ctx.incoming.args), literal=True)]

    @router.pending("test")
    async def test_input(ctx: Context) -> list[Action]:
        router.clear_pending(ctx.user_id)
        return [Reply(await _test_text(ctx, ctx.incoming.text or ""), literal=True)]

    async def _test_text(ctx: Context, text: str) -> str:
        prepared = prepare_text(text)
        chats = await storage.list_subscriptions(ctx.user_id)
        lines: list[str] = []
        if not chats:
            lines.append(ctx.t("bot.test.no_chats"))
        for chat in chats:
            rules = await storage.rules_for_chat(chat.id)
            mine = [r for r in rules if r.user_id == ctx.user_id]
            verdict = _evaluate_preview(prepared, mine, ctx.lang, chat.ref.title)
            lines.append(verdict)
        return "\n".join(lines) if lines else ctx.t("bot.test.no_chats")

    @router.fallback
    async def fallback(ctx: Context) -> list[Action]:
        return [Reply("bot.err.not_waiting", buttons=menus.main_menu(is_owner=ctx.is_owner))]

    return router


def _evaluate_preview(prepared: Any, rules: Sequence[Any], lang: str, chat_title: str) -> str:
    """Mirror the production order: exclusions first, then chat rules before global ones."""
    excludes = [r for r in rules if r.kind is RuleKind.EXCLUDE]
    includes = [r for r in rules if r.kind is RuleKind.INCLUDE]
    for rule in excludes:
        try:
            compiled = compile_rule(rule.pattern, timeout=0.2)
        except RuleError:
            continue
        if compiled.search(prepared.shadow, 0.2):
            return t(lang, "bot.test.excluded", chat=chat_title, rule=rule.pattern)
    for rule in includes:
        try:
            compiled = compile_rule(rule.pattern, timeout=0.2)
        except RuleError:
            continue
        span = compiled.search(prepared.shadow, 0.2)
        if span:
            original = prepared.to_original(span)
            fragment = prepared.original[original.start : original.end]
            return t(lang, "bot.test.match", chat=chat_title, rule=rule.pattern, fragment=fragment)
    return t(lang, "bot.test.nomatch", chat=chat_title)


def propose_exclusion(text: str, spans: Sequence[tuple[int, int]]) -> str | None:
    """Expand the matched fragment to whole whitespace-delimited tokens.

    Whitespace, not ``\\w``, so hyphenated or punctuated tokens (``казино-бонус``,
    ``@casino_bot``, ``casino.com``) stay in one piece.
    """
    if not text or not spans:
        return None
    start, end = spans[0]
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    token = text[start:end].strip().strip(".,;:!?()[]{}\"'«»…")
    if not token:
        return None
    return f"={token}"


def _conflicts(candidate: str, include_pattern: str) -> bool:
    """True when the proposal would silence the keyword entirely.

    Only equality counts. A longer token (``=казино-бонус`` against the keyword ``казино``) is
    exactly the useful case: it mutes that phrase while the keyword keeps firing elsewhere.
    """
    left = candidate.removeprefix("=").casefold()
    right = include_pattern.removeprefix("=").removeprefix("re:").casefold()
    return bool(right) and left == right


def _shorten(pattern: str) -> str:
    return pattern if len(pattern) <= EXCLUDE_PREVIEW else pattern[: EXCLUDE_PREVIEW - 1] + "…"


def _int(raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        return 0

**Identifying security risks in callback and rule handling**
**Addressing race conditions in subscription logic**
exec
/bin/zsh -lc "sed -n '1,340p' ddiver/listener.py && sed -n '1,260p' ddiver/matcher.py && sed -n '1,300p' ddiver/rules.py" in /Users/L/Code/ddiver
 succeeded in 0ms:
"""The user-session side: decide what to look at, match it, and make the match durable.

Hot-path rules (see DESIGN.md "Invariants"):

* no RPC on the listener session inside a handler — only entities attached to the update;
* a private chat is never monitored, whatever the database says;
* the outbox row is written before the cursor moves past the message.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from telethon import utils
from telethon.tl import types as tl

from ddiver.formatter import Payload
from ddiver.links import ChatRef, topic_id_of
from ddiver.matcher import Match, Matcher
from ddiver.rules import prepare_text
from ddiver.senders import SenderCache, SenderInfo, sender_of
from ddiver.storage import Cursor, OutboxItem, Storage, safe_write
from ddiver.timeutil import Clock, utcnow

log = logging.getLogger(__name__)

CURSOR_FLUSH_SECONDS = 5.0

DeliverHook = Callable[[OutboxItem], Awaitable[Any]]


@dataclass(slots=True)
class ListenerMetrics:
    seen: int = 0
    considered: int = 0
    matched: int = 0
    claimed: int = 0
    duplicates: int = 0
    skipped_bot: int = 0
    skipped_private: int = 0
    skipped_own: int = 0


@dataclass(slots=True)
class CursorTracker:
    """Highest message id seen per chat, flushed periodically.

    A chat's flush is withheld until its catch-up finished, so a crash in the middle of a
    backfill cannot leave the cursor past messages that were never examined.
    """

    storage: Storage
    pending: dict[int, tuple[int, dt.datetime]] = field(default_factory=dict)
    blocked: set[int] = field(default_factory=set)

    def note(self, chat_id: int, msg_id: int, moment: dt.datetime) -> None:
        current = self.pending.get(chat_id)
        if current is None or msg_id > current[0]:
            self.pending[chat_id] = (msg_id, moment)

    def block(self, chat_id: int) -> None:
        self.blocked.add(chat_id)

    def unblock(self, chat_id: int) -> None:
        self.blocked.discard(chat_id)

    async def flush(self) -> int:
        ready = [
            Cursor(chat_id, msg_id, moment)
            for chat_id, (msg_id, moment) in self.pending.items()
            if chat_id not in self.blocked
        ]
        if not ready:
            return 0
        if not await safe_write(self.storage.upsert_cursors(ready), "cursor flush"):
            return 0
        for cursor in ready:
            current = self.pending.get(cursor.chat_id)
            if current is not None and current[0] == cursor.last_msg_id:
                del self.pending[cursor.chat_id]
        return len(ready)


def chat_ref_of(entity: Any) -> ChatRef | None:
    """Chat metadata from an update entity, or ``None`` for private chats and unknown types."""
    if entity is None or isinstance(entity, tl.User):
        return None
    if isinstance(entity, (tl.Chat, tl.Channel, tl.ChatForbidden, tl.ChannelForbidden)):
        return ChatRef.from_entity(entity)
    return None


def should_consider(message: tl.Message, *, ignore_own: bool = True) -> bool:
    """Shared by the live handler and catch-up so both paths filter identically."""
    if not isinstance(message, tl.Message):  # MessageService / MessageEmpty
        return False
    if getattr(message, "action", None) is not None:
        return False
    if ignore_own and getattr(message, "out", False):
        return False
    return bool(message.message)


class Listener:
    def __init__(
        self,
        storage: Storage,
        matcher: Matcher,
        senders: SenderCache,
        *,
        deliver: DeliverHook,
        clock: Clock = utcnow,
        ignore_own: bool = True,
    ) -> None:
        self._storage = storage
        self._matcher = matcher
        self._senders = senders
        self._deliver = deliver
        self._clock = clock
        self._ignore_own = ignore_own
        self.cursors = CursorTracker(storage)
        self.metrics = ListenerMetrics()
        self.last_update_at: dt.datetime | None = None
        self._chat_cache: dict[int, ChatRef] = {}

    async def on_message(self, event: Any) -> None:
        """Entry point for ``events.NewMessage``. Never raises: one bad message must not
        stop the listener."""
        try:
            await self.handle(event.message, chat=getattr(event, "chat", None))
        except Exception:
            log.exception("listener failed on a message")

    async def handle(
        self,
        message: tl.Message,
        *,
        chat: Any = None,
        catchup: bool = False,
        catchup_skipped: int = 0,
    ) -> list[OutboxItem]:
        self.metrics.seen += 1
        self.last_update_at = self._clock()
        if not should_consider(message, ignore_own=self._ignore_own):
            if getattr(message, "out", False):
                self.metrics.skipped_own += 1
            return []

        chat_id = _chat_id_of(message)
        if chat_id is None or chat_id >= 0:  # private chats are never monitored
            self.metrics.skipped_private += 1
            return []
        if not self._matcher.is_monitored(chat_id):
            return []

        await self._refresh_chat(chat_id, chat if chat is not None else message.chat)

        moment = message.date or self._clock()
        self.metrics.considered += 1
        prepared = prepare_text(message.message)
        matches = await self._matcher.evaluate(chat_id, prepared.shadow, _sender_id_of(message))
        if not matches:
            self.cursors.note(chat_id, message.id, moment)
            return []

        sender = await sender_of(message, self._senders)
        if sender is not None and sender.is_bot:
            self.metrics.skipped_bot += 1
            self.cursors.note(chat_id, message.id, moment)
            return []

        self.metrics.matched += 1
        claimed: list[OutboxItem] = []
        for match in matches:
            item = await self._claim(
                message,
                chat_id,
                match,
                prepared,
                sender,
                catchup=catchup,
                catchup_skipped=catchup_skipped,
            )
            if item is not None:
                claimed.append(item)
        # The cursor may only advance once every match for this message is durable.
        self.cursors.note(chat_id, message.id, moment)
        for item in claimed:
            await self._deliver(item)
        return claimed

    async def _claim(
        self,
        message: tl.Message,
        chat_id: int,
        match: Match,
        prepared: Any,
        sender: SenderInfo | None,
        *,
        catchup: bool,
        catchup_skipped: int,
    ) -> OutboxItem | None:
        chat = self._chat_cache.get(chat_id) or ChatRef(chat_id, str(chat_id), None, "supergroup")
        spans = [prepared.to_original(s) for s in match.spans]
        payload = Payload(
            chat_id=chat_id,
            chat_title=chat.title,
            chat_username=chat.username,
            chat_kind=chat.kind,
            msg_id=message.id,
            topic_id=topic_id_of(message),
            date=message.date or self._clock(),
            text=message.message or "",
            spans=[(s.start, s.end) for s in spans],
            rule_pattern=match.rule.pattern,
            rule_kind=match.rule.kind.value,
            rule_is_global=match.rule.is_global,
            sender_id=sender.id if sender else None,
            sender_name=sender.display_name if sender else "",
            sender_username=sender.username if sender else None,
            sender_is_channel=bool(sender and sender.is_channel),
            sender_is_deleted=bool(sender and sender.is_deleted),
            is_forwarded=message.fwd_from is not None,
            catchup=catchup,
            catchup_skipped=catchup_skipped,
        )
        item = await self._storage.claim_delivery(
            match.user_id, chat_id, message.id, match.rule.id, payload.to_json()
        )
        if item is None:
            self.metrics.duplicates += 1
            return None
        self.metrics.claimed += 1
        self._matcher.note_hit(match.rule.id)
        return item

    async def _refresh_chat(self, chat_id: int, entity: Any) -> None:
        ref = chat_ref_of(entity)
        if ref is None:
            return
        cached = self._chat_cache.get(chat_id)
        if cached == ref:
            return
        self._chat_cache[chat_id] = ref
        access_hash = getattr(entity, "access_hash", None)
        await safe_write(self._storage.upsert_chat(ref, access_hash=access_hash), "chat upsert")

    async def prime_chat_cache(self) -> None:
        """Load known chat metadata so notifications have titles before the first update."""
        for row in await self._storage.list_chats():
            self._chat_cache[row.id] = row.ref


def _chat_id_of(message: tl.Message) -> int | None:
    peer = message.peer_id
    if peer is None:
        return None
    try:
        return int(utils.get_peer_id(peer))
    except (TypeError, ValueError):  # pragma: no cover - malformed update
        return None


def _sender_id_of(message: tl.Message) -> int | None:
    sender_id = getattr(message, "sender_id", None)
    return int(sender_id) if sender_id is not None else None
"""Per-chat rule sets for all subscribed users, with lazy compilation and invalidation."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ddiver.rules import CompiledRule, RuleError, RuleKind, RuleTimeout, Span, compile_rule
from ddiver.storage import Rule, Storage

log = logging.getLogger(__name__)

TIMEOUTS_BEFORE_DISABLE = 3
DISABLED_TIMEOUT = "timeout"
DISABLED_INVALID = "invalid"

RuleDisabledHook = Callable[[Rule, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Match:
    user_id: int
    rule: Rule
    spans: list[Span]

    @property
    def span(self) -> Span:
        return self.spans[0]


@dataclass(slots=True)
class _UserRules:
    user_id: int
    includes: list[tuple[Rule, CompiledRule]] = field(default_factory=list)
    excludes: list[tuple[Rule, CompiledRule]] = field(default_factory=list)
    ignored: set[int] = field(default_factory=set)


@dataclass(slots=True)
class _ChatRuleSet:
    chat_id: int
    users: list[_UserRules]


class Matcher:
    """Evaluates messages against every subscribed user's rules for a chat.

    The monitored-chat set and the compiled rule sets are cached; the bot UI calls
    :meth:`invalidate` after any change to subscriptions, rules or ignore lists.
    """

    def __init__(
        self,
        storage: Storage,
        *,
        timeout: float,
        on_rule_disabled: RuleDisabledHook | None = None,
    ) -> None:
        self._storage = storage
        self._timeout = timeout
        self._on_rule_disabled = on_rule_disabled
        self._monitored: set[int] = set()
        self._cache: dict[int, _ChatRuleSet] = {}
        self._timeouts: dict[int, int] = {}
        self._hits: Counter[int] = Counter()
        self.stats_evaluated = 0
        self.stats_matched = 0
        self.stats_timeouts = 0

    def note_hit(self, rule_id: int) -> None:
        """Count a delivered match; flushed to storage together with the cursors."""
        self._hits[rule_id] += 1

    async def flush_hits(self) -> None:
        if not self._hits:
            return
        hits, self._hits = dict(self._hits), Counter()
        await self._storage.flush_rule_hits(hits)

    # -- monitored set ---------------------------------------------------------------------

    @property
    def monitored(self) -> frozenset[int]:
        return frozenset(self._monitored)

    def is_monitored(self, chat_id: int) -> bool:
        return chat_id in self._monitored

    async def reload(self) -> None:
        """Reload the monitored set from storage and drop every cached rule set."""
        self._monitored = await self._storage.monitored_chat_ids()
        self._cache.clear()

    async def invalidate(self, chat_id: int | None = None) -> None:
        """Drop cached rules for one chat (or all) and refresh the monitored set."""
        if chat_id is None:
            self._cache.clear()
        else:
            self._cache.pop(chat_id, None)
        self._monitored = await self._storage.monitored_chat_ids()

    # -- evaluation ------------------------------------------------------------------------

    async def evaluate(self, chat_id: int, text: str, sender_id: int | None) -> list[Match]:
        """Return at most one :class:`Match` per subscribed user for ``text`` in ``chat_id``.

        Per user: ignored sender → no match; any exclusion hit → no match; otherwise the first
        include rule (chat-specific before global) that matches. A rule that times out counts
        as "no match" and is disabled after :data:`TIMEOUTS_BEFORE_DISABLE` timeouts.
        """
        if not text or chat_id not in self._monitored:
            return []
        self.stats_evaluated += 1
        ruleset = await self._get(chat_id)
        matches: list[Match] = []
        for user in ruleset.users:
            if sender_id is not None and sender_id in user.ignored:
                continue
            if await self._any_excluded(user, text):
                continue
            for rule, compiled in user.includes:
                spans = await self._search_all(rule, compiled, text)
                if spans:
                    matches.append(Match(user.user_id, rule, spans))
                    break
        if matches:
            self.stats_matched += 1
        return matches

    async def _any_excluded(self, user: _UserRules, text: str) -> bool:
        for rule, compiled in user.excludes:
            if await self._search_all(rule, compiled, text):
                return True
        return False

    async def _search_all(self, rule: Rule, compiled: CompiledRule, text: str) -> list[Span]:
        try:
            return compiled.search_all(text, self._timeout)
        except RuleTimeout:
            self.stats_timeouts += 1
            count = self._timeouts.get(rule.id, 0) + 1
            self._timeouts[rule.id] = count
            log.warning("rule %s timed out (%d/%d)", rule.id, count, TIMEOUTS_BEFORE_DISABLE)
            if count >= TIMEOUTS_BEFORE_DISABLE:
                await self._disable(rule, DISABLED_TIMEOUT)
            return []

    async def _disable(self, rule: Rule, reason: str) -> None:
        await self._storage.disable_rule(rule.id, reason)
        self._timeouts.pop(rule.id, None)
        self._cache.clear()  # the rule may be global, so every chat could be affected
        log.warning("rule %s (%r) disabled: %s", rule.id, rule.pattern, reason)
        if self._on_rule_disabled is not None:
            await self._on_rule_disabled(rule, reason)

    # -- loading ---------------------------------------------------------------------------

    async def _get(self, chat_id: int) -> _ChatRuleSet:
        cached = self._cache.get(chat_id)
        if cached is not None:
            return cached
        ruleset = await self._load(chat_id)
        self._cache[chat_id] = ruleset
        return ruleset

    async def _load(self, chat_id: int) -> _ChatRuleSet:
        rules = await self._storage.rules_for_chat(chat_id)
        ignored = await self._storage.ignored_for_chat(chat_id)
        by_user: dict[int, _UserRules] = {}
        for rule in rules:  # ordered by user, chat-specific before global
            bucket = by_user.setdefault(rule.user_id, _UserRules(rule.user_id))
            try:
                compiled = compile_rule(rule.pattern, timeout=self._timeout)
            except RuleError as exc:
                await self._disable(rule, f"{DISABLED_INVALID}:{exc.key}")
                continue
            target = bucket.includes if rule.kind is RuleKind.INCLUDE else bucket.excludes
            target.append((rule, compiled))
        for user_id, senders in ignored.items():
            if user_id in by_user:
                by_user[user_id].ignored = set(senders)
        return _ChatRuleSet(chat_id, list(by_user.values()))
"""Rule syntax and compiled matching.

Syntax (fixed by the requirements):

* ``word``     case-insensitive substring
* ``=word``    whole word; lookarounds instead of ``\\b`` so ``=c++`` and ``=#tag`` work
* ``re:expr``  regular expression, IGNORECASE by default (opt out inline with ``(?-i:...)``)

Everything is compiled with the third-party :mod:`regex` module because it supports a
``timeout`` on matching, which is the only practical defence against a user-supplied
catastrophic pattern freezing the event loop.
"""

from __future__ import annotations

import enum
import unicodedata
from dataclasses import dataclass
from typing import Any

import regex

MAX_PATTERN_LEN = 300
MAX_HIGHLIGHTS = 10
RE_PREFIX = "re:"
WORD_PREFIX = "="

# Inputs that make naive catastrophic patterns blow up; used only at validation time.
_PROBE_INPUTS = (
    "a" * 64 + "!",
    "aa " * 32 + "!",
    "x" * 32 + "y" * 32,
    "а" * 64 + "!",  # Cyrillic 'а' for patterns written in Cyrillic
)


class RuleKind(enum.StrEnum):
    INCLUDE = "include"
    EXCLUDE = "exclude"


class MatchMode(enum.StrEnum):
    SUBSTRING = "substring"
    WORD = "word"
    REGEX = "regex"


class RuleError(ValueError):
    """Invalid rule pattern. ``key`` is an i18n key, ``params`` its format arguments."""

    def __init__(self, key: str, **params: Any) -> None:
        super().__init__(key)
        self.key = key
        self.params = params


class RuleTimeout(Exception):  # noqa: N818 - "Timeout" reads better than "TimeoutError" here
    """Matching exceeded the configured time budget."""


@dataclass(frozen=True, slots=True)
class Span:
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class CompiledRule:
    """A parsed and compiled rule. ``pattern`` is the text exactly as the user typed it."""

    pattern: str
    mode: MatchMode
    compiled: regex.Pattern[str]

    def search(self, text: str, timeout: float) -> Span | None:
        """First non-empty match span, or ``None``; raises :class:`RuleTimeout` on timeout.

        Zero-length matches (``re:x*`` and friends) are skipped: Telegram rejects zero-length
        entities, and "matched nothing" is not a useful notification.
        """
        spans = self.search_all(text, timeout, limit=1)
        return spans[0] if spans else None

    def search_all(self, text: str, timeout: float, limit: int = MAX_HIGHLIGHTS) -> list[Span]:
        """Up to ``limit`` non-empty, non-overlapping match spans in order."""
        out: list[Span] = []
        try:
            for m in self.compiled.finditer(text, timeout=timeout):
                if m.end() > m.start():
                    out.append(Span(m.start(), m.end()))
                    if len(out) >= limit:
                        break
        except TimeoutError as exc:
            raise RuleTimeout(self.pattern) from exc
        return out


def parse_pattern(raw: str) -> tuple[MatchMode, str]:
    """Split a raw rule into (mode, payload) without compiling it."""
    text = raw.strip()
    if text.startswith(RE_PREFIX):
        return MatchMode.REGEX, text[len(RE_PREFIX) :].strip()
    if text.startswith(WORD_PREFIX):
        return MatchMode.WORD, text[len(WORD_PREFIX) :].strip()
    return MatchMode.SUBSTRING, text


def clean_text(raw: str) -> tuple[str, int]:
    """NFC-normalize and drop invisible format characters (zero-width spaces/joiners, BOM…).

    Returns the cleaned text and how many characters were removed, so the UI can tell the
    user that a pasted keyword contained invisible junk.
    """
    normalized = unicodedata.normalize("NFC", raw)
    cleaned = "".join(ch for ch in normalized if unicodedata.category(ch) != "Cf")
    return cleaned, len(normalized) - len(cleaned)


def normalize_pattern(raw: str) -> str:
    """Canonical stored form: cleaned, trimmed, prefix kept, inner whitespace preserved."""
    mode, payload = parse_pattern(clean_text(raw)[0])
    if mode is MatchMode.REGEX:
        return f"{RE_PREFIX}{payload}"
    if mode is MatchMode.WORD:
        return f"{WORD_PREFIX}{payload}"
    return payload


def pattern_key(pattern: str) -> str:
    """Uniqueness key: case-folded for substring/word rules (they match case-insensitively
    anyway), verbatim for regexes."""
    mode, payload = parse_pattern(pattern)
    if mode is MatchMode.REGEX:
        return f"{RE_PREFIX}{payload}"
    folded = payload.casefold()
    return f"{WORD_PREFIX}{folded}" if mode is MatchMode.WORD else folded


@dataclass(frozen=True, slots=True)
class PreparedText:
    """Message text ready for matching.

    ``shadow`` is the NFC-normalized text with format characters (zero-width joiners, BOM…)
    removed; ``starts``/``ends`` map each shadow code point back to the half-open range of
    original code points it came from, so a match span can be turned into a highlight span on
    the original text.
    """

    original: str
    shadow: str
    starts: tuple[int, ...]
    ends: tuple[int, ...]

    def to_original(self, span: Span) -> Span:
        if not self.starts or span.end <= span.start:
            return Span(0, 0)
        last = len(self.starts) - 1
        return Span(self.starts[min(span.start, last)], self.ends[min(span.end - 1, last)])


def prepare_text(text: str) -> PreparedText:
    if not text:
        return PreparedText("", "", (), ())
    if unicodedata.is_normalized("NFC", text) and not any(
        unicodedata.category(ch) == "Cf" for ch in text
    ):
        positions = tuple(range(len(text)))
        return PreparedText(text, text, positions, tuple(p + 1 for p in positions))
    shadow: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    unit: list[str] = []
    unit_start = 0
    unit_end = 0

    def flush() -> None:
        if not unit:
            return
        for out in unicodedata.normalize("NFC", "".join(unit)):
            shadow.append(out)
            starts.append(unit_start)
            ends.append(unit_end)
        unit.clear()

    for pos, ch in enumerate(text):
        category = unicodedata.category(ch)
        if category == "Cf":
            continue
        if category[0] == "M" and unit:  # combining mark: stays with the current base character
            unit.append(ch)
            unit_end = pos + 1
            continue
        flush()
        unit_start, unit_end = pos, pos + 1
        unit.append(ch)
    flush()
    return PreparedText(text, "".join(shadow), tuple(starts), tuple(ends))


def compile_rule(raw: str, *, timeout: float) -> CompiledRule:
    """Validate and compile a rule.

    Raises :class:`RuleError` with an i18n key for: empty pattern, pattern too long, invalid
    regex, regex that matches empty text, regex that times out on probe inputs.
    """
    pattern = normalize_pattern(raw)
    mode, payload = parse_pattern(pattern)
    if not payload:
        raise RuleError("rule.err.empty")
    if len(pattern) > MAX_PATTERN_LEN:
        raise RuleError("rule.err.too_long", limit=MAX_PATTERN_LEN)

    if mode is MatchMode.SUBSTRING:
        expr = regex.escape(payload)
    elif mode is MatchMode.WORD:
        expr = rf"(?<!\w){regex.escape(payload)}(?!\w)"
    else:
        expr = payload

    try:
        compiled = regex.compile(expr, regex.IGNORECASE)
    except regex.error as exc:  # invalid user regex
        raise RuleError("rule.err.invalid_regex", detail=str(exc)) from exc

    if mode is MatchMode.REGEX:
        _validate_regex(pattern, compiled, timeout)
    return CompiledRule(pattern=pattern, mode=mode, compiled=compiled)


def _validate_regex(pattern: str, compiled: regex.Pattern[str], timeout: float) -> None:
    """Heuristic guard rails. The real protection against slow patterns is the runtime timeout
    plus auto-disable; this only catches the obvious cases at insert time."""
    try:
        if _matches_everything(compiled, timeout):
            raise RuleError("rule.err.matches_empty")
        for probe in _PROBE_INPUTS:
            compiled.search(probe, timeout=timeout)
    except TimeoutError as exc:
        raise RuleError("rule.err.too_slow", pattern=pattern) from exc


def _matches_everything(compiled: regex.Pattern[str], timeout: float) -> bool:
    """True when the pattern can match empty text (``re:.*``, ``re:^``, ``re:x*``): such a rule
    fires on every message, which is never what the user wants."""
    return compiled.search("", timeout=timeout) is not None


def parse_rule_lines(text: str) -> list[str]:
    """Split multi-line user input into candidate rules; blank lines dropped.

    There is deliberately no comment syntax: ``#промо`` is a hashtag keyword, not a comment.
    """
    return [line.strip() for line in text.splitlines() if line.strip()]

**Analyzing durability flaw in claim_delivery retry**
**Inspecting sender fallback design**
exec
/bin/zsh -lc "sed -n '1,260p' ddiver/senders.py && sed -n '1,260p' ddiver/catchup.py && sed -n '1,460p' ddiver/delivery.py" in /Users/L/Code/ddiver
 succeeded in 0ms:
"""Sender identity extracted from an update, without network calls in the hot path.

``event.sender`` is served from the entities attached to the update. In supergroups those are
``min`` User objects: they carry id, names, username and the bot/deleted flags — everything a
notification needs — but no usable access hash, so ``event.get_sender()`` would issue an RPC
per matched message on the moderator's personal account. We never do that; when an update
carries no entity at all we fall back to a rate-limited, cached resolve.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Protocol

from telethon import utils
from telethon.errors import RPCError
from telethon.tl import types as tl

from ddiver.timeutil import Clock, utcnow

log = logging.getLogger(__name__)

CACHE_SIZE = 10_000
CACHE_TTL = dt.timedelta(hours=1)
RESOLVE_TIMEOUT = 5.0
RESOLVE_MIN_INTERVAL = 1.0


@dataclass(frozen=True, slots=True)
class SenderInfo:
    """``id`` is the *marked* peer id, so a channel sender never collides with a user id."""

    id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    title: str | None = None  # set for channel/anonymous-admin senders
    is_bot: bool = False
    is_deleted: bool = False
    is_channel: bool = False

    @property
    def display_name(self) -> str:
        if self.title:
            return self.title
        name = " ".join(p for p in (self.first_name, self.last_name) if p).strip()
        return name or (f"@{self.username}" if self.username else "")


class EntityResolver(Protocol):
    async def __call__(self, peer_id: int) -> Any: ...


def active_username(entity: Any) -> str | None:
    """Primary username, or the first active collectible (fragment) username."""
    primary = getattr(entity, "username", None)
    if primary:
        return str(primary)
    for extra in getattr(entity, "usernames", None) or []:
        if getattr(extra, "active", False) and extra.username:
            return str(extra.username)
    return None


def sender_from_entity(entity: Any) -> SenderInfo | None:
    """Build :class:`SenderInfo` from a Telethon ``User``/``Channel``/``Chat`` object."""
    if entity is None:
        return None
    marked = utils.get_peer_id(entity)
    if isinstance(entity, tl.User):
        return SenderInfo(
            id=marked,
            first_name=entity.first_name,
            last_name=entity.last_name,
            username=active_username(entity),
            is_bot=bool(entity.bot),
            is_deleted=bool(entity.deleted),
        )
    return SenderInfo(
        id=marked,
        username=active_username(entity),
        title=getattr(entity, "title", None),
        is_channel=True,
    )


class SenderCache:
    """LRU + TTL cache in front of a rate-limited entity resolver."""

    def __init__(
        self,
        resolver: EntityResolver | None = None,
        *,
        clock: Clock = utcnow,
        sleep: Any = asyncio.sleep,
        ttl: dt.timedelta = CACHE_TTL,
        size: int = CACHE_SIZE,
        min_interval: float = RESOLVE_MIN_INTERVAL,
    ) -> None:
        self._resolver = resolver
        self._clock = clock
        self._sleep = sleep
        self._ttl = ttl
        self._size = size
        self._min_interval = min_interval
        self._entries: OrderedDict[int, tuple[dt.datetime, SenderInfo]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._next_allowed: dt.datetime | None = None
        self.stats_hits = 0
        self.stats_resolves = 0
        self.stats_failures = 0
        self.stats_throttled = 0

    def get(self, peer_id: int) -> SenderInfo | None:
        entry = self._entries.get(peer_id)
        if entry is None:
            return None
        stored_at, info = entry
        if self._clock() - stored_at > self._ttl:
            del self._entries[peer_id]
            return None
        self._entries.move_to_end(peer_id)
        self.stats_hits += 1
        return info

    def put(self, info: SenderInfo) -> None:
        self._entries[info.id] = (self._clock(), info)
        self._entries.move_to_end(info.id)
        while len(self._entries) > self._size:
            self._entries.popitem(last=False)

    async def resolve(self, peer_id: int) -> SenderInfo | None:
        """Cache lookup, then at most one resolve per ``min_interval``; never raises."""
        cached = self.get(peer_id)
        if cached is not None:
            return cached
        if self._resolver is None:
            return None
        async with self._lock:
            cached = self.get(peer_id)
            if cached is not None:
                return cached
            now = self._clock()
            if self._next_allowed is not None and now < self._next_allowed:
                self.stats_throttled += 1
                return None
            self._next_allowed = now + dt.timedelta(seconds=self._min_interval)
            self.stats_resolves += 1
            try:
                async with asyncio.timeout(RESOLVE_TIMEOUT):
                    entity = await self._resolver(peer_id)
            except (RPCError, ValueError, TypeError, TimeoutError, ConnectionError) as exc:
                self.stats_failures += 1
                log.info("sender resolve failed for %s: %s", peer_id, type(exc).__name__)
                return None
            except asyncio.CancelledError:
                raise
            except Exception:
                self.stats_failures += 1
                log.warning("unexpected sender resolve error for %s", peer_id, exc_info=True)
                return None
        info = sender_from_entity(entity)
        if info is not None:
            self.put(info)
        return info


async def sender_of(message: Any, cache: SenderCache) -> SenderInfo | None:
    """Sender of a Telethon message: entity attached to the update, else cache/resolve.

    Returns ``None`` only when the sender is unknown and cannot be resolved cheaply.
    """
    info = sender_from_entity(getattr(message, "sender", None))
    if info is not None:
        cache.put(info)
        return info
    sender_id = getattr(message, "sender_id", None)
    if sender_id is None:
        return None
    return await cache.resolve(int(sender_id))


def suppress_rpc() -> contextlib.AbstractContextManager[None]:
    return contextlib.suppress(RPCError, ValueError)
"""Backfill messages missed while the process was down (or while Telethon dropped a gap).

Direction matters: ``iter_messages(peer, min_id=cursor, limit=N)`` iterates **newest first** and
stops by itself at ``id <= min_id``. Iterating with ``reverse=True`` (as the first design draft
said) would return the *oldest* N messages after the cursor and, with a date cutoff, deliver
nothing at all after an outage longer than the window. Messages are buffered and then processed
oldest-first so notifications arrive in chronological order.

The per-chat cursor is held back until that chat's backfill finished, so a crash in the middle
cannot move the cursor past messages that were never examined.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from telethon import errors
from telethon.tl import types as tl

from ddiver.listener import Listener, should_consider
from ddiver.storage import Storage
from ddiver.timeutil import Clock, utcnow

log = logging.getLogger(__name__)

YIELD_EVERY = 20
INTER_CHAT_PAUSE = 0.5

History = Callable[[int, int, int], AsyncIterator[tl.Message]]
"""``history(chat_id, min_id, limit)`` → messages newest first."""

Sleeper = Callable[[float], Awaitable[None]]


@dataclass(slots=True)
class CatchupResult:
    chats: int = 0
    messages: int = 0
    claimed: int = 0
    capped_chats: list[int] = field(default_factory=list)
    unavailable: list[int] = field(default_factory=list)
    flood_waits: int = 0
    skipped: int = 0


class Catchup:
    def __init__(
        self,
        storage: Storage,
        listener: Listener,
        history: History,
        *,
        clock: Clock = utcnow,
        sleep: Sleeper = asyncio.sleep,
        hours: int = 24,
        per_chat: int = 500,
        total: int = 5000,
        on_unavailable: Callable[[int, str], Awaitable[Any]] | None = None,
    ) -> None:
        self._storage = storage
        self._listener = listener
        self._history = history
        self._clock = clock
        self._sleep = sleep
        self._hours = hours
        self._per_chat = per_chat
        self._total = total
        self._on_unavailable = on_unavailable

    async def run(self, chat_ids: set[int] | None = None) -> CatchupResult:
        result = CatchupResult()
        if self._hours <= 0 or self._per_chat <= 0:
            return result
        cursors = await self._storage.get_cursors()
        monitored = chat_ids if chat_ids is not None else await self._storage.monitored_chat_ids()
        cutoff = self._clock() - dt.timedelta(hours=self._hours)

        for chat_id in sorted(monitored):
            cursor = cursors.get(chat_id)
            if cursor is None:
                # A chat we have never seen starts "now": no backfill on first subscribe.
                continue
            chat = await self._storage.get_chat(chat_id)
            if chat is not None and not chat.available:
                result.skipped += 1
                continue
            if result.messages >= self._total:
                log.warning(
                    "catch-up budget reached; %d chats not checked", len(monitored) - result.chats
                )
                break
            await self._catch_up_chat(chat_id, cursor.last_msg_id, cutoff, result)
            await self._sleep(INTER_CHAT_PAUSE)
        return result

    async def _catch_up_chat(
        self, chat_id: int, min_id: int, cutoff: dt.datetime, result: CatchupResult
    ) -> None:
        self._listener.cursors.block(chat_id)
        try:
            buffered, capped, newest = await self._fetch(chat_id, min_id, cutoff, result)
        except errors.FloodWaitError as exc:
            result.flood_waits += 1
            log.warning(
                "catch-up flood wait %ss on chat %s; skipping it this round", exc.seconds, chat_id
            )
            self._listener.cursors.unblock(chat_id)
            return
        except (errors.ChannelPrivateError, errors.ChatForbiddenError, ValueError) as exc:
            reason = type(exc).__name__
            log.warning("catch-up cannot read chat %s (%s)", chat_id, reason)
            result.unavailable.append(chat_id)
            if await self._storage.set_chat_available(chat_id, False) and self._on_unavailable:
                await self._on_unavailable(chat_id, reason)
            self._listener.cursors.unblock(chat_id)
            return
        except Exception:
            log.exception("catch-up failed for chat %s", chat_id)
            self._listener.cursors.unblock(chat_id)
            return

        result.chats += 1
        skipped = self._per_chat if capped else 0
        if capped:
            result.capped_chats.append(chat_id)
        for index, message in enumerate(reversed(buffered)):
            claimed = await self._listener.handle(
                message,
                chat=message.chat,
                catchup=True,
                catchup_skipped=skipped if index == 0 else 0,
            )
            result.claimed += len(claimed)
            if index % YIELD_EVERY == 0:
                await asyncio.sleep(0)
        if newest is not None:
            # Advance even when everything was filtered out, so the next round starts here.
            self._listener.cursors.note(chat_id, newest[0], newest[1])
        self._listener.cursors.unblock(chat_id)
        await self._listener.cursors.flush()

    async def _fetch(
        self, chat_id: int, min_id: int, cutoff: dt.datetime, result: CatchupResult
    ) -> tuple[list[tl.Message], bool, tuple[int, dt.datetime] | None]:
        buffered: list[tl.Message] = []
        newest: tuple[int, dt.datetime] | None = None
        fetched = 0
        async for message in self._history(chat_id, min_id, self._per_chat):
            fetched += 1
            result.messages += 1
            date = getattr(message, "date", None) or self._clock()
            if newest is None:
                newest = (message.id, date)
            if date < cutoff:
                return buffered, False, newest
            if should_consider(message, ignore_own=True):
                buffered.append(message)
            if fetched % YIELD_EVERY == 0:
                await asyncio.sleep(0)
        return buffered, fetched >= self._per_chat, newest


def telethon_history(client: Any) -> History:
    """Adapt a Telethon client to the ``History`` callable used by :class:`Catchup`."""

    def history(chat_id: int, min_id: int, limit: int) -> AsyncIterator[tl.Message]:
        messages: AsyncIterator[tl.Message] = client.iter_messages(
            chat_id, min_id=min_id, limit=limit
        )
        return messages

    return history
"""Deliver notifications to moderators' DMs through the bot client.

Durability: the caller writes an outbox row (``status='pending'``) *before* the listener's
cursor moves past the message, and this module only flips the status once the RPC succeeded.
Semantics are therefore at-least-once — for a moderation tool a rare duplicate beats a loss.

Flood control: Telethon gates flood waits per request constructor and per client, so a
``FloodWaitError`` on ``SendMessageRequest`` for one moderator applies to all of them. Instead of
pretending otherwise, one shared gate pauses every worker for the required time.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from telethon import errors
from telethon.tl import types as tl

from ddiver.formatter import DigestEntry, Payload, Rendered, render_digest, render_notification
from ddiver.i18n import DEFAULT_LANG
from ddiver.storage import DeliveryStatus, OutboxItem, Storage
from ddiver.timeutil import Clock, utcnow

log = logging.getLogger(__name__)

QUEUE_MAXSIZE = 2000
GLOBAL_RATE = 20.0  # messages per second, bot-wide
USER_RATE = 1.0  # messages per second into one private chat
DIGEST_MIN_QUEUE = 5
DIGEST_AGE = dt.timedelta(seconds=30)
DIGEST_MAX_ITEMS = 200
MAX_ATTEMPTS = 5
BACKOFF_BASE = 1.0
BACKOFF_CAP = 32.0

Sleeper = Callable[[float], Awaitable[None]]
ButtonFactory = Callable[[OutboxItem, str], Any]

RETRYABLE = (errors.ServerError, errors.TimedOutError, ConnectionError, OSError)
PAUSE_ERRORS = (errors.UserIsBlockedError, errors.InputUserDeactivatedError)


class BotPort(Protocol):
    """The only Telethon surface delivery uses, so tests can capture sends."""

    async def send_message(
        self,
        user_id: int,
        text: str,
        *,
        formatting_entities: list[tl.TypeMessageEntity] | None = ...,
        buttons: Any = ...,
        link_preview: bool = ...,
    ) -> Any: ...


@dataclass(slots=True)
class Metrics:
    delivered: int = 0
    digested: int = 0
    failed: int = 0
    dropped_overflow: int = 0
    dropped_inactive: int = 0
    flood_waits: int = 0
    flood_seconds: float = 0.0
    worker_restarts: int = 0
    degraded_sends: int = 0


@dataclass(slots=True)
class _UserQueue:
    queue: asyncio.Queue[OutboxItem]
    worker: asyncio.Task[None] | None = None
    overflow: int = 0
    enqueued_at: dict[tuple[int, int, int], dt.datetime] = field(default_factory=dict)


class TokenBucket:
    """Simple rate limiter driven by an injected monotonic clock and sleep."""

    def __init__(self, rate: float, *, monotonic: Callable[[], float], sleep: Sleeper) -> None:
        self._interval = 1.0 / rate if rate > 0 else 0.0
        self._monotonic = monotonic
        self._sleep = sleep
        self._next_at = 0.0

    async def acquire(self) -> None:
        if self._interval <= 0:
            return
        now = self._monotonic()
        wait = self._next_at - now
        if wait > 0:
            await self._sleep(wait)
            now = self._monotonic()
        self._next_at = max(now, self._next_at) + self._interval


class Delivery:
    def __init__(
        self,
        bot: BotPort,
        storage: Storage,
        *,
        clock: Clock = utcnow,
        monotonic: Callable[[], float] | None = None,
        sleep: Sleeper = asyncio.sleep,
        buttons: ButtonFactory | None = None,
        default_threshold: int = 20,
    ) -> None:
        self._bot = bot
        self._storage = storage
        self._clock = clock
        self._monotonic = monotonic or (lambda: asyncio.get_running_loop().time())
        self._sleep = sleep
        self._buttons = buttons
        self._default_threshold = default_threshold
        self._queues: dict[int, _UserQueue] = {}
        self._global_bucket = TokenBucket(GLOBAL_RATE, monotonic=self._monotonic, sleep=sleep)
        self._user_buckets: dict[int, TokenBucket] = {}
        self._flood_until = 0.0
        self._stopping = False
        self.metrics = Metrics()

    # -- lifecycle -------------------------------------------------------------------------

    async def replay_pending(self) -> int:
        """Re-queue outbox rows left behind by a crash or a shutdown. Runs before catch-up."""
        items = await self._storage.pending_deliveries()
        for item in items:
            await self.enqueue(item)
        if items:
            log.info("replayed %d pending notifications from the outbox", len(items))
        return len(items)

    async def enqueue(self, item: OutboxItem) -> bool:
        """Hand an outbox row to the user's worker. Never raises, never blocks the caller."""
        user = await self._storage.get_user(item.user_id)
        if user is None or not user.active or user.paused:
            self.metrics.dropped_inactive += 1
            return False
        state = self._queue_for(item.user_id)
        try:
            state.queue.put_nowait(item)
        except asyncio.QueueFull:
            state.overflow += 1
            self.metrics.dropped_overflow += 1
            log.warning(
                "delivery queue full for user %s (%d dropped)", item.user_id, state.overflow
            )
            return False
        state.enqueued_at[item.key] = self._clock()
        return True

    def queue_length(self, user_id: int) -> int:
        state = self._queues.get(user_id)
        return state.queue.qsize() if state else 0

    def total_queued(self) -> int:
        return sum(s.queue.qsize() for s in self._queues.values())

    async def drop_user(self, user_id: int) -> None:
        """Forget everything queued for a user (they blocked the bot or lost access)."""
        state = self._queues.pop(user_id, None)
        if state is None:
            return
        if state.worker is not None:
            state.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state.worker
        while not state.queue.empty():
            state.queue.get_nowait()

    async def wait_idle(self, timeout: float = 5.0) -> bool:
        """Wait until every queue is empty and its worker finished the item it was holding."""

        async def join_all() -> None:
            while True:
                pending = [s.queue.join() for s in list(self._queues.values())]
                if not pending:
                    return
                await asyncio.gather(*pending)
                if all(s.queue.empty() for s in self._queues.values()):
                    return

        try:
            await asyncio.wait_for(join_all(), timeout)
        except TimeoutError:
            return False
        return True

    async def drain(self, timeout: float = 15.0) -> int:
        """Give the workers a bounded chance to finish; returns how many items were left."""
        self._stopping = True
        if not await self.wait_idle(timeout):
            log.warning("shutdown: %d notifications left in queues", self.total_queued())
        left = self.total_queued()
        for state in self._queues.values():
            if state.worker is not None:
                state.worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await state.worker
        return left

    # -- internals -------------------------------------------------------------------------

    def _queue_for(self, user_id: int) -> _UserQueue:
        state = self._queues.get(user_id)
        if state is None:
            state = _UserQueue(queue=asyncio.Queue(maxsize=QUEUE_MAXSIZE))
            self._queues[user_id] = state
            self._user_buckets[user_id] = TokenBucket(
                USER_RATE, monotonic=self._monotonic, sleep=self._sleep
            )
        if state.worker is None or state.worker.done():
            if state.worker is not None and state.worker.done():
                self.metrics.worker_restarts += 1
            state.worker = asyncio.create_task(
                self._run_worker(user_id, state), name=f"delivery-{user_id}"
            )
        return state

    async def _run_worker(self, user_id: int, state: _UserQueue) -> None:
        """The worker loop. No exception may end it — that would silently stop notifications."""
        while True:
            item = await state.queue.get()
            try:
                await self._process(user_id, state, item)
            except asyncio.CancelledError:
                state.queue.task_done()
                raise
            except Exception:
                log.exception("delivery failed for %s; item marked failed", item.key)
                self.metrics.failed += 1
                with contextlib.suppress(Exception):
                    await self._storage.set_delivery_status(
                        *item.key, DeliveryStatus.FAILED, error="unhandled"
                    )
            finally:
                state.enqueued_at.pop(item.key, None)
                state.queue.task_done()

    async def _process(self, user_id: int, state: _UserQueue, item: OutboxItem) -> None:
        user = await self._storage.get_user(user_id)
        if user is None or not user.active or user.paused:
            self.metrics.dropped_inactive += 1
            return
        lang = user.lang or DEFAULT_LANG
        tz = user.tz or "UTC"

        batch = self._collect_digest(state, item, user.digest_threshold or self._default_threshold)
        if batch is not None:
            entries = [DigestEntry(Payload.from_json(i.payload), i.key) for i in batch]
            overflow, state.overflow = state.overflow, 0
            rendered = render_digest(
                entries, lang=lang, tz=tz, now=self._clock(), overflow=overflow
            )
            sent = await self._send(user_id, rendered, buttons=None, item=batch[0])
            if sent:
                await self._storage.mark_digested([i.key for i in batch])
                self.metrics.digested += len(batch)
            return

        payload = Payload.from_json(item.payload)
        rendered = render_notification(payload, lang=lang, tz=tz, now=self._clock())
        buttons = self._buttons(item, lang) if self._buttons else None
        if await self._send(user_id, rendered, buttons=buttons, item=item):
            await self._storage.set_delivery_status(*item.key, DeliveryStatus.SENT)
            self.metrics.delivered += 1

    def _collect_digest(
        self, state: _UserQueue, item: OutboxItem, threshold: int
    ) -> list[OutboxItem] | None:
        """Decide whether ``item`` starts a digest and, if so, drain the queue into it."""
        if threshold <= 0:
            return None
        backlog = state.queue.qsize() + 1  # the item we just popped counts too
        oldest = min(state.enqueued_at.values(), default=None)
        aged = (
            oldest is not None
            and backlog >= DIGEST_MIN_QUEUE
            and self._clock() - oldest >= DIGEST_AGE
        )
        if backlog < threshold and not aged:
            return None
        batch = [item]
        while len(batch) < DIGEST_MAX_ITEMS and not state.queue.empty():
            extra = state.queue.get_nowait()
            state.enqueued_at.pop(extra.key, None)
            state.queue.task_done()
            batch.append(extra)
        return batch

    # -- sending ---------------------------------------------------------------------------

    async def _send(
        self, user_id: int, rendered: Rendered, *, buttons: Any, item: OutboxItem
    ) -> bool:
        attempts = item.attempts
        degraded = False
        while True:
            await self._wait_flood_gate()
            await self._global_bucket.acquire()
            bucket = self._user_buckets.get(user_id)
            if bucket is not None:
                await bucket.acquire()
            attempts += 1
            try:
                await self._bot.send_message(
                    user_id,
                    rendered.text,
                    formatting_entities=None if degraded else rendered.entities,
                    buttons=None if degraded else buttons,
                    link_preview=False,
                )
            except asyncio.CancelledError:
                raise
            except errors.FloodWaitError as exc:
                self._open_flood_gate(exc.seconds, user_id)
                attempts -= 1  # a flood wait is not the user's fault; do not burn an attempt
                continue
            except PAUSE_ERRORS as exc:
                log.info("pausing delivery for %s: %s", user_id, type(exc).__name__)
                await self._storage.pause_delivery(user_id, type(exc).__name__)
                await self._storage.set_delivery_status(
                    *item.key, DeliveryStatus.FAILED, error=type(exc).__name__, attempts=attempts
                )
                await self.drop_user(user_id)
                self.metrics.failed += 1
                return False
            except errors.BadRequestError as exc:
                if not degraded and _is_rendering_error(exc):
                    degraded = True
                    self.metrics.degraded_sends += 1
                    log.warning("retrying plain-text after %s for %s", type(exc).__name__, item.key)
                    continue
                await self._fail(item, exc, attempts)
                return False
            except (*RETRYABLE, ValueError) as exc:
                if isinstance(exc, ValueError) or attempts >= MAX_ATTEMPTS:
                    await self._fail(item, exc, attempts)
                    return False
                delay = min(BACKOFF_CAP, BACKOFF_BASE * 2 ** (attempts - 1))
                log.warning(
                    "send to %s failed (%s); retry %d in %.0fs",
                    user_id,
                    type(exc).__name__,
                    attempts,
                    delay,
                )
                await self._sleep(delay)
                continue
            return True

    async def _fail(self, item: OutboxItem, exc: BaseException, attempts: int) -> None:
        log.error("giving up on %s: %s", item.key, type(exc).__name__)
        self.metrics.failed += 1
        await self._storage.set_delivery_status(
            *item.key, DeliveryStatus.FAILED, error=type(exc).__name__, attempts=attempts
        )

    def _open_flood_gate(self, seconds: float, user_id: int) -> None:
        until = self._monotonic() + seconds + 1
        if until > self._flood_until:
            self._flood_until = until
            self.metrics.flood_waits += 1
            self.metrics.flood_seconds += seconds
            log.warning(
                "flood wait %.0fs (triggered by user %s); all workers paused", seconds, user_id
            )

    async def _wait_flood_gate(self) -> None:
        while True:
            remaining = self._flood_until - self._monotonic()
            if remaining <= 0:
                return
            await self._sleep(remaining)


def _is_rendering_error(exc: errors.BadRequestError) -> bool:
    """Errors that a plain-text resend without entities or buttons can survive."""
    return isinstance(
        exc,
        (
            errors.EntitiesTooLongError,
            errors.EntityBoundsInvalidError,
            errors.ButtonUrlInvalidError,
            errors.ButtonDataInvalidError,
            errors.ButtonUserPrivacyRestrictedError,
        ),
    )

**Identifying sender resolver defects and notification issues**
**Analyzing catchup cursor exception handling and access hash usage**
exec
/bin/zsh -lc "sed -n '1,280p' ddiver/runtime.py && sed -n '281,620p' ddiver/runtime.py" in /Users/L/Code/ddiver
 succeeded in 0ms:
"""The only module that talks to Telethon at runtime: adapters plus the assembled application.

Everything below the adapters (matcher, listener, catch-up, delivery, bot UI, supervisor) is
plain objects with injected dependencies and is covered by offline tests; this module wires
them to real clients, registers the Telethon event handlers and runs the background tasks.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from pathlib import Path
from typing import Any

from telethon import Button, TelegramClient, errors, events, utils
from telethon.tl import types as tl
from telethon.tl.functions.updates import GetStateRequest

from ddiver.app import (
    EXIT_OK,
    PeriodicTask,
    Supervisor,
    boot_delay,
    format_uptime,
)
from ddiver.bot import menus
from ddiver.bot.handlers import StatusInfo, build_router
from ddiver.bot.router import Edit, Incoming, Reply, Router, Send, Toast
from ddiver.catchup import Catchup, telethon_history
from ddiver.clients import connect_with_timeout, make_bot_client, make_user_client
from ddiver.config import Settings
from ddiver.delivery import Delivery
from ddiver.formatter import Payload
from ddiver.health import write_heartbeat
from ddiver.i18n import DEFAULT_LANG, t
from ddiver.links import ChatRef
from ddiver.listener import CURSOR_FLUSH_SECONDS, Listener
from ddiver.matcher import Matcher
from ddiver.senders import SenderCache
from ddiver.storage import OutboxItem, Rule, Storage, safe_write
from ddiver.timeutil import relative, utcnow

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 30.0
PING_SECONDS = 60.0
PING_TIMEOUT = 15.0
RECONCILE_SECONDS = 15 * 60
SUMMARY_SECONDS = 60.0
PRUNE_SECONDS = 24 * 3600
SILENCE_WARN = dt.timedelta(minutes=10)


class BotAdapter:
    """Turns :mod:`ddiver.bot.router` actions into Telethon calls."""

    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    @staticmethod
    def _buttons(rows: Any, lang: str) -> Any:
        if not rows:
            return None
        return [
            [
                Button.inline(
                    menus.unwrap(label) if menus.is_literal(label) else t(lang, label), data
                )
                for label, data in row
            ]
            for row in rows
        ]

    @staticmethod
    def _text(action: Any, lang: str) -> str:
        if action.literal:
            return str(action.key_or_text)
        return t(lang, action.key_or_text, **action.params)

    async def perform(self, event: Any, actions: list[Any], lang: str) -> None:
        for action in actions:
            try:
                await self._one(event, action, lang)
            except errors.QueryIdInvalidError:
                log.debug("callback answer arrived too late")
            except (errors.RPCError, ValueError):
                log.warning("could not perform %s", type(action).__name__, exc_info=True)

    async def _one(self, event: Any, action: Any, lang: str) -> None:
        if isinstance(action, Toast):
            if hasattr(event, "answer"):
                await event.answer(self._text(action, lang), alert=action.alert)
            return
        if isinstance(action, Edit):
            if hasattr(event, "edit"):
                with contextlib.suppress(errors.MessageNotModifiedError):
                    await event.edit(
                        self._text(action, lang),
                        buttons=self._buttons(action.buttons, lang),
                        link_preview=False,
                    )
                return
            action = Reply(action.key_or_text, action.params, action.buttons, action.literal)
        if isinstance(action, Reply):
            await self._client.send_message(
                await self._peer(event),
                self._text(action, lang),
                buttons=self._buttons(action.buttons, lang),
                link_preview=False,
            )
            return
        if isinstance(action, Send):
            await self._client.send_message(
                action.user_id,
                self._text(action, lang),
                buttons=self._buttons(action.buttons, lang),
                link_preview=False,
            )

    @staticmethod
    async def _peer(event: Any) -> int:
        return int(event.sender_id)

    async def send(
        self,
        user_id: int,
        text: str,
        *,
        formatting_entities: list[tl.TypeMessageEntity] | None = None,
        buttons: Any = None,
        link_preview: bool = False,
    ) -> Any:
        return await self._client.send_message(
            user_id,
            text,
            formatting_entities=formatting_entities,
            buttons=buttons,
            link_preview=link_preview,
        )

    # Delivery's BotPort protocol
    send_message = send


def notification_buttons_factory(lang_of: Any) -> Any:
    """Build the inline keyboard under a notification from its outbox row."""

    def factory(item: OutboxItem, lang: str) -> Any:
        payload = item.payload
        rows = menus.notification_buttons(item.chat_id, item.msg_id, payload.get("sender_id"))
        return [[Button.inline(t(lang, label), data) for label, data in row] for row in rows]

    del lang_of
    return factory


class Application:
    """Everything wired together, with a lifecycle the CLI drives."""

    def __init__(self, settings: Settings, *, clock: Any = utcnow) -> None:
        self.settings = settings
        self.clock = clock
        self.storage = Storage(settings.db_path, owner_id=settings.owner_id, clock=clock)
        self.user_client = make_user_client(settings)
        self.bot_client = make_bot_client(settings)
        self.bot = BotAdapter(self.bot_client)
        self.matcher: Matcher | None = None
        self.listener: Listener | None = None
        self.delivery: Delivery | None = None
        self.catchup: Catchup | None = None
        self.router: Router | None = None
        self.supervisor: Supervisor | None = None
        self.senders = SenderCache(self._resolve_entity, clock=clock)
        self._tasks: list[PeriodicTask] = []
        self._last_ping_ok: dt.datetime | None = None
        self._catchup_lock = asyncio.Lock()

    # -- lifecycle -------------------------------------------------------------------------

    async def setup(self) -> None:
        await self.storage.open()
        await self.storage.ensure_user(self.settings.owner_id)

        async def on_rule_disabled(rule: Rule, reason: str) -> None:
            user = await self.storage.get_user(rule.user_id)
            lang = (user.lang if user else None) or DEFAULT_LANG
            await self._tell(
                rule.user_id, t(lang, "bot.rules.disabled", pattern=rule.pattern, reason=reason)
            )

        self.matcher = Matcher(
            self.storage,
            timeout=self.settings.regex_timeout,
            on_rule_disabled=on_rule_disabled,
        )
        await self.matcher.reload()

        self.delivery = Delivery(
            self.bot,
            self.storage,
            clock=self.clock,
            buttons=notification_buttons_factory(None),
            default_threshold=self.settings.digest_threshold,
        )
        self.listener = Listener(
            self.storage,
            self.matcher,
            self.senders,
            deliver=self.delivery.enqueue,
            clock=self.clock,
            ignore_own=self.settings.ignore_own_messages,
        )
        await self.listener.prime_chat_cache()
        self.catchup = Catchup(
            self.storage,
            self.listener,
            telethon_history(self.user_client),
            clock=self.clock,
            hours=self.settings.catchup_hours,
            per_chat=self.settings.catchup_max_per_chat,
            on_unavailable=self._notify_lost_access,
        )
        self.router = build_router(
            self.storage,
            matcher=self.matcher,
            status=self._status,
            regex_timeout=self.settings.regex_timeout,
            clock=self.clock,
        )
        self.supervisor = Supervisor(
            run_listener=self._run_listener,
            notify_owner=self._notify_degraded,
            clock=self.clock,
        )
        self._register_handlers()

    async def close(self) -> None:
        for task in self._tasks:
            await task.stop()
        with contextlib.suppress(Exception):
            await self.user_client.disconnect()
        if self.listener is not None:
            await self.listener.cursors.flush()
        if self.matcher is not None:
            await self.matcher.flush_hits()
        if self.delivery is not None:
            left = await self.delivery.drain()
            if left:
                log.warning("%d notifications stay pending for the next start", left)
        with contextlib.suppress(Exception):
            await self.bot_client.disconnect()
        await self.storage.close()

    async def run(self) -> int:
        assert self.supervisor is not None  # noqa: S101 - set up in setup()
        await connect_with_timeout(self.bot_client)
        await self.bot_client.start(bot_token=self.settings.bot_token.get_secret_value())
        await connect_with_timeout(self.user_client)
        if not await self.user_client.is_user_authorized():
            log.error("the user session is not authorized; run `ddiver login`")
            return 3
        boots = await self.storage.record_boot()
        assert self.delivery is not None  # noqa: S101
        await self.delivery.replay_pending()
        self._start_background_tasks()
        await boot_delay(boots)
        asyncio.create_task(self._catch_up(), name="catchup-boot")  # noqa: RUF006
        return await self.supervisor.run()

    def request_stop(self) -> None:
        if self.supervisor is not None:
            self.supervisor.request_stop()
        with contextlib.suppress(Exception):
            self.user_client.disconnect()

    # -- handlers --------------------------------------------------------------------------

    def _register_handlers(self) -> None:
        listener = self.listener
        assert listener is not None  # noqa: S101

        @self.user_client.on(events.NewMessage())
        async def on_message(event: Any) -> None:
            await listener.on_message(event)

        @self.user_client.on(events.Raw(tl.UpdateChannel))
        async def on_channel_update(update: Any) -> None:
            await self._check_channel_access(update)

        @self.user_client.on(events.ChatAction())
        async def on_chat_action(event: Any) -> None:
            await self._on_chat_action(event)

        @self.bot_client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
        async def on_bot_message(event: Any) -> None:
            await self._on_bot_update(event, text=event.raw_text)

        @self.bot_client.on(events.CallbackQuery(func=lambda e: e.is_private))
        async def on_bot_callback(event: Any) -> None:
            data = event.data.decode("utf-8", "replace") if event.data else ""
            await self._on_bot_update(event, data=data)

    async def _on_bot_update(
        self, event: Any, *, text: str | None = None, data: str | None = None
    ) -> None:
        assert self.router is not None  # noqa: S101
        try:
            sender = await event.get_sender()
            incoming = Incoming(
                user_id=int(event.sender_id),
                text=text,
                data=data,
                msg_id=getattr(event, "message_id", None),
                lang_code=getattr(sender, "lang_code", None),
                is_private=True,
            )
            actions = await self.router.dispatch(incoming)
            user = await self.storage.get_user(incoming.user_id)
            lang = (user.lang if user else None) or DEFAULT_LANG
            await self.bot.perform(event, actions, lang)
        except Exception:
            log.exception("bot update failed")

    async def _on_chat_action(self, event: Any) -> None:
        try:
            me = await self.user_client.get_me()
            if event.user_id != me.id:
                return
            chat_id = int(utils.get_peer_id(event.action_message.peer_id))
            if event.user_kicked or event.user_left:
                if await self.storage.set_chat_available(chat_id, False):
                    await self._notify_lost_access(chat_id, "left")
                await self._invalidate(chat_id)
            elif event.user_added or event.user_joined:
                if await self.storage.set_chat_available(chat_id, True):
                    await self._notify_regained_access(chat_id)
                await self._invalidate(chat_id)
        except Exception:
            log.debug("chat action handling failed", exc_info=True)

    async def _check_channel_access(self, update: Any) -> None:
        """``UpdateChannel`` is what Telegram sends when our membership changed."""
        try:
            bare = int(getattr(update, "channel_id", 0))
            if not bare:
                return
            chat_id = int(utils.get_peer_id(tl.PeerChannel(bare)))
            entity = (getattr(update, "_entities", None) or {}).get(chat_id)
            if entity is None:
                return
            lost = isinstance(entity, tl.ChannelForbidden) or bool(getattr(entity, "left", False))
            if lost:
                if await self.storage.set_chat_available(chat_id, False):
                    await self._notify_lost_access(chat_id, type(entity).__name__)
            elif await self.storage.set_chat_available(chat_id, True):
                await self._notify_regained_access(chat_id)
            await self._invalidate(chat_id)
        except Exception:
            log.debug("channel update handling failed", exc_info=True)

    async def _invalidate(self, chat_id: int | None = None) -> None:
        if self.matcher is not None:
            await self.matcher.invalidate(chat_id)

    # -- background tasks ------------------------------------------------------------------

    def _start_background_tasks(self) -> None:
        listener = self.listener
        assert listener is not None  # noqa: S101
        self._tasks = [
            PeriodicTask("cursor-flush", self._flush_state, CURSOR_FLUSH_SECONDS),
            PeriodicTask("heartbeat", self._heartbeat, HEARTBEAT_SECONDS),
            PeriodicTask("ping", self._ping, PING_SECONDS),
            PeriodicTask("reconcile", self._reconcile, RECONCILE_SECONDS),
            PeriodicTask("summary", self._log_summary, SUMMARY_SECONDS),
            PeriodicTask("prune", self._prune, PRUNE_SECONDS),
        ]
        for task in self._tasks:
            task.start()

    async def _flush_state(self) -> None:
        if self.listener is not None:
            await self.listener.cursors.flush()
        if self.matcher is not None:
            await safe_write(self.matcher.flush_hits(), "rule hits")

    async def _heartbeat(self) -> None:
        write_heartbeat(self.settings.heartbeat_path, self.clock())

    async def _ping(self) -> None:
        try:
            async with asyncio.timeout(PING_TIMEOUT):
                await self.user_client(GetStateRequest())
            self._last_ping_ok = self.clock()
        except (TimeoutError, errors.RPCError, ConnectionError):
            log.warning("listener ping failed")

    async def _reconcile(self) -> None:
        """Telethon can silently drop a channel gap; re-check the tail of every chat."""
        await self._catch_up()

    async def _catch_up(self) -> None:
        if self.catchup is None:
            return
        async with self._catchup_lock:
            try:
                result = await self.catchup.run()
            except Exception:
                log.exception("catch-up round failed")
                return
        if result.chats:
            log.info(
                "catch-up: %d chats, %d messages, %d notifications",
                result.chats,
                result.messages,
                result.claimed,
            )

    async def _log_summary(self) -> None:
        if self.listener is None or self.delivery is None:
            return
        metrics = self.listener.metrics
        delivery = self.delivery.metrics
        log.info(
            "seen=%d matched=%d claimed=%d queued=%d delivered=%d digested=%d failed=%d "
            "floodwait_s=%.0f",
            metrics.seen,
            metrics.matched,
            metrics.claimed,
            self.delivery.total_queued(),
            delivery.delivered,
            delivery.digested,
            delivery.failed,
            delivery.flood_seconds,
        )
        last = self.listener.last_update_at
        if last is not None and self.clock() - last > SILENCE_WARN:
            log.warning("no updates for over 10 minutes; forcing a catch-up")
            with contextlib.suppress(Exception):
                await self.user_client.catch_up()
            await self._catch_up()

    async def _prune(self) -> None:
        removed = await self.storage.prune_outbox()
        if removed:
            log.info("pruned %d old outbox rows", removed)

    async def _run_listener(self) -> None:
        if self.user_client.reconnected.is_set():
            self.user_client.reconnected.clear()
            await self._catch_up()
        await self.user_client.run_until_disconnected()

    # -- helpers ---------------------------------------------------------------------------

    async def _resolve_entity(self, peer_id: int) -> Any:
        return await self.user_client.get_entity(peer_id)

    async def _tell(self, user_id: int, text: str) -> None:
        with contextlib.suppress(errors.RPCError, ValueError, ConnectionError):
            await self.bot.send(user_id, text)

    async def _notify_lost_access(self, chat_id: int, reason: str) -> None:
        log.warning("lost access to chat %s (%s)", chat_id, reason)
        await self._broadcast(chat_id, "bot.chats.lost_access")

    async def _notify_regained_access(self, chat_id: int) -> None:
        await self._broadcast(chat_id, "bot.chats.regained_access")

    async def _broadcast(self, chat_id: int, key: str) -> None:
        chat = await self.storage.get_chat(chat_id)
        title = chat.ref.title if chat else str(chat_id)
        for user_id in await self.storage.subscribers_of(chat_id):
            user = await self.storage.get_user(user_id)
            lang = (user.lang if user else None) or DEFAULT_LANG
            await self._tell(user_id, t(lang, key, chat=title))

    async def _notify_degraded(self, reason: str) -> None:
        user = await self.storage.get_user(self.settings.owner_id)
        lang = (user.lang if user else None) or DEFAULT_LANG
        await self._tell(
            self.settings.owner_id,
            f"{t(lang, 'bot.status.listener_degraded')} ({reason})",
        )

    async def _status(self) -> StatusInfo:
        chats = await self.storage.list_chats()
        listener = self.listener
        delivery = self.delivery
        supervisor = self.supervisor
        now = self.clock()
        last_update = "—"
        if listener is not None and listener.last_update_at is not None:
            last_update = relative(listener.last_update_at, now, DEFAULT_LANG)
        return StatusInfo(
            listener_ok=not (supervisor.state.degraded if supervisor else False),
            uptime=format_uptime(supervisor.state.started_at if supervisor else None, now),
            monitored=len(self.matcher.monitored) if self.matcher else 0,
            unavailable=sum(1 for c in chats if not c.available),
            seen=listener.metrics.seen if listener else 0,
            matched=listener.metrics.matched if listener else 0,
            delivered=delivery.metrics.delivered if delivery else 0,
            digested=delivery.metrics.digested if delivery else 0,
            failed=delivery.metrics.failed if delivery else 0,
            queue=delivery.total_queued() if delivery else 0,
            last_update=last_update,
        )


async def refresh_dialogs(client: TelegramClient, storage: Storage) -> int:
    """Load the account's group-like dialogs so catch-up can build input peers offline."""
    count = 0
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, tl.User):
            continue
        ref = ChatRef.from_entity(entity)
        await storage.upsert_chat(ref, access_hash=getattr(entity, "access_hash", None))
        count += 1
    with contextlib.suppress(Exception):
        client.session.save()  # commit entities now, not in a minute
    return count


def payload_of(item: OutboxItem) -> Payload:
    return Payload.from_json(item.payload)


__all__ = [
    "EXIT_OK",
    "Application",
    "BotAdapter",
    "Path",
    "payload_of",
    "refresh_dialogs",
]

**Identifying reconnect catch-up delay**
**Analyzing cursor handling in catch-up**
exec
/bin/zsh -lc "sed -n '1,240p' ddiver/clients.py && sed -n '1,280p' ddiver/app.py && sed -n '1,300p' tests/test_runtime.py && sed -n '1,290p' tests/test_app.py" in /Users/L/Code/ddiver
 succeeded in 0ms:
"""Telethon client construction, connection handling and fatal-error classification."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any

from telethon import TelegramClient, errors
from telethon.errors import common as common_errors

from ddiver.config import Settings

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 30.0
USER_FLOOD_THRESHOLD = 60  # catch-up wants Telethon's transparent sleep-and-retry
BOT_FLOOD_THRESHOLD = 0  # delivery owns the retry policy, so it must see the error
ENTITY_CACHE_LIMIT = 20_000


class ListenerClient(TelegramClient):  # type: ignore[misc]
    """Telethon client that tells us when it reconnected.

    Telethon requests ``getChannelDifference`` with a limit of 100 for user accounts and, on
    ``ChannelDifferenceTooLong``, jumps ``pts`` and returns no updates at all. In a busy chat
    that silently loses everything beyond 100 messages per gap, so after every reconnect we
    re-run the cursor-based catch-up.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.reconnected = asyncio.Event()
        self.reconnects = 0

    async def _handle_auto_reconnect(self) -> None:
        try:
            await super()._handle_auto_reconnect()
        except Exception:  # the callback runs detached; an exception here would vanish
            log.warning("auto-reconnect handler failed", exc_info=True)
        self.reconnects += 1
        self.reconnected.set()
        log.info("listener reconnected (%d since start); scheduling a catch-up", self.reconnects)


def is_fatal_auth_error(exc: BaseException) -> bool:
    """Errors that mean the session is dead and only ``ddiver login`` can fix it.

    Classified by base class rather than by a list of names: ``UnauthorizedError`` (401) covers
    session revoked/expired and auth key unregistered, ``AuthKeyError`` (406) covers the
    duplicated key you get when two processes share one session file, and ``AuthKeyNotFound``
    is a plain ``Exception`` raised by the transport, not an ``RPCError``.
    """
    if isinstance(exc, errors.SessionPasswordNeededError):
        return False  # 401, but it only means "2FA password required" during login
    return isinstance(
        exc,
        (
            errors.UnauthorizedError,
            errors.AuthKeyError,
            common_errors.AuthKeyNotFound,
            errors.PhoneNumberBannedError,
        ),
    )


def is_session_file_error(exc: BaseException) -> bool:
    """A locked or corrupt Telethon session file — usually a second instance."""
    return isinstance(exc, sqlite3.OperationalError | sqlite3.DatabaseError)


async def connect_with_timeout(client: TelegramClient, timeout: float = CONNECT_TIMEOUT) -> None:
    """``connect()`` can hang forever (telethon#1536); on timeout tear the half-open sender down."""
    try:
        async with asyncio.timeout(timeout):
            await client.connect()
    except TimeoutError:
        log.warning("connect() timed out after %.0fs; disconnecting before retrying", timeout)
        try:
            await client.disconnect()
        except Exception:
            log.debug("disconnect after a failed connect also failed", exc_info=True)
        raise


def make_user_client(settings: Settings, *, session: str | Path | None = None) -> ListenerClient:
    return ListenerClient(
        str(session or settings.user_session),
        settings.api_id,
        settings.api_hash.get_secret_value(),
        flood_sleep_threshold=USER_FLOOD_THRESHOLD,
        connection_retries=None,
        retry_delay=5,
        auto_reconnect=True,
        catch_up=False,
        sequential_updates=False,
        receive_updates=True,
        entity_cache_limit=ENTITY_CACHE_LIMIT,
        request_retries=5,
    )


def make_bot_client(settings: Settings, *, session: str | Path | None = None) -> TelegramClient:
    return TelegramClient(
        str(session or settings.bot_session),
        settings.api_id,
        settings.api_hash.get_secret_value(),
        flood_sleep_threshold=BOT_FLOOD_THRESHOLD,
        raise_last_call_error=True,
        request_retries=3,
        connection_retries=None,
        retry_delay=5,
        auto_reconnect=True,
        receive_updates=True,
    )
"""Wiring and the supervisor loop.

The supervisor is a plain object with injected clients, clock, sleep and rng, and it *returns*
an exit code instead of calling ``sys.exit`` — so the whole restart/degraded-mode policy is
unit-testable without a Telegram connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ddiver.clients import is_fatal_auth_error, is_session_file_error
from ddiver.timeutil import Clock, utcnow

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_AUTHORIZED = 3
EXIT_LOCKED = 4
EXIT_DB_CORRUPT = 5

BACKOFF_BASE = 2.0
BACKOFF_CAP = 300.0
OWNER_REMINDER = dt.timedelta(hours=6)
BOOT_STORM_BOOTS = 3
BOOT_STORM_DELAY = 300.0

Sleeper = Callable[[float], Awaitable[None]]


@dataclass(slots=True)
class SupervisorState:
    """What ``/status`` and the tests look at."""

    started_at: dt.datetime | None = None
    degraded: bool = False
    degraded_reason: str | None = None
    attempts: int = 0
    reconnects: int = 0
    owner_notified_at: dt.datetime | None = None
    backoffs: list[float] = field(default_factory=list)


class Supervisor:
    """Keeps the listener alive, or parks it in degraded mode when only a re-login can help.

    Degraded mode matters because the container policy is ``restart: unless-stopped``, which
    ignores exit codes: exiting on a dead session would produce an infinite restart loop that
    re-notifies the owner and re-runs catch-up every few seconds.
    """

    def __init__(
        self,
        *,
        run_listener: Callable[[], Awaitable[None]],
        notify_owner: Callable[[str], Awaitable[None]] | None = None,
        clock: Clock = utcnow,
        sleep: Sleeper = asyncio.sleep,
        rng: random.Random | None = None,
        max_attempts: int | None = None,
    ) -> None:
        self._run_listener = run_listener
        self._notify_owner = notify_owner
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random(0)  # jitter for reconnects, not cryptography
        self._max_attempts = max_attempts
        self.state = SupervisorState()
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    async def run(self) -> int:
        self.state.started_at = self._clock()
        while not self._stop.is_set():
            self.state.attempts += 1
            try:
                await self._run_listener()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if is_fatal_auth_error(exc):
                    await self._enter_degraded(type(exc).__name__)
                    return EXIT_OK
                if is_session_file_error(exc):
                    await self._enter_degraded(f"session file: {exc}")
                    return EXIT_OK
                log.warning("listener stopped with %s", type(exc).__name__, exc_info=True)
            else:
                if self._stop.is_set():
                    break
                log.info("listener disconnected; reconnecting")
            if self._max_attempts is not None and self.state.attempts >= self._max_attempts:
                return EXIT_ERROR
            if self._stop.is_set():
                break
            await self._backoff()
        return EXIT_OK

    async def _backoff(self) -> None:
        delay = min(BACKOFF_CAP, BACKOFF_BASE * 2 ** (self.state.attempts - 1))
        delay += self._rng.uniform(0, delay / 4)
        self.state.backoffs.append(delay)
        log.info("reconnecting in %.0fs", delay)
        await self._sleep(delay)

    async def _enter_degraded(self, reason: str) -> None:
        self.state.degraded = True
        self.state.degraded_reason = reason
        log.error("listener needs a new login (%s); entering degraded mode", reason)
        await self.notify_owner_if_due(force=True)

    async def notify_owner_if_due(self, *, force: bool = False) -> bool:
        """Tell the owner about degraded mode, at most once every six hours."""
        if not self.state.degraded or self._notify_owner is None:
            return False
        now = self._clock()
        last = self.state.owner_notified_at
        if not force and last is not None and now - last < OWNER_REMINDER:
            return False
        self.state.owner_notified_at = now
        with contextlib.suppress(Exception):
            await self._notify_owner(self.state.degraded_reason or "unknown")
        return True


async def boot_delay(boots_recently: int, sleep: Sleeper = asyncio.sleep) -> float:
    """Delay catch-up when the process is crash-looping, so it stops hammering GetHistory."""
    if boots_recently < BOOT_STORM_BOOTS:
        return 0.0
    log.warning("%d boots in the last 10 minutes; delaying catch-up", boots_recently)
    await sleep(BOOT_STORM_DELAY)
    return BOOT_STORM_DELAY


def format_uptime(started_at: dt.datetime | None, now: dt.datetime) -> str:
    if started_at is None:
        return "—"
    seconds = max(0, int((now - started_at).total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


class PeriodicTask:
    """A cancellable ``while True: await work(); await sleep(interval)`` loop that never dies."""

    def __init__(
        self,
        name: str,
        work: Callable[[], Awaitable[Any]],
        interval: float,
        *,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        self._name = name
        self._work = work
        self._interval = interval
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self.runs = 0
        self.failures = 0

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name=self._name)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self._work()
                self.runs += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self.failures += 1
                log.exception("periodic task %s failed", self._name)
            await self._sleep(self._interval)
"""Tests for the Telethon adapter layer, driven by fake clients (no connection)."""

from __future__ import annotations

from typing import Any

import pytest
from telethon import errors
from telethon.tl import types as tl

from ddiver.bot import menus
from ddiver.bot.router import Edit, Reply, Send, Toast
from ddiver.links import ChatRef
from ddiver.runtime import BotAdapter, notification_buttons_factory, payload_of, refresh_dialogs
from ddiver.storage import OutboxItem, Storage
from tests.fakes import T0, make_channel, make_user

PAYLOAD = {
    "chat_id": -1001,
    "chat_title": "Чат",
    "msg_id": 7,
    "date": T0.isoformat(),
    "text": "казино",
    "spans": [[0, 6]],
    "rule_pattern": "казино",
    "sender_id": 42,
}


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.error: BaseException | None = None

    async def send_message(self, peer: Any, text: str, **kwargs: Any) -> None:
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        self.sent.append({"peer": peer, "text": text, **kwargs})


class FakeEvent:
    """A callback-query-like event: it can answer and edit."""

    def __init__(self, sender_id: int = 1) -> None:
        self.sender_id = sender_id
        self.answers: list[tuple[str, bool]] = []
        self.edits: list[str] = []
        self.error: BaseException | None = None

    async def answer(self, text: str = "", *, alert: bool = False) -> None:
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        self.answers.append((text, alert))

    async def edit(self, text: str, **_kwargs: Any) -> None:
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        self.edits.append(text)


class FakeMessageEvent:
    """A plain message event: it can neither answer nor edit."""

    def __init__(self, sender_id: int = 1) -> None:
        self.sender_id = sender_id


class TestBotAdapter:
    async def test_toast_is_answered(self):
        bot, event = FakeBot(), FakeEvent()
        await BotAdapter(bot).perform(event, [Toast("bot.cancelled", alert=True)], "ru")
        assert event.answers == [("Отменено.", True)]
        assert bot.sent == []

    async def test_edit_uses_the_event_and_renders_buttons(self):
        bot, event = FakeBot(), FakeEvent()
        actions = [Edit("bot.menu.title", buttons=[[("bot.menu.chats", "chats:0")]])]
        await BotAdapter(bot).perform(event, actions, "ru")
        assert event.edits == ["Что делаем?"]

    async def test_edit_falls_back_to_a_reply_when_the_event_cannot_edit(self):
        bot, event = FakeBot(), FakeMessageEvent(sender_id=5)
        await BotAdapter(bot).perform(event, [Edit("bot.menu.title")], "en")
        assert bot.sent[0]["peer"] == 5
        assert bot.sent[0]["text"] == "What would you like to do?"

    async def test_reply_goes_to_the_sender_with_no_link_preview(self):
        bot, event = FakeBot(), FakeEvent(sender_id=9)
        await BotAdapter(bot).perform(event, [Reply("bot.help")], "uk")
        assert bot.sent[0]["peer"] == 9
        assert bot.sent[0]["link_preview"] is False
        assert "Синтаксис правил" in bot.sent[0]["text"]

    async def test_send_targets_another_user(self):
        bot, event = FakeBot(), FakeEvent()
        actions = [
            Send(
                777,
                "bot.gate.card",
                {"user_id": 3},
                buttons=[[("bot.btn.allow", "allow:3")]],
            )
        ]
        await BotAdapter(bot).perform(event, actions, "en")
        assert bot.sent[0]["peer"] == 777
        assert "3" in bot.sent[0]["text"]
        assert bot.sent[0]["buttons"][0][0].text == "✅ Allow"

    async def test_literal_text_is_not_translated(self):
        bot, event = FakeBot(), FakeEvent()
        await BotAdapter(bot).perform(event, [Reply("Готово: 5", literal=True)], "ru")
        assert bot.sent[0]["text"] == "Готово: 5"

    async def test_literal_button_labels_are_kept(self):
        bot, event = FakeBot(), FakeEvent()
        actions = [Reply("x", literal=True, buttons=[[(menus.literal("Мой чат"), "sub:1:1")]])]
        await BotAdapter(bot).perform(event, actions, "ru")
        assert bot.sent[0]["buttons"][0][0].text == "Мой чат"

    async def test_late_callback_answers_are_ignored(self, caplog):
        bot, event = FakeBot(), FakeEvent()
        event.error = errors.QueryIdInvalidError(request=None)
        await BotAdapter(bot).perform(event, [Toast("bot.cancelled")], "en")
        assert event.answers == []  # no exception escaped

    async def test_rpc_errors_are_logged_not_raised(self, caplog):
        bot, event = FakeBot(), FakeEvent()
        bot.error = errors.UserIsBlockedError(request=None)
        await BotAdapter(bot).perform(event, [Reply("bot.help")], "en")
        assert "could not perform" in caplog.text

    async def test_message_not_modified_is_silent(self):
        bot, event = FakeBot(), FakeEvent()
        event.error = errors.MessageNotModifiedError(request=None)
        await BotAdapter(bot).perform(event, [Edit("bot.menu.title")], "en")
        assert event.edits == []

    async def test_send_message_port_passes_entities(self):
        bot = FakeBot()
        adapter = BotAdapter(bot)
        entities = [tl.MessageEntityBold(0, 2)]
        await adapter.send_message(5, "hi", formatting_entities=entities, buttons=None)
        assert bot.sent[0]["formatting_entities"] == entities
        assert bot.sent[0]["link_preview"] is False


class TestNotificationButtons:
    def test_both_buttons_when_the_sender_is_known(self):
        item = OutboxItem(1, -1001, 7, 3, "pending", 0, PAYLOAD, T0)  # type: ignore[arg-type]
        rows = notification_buttons_factory(None)(item, "ru")
        labels = [button.text for button in rows[0]]
        assert labels == ["🚫 Игнорировать отправителя", "➖ Добавить исключение"]
        data = [button.type.data.decode() for button in rows[0]]
        assert data == ["ig:-1001:42", "ex:-1001:7"]

    def test_only_exclude_when_the_sender_is_unknown(self):
        payload = {**PAYLOAD, "sender_id": None}
        item = OutboxItem(1, -1001, 7, 3, "pending", 0, payload, T0)  # type: ignore[arg-type]
        rows = notification_buttons_factory(None)(item, "en")
        assert [b.type.data.decode() for b in rows[0]] == ["ex:-1001:7"]

    def test_payload_of_parses_the_row(self):
        item = OutboxItem(1, -1001, 7, 3, "pending", 0, PAYLOAD, T0)  # type: ignore[arg-type]
        assert payload_of(item).chat_title == "Чат"


class FakeDialogClient:
    def __init__(self, entities: list[Any]) -> None:
        self._entities = entities
        self.saved = False

        class _Session:
            def __init__(self, outer: FakeDialogClient) -> None:
                self._outer = outer

            def save(self) -> None:
                self._outer.saved = True

        self.session = _Session(self)

    def iter_dialogs(self) -> Any:
        entities = self._entities

        async def gen() -> Any:
            for entity in entities:
                yield type("Dialog", (), {"entity": entity})()

        return gen()


class TestRefreshDialogs:
    async def test_stores_group_chats_and_skips_users(self, tmp_path):
        storage = Storage(tmp_path / "db.sqlite3", owner_id=1)
        await storage.open()
        try:
            channel = make_channel(-1001234567890, "Группа", username="grp")
            channel.access_hash = 4242
            client = FakeDialogClient([channel, make_user(5, "Человек", "human")])
            assert await refresh_dialogs(client, storage) == 1
            row = await storage.get_chat(-1001234567890)
            assert row is not None
            assert row.ref == ChatRef(-1001234567890, "Группа", "grp", "supergroup")
            assert row.access_hash == 4242
            assert client.saved is True  # entities committed immediately, not in a minute
        finally:
            await storage.close()

    async def test_survives_a_session_that_cannot_save(self, tmp_path):
        storage = Storage(tmp_path / "db.sqlite3", owner_id=1)
        await storage.open()
        try:
            client = FakeDialogClient([make_channel(-1002, "X")])

            def boom() -> None:
                raise RuntimeError("cannot save")

            client.session.save = boom  # type: ignore[method-assign]
            assert await refresh_dialogs(client, storage) == 1
        finally:
            await storage.close()


@pytest.mark.parametrize(
    ("keyboard", "expected_first"),
    [
        (menus.timezone_menu(), menus.literal("Europe/Kyiv")),
        (menus.settings_menu(), "bot.settings.language"),
        (menus.language_menu(), menus.literal("Українська")),
    ],
)
def test_static_menus(keyboard, expected_first):
    assert keyboard[0][0][0] == expected_first
import asyncio
import datetime as dt
import random
import sqlite3

import pytest
from telethon import errors
from telethon.errors import common as common_errors

from ddiver.app import (
    BOOT_STORM_DELAY,
    EXIT_ERROR,
    EXIT_OK,
    PeriodicTask,
    Supervisor,
    boot_delay,
    format_uptime,
)
from ddiver.clients import is_fatal_auth_error, is_session_file_error
from tests.fakes import T0, FakeClock


class Sleeps:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.tick(seconds)
        await asyncio.sleep(0)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def sleeps(clock):
    return Sleeps(clock)


def make_supervisor(runs, clock, sleeps, *, notify=None, max_attempts=5):
    calls = iter(runs)

    async def run_listener():
        outcome = next(calls)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return Supervisor(
        run_listener=run_listener,
        notify_owner=notify,
        clock=clock,
        sleep=sleeps,
        rng=random.Random(1),
        max_attempts=max_attempts,
    )


class TestFatalClassification:
    @pytest.mark.parametrize(
        "exc",
        [
            errors.SessionRevokedError(request=None),
            errors.AuthKeyUnregisteredError(request=None),
            errors.UserDeactivatedBanError(request=None),
            errors.AuthKeyDuplicatedError(request=None),  # two processes on one session
            common_errors.AuthKeyNotFound(),  # not an RPCError at all
            errors.PhoneNumberBannedError(request=None),
        ],
    )
    def test_fatal(self, exc):
        assert is_fatal_auth_error(exc)

    @pytest.mark.parametrize(
        "exc",
        [
            errors.SessionPasswordNeededError(request=None),  # only means "2FA needed"
            errors.FloodWaitError(request=None, capture=5),
            ConnectionError("reset"),
            TimeoutError(),
        ],
    )
    def test_not_fatal(self, exc):
        assert not is_fatal_auth_error(exc)

    def test_session_file_errors(self):
        assert is_session_file_error(sqlite3.OperationalError("database is locked"))
        assert is_session_file_error(sqlite3.DatabaseError("malformed"))
        assert not is_session_file_error(ValueError("nope"))


class TestSupervisor:
    async def test_fatal_auth_parks_in_degraded_mode_without_exiting(self, clock, sleeps):
        notes = []

        async def notify(reason):
            notes.append(reason)

        sup = make_supervisor(
            [errors.SessionRevokedError(request=None)], clock, sleeps, notify=notify
        )
        assert await sup.run() == EXIT_OK  # never a restart loop under `restart: unless-stopped`
        assert sup.state.degraded and sup.state.degraded_reason == "SessionRevokedError"
        assert notes == ["SessionRevokedError"]
        assert sleeps.calls == []  # no backoff, no reconnect attempts

    async def test_owner_reminder_is_rate_limited(self, clock, sleeps):
        notes = []

        async def notify(reason):
            notes.append(reason)

        sup = make_supervisor(
            [errors.AuthKeyDuplicatedError(request=None)], clock, sleeps, notify=notify
        )
        await sup.run()
        assert await sup.notify_owner_if_due() is False
        clock.tick(6 * 3600 + 1)
        assert await sup.notify_owner_if_due() is True
        assert len(notes) == 2

    async def test_notification_failure_does_not_break_degraded_mode(self, clock, sleeps):
        async def notify(_reason):
            raise ConnectionError("bot is down too")

        sup = make_supervisor(
            [errors.SessionRevokedError(request=None)], clock, sleeps, notify=notify
        )
        assert await sup.run() == EXIT_OK
        assert sup.state.degraded

    async def test_session_file_error_is_treated_as_fatal(self, clock, sleeps):
        sup = make_supervisor([sqlite3.OperationalError("database is locked")], clock, sleeps)
        assert await sup.run() == EXIT_OK
        assert sup.state.degraded
        assert "session file" in sup.state.degraded_reason

    async def test_transient_errors_back_off_with_jitter(self, clock, sleeps):
        sup = make_supervisor(
            [ConnectionError("a"), ConnectionError("b"), ConnectionError("c")],
            clock,
            sleeps,
            max_attempts=3,
        )
        assert await sup.run() == EXIT_ERROR
        assert len(sleeps.calls) == 2
        assert 2.0 <= sleeps.calls[0] < 2.5  # base 2s plus up to 25% jitter
        assert 4.0 <= sleeps.calls[1] < 5.0
        assert sup.state.attempts == 3

    async def test_backoff_is_capped(self, clock, sleeps):
        sup = make_supervisor([ConnectionError("x")] * 20, clock, sleeps, max_attempts=20)
        await sup.run()
        assert max(sleeps.calls) <= 300 * 1.25

    async def test_clean_return_reconnects(self, clock, sleeps):
        sup = make_supervisor([None, None, None], clock, sleeps, max_attempts=3)
        assert await sup.run() == EXIT_ERROR
        assert sup.state.attempts == 3

    async def test_stop_request_ends_the_loop(self, clock, sleeps):
        holder: dict[str, Supervisor] = {}

        async def run_listener():
            holder["sup"].request_stop()

        sup = Supervisor(run_listener=run_listener, clock=clock, sleep=sleeps)
        holder["sup"] = sup
        assert await sup.run() == EXIT_OK
        assert sup.stopped
        assert sleeps.calls == []

    async def test_cancellation_propagates(self, clock, sleeps):
        async def run_listener():
            raise asyncio.CancelledError

        sup = Supervisor(run_listener=run_listener, clock=clock, sleep=sleeps)
        with pytest.raises(asyncio.CancelledError):
            await sup.run()


class TestBootDelay:
    async def test_quiet_start_is_not_delayed(self, sleeps):
        assert await boot_delay(1, sleeps) == 0.0
        assert sleeps.calls == []

    async def test_crash_loop_is_slowed_down(self, sleeps):
        assert await boot_delay(3, sleeps) == BOOT_STORM_DELAY
        assert sleeps.calls == [BOOT_STORM_DELAY]


class TestUptime:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(0, "0m"), (90, "1m"), (3700, "1h 1m"), (200000, "2d 7h"), (-5, "0m")],
    )
    def test_format(self, seconds, expected):
        assert format_uptime(T0, T0 + dt.timedelta(seconds=seconds)) == expected

    def test_not_started(self):
        assert format_uptime(None, T0) == "—"


class TestPeriodicTask:
    async def test_runs_and_survives_failures(self):
        calls: list[int] = []
        sleeps: list[float] = []

        async def work():
            calls.append(len(calls))
            if len(calls) == 2:
                raise RuntimeError("boom")

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 4:
                raise asyncio.CancelledError
            await asyncio.sleep(0)

        task = PeriodicTask("test", work, 5.0, sleep=sleep)
        task.start()
        await asyncio.sleep(0.05)
        await task.stop()
        assert len(calls) >= 3  # kept going after the exception
        assert task.failures == 1
        assert task.runs >= 2
        assert sleeps[:2] == [5.0, 5.0]

    async def test_stop_is_idempotent(self):
        async def work():
            await asyncio.sleep(0)

        task = PeriodicTask("test", work, 0.01)
        await task.stop()
        task.start()
        await task.stop()
        await task.stop()

**Inspecting CLI lifecycle for testing**
exec
/bin/zsh -lc "sed -n '1,320p' ddiver/__main__.py && sed -n '1,180p' ddiver/config.py && sed -n '1,260p' tests/test_cli.py" in /Users/L/Code/ddiver
 succeeded in 0ms:
"""Command line: ``ddiver login | run | health | check-config``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import stat
import sys
from pathlib import Path
from typing import Any

from ddiver.app import (
    EXIT_DB_CORRUPT,
    EXIT_ERROR,
    EXIT_LOCKED,
    EXIT_NOT_AUTHORIZED,
    EXIT_OK,
)
from ddiver.config import Settings
from ddiver.health import MAX_AGE_SECONDS, heartbeat_age, is_healthy
from ddiver.storage import DatabaseCorruptError, Storage

log = logging.getLogger("ddiver")

LOCK_NAME = "ddiver.lock"
LOGIN_BLOCK_SECONDS = 120


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    # Telethon logs its automatic flood sleeps here at INFO; without this they are invisible.
    logging.getLogger("telethon.client.users").setLevel(logging.INFO)


def harden_data_dir(data_dir: Path) -> None:
    """The session file is the account: keep the data directory to the owner only."""
    data_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        data_dir.chmod(0o700)
    for path in data_dir.glob("*"):
        if path.is_file() and path.suffix in {".session", ".sqlite3", ".sqlite", ".env"}:
            with contextlib.suppress(OSError):
                path.chmod(0o600)


def world_readable(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return bool(mode & (stat.S_IRGRP | stat.S_IROTH))


class InstanceLock:
    """One process per data directory; two would poison the Telethon session."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: Any = None

    def acquire(self) -> bool:
        import fcntl

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("w")
        try:
            fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._handle.close()
            self._handle = None
            return False
        self._handle.write(str(os.getpid()))
        self._handle.flush()
        return True

    def release(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(OSError):
                self._handle.close()
            self._handle = None


async def cmd_login(settings: Settings) -> int:
    from ddiver.clients import make_user_client
    from ddiver.runtime import refresh_dialogs

    harden_data_dir(settings.data_dir)
    age = heartbeat_age(settings.heartbeat_path)
    if age is not None and age < LOGIN_BLOCK_SECONDS:
        print("ddiver looks like it is running. Stop it first: two processes on one session")
        print("file make Telegram invalidate it.")
        return EXIT_LOCKED
    lock = InstanceLock(settings.data_dir / LOCK_NAME)
    if not lock.acquire():
        print("Another ddiver process holds the lock; stop it first.")
        return EXIT_LOCKED
    client = make_user_client(settings)
    try:
        await client.start()  # interactive: phone, code, 2FA password
        me = await client.get_me()
        print(f"Logged in as {me.first_name} (id {me.id}).")
        if me.id != settings.owner_id:
            print(f"⚠️  OWNER_ID is {settings.owner_id} but this account is {me.id}.")
        storage = Storage(settings.db_path, owner_id=settings.owner_id)
        await storage.open()
        try:
            count = await refresh_dialogs(client, storage)
            print(f"Cached {count} chats.")
        finally:
            await storage.close()
    finally:
        await client.disconnect()
        lock.release()
        harden_data_dir(settings.data_dir)
    return EXIT_OK


async def cmd_run(settings: Settings) -> int:
    from ddiver.runtime import Application

    harden_data_dir(settings.data_dir)
    lock = InstanceLock(settings.data_dir / LOCK_NAME)
    if not lock.acquire():
        log.error("another ddiver process is already running for %s", settings.data_dir)
        return EXIT_LOCKED

    app = Application(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, app.request_stop)
    try:
        await app.setup()
    except DatabaseCorruptError as exc:
        log.error("database is corrupt (%s); restore %s from a backup", exc, settings.db_path)
        lock.release()
        return EXIT_DB_CORRUPT
    try:
        return await app.run()
    finally:
        await app.close()
        lock.release()


async def cmd_check_config(settings: Settings) -> int:
    from ddiver.clients import make_bot_client

    problems: list[str] = []
    print(f"Data directory: {settings.data_dir}")
    print(f"Owner id: {settings.owner_id}")
    print(f"Bot id: {settings.bot_id}")
    if not settings.user_session.exists():
        problems.append("no user session yet — run `ddiver login`")
    problems.extend(
        f"{path} is readable by other users; run `chmod 600 {path}`"
        for path in (settings.data_dir, settings.user_session, settings.db_path, Path(".env"))
        if path.exists() and world_readable(path)
    )

    client = make_bot_client(settings)
    try:
        await client.start(bot_token=settings.bot_token.get_secret_value())
        me = await client.get_me()
        if me.id != settings.bot_id:
            problems.append(
                f"bot.session belongs to bot {me.id}, but BOT_TOKEN is for {settings.bot_id}; "
                f"delete {settings.bot_session}"
            )
        else:
            print(f"Bot: @{me.username}")
        try:
            await client.get_input_entity(settings.owner_id)
        except Exception:  # Telethon raises ValueError or an RPCError here
            problems.append(
                f"the owner has not started the bot yet — open t.me/{me.username} and press Start"
            )
    except Exception as exc:  # this command reports problems, it never raises
        problems.append(f"cannot use BOT_TOKEN: {type(exc).__name__}: {exc}")
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()

    if problems:
        print("\nProblems:")
        for problem in problems:
            print(f"  ✖ {problem}")
        return EXIT_ERROR
    print("\nEverything looks fine.")
    return EXIT_OK


def cmd_health(settings: Settings) -> int:
    age = heartbeat_age(settings.heartbeat_path)
    if age is None:
        print("no heartbeat file")
        return EXIT_ERROR
    if not is_healthy(settings.heartbeat_path, MAX_AGE_SECONDS):
        print(f"heartbeat is {age:.0f}s old")
        return EXIT_ERROR
    print(f"ok ({age:.0f}s)")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ddiver", description=__doc__)
    parser.add_argument("--env-file", default=".env", help="path to the .env file")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="log the listener account in (interactive)")
    sub.add_parser("run", help="run the monitor")
    sub.add_parser("health", help="check the heartbeat (for Docker HEALTHCHECK)")
    sub.add_parser("check-config", help="validate configuration and permissions")
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)  # before anything creates a session, a database or a heartbeat
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.load(args.env_file)
    except Exception as exc:  # this command reports problems, it never raises
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    configure_logging(settings.log_level)

    if args.command == "health":
        return cmd_health(settings)
    if args.command == "login":
        return asyncio.run(cmd_login(settings))
    if args.command == "check-config":
        return asyncio.run(cmd_check_config(settings))
    code = asyncio.run(cmd_run(settings))
    if code == EXIT_NOT_AUTHORIZED:
        log.error("run `ddiver login` first")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
"""Runtime settings, loaded from environment variables / ``.env``."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    api_id: int = Field(description="my.telegram.org → API development tools")
    api_hash: SecretStr
    bot_token: SecretStr = Field(description="@BotFather token of the delivery/config bot")
    owner_id: int = Field(description="Telegram user id of the owner (auto-allowed)")

    data_dir: Path = Field(default=Path("data"), description="session files, SQLite, heartbeat")
    log_level: str = "INFO"

    catchup_hours: int = Field(default=24, ge=0, le=24 * 14)
    catchup_max_per_chat: int = Field(default=500, ge=0, le=5000)
    digest_threshold: int = Field(default=20, ge=2, le=500)
    regex_timeout_ms: int = Field(default=20, ge=5, le=5000)
    ignore_own_messages: bool = Field(default=True, description="set false only for tests")

    @field_validator("bot_token")
    @classmethod
    def _bot_token_shape(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        digits, sep, rest = raw.partition(":")
        if not sep or not digits.isdigit() or len(rest) < 20:
            msg = "BOT_TOKEN must look like 123456789:AA... (from @BotFather)"
            raise ValueError(msg)
        return value

    @field_validator("log_level")
    @classmethod
    def _log_level(cls, value: str) -> str:
        upper = value.upper()
        if upper not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            msg = "LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR"
            raise ValueError(msg)
        return upper

    @classmethod
    def load(cls, env_file: Path | str | None = ".env") -> Settings:
        """Build settings; ``env_file=None`` reads the process environment only (tests)."""
        return cls(_env_file=env_file)  # type: ignore[call-arg]

    @property
    def user_session(self) -> Path:
        return self.data_dir / "user.session"

    @property
    def bot_session(self) -> Path:
        return self.data_dir / "bot.session"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ddiver.sqlite3"

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / "heartbeat"

    @property
    def regex_timeout(self) -> float:
        return self.regex_timeout_ms / 1000

    @property
    def bot_id(self) -> int:
        return int(self.bot_token.get_secret_value().partition(":")[0])
import datetime as dt
import stat

import pytest

from ddiver.__main__ import (
    InstanceLock,
    build_parser,
    cmd_health,
    harden_data_dir,
    main,
    world_readable,
)
from ddiver.app import EXIT_ERROR, EXIT_OK
from ddiver.config import Settings
from ddiver.health import write_heartbeat
from tests.fakes import T0

ENV = {
    "API_ID": "12345",
    "API_HASH": "0123456789abcdef0123456789abcdef",
    "BOT_TOKEN": "123456789:AAExampleTokenWithEnoughLength_xyz",
    "OWNER_ID": "777",
}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for key, value in {**ENV, "DATA_DIR": str(tmp_path / "data")}.items():
        monkeypatch.setenv(key, value)
    return Settings.load(None)


class TestParser:
    def test_commands(self):
        parser = build_parser()
        assert parser.parse_args(["run"]).command == "run"
        assert parser.parse_args(["--env-file", "x.env", "login"]).env_file == "x.env"

    def test_command_is_required(self, capsys):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])


class TestHealth:
    def test_missing_heartbeat(self, settings, capsys):
        assert cmd_health(settings) == EXIT_ERROR
        assert "no heartbeat" in capsys.readouterr().out

    def test_fresh_heartbeat(self, settings, capsys):
        write_heartbeat(settings.heartbeat_path)
        assert cmd_health(settings) == EXIT_OK
        assert "ok" in capsys.readouterr().out

    def test_stale_heartbeat(self, settings, capsys):
        write_heartbeat(settings.heartbeat_path, T0 - dt.timedelta(hours=1))
        assert cmd_health(settings) == EXIT_ERROR
        assert "old" in capsys.readouterr().out

    def test_health_via_main(self, tmp_path, monkeypatch, capsys):
        for key, value in {**ENV, "DATA_DIR": str(tmp_path)}.items():
            monkeypatch.setenv(key, value)
        write_heartbeat(tmp_path / "heartbeat")
        assert main(["--env-file", "", "health"]) == EXIT_OK

    def test_bad_configuration_is_reported(self, tmp_path, monkeypatch, capsys):
        for key in ENV:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("DDIVER_UNUSED", "1")
        assert main(["--env-file", "", "health"]) == EXIT_ERROR
        assert "Configuration error" in capsys.readouterr().err


class TestPermissions:
    def test_harden_sets_restrictive_modes(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        session = data / "user.session"
        session.write_text("secret")
        session.chmod(0o644)
        harden_data_dir(data)
        assert stat.S_IMODE(data.stat().st_mode) == 0o700
        assert stat.S_IMODE(session.stat().st_mode) == 0o600
        assert not world_readable(session)

    def test_world_readable_detection(self, tmp_path):
        path = tmp_path / "f"
        path.write_text("x")
        path.chmod(0o644)
        assert world_readable(path)
        path.chmod(0o600)
        assert not world_readable(path)
        assert not world_readable(tmp_path / "missing")


class TestInstanceLock:
    def test_second_acquire_fails_while_held(self, tmp_path):
        first = InstanceLock(tmp_path / "ddiver.lock")
        second = InstanceLock(tmp_path / "ddiver.lock")
        assert first.acquire() is True
        assert second.acquire() is False
        first.release()
        assert second.acquire() is True
        second.release()

    def test_release_is_idempotent(self, tmp_path):
        lock = InstanceLock(tmp_path / "ddiver.lock")
        lock.release()
        assert lock.acquire() is True
        lock.release()
        lock.release()

**Identifying async task leak in shutdown sequence**
exec
/bin/zsh -lc "sed -n '1,120p' ddiver/health.py && sed -n '1,120p' tests/test_health.py && sed -n '1,440p' tests/test_delivery.py" in /Users/L/Code/ddiver
 succeeded in 0ms:
"""Heartbeat file used by ``ddiver health`` / Docker HEALTHCHECK."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from ddiver.timeutil import utcnow

MAX_AGE_SECONDS = 120


def write_heartbeat(path: Path, now: dt.datetime | None = None) -> None:
    moment = now or utcnow()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(moment.isoformat())
    tmp.replace(path)


def heartbeat_age(path: Path, now: dt.datetime | None = None) -> float | None:
    """Seconds since the last heartbeat, or ``None`` if the file is missing/unreadable."""
    try:
        stamp = dt.datetime.fromisoformat(path.read_text().strip())
    except (OSError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.UTC)
    return ((now or utcnow()) - stamp).total_seconds()


def is_healthy(
    path: Path, max_age: float = MAX_AGE_SECONDS, now: dt.datetime | None = None
) -> bool:
    age = heartbeat_age(path, now)
    return age is not None and 0 <= age <= max_age
import datetime as dt

from ddiver.health import heartbeat_age, is_healthy, write_heartbeat


def test_heartbeat_roundtrip(tmp_path):
    path = tmp_path / "sub" / "heartbeat"
    now = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.UTC)
    write_heartbeat(path, now)
    assert heartbeat_age(path, now + dt.timedelta(seconds=30)) == 30
    assert is_healthy(path, now=now + dt.timedelta(seconds=119))
    assert not is_healthy(path, now=now + dt.timedelta(seconds=121))


def test_missing_or_garbage_file(tmp_path):
    path = tmp_path / "heartbeat"
    assert heartbeat_age(path) is None
    assert not is_healthy(path)
    path.write_text("not a date")
    assert not is_healthy(path)


def test_future_stamp_is_unhealthy(tmp_path):
    path = tmp_path / "heartbeat"
    now = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.UTC)
    write_heartbeat(path, now + dt.timedelta(minutes=5))
    assert not is_healthy(path, now=now)
import asyncio
import datetime as dt

import pytest
from telethon import errors
from telethon.tl import types as tl

from ddiver.delivery import QUEUE_MAXSIZE, Delivery
from ddiver.storage import DeliveryStatus, Role, Storage
from tests.fakes import T0, FakeBotClient, FakeClock

OWNER = 1
MOD = 2

BASE_PAYLOAD = {
    "chat_id": -1001234567890,
    "chat_title": "Чат",
    "chat_username": None,
    "chat_kind": "supergroup",
    "msg_id": 1,
    "topic_id": None,
    "date": T0.isoformat(),
    "text": "казино",
    "spans": [[0, 6]],
    "rule_pattern": "казино",
    "rule_kind": "include",
    "rule_is_global": False,
    "sender_id": 42,
    "sender_name": "Иван",
    "sender_username": "ivan",
}


def payload(msg_id: int = 1, **over):
    return {**BASE_PAYLOAD, "msg_id": msg_id, **over}


class Sleeps:
    """Records sleeps and advances the monotonic clock without real waiting."""

    def __init__(self) -> None:
        self.now = 0.0
        self.calls: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def sleeps():
    return Sleeps()


@pytest.fixture
async def store(tmp_path, clock):
    s = Storage(tmp_path / "db.sqlite3", owner_id=OWNER, clock=clock)
    await s.open()
    await s.ensure_user(OWNER)
    await s.ensure_user(MOD)
    await s.update_user(MOD, role=Role.ALLOWED, lang="ru", tz="Europe/Kyiv")
    await s.update_user(OWNER, lang="en", tz="UTC")
    yield s
    await s.close()


@pytest.fixture
def bot():
    return FakeBotClient()


@pytest.fixture
def delivery(bot, store, clock, sleeps):
    return Delivery(
        bot, store, clock=clock, monotonic=sleeps.monotonic, sleep=sleeps.sleep, buttons=None
    )


async def claim(store, user_id: int, msg_id: int, **over):
    item = await store.claim_delivery(
        user_id, BASE_PAYLOAD["chat_id"], msg_id, 7, payload(msg_id, **over)
    )
    assert item is not None
    return item


async def settle(delivery, *, timeout: float = 5.0):
    """Let the worker tasks run until every queue is drained."""
    assert await delivery.wait_idle(timeout) is True


class TestHappyPath:
    async def test_single_notification_is_rendered_and_marked_sent(
        self, delivery, bot, store, clock
    ):
        item = await claim(store, MOD, 1)
        assert await delivery.enqueue(item) is True
        clock.tick(12)
        await settle(delivery)
        assert len(bot.sent) == 1
        sent = bot.sent[0]
        assert sent.user_id == MOD
        assert "казино" in sent.text
        assert "12 сек назад" in sent.text  # rendered at send time, in the user's language
        assert sent.link_preview is False
        assert any(isinstance(e, tl.MessageEntityBold) for e in sent.entities)
        row = await store.get_delivery(*item.key)
        assert row.status is DeliveryStatus.SENT
        assert delivery.metrics.delivered == 1

    async def test_replay_pending_requeues_outbox_rows(self, delivery, bot, store):
        await claim(store, MOD, 1)
        await claim(store, MOD, 2)
        await claim(store, OWNER, 3)
        assert await delivery.replay_pending() == 3
        await settle(delivery)
        assert {m.user_id for m in bot.sent} == {MOD, OWNER}
        assert len(bot.sent) == 3
        assert await store.pending_deliveries() == []

    async def test_users_are_independent(self, delivery, bot, store):
        await delivery.enqueue(await claim(store, MOD, 1))
        await delivery.enqueue(await claim(store, OWNER, 2))
        await settle(delivery)
        langs = {m.user_id: m.text for m in bot.sent}
        assert "сек назад" in langs[MOD]  # ru
        assert "s ago" in langs[OWNER]  # en

    async def test_inactive_and_paused_users_are_skipped(self, delivery, bot, store):
        await store.update_user(MOD, role=Role.DENIED)
        assert await delivery.enqueue(await claim(store, MOD, 1)) is False
        await store.update_user(MOD, role=Role.ALLOWED)
        await store.pause_delivery(MOD, "blocked")
        assert await delivery.enqueue(await claim(store, MOD, 2)) is False
        await settle(delivery)
        assert bot.sent == []
        assert delivery.metrics.dropped_inactive == 2


class TestFlood:
    async def test_flood_wait_retries_the_same_item_and_pauses_everyone(
        self, delivery, bot, store, sleeps
    ):
        bot.fail_next(errors.FloodWaitError(request=None, capture=30))
        await delivery.enqueue(await claim(store, MOD, 1))
        await delivery.enqueue(await claim(store, OWNER, 2))
        await settle(delivery)
        assert len(bot.sent) == 2
        assert delivery.metrics.flood_waits == 1
        assert delivery.metrics.flood_seconds == 30
        assert any(s >= 30 for s in sleeps.calls)  # the shared gate, not a per-user sleep
        assert delivery.metrics.delivered == 2

    async def test_repeated_flood_waits_never_fail_the_item(self, delivery, bot, store):
        bot.fail_next(*[errors.FloodWaitError(request=None, capture=5) for _ in range(8)])
        item = await claim(store, MOD, 1)
        await delivery.enqueue(item)
        await settle(delivery)
        assert len(bot.sent) == 1
        row = await store.get_delivery(*item.key)
        assert row.status is DeliveryStatus.SENT


class TestErrors:
    async def test_blocked_user_is_paused_and_queue_dropped(self, delivery, bot, store):
        bot.fail_next(errors.UserIsBlockedError(request=None))
        item = await claim(store, MOD, 1)
        await delivery.enqueue(item)
        await delivery.enqueue(await claim(store, MOD, 2))
        await settle(delivery)
        user = await store.get_user(MOD)
        assert user.paused and user.pause_reason == "UserIsBlockedError"
        assert user.active  # no re-approval needed after they unblock
        assert delivery.queue_length(MOD) == 0
        assert (await store.get_delivery(*item.key)).status is DeliveryStatus.FAILED

    async def test_entity_error_falls_back_to_plain_text(self, delivery, bot, store):
        bot.fail_next(errors.EntitiesTooLongError(request=None))
        item = await claim(store, MOD, 1)
        await delivery.enqueue(item)
        await settle(delivery)
        assert len(bot.sent) == 1
        assert bot.sent[0].entities == []
        assert bot.sent[0].buttons is None
        assert delivery.metrics.degraded_sends == 1
        assert (await store.get_delivery(*item.key)).status is DeliveryStatus.SENT

    async def test_deterministic_bad_request_is_not_retried(self, delivery, bot, store):
        bot.fail_next(errors.MessageTooLongError(request=None))
        item = await claim(store, MOD, 1)
        await delivery.enqueue(item)
        await settle(delivery)
        assert bot.sent == []
        row = await store.get_delivery(*item.key)
        assert row.status is DeliveryStatus.FAILED
        assert row.attempts == 1
        assert delivery.metrics.failed == 1

    async def test_transient_errors_back_off_then_succeed(self, delivery, bot, store, sleeps):
        bot.fail_next(ConnectionError("reset"), errors.TimedOutError(request=None, message="T"))
        item = await claim(store, MOD, 1)
        await delivery.enqueue(item)
        await settle(delivery)
        assert len(bot.sent) == 1
        assert sleeps.calls[:2] == [1.0, 2.0]
        assert (await store.get_delivery(*item.key)).status is DeliveryStatus.SENT

    async def test_transient_errors_give_up_after_max_attempts(self, delivery, bot, store):
        bot.fail_next(*[ConnectionError("reset") for _ in range(9)])
        item = await claim(store, MOD, 1)
        await delivery.enqueue(item)
        await settle(delivery)
        row = await store.get_delivery(*item.key)
        assert row.status is DeliveryStatus.FAILED
        assert row.attempts == 5
        assert bot.sent == []

    async def test_unresolvable_peer_fails_immediately(self, delivery, bot, store):
        bot.fail_next(ValueError("Could not find the input entity"))
        item = await claim(store, MOD, 1)
        await delivery.enqueue(item)
        await settle(delivery)
        assert (await store.get_delivery(*item.key)).status is DeliveryStatus.FAILED

    async def test_worker_survives_an_unexpected_exception(self, delivery, bot, store):
        bot.fail_next(RuntimeError("boom"))
        first = await claim(store, MOD, 1)
        await delivery.enqueue(first)
        await settle(delivery)
        assert (await store.get_delivery(*first.key)).status is DeliveryStatus.FAILED
        # the same worker keeps serving the next item
        second = await claim(store, MOD, 2)
        await delivery.enqueue(second)
        await settle(delivery)
        assert (await store.get_delivery(*second.key)).status is DeliveryStatus.SENT


class TestDigest:
    async def test_threshold_collapses_the_queue_into_one_message(self, delivery, bot, store):
        await store.update_user(MOD, digest_threshold=5)
        bot.hold()  # build a backlog, as a burst of matches would
        items = [await claim(store, MOD, i) for i in range(1, 8)]
        for item in items:
            await delivery.enqueue(item)
        bot.release()
        await settle(delivery)
        # the worker was already sending the first item when the burst arrived, so the rest
        # collapse into one digest
        assert len(bot.sent) == 2
        assert "6 совпадений" in bot.sent[1].text
        rows = [await store.get_delivery(*i.key) for i in items]
        assert rows[0].status is DeliveryStatus.SENT
        assert all(r.status is DeliveryStatus.DIGESTED for r in rows[1:])
        assert delivery.metrics.digested == 6
        assert delivery.metrics.delivered == 1

    async def test_below_threshold_sends_individually(self, delivery, bot, store):
        await store.update_user(MOD, digest_threshold=10)
        bot.hold()
        for i in range(1, 4):
            await delivery.enqueue(await claim(store, MOD, i))
        bot.release()
        await settle(delivery)
        assert len(bot.sent) == 3

    async def test_age_triggers_a_digest_below_the_threshold(self, delivery, bot, store, clock):
        await store.update_user(MOD, digest_threshold=50)
        bot.hold()
        for i in range(1, 7):
            await delivery.enqueue(await claim(store, MOD, i))
        clock.tick(31)  # the oldest queued item is now older than DIGEST_AGE
        bot.release()
        await settle(delivery)
        assert len(bot.sent) == 2
        assert "5 совпадений" in bot.sent[1].text  # threshold not reached, age triggered it
        assert delivery.metrics.digested == 5

    async def test_digest_threshold_zero_disables_digests(self, delivery, bot, store):
        await store.update_user(MOD, digest_threshold=0)
        for i in range(1, 6):
            await delivery.enqueue(await claim(store, MOD, i))
        await settle(delivery)
        assert len(bot.sent) == 5


class TestQueueLimits:
    async def test_overflow_is_counted_and_reported_in_the_next_digest(
        self, delivery, bot, store, monkeypatch
    ):
        monkeypatch.setattr("ddiver.delivery.QUEUE_MAXSIZE", 3)
        await store.update_user(MOD, digest_threshold=3)
        bot.hold()  # fill the queue without letting the worker drain it
        accepted = 0
        for i in range(1, 8):
            if await delivery.enqueue(await claim(store, MOD, i)):
                accepted += 1
        assert accepted == 4  # three queued plus the one the worker is already sending
        assert delivery.metrics.dropped_overflow == 3
        bot.release()
        await settle(delivery)
        assert "отброшено" in bot.sent[-1].text

    async def test_default_queue_size_is_bounded(self):
        assert QUEUE_MAXSIZE == 2000


class TestShutdown:
    async def test_drain_reports_what_is_left(self, delivery, bot, store, sleeps):
        await store.update_user(MOD, digest_threshold=0)
        for i in range(1, 4):
            await delivery.enqueue(await claim(store, MOD, i))
        left = await delivery.drain(timeout=5)
        assert left == 0
        assert len(bot.sent) == 3

    async def test_undelivered_items_stay_pending_for_the_next_start(self, delivery, store, bot):
        item = await claim(store, MOD, 1)
        await delivery.enqueue(item)
        await delivery.drop_user(MOD)  # simulates a hard stop before the worker ran
        assert bot.sent == []
        assert [p.key for p in await store.pending_deliveries()] == [item.key]


class TestRateLimiting:
    async def test_per_user_bucket_spaces_messages_out(self, delivery, bot, store, sleeps):
        await store.update_user(MOD, digest_threshold=0)
        for i in range(1, 4):
            await delivery.enqueue(await claim(store, MOD, i))
        await settle(delivery)
        assert len(bot.sent) == 3
        assert sum(1 for s in sleeps.calls if s >= 0.9) >= 2  # ~1 msg/s into a private chat


class TestTimeRendering:
    async def test_relative_time_is_computed_when_the_message_is_sent(
        self, delivery, bot, store, clock
    ):
        item = await claim(store, MOD, 1, date=(T0 - dt.timedelta(minutes=3)).isoformat())
        await delivery.enqueue(item)
        clock.tick(120)
        await settle(delivery)
        assert "5 мин назад" in bot.sent[0].text

**Identifying queue overflow notification loss**
**Planning pending notification requeue and task cancellation fix**
**Fixing double task_done call error**
**Investigating task exception handling in tests**
**Analyzing task cancellation leak**
**Fixing worker shutdown and task double completion**
**Correcting batch failure handling**
**Planning queue overflow handling**
**Assessing concurrency and flood risks**
**Inspecting richtext formatter links**
exec
/bin/zsh -lc "sed -n '1,340p' ddiver/formatter.py && sed -n '1,240p' ddiver/richtext.py && sed -n '1,160p' ddiver/links.py && sed -n '1,240p' ddiver/timeutil.py" in /Users/L/Code/ddiver
 succeeded in 0ms:
"""Render notifications and digests.

Rendering happens in the delivery worker, just before sending, from the immutable payload
snapshot taken when the match was found plus the user's *current* language and timezone. A
message that waited in the queue or a flood gate therefore shows the right relative time and
the right language, even if the user changed settings meanwhile.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from telethon.tl import types as tl

from ddiver.i18n import t
from ddiver.links import ChatRef, chat_link, message_link
from ddiver.richtext import MAX_MESSAGE_UTF16, RichText, Span, window
from ddiver.rules import RuleKind
from ddiver.timeutil import format_local, relative

TEXT_BUDGET = 3200
"""UTF-16 units left for the message body after the header lines."""

DIGEST_BUDGET = 3500
DIGEST_CHATS = 12
DIGEST_ITEMS_PER_CHAT = 5
SNIPPET_CHARS = 60


@dataclass(frozen=True, slots=True)
class Payload:
    """Everything needed to render one notification, captured at match time.

    Stored as JSON in the outbox, so it must stay plain-data and backwards compatible.
    """

    chat_id: int
    chat_title: str
    chat_username: str | None
    chat_kind: str
    msg_id: int
    topic_id: int | None
    date: dt.datetime
    text: str
    spans: list[tuple[int, int]]
    rule_pattern: str
    rule_kind: str
    rule_is_global: bool
    sender_id: int | None
    sender_name: str
    sender_username: str | None
    sender_is_channel: bool = False
    sender_is_deleted: bool = False
    is_forwarded: bool = False
    catchup: bool = False
    catchup_skipped: int = 0

    @property
    def chat_ref(self) -> ChatRef:
        return ChatRef(
            id=self.chat_id,
            title=self.chat_title,
            username=self.chat_username,
            kind=self.chat_kind,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "chat_title": self.chat_title,
            "chat_username": self.chat_username,
            "chat_kind": self.chat_kind,
            "msg_id": self.msg_id,
            "topic_id": self.topic_id,
            "date": self.date.astimezone(dt.UTC).isoformat(),
            "text": self.text,
            "spans": [list(s) for s in self.spans],
            "rule_pattern": self.rule_pattern,
            "rule_kind": self.rule_kind,
            "rule_is_global": self.rule_is_global,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "sender_username": self.sender_username,
            "sender_is_channel": self.sender_is_channel,
            "sender_is_deleted": self.sender_is_deleted,
            "is_forwarded": self.is_forwarded,
            "catchup": self.catchup,
            "catchup_skipped": self.catchup_skipped,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Payload:
        moment = dt.datetime.fromisoformat(data["date"])
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.UTC)
        return cls(
            chat_id=data["chat_id"],
            chat_title=data["chat_title"],
            chat_username=data.get("chat_username"),
            chat_kind=data.get("chat_kind", "supergroup"),
            msg_id=data["msg_id"],
            topic_id=data.get("topic_id"),
            date=moment,
            text=data.get("text", ""),
            spans=[(int(a), int(b)) for a, b in data.get("spans", [])],
            rule_pattern=data.get("rule_pattern", ""),
            rule_kind=data.get("rule_kind", RuleKind.INCLUDE.value),
            rule_is_global=bool(data.get("rule_is_global", False)),
            sender_id=data.get("sender_id"),
            sender_name=data.get("sender_name", ""),
            sender_username=data.get("sender_username"),
            sender_is_channel=bool(data.get("sender_is_channel", False)),
            sender_is_deleted=bool(data.get("sender_is_deleted", False)),
            is_forwarded=bool(data.get("is_forwarded", False)),
            catchup=bool(data.get("catchup", False)),
            catchup_skipped=int(data.get("catchup_skipped", 0)),
        )


@dataclass(frozen=True, slots=True)
class Rendered:
    text: str
    entities: list[tl.TypeMessageEntity] = field(default_factory=list)


def sender_label(payload: Payload, lang: str) -> str:
    if payload.sender_is_deleted:
        return t(lang, "notif.sender.deleted")
    if payload.sender_name:
        return payload.sender_name
    if payload.sender_is_channel:
        return t(lang, "notif.sender.anonymous")
    return t(lang, "notif.sender.unknown")


def render_notification(payload: Payload, *, lang: str, tz: str, now: dt.datetime) -> Rendered:
    chat = payload.chat_ref
    link = message_link(chat, payload.msg_id, payload.topic_id)
    rt = RichText()

    # chat line
    rt.add(t(lang, "notif.chat")).add(" ")
    rt.add(payload.chat_title or str(payload.chat_id), url=chat_link(chat) or link)
    rt.newline()

    # sender line: the name points at the message (works on every client), the username at the
    # profile, and the numeric id is copyable code.
    rt.add(t(lang, "notif.sender")).add(" ")
    rt.add(sender_label(payload, lang), url=link)
    if payload.sender_username:
        rt.add(" · ")
        rt.add(f"@{payload.sender_username}", url=f"https://t.me/{payload.sender_username}")
    if payload.sender_id is not None:
        rt.add(" · ").add(str(payload.sender_id), code=True)
    if payload.is_forwarded:
        rt.add(" · ").add(t(lang, "notif.forwarded"))
    rt.newline()

    if link:
        rt.add(t(lang, "notif.link")).add(" ").add(link, url=link).newline()

    rt.add(t(lang, "notif.time")).add(" ")
    rt.add(format_local(payload.date, tz))
    rt.add(" · ").add(relative(payload.date, now, lang))
    if payload.catchup:
        rt.add(" · ").add(t(lang, "notif.missed"))
    rt.newline()

    scope = "notif.rule_scope.global" if payload.rule_is_global else "notif.rule_scope.chat"
    rt.add(t(lang, "notif.rule")).add(" ")
    rt.add(payload.rule_pattern, code=True).add(" (").add(t(lang, scope)).add(")")
    if payload.catchup_skipped:
        rt.newline().add(t(lang, "notif.catchup_capped", n=payload.catchup_skipped))
    rt.newline(2)

    spans = [Span(a, b) for a, b in payload.spans]
    budget = min(TEXT_BUDGET, MAX_MESSAGE_UTF16 - rt.utf16_length - 64)
    view = window(payload.text, spans, budget)
    if view.cut_before:
        rt.add("…")
    rt.add_with_bold_spans(view.text, view.spans)
    if view.cut_after:
        rt.add("…").newline().add(t(lang, "notif.truncated", n=view.cut_after))

    text, entities = rt.build()
    return Rendered(text, entities)


@dataclass(frozen=True, slots=True)
class DigestEntry:
    payload: Payload
    key: tuple[int, int, int]


def render_digest(
    entries: list[DigestEntry],
    *,
    lang: str,
    tz: str,
    now: dt.datetime,
    overflow: int = 0,
) -> Rendered:
    """One message summarising many matches, grouped by chat, newest chats first by count."""
    del tz, now  # only absolute grouping is shown; per-item times stay in the linked messages
    by_chat: dict[int, list[Payload]] = {}
    for entry in entries:
        by_chat.setdefault(entry.payload.chat_id, []).append(entry.payload)
    ordered = sorted(by_chat.items(), key=lambda kv: (-len(kv[1]), kv[1][0].chat_title))

    rt = RichText().add(t(lang, "digest.title", n=len(entries)), bold=True).newline(2)
    shown_chats = 0
    for chat_id, payloads in ordered:
        if shown_chats >= DIGEST_CHATS or rt.utf16_length > DIGEST_BUDGET:
            break
        first = payloads[0]
        chat = first.chat_ref
        head_link = chat_link(chat) or message_link(chat, first.msg_id, first.topic_id)
        rt.add("📡 ")
        rt.add(first.chat_title or str(chat_id), bold=True, url=head_link)
        rt.add(f" — {len(payloads)}").newline()
        for payload in payloads[:DIGEST_ITEMS_PER_CHAT]:
            link = message_link(chat, payload.msg_id, payload.topic_id)
            snippet = _snippet(payload)
            line = t(
                lang,
                "digest.item",
                rule=payload.rule_pattern,
                sender=sender_label(payload, lang) or "?",
                snippet=snippet,
            )
            rt.add("  • ")
            rt.add(line, url=link) if link else rt.add(line)
            rt.newline()
        if len(payloads) > DIGEST_ITEMS_PER_CHAT:
            rt.add("  ").add(
                t(lang, "digest.more_items", n=len(payloads) - DIGEST_ITEMS_PER_CHAT)
            ).newline()
        shown_chats += 1

    if shown_chats < len(ordered):
        remaining = ordered[shown_chats:]
        rt.newline().add(
            t(
                lang,
                "digest.more_chats",
                n=len(remaining),
                m=sum(len(p) for _, p in remaining),
            )
        )
    if overflow:
        rt.newline().add(t(lang, "digest.overflow", n=overflow))

    text, entities = rt.build()
    return Rendered(text, entities)


def _snippet(payload: Payload) -> str:
    text = " ".join(payload.text.split())
    if not text:
        return "—"
    start = payload.spans[0][0] if payload.spans else 0
    begin = max(0, start - SNIPPET_CHARS // 3)
    cut = text[begin : begin + SNIPPET_CHARS]
    prefix = "…" if begin else ""
    suffix = "…" if begin + SNIPPET_CHARS < len(text) else ""
    return f"{prefix}{cut}{suffix}"
"""Build message text plus Telethon ``MessageEntity`` objects with correct UTF-16 offsets.

Telegram measures entity offsets and the 4096-character message limit in UTF-16 code units,
so anything outside the BMP (most emoji) counts twice. Building entities ourselves (instead of
HTML) avoids escaping bugs; Telethon sends ``formatting_entities=`` verbatim.

Server-side rules encoded here: trailing whitespace is stripped from messages (an entity that
reaches into it is rejected with ``EntityBoundsInvalid``), zero-length entities are invalid and
at most 100 entities are accepted per message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Self

from telethon.tl import types as tl

MAX_MESSAGE_UTF16 = 4096
MAX_ENTITIES = 90  # Telegram allows 100; keep headroom for the caller's own additions
ELLIPSIS = "…"


def utf16_len(text: str) -> int:
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def truncate_utf16(text: str, limit: int) -> str:
    """Cut ``text`` so that its UTF-16 length is ``<= limit`` without splitting a code point."""
    if limit <= 0:
        return ""
    units = 0
    for i, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit:
            return text[:i]
    return text


@dataclass(frozen=True, slots=True)
class Span:
    start: int  # code-point offsets within the text they were computed for
    end: int


@dataclass(frozen=True, slots=True)
class Window:
    text: str
    spans: list[Span]
    cut_before: int  # code points removed before the window
    cut_after: int  # code points removed after the window


def window(text: str, spans: list[Span], limit: int, *, context: int = 160) -> Window:
    """Fit ``text`` into ``limit`` UTF-16 units keeping the first span visible.

    If the whole text fits it is returned untouched. Otherwise a head slice is used when the
    first span starts early enough; else a slice beginning ``context`` code points before the
    first span. Spans are re-based to the returned text and clipped to it.
    """
    if utf16_len(text) <= limit:
        return Window(text, list(spans), 0, 0)
    ordered = sorted((s for s in spans if s.end > s.start), key=lambda s: s.start)
    first_start = ordered[0].start if ordered else 0
    head = truncate_utf16(text, limit)
    if first_start < len(head) - context // 2:
        start = 0
        body = head
    else:
        start = max(0, first_start - context)
        body = truncate_utf16(text[start:], limit)
    end = start + len(body)
    rebased = [
        Span(max(s.start, start) - start, min(s.end, end) - start)
        for s in ordered
        if s.start < end and s.end > start
    ]
    return Window(body, rebased, start, len(text) - end)


@dataclass(slots=True)
class RichText:
    """Append-only builder. Offsets are tracked in UTF-16 units as parts are added."""

    _parts: list[str] = field(default_factory=list)
    _entities: list[tl.TypeMessageEntity] = field(default_factory=list)
    _units: int = 0

    @property
    def utf16_length(self) -> int:
        return self._units

    def add(
        self,
        text: str,
        *,
        bold: bool = False,
        code: bool = False,
        url: str | None = None,
    ) -> Self:
        if not text:
            return self
        length = utf16_len(text)
        if url:
            self._entities.append(tl.MessageEntityTextUrl(self._units, length, url))
        if bold:
            self._entities.append(tl.MessageEntityBold(self._units, length))
        if code:
            self._entities.append(tl.MessageEntityCode(self._units, length))
        self._parts.append(text)
        self._units += length
        return self

    def add_with_bold_spans(self, text: str, spans: list[Span]) -> Self:
        """Add ``text`` making the given code-point spans bold; spans may overlap or be unsorted."""
        merged = _merge_spans(spans, len(text))
        cursor = 0
        for span in merged:
            self.add(text[cursor : span.start])
            self.add(text[span.start : span.end], bold=True)
            cursor = span.end
        self.add(text[cursor:])
        return self

    def newline(self, count: int = 1) -> Self:
        return self.add("\n" * count)

    def text(self) -> str:
        return "".join(self._parts)

    def entities(self) -> list[tl.TypeMessageEntity]:
        return list(self._entities)

    def build(self) -> tuple[str, list[tl.TypeMessageEntity]]:
        """Final text and entities: trailing whitespace stripped, entities clipped/dropped to stay
        inside the text, zero-length ones removed, at most :data:`MAX_ENTITIES` kept."""
        text = self.text().rstrip()
        length = utf16_len(text)
        kept: list[tl.TypeMessageEntity] = []
        for ent in self._entities:
            end = min(ent.offset + ent.length, length)
            if ent.offset >= end:
                continue
            clipped = ent if end == ent.offset + ent.length else _with_length(ent, end - ent.offset)
            kept.append(clipped)
            if len(kept) >= MAX_ENTITIES:
                break
        return text, kept


def _with_length(ent: tl.TypeMessageEntity, length: int) -> tl.TypeMessageEntity:
    if isinstance(ent, tl.MessageEntityTextUrl):
        return tl.MessageEntityTextUrl(ent.offset, length, ent.url)
    return type(ent)(ent.offset, length)


def _merge_spans(spans: list[Span], text_len: int) -> list[Span]:
    clipped = sorted(
        (Span(max(0, s.start), min(text_len, s.end)) for s in spans if s.end > s.start),
        key=lambda s: s.start,
    )
    out: list[Span] = []
    for span in clipped:
        if span.end <= span.start:
            continue
        if out and span.start <= out[-1].end:
            out[-1] = Span(out[-1].start, max(out[-1].end, span.end))
        else:
            out.append(span)
    return out
"""Build Telegram links for chats, messages and senders.

Facts this module encodes (verified against Telegram clients):

* Public chats:      ``https://t.me/<username>``, message ``https://t.me/<username>/<msg_id>``
* Private supergroups/channels: message ``https://t.me/c/<bare_id>/<msg_id>`` where ``bare_id``
  is the channel id without the ``-100`` prefix. The chat itself has no standalone link, so we
  point the chat title at the message link.
* Forum topics insert the topic id: ``.../<topic_id>/<msg_id>`` (general topic id is 1).
* Basic (non-super) groups have no message links at all.
* Users: ``https://t.me/<username>`` or, without a username, ``tg://user?id=<id>``.
"""

from __future__ import annotations

from dataclasses import dataclass

from telethon import utils
from telethon.tl import types as tl

GENERAL_TOPIC_ID = 1


@dataclass(frozen=True, slots=True)
class ChatRef:
    """The subset of chat metadata links need. ``id`` is the Telethon *marked* id."""

    id: int
    title: str
    username: str | None
    kind: str  # 'supergroup' | 'channel' | 'group'
    is_forum: bool = False

    @property
    def bare_id(self) -> int:
        bare: int = utils.resolve_id(self.id)[0]
        return bare

    @property
    def is_basic_group(self) -> bool:
        return self.kind == "group"

    @classmethod
    def from_entity(
        cls, entity: tl.Chat | tl.Channel | tl.ChatForbidden | tl.ChannelForbidden
    ) -> ChatRef:
        marked = utils.get_peer_id(entity)
        title = getattr(entity, "title", None) or str(marked)
        if isinstance(entity, tl.Channel):
            kind = "supergroup" if entity.megagroup else "channel"
            return cls(marked, title, entity.username, kind, bool(entity.forum))
        if isinstance(entity, tl.ChannelForbidden):
            kind = "supergroup" if entity.megagroup else "channel"
            return cls(marked, title, None, kind, False)
        return cls(marked, title, None, "group", False)


def chat_link(chat: ChatRef) -> str | None:
    """Link to the chat itself; private chats have none (callers fall back to the message link)."""
    if chat.username:
        return f"https://t.me/{chat.username}"
    return None


def message_link(chat: ChatRef, msg_id: int, topic_id: int | None = None) -> str | None:
    if chat.is_basic_group:
        return None
    topic = f"/{topic_id}" if topic_id else ""
    if chat.username:
        return f"https://t.me/{chat.username}{topic}/{msg_id}"
    return f"https://t.me/c/{chat.bare_id}{topic}/{msg_id}"


def user_link(user_id: int, username: str | None) -> str:
    if username:
        return f"https://t.me/{username}"
    return f"tg://user?id={user_id}"


def topic_id_of(message: tl.Message) -> int | None:
    """Topic id for messages in forum supergroups; ``None`` outside forums."""
    reply_to = message.reply_to
    if isinstance(reply_to, tl.MessageReplyHeader) and reply_to.forum_topic:
        return reply_to.reply_to_top_id or reply_to.reply_to_msg_id or GENERAL_TOPIC_ID
    return None
"""Timezone resolution, timestamp formatting and short relative-time strings."""

from __future__ import annotations

import datetime as dt
import functools
import zoneinfo
from collections.abc import Callable
from dataclasses import dataclass, field

Clock = Callable[[], dt.datetime]
"""Injectable UTC-now source so tests can freeze time."""

MAX_CANDIDATES = 5

# Legacy / colloquial spellings people type; values must exist in the tz database.
_ALIASES: dict[str, str] = {
    "kiev": "Europe/Kyiv",
    "kyiv": "Europe/Kyiv",
    "київ": "Europe/Kyiv",
    "киев": "Europe/Kyiv",
    "moscow": "Europe/Moscow",
    "москва": "Europe/Moscow",
    "warsaw": "Europe/Warsaw",
    "варшава": "Europe/Warsaw",
    "berlin": "Europe/Berlin",
    "берлин": "Europe/Berlin",
    "берлін": "Europe/Berlin",
    "london": "Europe/London",
    "лондон": "Europe/London",
    "utc": "UTC",
    "gmt": "UTC",
    "z": "UTC",
}


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@functools.cache
def _zones() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones())


@functools.cache
def _zones_lower() -> dict[str, str]:
    return {z.lower(): z for z in _zones()}


@dataclass(frozen=True, slots=True)
class TzResolution:
    """``exact`` when the input unambiguously names a zone; otherwise up to five candidates."""

    exact: str | None = None
    candidates: list[str] = field(default_factory=list)
    fixed_offset: bool = False  # Etc/GMT±N zones have no DST; the UI warns about it


def validate_tz(name: str) -> str | None:
    """Exact validation used for stored values: the name must exist in the tz database.

    Matching is case-sensitive on purpose (macOS resolves ``europe/kyiv`` from its
    case-insensitive filesystem, Linux does not), except for the ``UTC`` spellings.
    """
    candidate = name.strip()
    if not candidate:
        return None
    if candidate.upper() in {"UTC", "Z", "GMT"}:
        return "UTC"
    if candidate in _zones():
        return candidate
    return None


def resolve_tz(text: str) -> TzResolution:
    """Resolve user input into a zone name or a short list of suggestions."""
    raw = text.strip()
    if not raw:
        return TzResolution()
    exact = validate_tz(raw)
    if exact:
        return TzResolution(exact=exact)
    offset = _parse_fixed_offset(raw)
    if offset is not None:
        return TzResolution(exact=offset, fixed_offset=offset != "UTC")
    lowered = raw.lower()
    alias = _ALIASES.get(lowered)
    if alias:
        return TzResolution(exact=alias)
    case_fixed = _zones_lower().get(lowered)
    if case_fixed:
        return TzResolution(exact=case_fixed)
    needle = lowered.replace(" ", "_")
    suffix_hits = sorted(z for z in _zones() if z.lower().rsplit("/", 1)[-1] == needle)
    if len(suffix_hits) == 1:
        return TzResolution(exact=suffix_hits[0])
    if suffix_hits:
        return TzResolution(candidates=suffix_hits[:MAX_CANDIDATES])
    partial = sorted(z for z in _zones() if needle in z.lower() and "/" in z)
    return TzResolution(candidates=partial[:MAX_CANDIDATES])


def _parse_fixed_offset(text: str) -> str | None:
    """``+3`` / ``-05`` / ``+03:00`` / ``UTC+3`` → the matching ``Etc/GMT∓N`` zone (whole hours)."""
    body = text.upper().removeprefix("UTC").removeprefix("GMT")
    if not body or body[0] not in "+-" or len(body) > 6:
        return None
    hours_part, _, minutes_part = body[1:].partition(":")
    if not hours_part.isdigit() or (minutes_part and minutes_part != "00"):
        return None
    hours = int(hours_part)
    if hours > 14:
        return None
    if hours == 0:
        return "UTC"
    sign = "-" if body[0] == "+" else "+"  # POSIX sign inversion: Etc/GMT-3 is UTC+3
    name = f"Etc/GMT{sign}{hours}"
    return name if name in _zones() else None


def to_zone(moment: dt.datetime, tz: str) -> dt.datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(zoneinfo.ZoneInfo(tz))


def utc_offset_label(local: dt.datetime) -> str:
    offset = local.utcoffset() or dt.timedelta()
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def format_local(moment: dt.datetime, tz: str) -> str:
    """``2026-09-22 14:03:05 +03:00 (Europe/Kyiv)``; the offset disambiguates DST folds."""
    local = to_zone(moment, tz)
    label = "UTC" if tz.startswith("Etc/GMT") else tz
    return f"{local:%Y-%m-%d %H:%M:%S} {utc_offset_label(local)} ({label})"


def format_clock(moment: dt.datetime, tz: str) -> str:
    """``14:03 (UTC+03:00)`` for the timezone confirmation prompt."""
    local = to_zone(moment, tz)
    return f"{local:%H:%M} (UTC{utc_offset_label(local)})"


_UNITS: dict[str, tuple[str, str, str, str]] = {
    # seconds, minutes, hours, days — abbreviations, so no plural forms are needed
    "en": ("s", "min", "h", "d"),
    "ru": ("сек", "мин", "ч", "д"),
    "uk": ("с", "хв", "год", "д"),
}
_AGO: dict[str, str] = {"en": "{n} {u} ago", "ru": "{n} {u} назад", "uk": "{n} {u} тому"}


def relative(moment: dt.datetime, now: dt.datetime, lang: str) -> str:
    """Short 'N unit ago' string; negative deltas (clock skew) are shown as 0 s."""
    seconds = max(0, int((now - moment).total_seconds()))
    units = _UNITS.get(lang, _UNITS["en"])
    template = _AGO.get(lang, _AGO["en"])
    if seconds < 60:
        n, u = seconds, units[0]
    elif seconds < 3600:
        n, u = seconds // 60, units[1]
    elif seconds < 86400:
        n, u = seconds // 3600, units[2]
    else:
        n, u = seconds // 86400, units[3]
    return template.format(n=n, u=u)

**Assessing message length limits and error handling**
**Identifying render_digest truncation issues**
exec
/bin/zsh -lc "rg -n \"to_thread|run_in_executor|asyncio\\.create_task|TaskGroup|subscribe\\(|get_delivery|get_rule|list_rules|list_subscriptions|pending_deliveries|set_delivery_status|drop_user|reconnected|InputPeer\" ddiver tests" in /Users/L/Code/ddiver
 succeeded in 0ms:
tests/test_delivery.py:116:        row = await store.get_delivery(*item.key)
tests/test_delivery.py:128:        assert await store.pending_deliveries() == []
tests/test_delivery.py:169:        row = await store.get_delivery(*item.key)
tests/test_delivery.py:184:        assert (await store.get_delivery(*item.key)).status is DeliveryStatus.FAILED
tests/test_delivery.py:195:        assert (await store.get_delivery(*item.key)).status is DeliveryStatus.SENT
tests/test_delivery.py:203:        row = await store.get_delivery(*item.key)
tests/test_delivery.py:215:        assert (await store.get_delivery(*item.key)).status is DeliveryStatus.SENT
tests/test_delivery.py:222:        row = await store.get_delivery(*item.key)
tests/test_delivery.py:232:        assert (await store.get_delivery(*item.key)).status is DeliveryStatus.FAILED
tests/test_delivery.py:239:        assert (await store.get_delivery(*first.key)).status is DeliveryStatus.FAILED
tests/test_delivery.py:244:        assert (await store.get_delivery(*second.key)).status is DeliveryStatus.SENT
tests/test_delivery.py:260:        rows = [await store.get_delivery(*i.key) for i in items]
tests/test_delivery.py:328:        await delivery.drop_user(MOD)  # simulates a hard stop before the worker ran
tests/test_delivery.py:330:        assert [p.key for p in await store.pending_deliveries()] == [item.key]
tests/test_bot.py:193:        assert [c.id for c in await store.list_subscriptions(MOD)] == [CHAT_A.id]
tests/test_bot.py:196:        assert await store.list_subscriptions(MOD) == []
tests/test_bot.py:202:        assert await store.list_subscriptions(MOD) == []
tests/test_bot.py:207:        assert await store.list_subscriptions(MOD) == []
tests/test_bot.py:213:        assert await store.list_subscriptions(MOD) == []
tests/test_bot.py:233:        await store.subscribe(OWNER, CHAT_A.id)
tests/test_bot.py:244:        patterns = [r.pattern for r in await store.list_rules(OWNER, CHAT_A.id)]
tests/test_bot.py:261:        assert await store.get_rule(rule.id, MOD) is not None
tests/test_bot.py:264:        await store.subscribe(OWNER, CHAT_A.id)
tests/test_bot.py:272:        await store.subscribe(OWNER, CHAT_A.id)
tests/test_bot.py:310:        rules = await store.list_rules(OWNER, CHAT_A.id, RuleKind.EXCLUDE)
tests/test_bot.py:342:        rules = await store.list_rules(OWNER, CHAT_A.id, RuleKind.EXCLUDE)
tests/test_bot.py:372:        assert await store.list_rules(OWNER) == []
tests/test_bot.py:383:        patterns = [r.pattern for r in await store.list_rules(OWNER, CHAT_A.id)]
tests/test_bot.py:394:        await store.subscribe(OWNER, CHAT_A.id)
tests/test_bot.py:405:        await store.subscribe(OWNER, CHAT_A.id)
tests/test_listener.py:35:    await s.subscribe(OWNER, -1001234567890)
tests/test_listener.py:36:    await s.subscribe(OWNER, -1009876543210)
tests/test_listener.py:144:        await store.subscribe(MOD, -1001234567890)
tests/test_listener.py:177:        rules = await store.list_rules(OWNER)
tests/test_listener.py:182:        assert (await store.get_rule(rules[0].id, OWNER)).hits == 3
tests/test_catchup.py:36:    await s.subscribe(OWNER, CHAT_ID)
tests/test_catchup.py:37:    await s.subscribe(OWNER, OTHER_ID)
tests/test_storage.py:176:        assert await seeded.subscribe(MOD, -9999) is False  # unknown chat
tests/test_storage.py:177:        assert await seeded.subscribe(MOD, 12345) is False  # a positive id is a private chat
tests/test_storage.py:178:        assert await seeded.subscribe(MOD, CHAT_A.id) is True
tests/test_storage.py:179:        assert await seeded.subscribe(MOD, CHAT_A.id) is False  # idempotent
tests/test_storage.py:182:        await seeded.subscribe(OWNER, CHAT_A.id)
tests/test_storage.py:183:        await seeded.subscribe(MOD, CHAT_B.id)
tests/test_storage.py:184:        await seeded.subscribe(STRANGER, CHAT_B.id)
tests/test_storage.py:193:        await seeded.subscribe(MOD, CHAT_A.id)
tests/test_storage.py:194:        await seeded.subscribe(STRANGER, CHAT_A.id)
tests/test_storage.py:199:        await seeded.subscribe(OWNER, CHAT_A.id)
tests/test_storage.py:200:        await seeded.subscribe(MOD, CHAT_A.id)
tests/test_storage.py:202:        assert [c.id for c in await seeded.list_subscriptions(OWNER)] == [CHAT_A.id]
tests/test_storage.py:203:        assert await seeded.list_subscriptions(MOD) == []
tests/test_storage.py:204:        assert await seeded.unsubscribe(OWNER, CHAT_A.id) is True
tests/test_storage.py:205:        assert await seeded.unsubscribe(OWNER, CHAT_A.id) is False
tests/test_storage.py:208:        await seeded.subscribe(MOD, CHAT_A.id)
tests/test_storage.py:215:            await store.subscribe(404, CHAT_A.id)
tests/test_storage.py:219:        await seeded.subscribe(MOD, BASIC.id)
tests/test_storage.py:225:        assert [c.id for c in await seeded.list_subscriptions(MOD)] == [new_ref.id]
tests/test_storage.py:226:        assert (await seeded.get_rule(rule.id, MOD)).chat_id == new_ref.id
tests/test_storage.py:235:        await seeded.subscribe(OWNER, CHAT_A.id)
tests/test_storage.py:236:        await seeded.subscribe(MOD, CHAT_A.id)
tests/test_storage.py:241:        assert [r.id for r in await seeded.list_rules(OWNER)] == [global_rule.id, chat_rule.id]
tests/test_storage.py:242:        assert [r.id for r in await seeded.list_rules(OWNER, CHAT_A.id)] == [chat_rule.id]
tests/test_storage.py:243:        assert await seeded.get_rule(mod_rule.id, OWNER) is None
tests/test_storage.py:260:        await seeded.subscribe(OWNER, CHAT_A.id)
tests/test_storage.py:261:        await seeded.subscribe(MOD, CHAT_A.id)
tests/test_storage.py:262:        await seeded.subscribe(STRANGER, CHAT_A.id)
tests/test_storage.py:277:        await seeded.subscribe(OWNER, CHAT_A.id)
tests/test_storage.py:281:        assert (await seeded.get_rule(rule.id, OWNER)).disabled_reason == "timeout"
tests/test_storage.py:291:        assert (await seeded.get_rule(r1.id, OWNER)).hits == 3
tests/test_storage.py:292:        assert (await seeded.get_rule(r2.id, OWNER)).hits == 1
tests/test_storage.py:295:        await seeded.subscribe(MOD, CHAT_A.id)
tests/test_storage.py:299:        assert await seeded.list_rules(MOD) == []
tests/test_storage.py:355:        await store.set_delivery_status(OWNER, -1001, 10, DeliveryStatus.SENT)
tests/test_storage.py:356:        pending = await store.pending_deliveries()
tests/test_storage.py:361:        await store.set_delivery_status(
tests/test_storage.py:364:        item = await store.get_delivery(OWNER, -1001, 10)
tests/test_storage.py:366:        await store.set_delivery_status(OWNER, -1001, 10, DeliveryStatus.SENT)
tests/test_storage.py:367:        item = await store.get_delivery(OWNER, -1001, 10)
tests/test_storage.py:377:        assert await store.pending_deliveries() == []
tests/test_storage.py:388:        await store.set_delivery_status(OWNER, -1001, 2, DeliveryStatus.SENT)
tests/test_storage.py:391:        await store.set_delivery_status(OWNER, -1001, 3, DeliveryStatus.SENT)
tests/test_storage.py:393:        assert {i.msg_id for i in await store.pending_deliveries()} == {1}
tests/test_storage.py:394:        assert await store.get_delivery(OWNER, -1001, 3) is not None
tests/test_matcher.py:22:    await s.subscribe(1, CHAT_A.id)
tests/test_matcher.py:23:    await s.subscribe(2, CHAT_A.id)
tests/test_matcher.py:24:    await s.subscribe(1, CHAT_B.id)
tests/test_matcher.py:90:    await store.unsubscribe(1, CHAT_B.id)
tests/test_matcher.py:108:    row = await store.get_rule(bad.id, 1)
tests/test_matcher.py:130:    row = await store.get_rule(slow.id, 1)
ddiver/delivery.py:134:        items = await self._storage.pending_deliveries()
ddiver/delivery.py:167:    async def drop_user(self, user_id: int) -> None:
ddiver/delivery.py:223:            state.worker = asyncio.create_task(
ddiver/delivery.py:241:                    await self._storage.set_delivery_status(
ddiver/delivery.py:273:            await self._storage.set_delivery_status(*item.key, DeliveryStatus.SENT)
ddiver/delivery.py:330:                await self._storage.set_delivery_status(
ddiver/delivery.py:333:                await self.drop_user(user_id)
ddiver/delivery.py:363:        await self._storage.set_delivery_status(
ddiver/clients.py:25:    """Telethon client that tells us when it reconnected.
ddiver/clients.py:35:        self.reconnected = asyncio.Event()
ddiver/clients.py:44:        self.reconnected.set()
ddiver/clients.py:45:        log.info("listener reconnected (%d since start); scheduling a catch-up", self.reconnects)
ddiver/app.py:184:            self._task = asyncio.create_task(self._loop(), name=self._name)
ddiver/runtime.py:269:        asyncio.create_task(self._catch_up(), name="catchup-boot")  # noqa: RUF006
ddiver/runtime.py:451:        if self.user_client.reconnected.is_set():
ddiver/runtime.py:452:            self.user_client.reconnected.clear()
ddiver/storage.py:486:    async def subscribe(self, user_id: int, chat_id: int) -> bool:
ddiver/storage.py:497:    async def unsubscribe(self, user_id: int, chat_id: int) -> bool:
ddiver/storage.py:526:    async def list_subscriptions(self, user_id: int) -> list[ChatRow]:
ddiver/storage.py:583:        rule = await self.get_rule(int(cur.lastrowid or 0), user_id)
ddiver/storage.py:588:    async def get_rule(self, rule_id: int, user_id: int) -> Rule | None:
ddiver/storage.py:611:    async def list_rules(
ddiver/storage.py:777:        return await self.get_delivery(user_id, chat_id, msg_id)
ddiver/storage.py:779:    async def get_delivery(self, user_id: int, chat_id: int, msg_id: int) -> OutboxItem | None:
ddiver/storage.py:787:    async def pending_deliveries(self, limit: int = 5000) -> list[OutboxItem]:
ddiver/storage.py:794:    async def set_delivery_status(
ddiver/bot/handlers.py:187:        subscribed = {c.id for c in await storage.list_subscriptions(ctx.user_id)}
ddiver/bot/handlers.py:207:        subscribed = {c.id for c in await storage.list_subscriptions(ctx.user_id)}
ddiver/bot/handlers.py:217:    async def subscribe(ctx: Context) -> list[Action]:
ddiver/bot/handlers.py:224:            await storage.subscribe(ctx.user_id, chat_id)
ddiver/bot/handlers.py:226:            await storage.unsubscribe(ctx.user_id, chat_id)
ddiver/bot/handlers.py:229:        subscribed = {c.id for c in await storage.list_subscriptions(ctx.user_id)}
ddiver/bot/handlers.py:297:            chats = len(await storage.list_subscriptions(user_id))
ddiver/bot/handlers.py:351:        rules = await storage.list_rules(ctx.user_id, chat_id, kind)
ddiver/bot/handlers.py:371:            chats = await storage.list_subscriptions(ctx.user_id)
ddiver/bot/handlers.py:381:        chats = await storage.list_subscriptions(ctx.user_id)
ddiver/bot/handlers.py:447:        rule = await storage.get_rule(rule_id, ctx.user_id)
ddiver/bot/handlers.py:518:        item = await storage.get_delivery(ctx.user_id, chat_id, msg_id)
ddiver/bot/handlers.py:548:        item = await storage.get_delivery(ctx.user_id, chat_id, msg_id)
ddiver/bot/handlers.py:670:        chats = await storage.list_subscriptions(ctx.user_id)

**Assessing regex-induced event loop blocking**
**Identifying migration unique constraint conflict**
exec
/bin/zsh -lc "rg -n \"migrate_chat|ChatMigrate|migrate\" ddiver tests/test_listener.py tests/test_storage.py && sed -n '1,280p' tests/test_listener.py && sed -n '1,300p' tests/test_catchup.py" in /Users/L/Code/ddiver
 succeeded in 0ms:
tests/test_storage.py:217:    async def test_migrate_basic_group_to_supergroup(self, seeded):
tests/test_storage.py:224:        await seeded.migrate_chat(BASIC.id, new_ref, access_hash=42)
ddiver/i18n/en.py:94:        "bot.chats.migrated": "ℹ️ {chat} became a supergroup; your rules moved with it.",
ddiver/i18n/uk.py:90:        "bot.chats.migrated": "ℹ️ {chat} став супергрупою; правила перенесено.",
ddiver/i18n/ru.py:90:        "bot.chats.migrated": "ℹ️ {chat} стал супергруппой; правила перенесены.",
ddiver/storage.py:251:            await self._migrate()
ddiver/storage.py:276:    async def _migrate(self) -> None:
ddiver/storage.py:465:    async def migrate_chat(self, old_id: int, new_ref: ChatRef, access_hash: int | None) -> None:
import datetime as dt

import pytest
from telethon.tl import types as tl

from ddiver.links import ChatRef
from ddiver.listener import CursorTracker, Listener, chat_ref_of, should_consider
from ddiver.matcher import Matcher
from ddiver.rules import RuleKind
from ddiver.senders import SenderCache
from ddiver.storage import ALL_CHATS, Cursor, Role, Storage
from tests.fakes import T0, FakeClock, make_channel, make_event, make_user

OWNER = 1
MOD = 2
CHAT = make_channel(-1001234567890, "Чат модерации")
FORUM = make_channel(-1009876543210, "Forum", forum=True)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
async def store(tmp_path, clock):
    s = Storage(tmp_path / "db.sqlite3", owner_id=OWNER, clock=clock)
    await s.open()
    await s.ensure_user(OWNER)
    await s.update_user(OWNER, lang="ru", tz="Europe/Kyiv")
    await s.ensure_user(MOD)
    await s.update_user(MOD, role=Role.ALLOWED, lang="en", tz="UTC")
    await s.upsert_chat(ChatRef.from_entity(CHAT))
    await s.upsert_chat(ChatRef.from_entity(FORUM))
    await s.subscribe(OWNER, -1001234567890)
    await s.subscribe(OWNER, -1009876543210)
    await s.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "казино")
    yield s
    await s.close()


@pytest.fixture
async def listener(store, clock):
    matcher = Matcher(store, timeout=0.2)
    await matcher.reload()
    delivered = []

    async def deliver(item):
        delivered.append(item)

    listener = Listener(
        store, matcher, SenderCache(None), deliver=deliver, clock=clock, ignore_own=True
    )
    await listener.prime_chat_cache()
    listener.delivered = delivered
    listener.matcher = matcher
    return listener


class TestShouldConsider:
    def test_plain_message(self):
        assert should_consider(make_event("казино").message)

    def test_service_message_is_ignored(self):
        service = tl.MessageService(
            id=1,
            peer_id=tl.PeerChannel(1),
            date=T0,
            action=tl.MessageActionChatJoinedByLink(inviter_id=1),
        )
        assert not should_consider(service)

    def test_media_without_caption_is_ignored(self):
        message = make_event("").message
        message.message = None
        assert not should_consider(message)
        message.message = ""
        assert not should_consider(message)

    def test_outgoing_is_ignored_unless_the_test_flag_is_set(self):
        message = make_event("казино", out=True).message
        assert not should_consider(message)
        assert should_consider(message, ignore_own=False)


class TestHandle:
    async def test_match_claims_an_outbox_row_and_delivers(self, listener, store, clock):
        event = make_event("Заходи в казино", chat=CHAT)
        items = await listener.handle(event.message, chat=event.chat)
        assert len(items) == 1
        item = items[0]
        assert item.user_id == OWNER
        assert item.payload["chat_title"] == "Чат модерации"
        assert item.payload["spans"] == [[9, 15]]
        assert item.payload["sender_name"] == "Иван"
        assert item.payload["sender_username"] == "ivan"
        assert item.payload["catchup"] is False
        assert listener.delivered == items
        # the cursor only advances after the row exists
        assert listener.cursors.pending[-1001234567890][0] == event.message.id

    async def test_no_match_still_advances_the_cursor(self, listener):
        event = make_event("ничего интересного", chat=CHAT)
        assert await listener.handle(event.message, chat=event.chat) == []
        assert listener.cursors.pending[-1001234567890][0] == event.message.id

    async def test_unmonitored_chat_is_ignored(self, listener):
        other = make_channel(-1005555555555, "Other")
        event = make_event("казино", chat=other)
        assert await listener.handle(event.message, chat=event.chat) == []
        assert listener.cursors.pending == {}

    async def test_private_chat_is_never_monitored(self, listener, store):
        message = make_event("казино").message
        message.peer_id = tl.PeerUser(4242)
        assert await listener.handle(message) == []
        assert listener.metrics.skipped_private == 1

    async def test_bot_senders_are_dropped_after_matching(self, listener):
        bot_user = make_user(77, "Spam Bot", "spambot", bot=True)
        event = make_event("казино", chat=CHAT, sender=bot_user)
        assert await listener.handle(event.message, chat=event.chat) == []
        assert listener.metrics.skipped_bot == 1
        assert listener.cursors.pending[-1001234567890][0] == event.message.id

    async def test_own_messages_are_skipped(self, listener):
        event = make_event("казино", chat=CHAT, out=True)
        assert await listener.handle(event.message, chat=event.chat) == []
        assert listener.metrics.skipped_own == 1

    async def test_forwarded_and_topic_metadata(self, listener):
        event = make_event("казино", chat=FORUM, topic_id=7, fwd_from_name="Someone")
        items = await listener.handle(event.message, chat=event.chat)
        assert items[0].payload["topic_id"] == 7
        assert items[0].payload["is_forwarded"] is True

    async def test_duplicate_message_is_claimed_once(self, listener):
        event = make_event("казино", chat=CHAT)
        assert len(await listener.handle(event.message, chat=event.chat)) == 1
        assert await listener.handle(event.message, chat=event.chat) == []
        assert listener.metrics.duplicates == 1

    async def test_every_subscriber_gets_their_own_row(self, listener, store):
        await store.subscribe(MOD, -1001234567890)
        await store.add_rule(MOD, ALL_CHATS, RuleKind.INCLUDE, "казино")
        await listener.matcher.invalidate()
        event = make_event("казино", chat=CHAT)
        items = await listener.handle(event.message, chat=event.chat)
        assert {i.user_id for i in items} == {OWNER, MOD}

    async def test_all_occurrences_are_highlighted(self, listener):
        event = make_event("казино и ещё казино", chat=CHAT)
        items = await listener.handle(event.message, chat=event.chat)
        assert items[0].payload["spans"] == [[0, 6], [13, 19]]

    async def test_zero_width_text_maps_spans_back_to_the_original(self, listener):
        event = make_event("реклама ка\u200bзино тут", chat=CHAT)
        items = await listener.handle(event.message, chat=event.chat)
        start, end = items[0].payload["spans"][0]
        assert event.message.message[start:end] == "ка\u200bзино"

    async def test_anonymous_admin_sender(self, listener):
        event = make_event("казино", chat=CHAT, sender=CHAT)
        items = await listener.handle(event.message, chat=event.chat)
        assert items[0].payload["sender_id"] == -1001234567890
        assert items[0].payload["sender_is_channel"] is True

    async def test_chat_metadata_is_refreshed_when_it_changes(self, listener, store):
        renamed = make_channel(-1001234567890, "Новое название", username="newname")
        event = make_event("казино", chat=renamed)
        await listener.handle(event.message, chat=event.chat)
        row = await store.get_chat(-1001234567890)
        assert row.ref.title == "Новое название"
        assert row.ref.username == "newname"

    async def test_rule_hits_are_counted_and_flushed(self, listener, store):
        rules = await store.list_rules(OWNER)
        for i in range(3):
            event = make_event("казино", chat=CHAT, msg_id=100 + i)
            await listener.handle(event.message, chat=event.chat)
        await listener.matcher.flush_hits()
        assert (await store.get_rule(rules[0].id, OWNER)).hits == 3

    async def test_on_message_never_raises(self, listener, caplog):
        class Broken:
            @property
            def message(self):
                raise RuntimeError("boom")

            chat = None

        await listener.on_message(Broken())
        assert "listener failed" in caplog.text


class TestCursorTracker:
    async def test_keeps_the_highest_id_and_flushes(self, store):
        tracker = CursorTracker(store)
        tracker.note(-1001, 10, T0)
        tracker.note(-1001, 5, T0)
        tracker.note(-1002, 3, T0)
        assert await tracker.flush() == 2
        cursors = await store.get_cursors()
        assert cursors[-1001].last_msg_id == 10
        assert tracker.pending == {}

    async def test_blocked_chats_are_withheld(self, store):
        tracker = CursorTracker(store)
        tracker.note(-1001, 10, T0)
        tracker.note(-1002, 4, T0)
        tracker.block(-1001)
        assert await tracker.flush() == 1
        assert set(await store.get_cursors()) == {-1002}
        assert -1001 in tracker.pending
        tracker.unblock(-1001)
        assert await tracker.flush() == 1
        assert set(await store.get_cursors()) == {-1001, -1002}

    async def test_flush_with_nothing_pending(self, store):
        assert await CursorTracker(store).flush() == 0

    async def test_existing_cursor_is_never_regressed(self, store):
        await store.upsert_cursors([Cursor(-1001, 50, T0)])
        tracker = CursorTracker(store)
        tracker.note(-1001, 20, T0 + dt.timedelta(seconds=5))
        await tracker.flush()
        assert (await store.get_cursors())[-1001].last_msg_id == 50


class TestChatRefOf:
    def test_users_and_none_are_rejected(self):
        assert chat_ref_of(None) is None
        assert chat_ref_of(make_user()) is None

    def test_channel_and_forbidden(self):
        assert chat_ref_of(CHAT).title == "Чат модерации"
        forbidden = tl.ChannelForbidden(id=5, access_hash=0, title="Gone", megagroup=True)
        assert chat_ref_of(forbidden).kind == "supergroup"
import datetime as dt

import pytest
from telethon import errors
from telethon.tl import types as tl

from ddiver.catchup import Catchup
from ddiver.links import ChatRef
from ddiver.listener import Listener
from ddiver.matcher import Matcher
from ddiver.rules import RuleKind
from ddiver.senders import SenderCache
from ddiver.storage import ALL_CHATS, Cursor, Storage
from tests.fakes import T0, FakeClock, make_channel, make_event

OWNER = 1
CHAT_ID = -1001234567890
OTHER_ID = -1005555555555
CHAT = make_channel(CHAT_ID, "Чат")
OTHER = make_channel(OTHER_ID, "Другой")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
async def store(tmp_path, clock):
    s = Storage(tmp_path / "db.sqlite3", owner_id=OWNER, clock=clock)
    await s.open()
    await s.ensure_user(OWNER)
    await s.update_user(OWNER, lang="ru", tz="UTC")
    await s.upsert_chat(ChatRef.from_entity(CHAT))
    await s.upsert_chat(ChatRef.from_entity(OTHER))
    await s.subscribe(OWNER, CHAT_ID)
    await s.subscribe(OWNER, OTHER_ID)
    await s.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "казино")
    yield s
    await s.close()


@pytest.fixture
async def listener(store, clock):
    matcher = Matcher(store, timeout=0.2)
    await matcher.reload()
    delivered = []

    async def deliver(item):
        delivered.append(item)

    listener = Listener(store, matcher, SenderCache(None), deliver=deliver, clock=clock)
    await listener.prime_chat_cache()
    listener.delivered = delivered
    return listener


class FakeHistory:
    """Newest-first history, like Telethon's ``iter_messages(min_id=…)``."""

    def __init__(self, messages: dict[int, list[tl.Message]]) -> None:
        self.messages = messages
        self.calls: list[tuple[int, int, int]] = []
        self.errors: dict[int, BaseException] = {}

    def __call__(self, chat_id: int, min_id: int, limit: int):
        self.calls.append((chat_id, min_id, limit))
        error = self.errors.get(chat_id)

        async def gen():
            if error is not None:
                raise error
            ordered = sorted(self.messages.get(chat_id, []), key=lambda m: m.id, reverse=True)
            for count, message in enumerate(ordered, start=1):
                if message.id <= min_id:
                    return
                yield message
                if count >= limit:
                    return

        return gen()


def history_of(chat, ids_and_texts, *, base_time=T0, step=60):
    messages = []
    for index, (msg_id, text) in enumerate(ids_and_texts):
        event = make_event(
            text, chat=chat, msg_id=msg_id, date=base_time - dt.timedelta(seconds=step * index)
        )
        messages.append(event.message)
    return messages


@pytest.fixture
def sleeps():
    calls = []

    async def sleep(seconds):
        calls.append(seconds)

    sleep.calls = calls
    return sleep


class TestCatchup:
    async def test_replays_newest_messages_in_chronological_order(
        self, store, listener, clock, sleeps
    ):
        await store.upsert_cursors([Cursor(CHAT_ID, 100, T0 - dt.timedelta(minutes=30))])
        history = FakeHistory(
            {
                CHAT_ID: history_of(
                    CHAT,
                    [(103, "казино три"), (102, "тишина"), (101, "казино один")],
                )
            }
        )
        catchup = Catchup(store, listener, history, clock=clock, sleep=sleeps, hours=24)
        result = await catchup.run({CHAT_ID})
        assert history.calls == [(CHAT_ID, 100, 500)]
        assert result.chats == 1
        assert result.claimed == 2
        assert [i.msg_id for i in listener.delivered] == [101, 103]  # oldest first
        assert all(i.payload["catchup"] is True for i in listener.delivered)
        assert (await store.get_cursors())[CHAT_ID].last_msg_id == 103

    async def test_old_cursor_still_delivers_recent_messages(self, store, listener, clock, sleeps):
        # The cursor is three days old; only messages inside the window must be delivered.
        await store.upsert_cursors([Cursor(CHAT_ID, 10, T0 - dt.timedelta(days=3))])
        messages = history_of(
            CHAT,
            [(30, "казино свежее"), (20, "казино старое")],
            step=0,
        )
        messages[1].date = T0 - dt.timedelta(days=2)
        history = FakeHistory({CHAT_ID: messages})
        catchup = Catchup(store, listener, history, clock=clock, sleep=sleeps, hours=24)
        result = await catchup.run({CHAT_ID})
        assert [i.msg_id for i in listener.delivered] == [30]
        assert result.claimed == 1
        assert (await store.get_cursors())[CHAT_ID].last_msg_id == 30

    async def test_cap_is_reported_on_the_first_notification(self, store, listener, clock, sleeps):
        await store.upsert_cursors([Cursor(CHAT_ID, 0, T0)])
        history = FakeHistory(
            {CHAT_ID: history_of(CHAT, [(i, "казино") for i in range(20, 0, -1)], step=1)}
        )
        catchup = Catchup(store, listener, history, clock=clock, sleep=sleeps, per_chat=5)
        result = await catchup.run({CHAT_ID})
        assert result.capped_chats == [CHAT_ID]
        assert [i.msg_id for i in listener.delivered] == [16, 17, 18, 19, 20]
        assert listener.delivered[0].payload["catchup_skipped"] == 5
        assert listener.delivered[1].payload["catchup_skipped"] == 0

    async def test_chats_without_a_cursor_are_skipped(self, store, listener, clock, sleeps):
        history = FakeHistory({CHAT_ID: history_of(CHAT, [(1, "казино")])})
        result = await catchup_of(store, listener, history, clock, sleeps).run({CHAT_ID})
        assert history.calls == []
        assert result.chats == 0

    async def test_unavailable_chats_are_not_probed(self, store, listener, clock, sleeps):
        await store.upsert_cursors([Cursor(CHAT_ID, 1, T0)])
        await store.set_chat_available(CHAT_ID, False)
        history = FakeHistory({CHAT_ID: history_of(CHAT, [(2, "казино")])})
        result = await catchup_of(store, listener, history, clock, sleeps).run({CHAT_ID})
        assert history.calls == []
        assert result.skipped == 1

    async def test_lost_access_marks_the_chat_and_notifies_once(
        self, store, listener, clock, sleeps
    ):
        notified = []

        async def on_unavailable(chat_id, reason):
            notified.append((chat_id, reason))

        await store.upsert_cursors([Cursor(CHAT_ID, 1, T0)])
        history = FakeHistory({})
        history.errors[CHAT_ID] = errors.ChannelPrivateError(request=None)
        catchup = Catchup(
            store, listener, history, clock=clock, sleep=sleeps, on_unavailable=on_unavailable
        )
        result = await catchup.run({CHAT_ID})
        assert result.unavailable == [CHAT_ID]
        assert notified == [(CHAT_ID, "ChannelPrivateError")]
        assert (await store.get_chat(CHAT_ID)).available is False

    async def test_flood_wait_skips_only_that_chat(self, store, listener, clock, sleeps):
        await store.upsert_cursors(
            [Cursor(CHAT_ID, 1, T0), Cursor(OTHER_ID, 1, T0)],
        )
        history = FakeHistory({OTHER_ID: history_of(OTHER, [(5, "казино")])})
        history.errors[CHAT_ID] = errors.FloodWaitError(request=None, capture=30)
        catchup = Catchup(store, listener, history, clock=clock, sleep=sleeps)
        result = await catchup.run({CHAT_ID, OTHER_ID})
        assert result.flood_waits == 1
        assert result.claimed == 1
        assert [i.chat_id for i in listener.delivered] == [OTHER_ID]
        assert (await store.get_chat(CHAT_ID)).available is True  # a flood wait is temporary

    async def test_cursor_is_withheld_until_the_chat_finished(self, store, listener, clock):
        await store.upsert_cursors([Cursor(CHAT_ID, 1, T0)])
        history = FakeHistory({CHAT_ID: history_of(CHAT, [(9, "казино")])})
        seen_blocked = []

        async def sleep(_seconds):
            seen_blocked.append(set(listener.cursors.blocked))

        catchup = Catchup(store, listener, history, clock=clock, sleep=sleep)

        original = listener.handle

        async def spy(message, **kwargs):
            seen_blocked.append(set(listener.cursors.blocked))
            return await original(message, **kwargs)

        listener.handle = spy
        await catchup.run({CHAT_ID})
        assert seen_blocked[0] == {CHAT_ID}  # blocked while replaying
        assert listener.cursors.blocked == set()  # released afterwards

    async def test_total_budget_stops_the_round(self, store, listener, clock, sleeps):
        await store.upsert_cursors([Cursor(CHAT_ID, 0, T0), Cursor(OTHER_ID, 0, T0)])
        history = FakeHistory(
            {
                CHAT_ID: history_of(CHAT, [(i, "казино") for i in range(10, 0, -1)], step=1),
                OTHER_ID: history_of(OTHER, [(i, "казино") for i in range(10, 0, -1)], step=1),
            }
        )
        catchup = Catchup(store, listener, history, clock=clock, sleep=sleeps, total=5)
        result = await catchup.run({CHAT_ID, OTHER_ID})
        assert result.chats == 1
        assert len(history.calls) == 1  # the second chat is left for the next round

    async def test_service_and_own_messages_are_filtered(self, store, listener, clock, sleeps):
        await store.upsert_cursors([Cursor(CHAT_ID, 0, T0)])
        own = make_event("казино", chat=CHAT, msg_id=3, out=True).message
        service = tl.MessageService(
            id=2,
            peer_id=tl.PeerChannel(1234567890),
            date=T0,
            action=tl.MessageActionChatJoinedByLink(inviter_id=1),
        )
        normal = make_event("казино", chat=CHAT, msg_id=1).message
        history = FakeHistory({CHAT_ID: [own, service, normal]})
        result = await catchup_of(store, listener, history, clock, sleeps).run({CHAT_ID})
        assert result.claimed == 1
        assert [i.msg_id for i in listener.delivered] == [1]
        assert (await store.get_cursors())[CHAT_ID].last_msg_id == 3  # cursor still advances

    async def test_disabled_by_configuration(self, store, listener, clock, sleeps):
        await store.upsert_cursors([Cursor(CHAT_ID, 0, T0)])
        history = FakeHistory({CHAT_ID: history_of(CHAT, [(1, "казино")])})
        catchup = Catchup(store, listener, history, clock=clock, sleep=sleeps, hours=0)
        assert (await catchup.run({CHAT_ID})).chats == 0
        assert history.calls == []

    async def test_dedupe_between_catchup_and_live(self, store, listener, clock, sleeps):
        await store.upsert_cursors([Cursor(CHAT_ID, 0, T0)])
        event = make_event("казино", chat=CHAT, msg_id=7)
        await listener.handle(event.message, chat=event.chat)  # live first
        history = FakeHistory({CHAT_ID: [event.message]})
        result = await catchup_of(store, listener, history, clock, sleeps).run({CHAT_ID})
        assert result.claimed == 0
        assert len(listener.delivered) == 1


def catchup_of(store, listener, history, clock, sleeps) -> Catchup:
    return Catchup(store, listener, history, clock=clock, sleep=sleeps)

**Identifying missing runtime migration handling**
**Investigating access loss in channel updates**
exec
/bin/zsh -lc "uv run python - <<'PY'
import telethon, inspect
from telethon.client.updates import UpdateMethods
from telethon.events import raw
print(telethon.__version__)
print(inspect.getsource(raw.Raw.build))
print(inspect.getsource(UpdateMethods._dispatch_update))
PY" in /Users/L/Code/ddiver
 succeeded in 76ms:
1.45.0
    @classmethod
    def build(cls, update, others=None, self_id=None):
        return update

    async def _dispatch_update(self: 'TelegramClient', update):
        # TODO only used for AlbumHack, and MessageBox is not really designed for this
        others = None

        if not self._mb_entity_cache.self_id:
            # Some updates require our own ID, so we must make sure
            # that the event builder has offline access to it. Calling
            # `get_me()` will cache it under `self._mb_entity_cache`.
            #
            # It will return `None` if we haven't logged in yet which is
            # fine, we will just retry next time anyway.
            try:
                await self.get_me(input_peer=True)
            except OSError:
                pass  # might not have connection

        built = EventBuilderDict(self, update, others)
        for conv_set in self._conversations.values():
            for conv in conv_set:
                ev = built[events.NewMessage]
                if ev:
                    conv._on_new_message(ev)

                ev = built[events.MessageEdited]
                if ev:
                    conv._on_edit(ev)

                ev = built[events.MessageRead]
                if ev:
                    conv._on_read(ev)

                if conv._custom:
                    await conv._check_custom(built)

        for builder, callback in self._event_builders:
            event = built[type(builder)]
            if not event:
                continue

            if not builder.resolved:
                await builder.resolve(self)

            filter = builder.filter(event)
            if inspect.isawaitable(filter):
                filter = await filter
            if not filter:
                continue

            try:
                await callback(event)
            except errors.AlreadyInConversationError:
                name = getattr(callback, '__name__', repr(callback))
                self._log[__name__].debug(
                    'Event handler "%s" already has an open conversation, '
                    'ignoring new one', name)
            except events.StopPropagation:
                name = getattr(callback, '__name__', repr(callback))
                self._log[__name__].debug(
                    'Event handler "%s" stopped chain of propagation '
                    'for event %s.', name, type(event).__name__
                )
                break
            except Exception as e:
                if not isinstance(e, asyncio.CancelledError) or self.is_connected():
                    name = getattr(callback, '__name__', repr(callback))
                    self._log[__name__].exception('Unhandled exception on %s', name)


**Inspecting EventBuilderDict initialization**
exec
/bin/zsh -lc "uv run python - <<'PY'
import inspect
from telethon.client.updates import EventBuilderDict
print(inspect.getsource(EventBuilderDict))
PY" in /Users/L/Code/ddiver
 succeeded in 39ms:
class EventBuilderDict:
    """
    Helper "dictionary" to return events from types and cache them.
    """
    def __init__(self, client: 'TelegramClient', update, others):
        self.client = client
        self.update = update
        self.others = others

    def __getitem__(self, builder):
        try:
            return self.__dict__[builder]
        except KeyError:
            event = self.__dict__[builder] = builder.build(
                self.update, self.others, self.client._self_id)

            if isinstance(event, EventCommon):
                event.original_update = self.update
                event._entities = self.update._entities
                event._set_client(self.client)
            elif event:
                event._client = self.client

            return event


**Inspecting update entity handling**
exec
/bin/zsh -lc "uv run python - <<'PY'
import inspect
from telethon.client.updates import UpdateMethods
src=inspect.getsource(UpdateMethods._update_loop)
for i,line in enumerate(src.splitlines(),1):
 if '_entities' in line or 'entities' in line: print(i,line)
print(src)
PY" in /Users/L/Code/ddiver
 succeeded in 77ms:
35                     await self._save_states_and_entities()
38                         warnings.warn('in-memory entities exceed entity_cache_limit after flushing; consider setting a larger limit')
    async def _update_loop(self: 'TelegramClient'):
        # If the MessageBox is not empty, the account had to be logged-in to fill in its state.
        # This flag is used to propagate the "you got logged-out" error up (but getting logged-out
        # can only happen if it was once logged-in).
        was_once_logged_in = self._authorized is True or not self._message_box.is_empty()

        self._updates_error = None
        try:
            if self._catch_up:
                # User wants to catch up as soon as the client is up and running,
                # so this is the best place to do it.
                await self.catch_up()

            updates_to_dispatch = deque()

            while self.is_connected():
                if updates_to_dispatch:
                    if self._sequential_updates:
                        await self._dispatch_update(updates_to_dispatch.popleft())
                    else:
                        while updates_to_dispatch:
                            # TODO if _dispatch_update fails for whatever reason, it's not logged! this should be fixed
                            task = self.loop.create_task(self._dispatch_update(updates_to_dispatch.popleft()))
                            self._event_handler_tasks.add(task)
                            task.add_done_callback(self._event_handler_tasks.discard)

                    continue

                if len(self._mb_entity_cache) >= self._entity_cache_limit:
                    self._log[__name__].info(
                        'In-memory entity cache limit reached (%s/%s), flushing to session',
                        len(self._mb_entity_cache),
                        self._entity_cache_limit
                    )
                    await self._save_states_and_entities()
                    self._mb_entity_cache.retain(lambda id: id == self._mb_entity_cache.self_id or id in self._message_box.map)
                    if len(self._mb_entity_cache) >= self._entity_cache_limit:
                        warnings.warn('in-memory entities exceed entity_cache_limit after flushing; consider setting a larger limit')

                    self._log[__name__].info(
                        'In-memory entity cache at %s/%s after flushing to session',
                        len(self._mb_entity_cache),
                        self._entity_cache_limit
                    )


                get_diff = self._message_box.get_difference()
                if get_diff:
                    self._log[__name__].debug('Getting difference for account updates')
                    try:
                        diff = await self(get_diff)
                    except (
                        errors.ServerError,
                        errors.TimedOutError,
                        errors.FloodWaitError,
                        ValueError
                    ) as e:
                        # Telegram is having issues
                        self._log[__name__].info('Cannot get difference since Telegram is having issues: %s', type(e).__name__)
                        self._message_box.end_difference()
                        continue
                    except (errors.UnauthorizedError, errors.AuthKeyError) as e:
                        # Not logged in or broken authorization key, can't get difference
                        self._log[__name__].info('Cannot get difference since the account is not logged in: %s', type(e).__name__)
                        self._message_box.end_difference()
                        if was_once_logged_in:
                            self._updates_error = e
                            await self.disconnect()
                            break
                        continue
                    except (errors.TypeNotFoundError, OperationalError) as e:
                        # User is likely doing weird things with their account or session and Telegram gets confused as to what layer they use
                        self._log[__name__].warning('Cannot get difference since the account is likely misusing the session: %s', e)
                        self._message_box.end_difference()
                        self._updates_error = e
                        await self.disconnect()
                        break
                    except errors.RPCError as e:
                        # Fallback; treat as transient error (the amount of "fatal errors" reported seem to indicate this is most likely what we need to do)
                        self._log[__name__].warning(
                            "Cannot get difference due to unexpected error (this may be a bug "
                            f"in Telethon v{__version__} in that it could be handled better, but it's unlikely): %s",
                            e
                        )
                        self._message_box.end_difference()
                        continue
                    except OSError as e:
                        # Network is likely down, but it's unclear for how long.
                        # If disconnect is called this task will be cancelled along with the sleep.
                        # If disconnect is not called, getting difference should be retried after a few seconds.
                        self._log[__name__].info('Cannot get difference since the network is down: %s: %s', type(e).__name__, e)
                        await asyncio.sleep(5)
                        continue
                    updates, users, chats = self._message_box.apply_difference(diff, self._mb_entity_cache)
                    if updates:
                        self._log[__name__].info('Got difference for account updates')

                    _preprocess_updates = await self._preprocess_updates(updates, users, chats)
                    updates_to_dispatch.extend(_preprocess_updates)
                    continue

                get_diff = self._message_box.get_channel_difference(self._mb_entity_cache)
                if get_diff:
                    self._log[__name__].debug('Getting difference for channel %s updates', get_diff.channel.channel_id)
                    try:
                        diff = await self(get_diff)
                    except (errors.UnauthorizedError, errors.AuthKeyError) as e:
                        # Not logged in or broken authorization key, can't get difference
                        self._log[__name__].warning(
                            'Cannot get difference for channel %s since the account is not logged in: %s',
                            get_diff.channel.channel_id, type(e).__name__
                        )
                        self._message_box.end_channel_difference(
                            get_diff,
                            PrematureEndReason.TEMPORARY_SERVER_ISSUES,
                            self._mb_entity_cache
                        )
                        if was_once_logged_in:
                            self._updates_error = e
                            await self.disconnect()
                            break
                        continue
                    except (errors.TypeNotFoundError, OperationalError) as e:
                        self._log[__name__].warning(
                            'Cannot get difference for channel %s since the account is likely misusing the session: %s',
                            get_diff.channel.channel_id, e
                        )
                        self._message_box.end_channel_difference(
                            get_diff,
                            PrematureEndReason.TEMPORARY_SERVER_ISSUES,
                            self._mb_entity_cache
                        )
                        self._updates_error = e
                        await self.disconnect()
                        break
                    except (
                        errors.PersistentTimestampOutdatedError,
                        errors.PersistentTimestampInvalidError,
                        errors.ServerError,
                        errors.TimedOutError,
                        errors.FloodWaitError,
                        ValueError
                    ) as e:
                        # According to Telegram's docs:
                        # "Channel internal replication issues, try again later (treat this like an RPC_CALL_FAIL)."
                        # We can treat this as "empty difference" and not update the local pts.
                        # Then this same call will be retried when another gap is detected or timeout expires.
                        #
                        # Another option would be to literally treat this like an RPC_CALL_FAIL and retry after a few
                        # seconds, but if Telegram is having issues it's probably best to wait for it to send another
                        # update (hinting it may be okay now) and retry then.
                        #
                        # This is a bit hacky because MessageBox doesn't really have a way to "not update" the pts.
                        # Instead we manually extract the previously-known pts and use that.
                        #
                        # For PersistentTimestampInvalidError:
                        # Somehow our pts is either too new or the server does not know about this.
                        # We treat this as PersistentTimestampOutdatedError for now.
                        # TODO investigate why/when this happens and if this is the proper solution
                        self._log[__name__].warning(
                            'Getting difference for channel updates %s caused %s;'
                            ' ending getting difference prematurely until server issues are resolved',
                            get_diff.channel.channel_id, type(e).__name__
                        )
                        self._message_box.end_channel_difference(
                            get_diff,
                            PrematureEndReason.TEMPORARY_SERVER_ISSUES,
                            self._mb_entity_cache
                        )
                        continue
                    except (errors.ChannelPrivateError, errors.ChannelInvalidError):
                        # Timeout triggered a get difference, but we have been banned in the channel since then.
                        # Because we can no longer fetch updates from this channel, we should stop keeping track
                        # of it entirely.
                        self._log[__name__].info(
                            'Account is now banned in %d so we can no longer fetch updates from it',
                            get_diff.channel.channel_id
                        )
                        self._message_box.end_channel_difference(
                            get_diff,
                            PrematureEndReason.BANNED,
                            self._mb_entity_cache
                        )
                        continue
                    except errors.RPCError as e:
                        # Fallback; treat as transient error (the amount of "fatal errors" reported seem to indicate this is most likely what we need to do)
                        self._log[__name__].warning(
                            "Cannot get difference for channel %d due to unexpected error (this may be a bug "
                            f"in Telethon v{__version__} in that it could be handled better, but it's unlikely): %s",
                            get_diff.channel.channel_id, e
                        )
                        self._message_box.end_channel_difference(
                            get_diff,
                            PrematureEndReason.TEMPORARY_SERVER_ISSUES,
                            self._mb_entity_cache
                        )
                        continue
                    except OSError as e:
                        self._log[__name__].info(
                            'Cannot get difference for channel %d since the network is down: %s: %s',
                            get_diff.channel.channel_id, type(e).__name__, e
                        )
                        await asyncio.sleep(5)
                        continue

                    updates, users, chats = self._message_box.apply_channel_difference(get_diff, diff, self._mb_entity_cache)
                    if updates:
                        self._log[__name__].info('Got difference for channel %d updates', get_diff.channel.channel_id)

                    _preprocess_updates = await self._preprocess_updates(updates, users, chats)
                    updates_to_dispatch.extend(_preprocess_updates)
                    continue

                deadline = self._message_box.check_deadlines()
                deadline_delay = deadline - get_running_loop().time()
                if deadline_delay > 0:
                    # Don't bother sleeping and timing out if the delay is already 0 (pollutes the logs).
                    try:
                        updates = await asyncio.wait_for(self._updates_queue.get(), deadline_delay)
                    except asyncio.TimeoutError:
                        self._log[__name__].debug('Timeout waiting for updates expired')
                        continue
                else:
                    continue

                processed = []
                try:
                    users, chats = self._message_box.process_updates(updates, self._mb_entity_cache, processed)
                except GapError:
                    continue  # get(_channel)_difference will start returning requests

                _preprocess_updates = await self._preprocess_updates(processed, users, chats)
                updates_to_dispatch.extend(_preprocess_updates)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._log[__name__].exception(f'Fatal error handling updates (this is a bug in Telethon v{__version__}, please report it)')
            self._updates_error = e
            await self.disconnect()


**Inspecting preprocess assigns `_entities`**
exec
/bin/zsh -lc "uv run python - <<'PY'
import inspect
from telethon.client.updates import UpdateMethods
print(inspect.getsource(UpdateMethods._preprocess_updates))
PY" in /Users/L/Code/ddiver
 succeeded in 74ms:
    async def _preprocess_updates(self, updates, users, chats):
        self._mb_entity_cache.extend(users, chats)
        await utils.maybe_async(self.session.process_entities(types.contacts.ResolvedPeer(None, users, chats)))
        entities = {utils.get_peer_id(x): x
                    for x in itertools.chain(users, chats)}
        for u in updates:
            u._entities = entities
        return updates


**Assessing UpdateChannel entity presence and ChatAction RPC usage**
**Verifying dialog handling and permission checks**
**Analyzing race condition in chat sharing and unsubscribing**
**Tracing subscription timing and access checks**
**Identifying persistent notification loss issue**
**Analyzing message cursor handling on restart**
**Identifying notification misrouting after resubscription**
**Planning cursor reset on subscription changes**
**Verifying Telethon message sender property**
exec
/bin/zsh -lc "uv run python - <<'PY'
import inspect
from telethon.events.newmessage import NewMessage
print(inspect.getsource(NewMessage.Event._set_client))
from telethon.tl.custom.message import Message
print(inspect.getsource(Message._finish_init))
PY" in /Users/L/Code/ddiver
 succeeded in 67ms:
        def _set_client(self, client):
            super()._set_client(client)
            m = self.message
            m._finish_init(client, self._entities, None)
            self.__dict__['_init'] = True  # No new attributes can be set

    def _finish_init(self, client, entities, input_chat):
        """
        Finishes the initialization of this message by setting
        the client that sent the message and making use of the
        known entities.
        """
        self._client = client

        # Make messages sent to ourselves outgoing unless they're forwarded.
        # This makes it consistent with official client's appearance.
        if self.peer_id == types.PeerUser(client._self_id) and not self.fwd_from:
            self.out = True

        cache = client._mb_entity_cache

        self._sender, self._input_sender = utils._get_entity_pair(
            self.sender_id, entities, cache)

        self._chat, self._input_chat = utils._get_entity_pair(
            self.chat_id, entities, cache)

        if input_chat:  # This has priority
            self._input_chat = input_chat

        if self.via_bot_id:
            self._via_bot, self._via_input_bot = utils._get_entity_pair(
                self.via_bot_id, entities, cache)

        if self.fwd_from:
            self._forward = Forward(self._client, self.fwd_from, entities)

        if self.action:
            if isinstance(self.action, (types.MessageActionChatAddUser,
                                        types.MessageActionChatCreate)):
                self._action_entities = [entities.get(i)
                                         for i in self.action.users]
            elif isinstance(self.action, types.MessageActionChatDeleteUser):
                self._action_entities = [entities.get(self.action.user_id)]
            elif isinstance(self.action, types.MessageActionChatJoinedByLink):
                self._action_entities = [entities.get(self.action.inviter_id)]
            elif isinstance(self.action, types.MessageActionChatMigrateTo):
                self._action_entities = [entities.get(utils.get_peer_id(
                    types.PeerChannel(self.action.channel_id)))]
            elif isinstance(
                    self.action, types.MessageActionChannelMigrateFrom):
                self._action_entities = [entities.get(utils.get_peer_id(
                    types.PeerChat(self.action.chat_id)))]

        if self.replies and self.replies.channel_id:
            self._linked_chat = entities.get(utils.get_peer_id(
                    types.PeerChannel(self.replies.channel_id)))
        
        if isinstance(self.reply_to, types.MessageReplyHeader):
            if self.reply_to.reply_to_peer_id:
                self._reply_to_chat = entities.get(utils.get_peer_id(self.reply_to.reply_to_peer_id))
            if self.reply_to.reply_from:
                if self.reply_to.reply_from.from_id:
                    self._reply_to_sender = entities.get(utils.get_peer_id(self.reply_to.reply_from.from_id))


**Verifying Telethon event handling**
exec
/bin/zsh -lc "uv run python - <<'PY'
import inspect
from telethon import events
print(inspect.signature(events.Raw))
print(inspect.getsource(events.Raw))
print(inspect.signature(events.CallbackQuery))
PY" in /Users/L/Code/ddiver
 succeeded in 30ms:
(types=None, *, func=None)
class Raw(EventBuilder):
    """
    Raw events are not actual events. Instead, they are the raw
    :tl:`Update` object that Telegram sends. You normally shouldn't
    need these.

    Args:
        types (`list` | `tuple` | `type`, optional):
            The type or types that the :tl:`Update` instance must be.
            Equivalent to ``if not isinstance(update, types): return``.

    Example
        .. code-block:: python

            from telethon import events

            @client.on(events.Raw)
            async def handler(update):
                # Print all incoming updates
                print(update.stringify())
    """
    def __init__(self, types=None, *, func=None):
        super().__init__(func=func)
        if not types:
            self.types = None
        elif not utils.is_list_like(types):
            if not isinstance(types, type):
                raise TypeError('Invalid input type given: {}'.format(types))

            self.types = types
        else:
            if not all(isinstance(x, type) for x in types):
                raise TypeError('Invalid input types given: {}'.format(types))

            self.types = tuple(types)

    async def resolve(self, client):
        self.resolved = True

    @classmethod
    def build(cls, update, others=None, self_id=None):
        return update

    def filter(self, event):
        if not self.types or isinstance(event, self.types):
            if self.func:
                # Return the result of func directly as it may need to be awaited
                return self.func(event)
            return event

(chats=None, *, blacklist_chats=False, func=None, data=None, pattern=None)

**Confirming bot degraded mode shutdown behavior**
**Identifying Docker restart loop issue with fatal auth**
**Analyzing connect_with_timeout retry limitations**
**Inspecting reconnect handler signature**
exec
/bin/zsh -lc "uv run python - <<'PY'
import inspect
from telethon import TelegramClient
print(inspect.signature(TelegramClient._handle_auto_reconnect))
print(inspect.getsource(TelegramClient._handle_auto_reconnect))
PY" in /Users/L/Code/ddiver
 succeeded in 104ms:
(self: 'TelegramClient')
    async def _handle_auto_reconnect(self: 'TelegramClient'):
        # TODO Catch-up
        # For now we make a high-level request to let Telegram
        # know we are still interested in receiving more updates.
        try:
            await self.get_me()
        except Exception as e:
            self._log[__name__].warning('Error executing high-level request '
                                        'after reconnect: %s: %s', type(e), e)

        return
        try:
            self._log[__name__].info(
                'Asking for the current state after reconnect...')

            # TODO consider:
            # If there aren't many updates while the client is disconnected
            # (I tried with up to 20), Telegram seems to send them without
            # asking for them (via updates.getDifference).
            #
            # On disconnection, the library should probably set a "need
            # difference" or "catching up" flag so that any new updates are
            # ignored, and then the library should call updates.getDifference
            # itself to fetch them.
            #
            # In any case (either there are too many updates and Telegram
            # didn't send them, or there isn't a lot and Telegram sent them
            # but we dropped them), we fetch the new difference to get all
            # missed updates. I feel like this would be the best solution.

            # If a disconnection occurs, the old known state will be
            # the latest one we were aware of, so we can catch up since
            # the most recent state we were aware of.
            await self.catch_up()

            self._log[__name__].info('Successfully fetched missed updates')
        except errors.RPCError as e:
            self._log[__name__].warning('Failed to get missed updates after '
                                        'reconnect: %r', e)
        except Exception:
            self._log[__name__].exception(
                'Unhandled exception while getting update difference after reconnect')


**Verifying signal override without watcher**
exec
/bin/zsh -lc "sed -n '1,620p' tests/test_bot.py" in /Users/L/Code/ddiver
 succeeded in 0ms:
import datetime as dt

import pytest

from ddiver.bot import menus
from ddiver.bot.handlers import StatusInfo, build_router, propose_exclusion
from ddiver.bot.router import Edit, Incoming, Reply, Send, Toast
from ddiver.i18n import t
from ddiver.links import ChatRef
from ddiver.matcher import Matcher
from ddiver.rules import RuleKind
from ddiver.storage import ALL_CHATS, Role, Storage
from tests.fakes import T0, FakeClock

OWNER = 1
MOD = 2
STRANGER = 3

CHAT_A = ChatRef(-1001, "Модерация", "modchat", "supergroup")
CHAT_B = ChatRef(-1002, "Приватный семейный", None, "supergroup")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
async def store(tmp_path, clock):
    s = Storage(tmp_path / "db.sqlite3", owner_id=OWNER, clock=clock)
    await s.open()
    await s.ensure_user(OWNER)
    await s.update_user(OWNER, lang="ru", tz="Europe/Kyiv")
    await s.ensure_user(MOD)
    await s.update_user(MOD, role=Role.ALLOWED, lang="en", tz="UTC")
    await s.upsert_chat(CHAT_A)
    await s.upsert_chat(CHAT_B)
    yield s
    await s.close()


@pytest.fixture
async def matcher(store):
    m = Matcher(store, timeout=0.2)
    await m.reload()
    return m


@pytest.fixture
def router(store, matcher, clock):
    async def status():
        return StatusInfo(listener_ok=True, uptime="1h", monitored=2, seen=10, matched=3)

    return build_router(store, matcher=matcher, status=status, clock=clock)


def kinds(actions):
    return [type(a).__name__ for a in actions]


def texts(actions, lang="ru"):
    return [
        action.key_or_text if action.literal else t(lang, action.key_or_text, **action.params)
        for action in actions
        if isinstance(action, Toast | Reply | Edit | Send)
    ]


def callbacks(actions) -> list[str]:
    data: list[str] = []
    for action in actions:
        for row in getattr(action, "buttons", None) or []:
            data.extend(callback for _, callback in row)
    return data


async def msg(router, user_id, text, **kw):
    return await router.dispatch(Incoming(user_id=user_id, text=text, **kw))


async def cb(router, user_id, data, **kw):
    return await router.dispatch(Incoming(user_id=user_id, data=data, **kw))


class TestGate:
    async def test_stranger_gets_one_reply_and_the_owner_one_card(self, router, store, clock):
        actions = await msg(router, STRANGER, "/start")
        assert kinds(actions) == ["Reply", "Send"]
        card = actions[1]
        assert card.user_id == OWNER
        assert "allow:3" in callbacks([card]) and "deny:3" in callbacks([card])
        # a second message is silent
        assert await msg(router, STRANGER, "hello") == []
        clock.tick(dt.timedelta(days=1).total_seconds() + 1)
        assert kinds(await msg(router, STRANGER, "hello")) == ["Reply", "Send"]

    async def test_gate_message_is_in_the_client_language(self, router, store):
        actions = await msg(router, STRANGER, "/start", lang_code="uk-UA")
        assert texts(actions, "uk")[0] == t("uk", "bot.gate.pending")
        assert (await store.get_user(STRANGER)).lang == "uk"

    async def test_denied_users_are_silent(self, router, store):
        await store.ensure_user(STRANGER)
        await store.update_user(STRANGER, role=Role.DENIED)
        assert await msg(router, STRANGER, "/start") == []
        assert await cb(router, STRANGER, "menu") == []

    async def test_group_messages_are_ignored(self, router):
        assert await msg(router, OWNER, "/status", is_private=False) == []

    async def test_owner_is_never_gated(self, router, store):
        await store.update_user(OWNER, role=Role.DENIED)
        assert kinds(await msg(router, OWNER, "/menu")) == ["Reply"]

    async def test_incoming_update_clears_a_delivery_pause(self, router, store):
        await store.pause_delivery(MOD, "blocked")
        await msg(router, MOD, "/menu")
        assert not (await store.get_user(MOD)).paused


class TestOwnerOnly:
    @pytest.mark.parametrize("data", ["allow:3", "deny:3", "revoke:1", "shared:0", "share:-1001:1"])
    async def test_moderators_cannot_run_owner_actions(self, router, store, data):
        actions = await cb(router, MOD, data)
        assert kinds(actions) == ["Toast"]
        assert actions[0].key_or_text == "bot.err.owner_only"
        assert (await store.get_user(STRANGER)) is None or True

    async def test_owner_can_allow_and_revoke(self, router, store):
        await store.ensure_user(STRANGER)
        actions = await cb(router, OWNER, "allow:3")
        assert (await store.get_user(STRANGER)).role is Role.ALLOWED
        assert any(isinstance(a, Send) and a.user_id == STRANGER for a in actions)
        await cb(router, OWNER, "revoke:3")
        assert (await store.get_user(STRANGER)).role is Role.DENIED

    async def test_owner_cannot_revoke_themselves(self, router, store):
        actions = await cb(router, OWNER, "revoke:1")
        assert actions[0].key_or_text == "bot.err.owner_only"
        assert (await store.get_user(OWNER)).active


class TestOnboarding:
    async def test_language_then_timezone_then_menu(self, router, store):
        await store.update_user(MOD, lang=None, tz=None)
        actions = await msg(router, MOD, "/start")
        assert "lang:uk" in callbacks(actions)
        await cb(router, MOD, "lang:uk")
        user = await store.get_user(MOD)
        assert user.lang == "uk" and user.tz is None
        actions = await cb(router, MOD, "tz:Europe/Kyiv")
        assert (await store.get_user(MOD)).tz == "Europe/Kyiv"
        assert any("Europe/Kyiv" in text for text in texts(actions, "uk"))

    async def test_typed_timezone_with_suggestions(self, router, store):
        await store.update_user(MOD, tz=None)
        await cb(router, MOD, "set:tz")
        actions = await msg(router, MOD, "Europe")
        assert texts(actions, "en")[0] == t("en", "bot.start.tz_candidates")
        actions = await msg(router, MOD, "kyiv")
        assert (await store.get_user(MOD)).tz == "Europe/Kyiv"

    async def test_unknown_timezone_is_reported(self, router, store):
        await cb(router, MOD, "set:tz")
        actions = await msg(router, MOD, "Mars/Olympus")
        assert texts(actions, "en") == [t("en", "bot.start.tz_unknown")]

    async def test_fixed_offset_warns_about_dst(self, router, store):
        await cb(router, MOD, "set:tz")
        actions = await msg(router, MOD, "+3")
        assert any("daylight" in text for text in texts(actions, "en"))
        assert (await store.get_user(MOD)).tz == "Etc/GMT-3"


class TestChatsAndSharing:
    async def test_moderators_only_see_shared_chats(self, router, store):
        actions = await cb(router, MOD, "chats:0")
        assert texts(actions, "en") == [t("en", "bot.chats.none_shared")]
        await store.set_chat_shared(CHAT_A.id, True)
        actions = await cb(router, MOD, "chats:0")
        assert any("Модерация" in menus.unwrap(label) for label, _ in actions[0].buttons[0])
        assert all("Приватный" not in str(row) for row in actions[0].buttons)

    async def test_owner_sees_every_chat(self, router):
        actions = await cb(router, OWNER, "chats:0")
        labels = [menus.unwrap(label) for row in actions[0].buttons for label, _ in row]
        assert any("Приватный" in label for label in labels)

    async def test_subscribe_toggle_is_idempotent(self, router, store, matcher):
        await store.set_chat_shared(CHAT_A.id, True)
        await cb(router, MOD, f"sub:{CHAT_A.id}:1")
        await cb(router, MOD, f"sub:{CHAT_A.id}:1")
        assert [c.id for c in await store.list_subscriptions(MOD)] == [CHAT_A.id]
        assert matcher.is_monitored(CHAT_A.id)
        await cb(router, MOD, f"sub:{CHAT_A.id}:0")
        assert await store.list_subscriptions(MOD) == []
        assert not matcher.is_monitored(CHAT_A.id)

    async def test_moderator_cannot_subscribe_to_an_unshared_chat(self, router, store):
        actions = await cb(router, MOD, f"sub:{CHAT_B.id}:1")
        assert actions[0].key_or_text == "bot.err.not_found"
        assert await store.list_subscriptions(MOD) == []

    async def test_forged_private_chat_id_is_rejected(self, router, store):
        actions = await cb(router, MOD, "sub:777000:1")
        assert actions[0].key_or_text == "bot.err.not_found"
        assert await store.list_subscriptions(MOD) == []

    async def test_unsharing_drops_moderator_subscriptions_and_notifies(self, router, store):
        await store.set_chat_shared(CHAT_A.id, True)
        await cb(router, MOD, f"sub:{CHAT_A.id}:1")
        await cb(router, OWNER, f"share:{CHAT_A.id}:0")
        assert await store.list_subscriptions(MOD) == []
        notices = [a for a in await cb(router, OWNER, "shared:0") if isinstance(a, Send)]
        assert notices == []  # the notice was sent by the share callback, not the listing

    async def test_chat_search_matches_title_and_username(self, router, store):
        await store.set_chat_shared(CHAT_A.id, True)
        await cb(router, MOD, "chats:search")
        actions = await msg(router, MOD, "@modchat")
        labels = [menus.unwrap(label) for row in actions[0].buttons for label, _ in row]
        assert any("Модерация" in label for label in labels)
        await cb(router, MOD, "chats:search")
        actions = await msg(router, MOD, "nothing here")
        assert texts(actions, "en") == [t("en", "bot.chats.search_empty")]
        # the prompt stays active so the user can simply retype
        actions = await msg(router, MOD, "модерация")
        assert callbacks(actions)


class TestRules:
    async def test_multiline_add_reports_per_line(self, router, store, matcher):
        await store.subscribe(OWNER, CHAT_A.id)
        await cb(router, OWNER, f"radd:{CHAT_A.id}:include")
        actions = await msg(
            router,
            OWNER,
            "казино\n#промо\n=бан\nre:(unclosed\nказино\nre:.*",
        )
        report = texts(actions)[0]
        assert "Добавлено 3" in report
        assert "дубликатов 1" in report
        assert "ошибок 2" in report
        patterns = [r.pattern for r in await store.list_rules(OWNER, CHAT_A.id)]
        assert patterns == ["казино", "#промо", "=бан"]  # hashtags are keywords, not comments

    async def test_rule_limits(self, router, store, monkeypatch):
        monkeypatch.setattr("ddiver.bot.handlers.MAX_LINES_PER_MESSAGE", 2)
        await cb(router, OWNER, f"radd:{CHAT_A.id}:include")
        actions = await msg(router, OWNER, "a\nb\nc")
        assert texts(actions)[0] == t("ru", "bot.rules.too_many_lines", limit=2)
        monkeypatch.setattr("ddiver.bot.handlers.MAX_RULES_PER_CHAT", 1)
        await cb(router, OWNER, f"radd:{CHAT_A.id}:include")
        actions = await msg(router, OWNER, "один\nдва")
        assert "1" in texts(actions)[0]

    async def test_rules_are_private_to_their_owner(self, router, store, matcher):
        rule = await store.add_rule(MOD, ALL_CHATS, RuleKind.INCLUDE, "чужое")
        actions = await cb(router, OWNER, f"rdel:{rule.id}")
        assert actions[0].key_or_text == "bot.err.not_found"
        assert await store.get_rule(rule.id, MOD) is not None

    async def test_delete_rule_refreshes_the_matcher(self, router, store, matcher):
        await store.subscribe(OWNER, CHAT_A.id)
        rule = await store.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "казино")
        await matcher.invalidate()
        assert await matcher.evaluate(CHAT_A.id, "казино", 5) != []
        await cb(router, OWNER, f"rdel:{rule.id}")
        assert await matcher.evaluate(CHAT_A.id, "казино", 5) == []

    async def test_rules_list_shows_kind_icons(self, router, store):
        await store.subscribe(OWNER, CHAT_A.id)
        for pattern in ("казино", "=бан", "re:\\d+"):
            await store.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, pattern)
        actions = await cb(router, OWNER, f"rules:{CHAT_A.id}:include:0")
        labels = [menus.unwrap(label) for row in actions[0].buttons for label, _ in row]
        assert any("🔤" in label for label in labels)
        assert any("🔠" in label for label in labels)
        assert any("🧩" in label for label in labels)


class TestIgnoreAndExclude:
    async def test_ignore_button_scopes_to_the_chat_then_offers_global(self, router, store):
        actions = await cb(router, OWNER, f"ig:{CHAT_A.id}:500")
        assert [i.sender_id for i in await store.list_ignored(OWNER)] == [500]
        assert "igall:500" in callbacks(actions)
        await cb(router, OWNER, "igall:500")
        scopes = {i.chat_id for i in await store.list_ignored(OWNER)}
        assert scopes == {CHAT_A.id, ALL_CHATS}

    async def test_anonymous_admin_is_labelled(self, router, store):
        await cb(router, OWNER, f"ig:{CHAT_A.id}:{CHAT_A.id}")
        entry = (await store.list_ignored(OWNER))[0]
        assert entry.note == t("ru", "bot.ignored.anonymous")

    async def test_exclude_reads_the_outbox_row(self, router, store):
        payload = {
            "chat_id": CHAT_A.id,
            "chat_title": "Модерация",
            "msg_id": 9,
            "date": T0.isoformat(),
            "text": "купи казино-бонус сегодня",
            "spans": [[5, 11]],
            "rule_pattern": "казино",
        }
        await store.claim_delivery(OWNER, CHAT_A.id, 9, 1, payload)
        actions = await cb(router, OWNER, f"ex:{CHAT_A.id}:9")
        assert "exadd:-1001:9" in callbacks(actions)
        await cb(router, OWNER, f"exadd:{CHAT_A.id}:9")
        rules = await store.list_rules(OWNER, CHAT_A.id, RuleKind.EXCLUDE)
        assert [r.pattern for r in rules] == ["=казино-бонус"]

    async def test_exclude_on_a_pruned_row_expires(self, router):
        actions = await cb(router, OWNER, f"ex:{CHAT_A.id}:404")
        assert actions[0].key_or_text == "bot.err.expired"

    async def test_exclude_cannot_read_another_users_row(self, router, store):
        payload = {"chat_id": CHAT_A.id, "chat_title": "x", "msg_id": 9, "date": T0.isoformat()}
        await store.claim_delivery(MOD, CHAT_A.id, 9, 1, payload)
        actions = await cb(router, OWNER, f"ex:{CHAT_A.id}:9")
        assert actions[0].key_or_text == "bot.err.expired"

    async def test_exclusion_equal_to_the_keyword_is_refused(self, router, store):
        payload = {
            "chat_id": CHAT_A.id,
            "chat_title": "Модерация",
            "msg_id": 10,
            "date": T0.isoformat(),
            "text": "казино",
            "spans": [[0, 6]],
            "rule_pattern": "казино",
        }
        await store.claim_delivery(OWNER, CHAT_A.id, 10, 1, payload)
        actions = await cb(router, OWNER, f"ex:{CHAT_A.id}:10")
        assert any(a.key_or_text.startswith("⚠️") for a in actions if isinstance(a, Toast))
        assert "exadd:-1001:10" not in callbacks(actions)

    async def test_custom_exclusion_input(self, router, store):
        await cb(router, OWNER, f"excustom:{CHAT_A.id}")
        actions = await msg(router, OWNER, "re:промокод")
        assert "промокод" in texts(actions)[0]
        rules = await store.list_rules(OWNER, CHAT_A.id, RuleKind.EXCLUDE)
        assert [r.pattern for r in rules] == ["re:промокод"]


class TestProposeExclusion:
    @pytest.mark.parametrize(
        ("text", "span", "expected"),
        [
            ("купи казино-бонус сегодня", (5, 11), "=казино-бонус"),
            ("пиши @casino_bot тут", (6, 12), "=@casino_bot"),
            ("смотри casino.com!", (7, 13), "=casino.com"),
            ("казино", (0, 6), "=казино"),
            ("  казино  ", (2, 8), "=казино"),
        ],
    )
    def test_expands_to_whole_tokens(self, text, span, expected):
        assert propose_exclusion(text, [span]) == expected

    def test_empty_inputs(self):
        assert propose_exclusion("", [(0, 1)]) is None
        assert propose_exclusion("text", []) is None
        assert propose_exclusion("...", [(0, 3)]) is None


class TestPendingInput:
    async def test_expires_after_the_ttl(self, router, store, clock):
        await cb(router, OWNER, f"radd:{CHAT_A.id}:include")
        clock.tick(16 * 60)
        actions = await msg(router, OWNER, "казино")
        assert actions[0].key_or_text == "bot.err.not_waiting"
        assert await store.list_rules(OWNER) == []

    async def test_a_command_clears_pending_state(self, router, store):
        await cb(router, OWNER, f"radd:{CHAT_A.id}:include")
        await msg(router, OWNER, "/cancel")
        actions = await msg(router, OWNER, "казино")
        assert actions[0].key_or_text == "bot.err.not_waiting"

    async def test_rule_lines_starting_with_a_slash_are_kept(self, router, store):
        await cb(router, OWNER, f"radd:{CHAT_A.id}:include")
        await msg(router, OWNER, "/promo\nre:/promo\\b")
        patterns = [r.pattern for r in await store.list_rules(OWNER, CHAT_A.id)]
        assert patterns == ["/promo", "re:/promo\\b"]


class TestStatusAndTest:
    async def test_status_reports_counters(self, router):
        actions = await msg(router, OWNER, "/status")
        body = texts(actions)[0]
        assert "1h" in body and "10" in body

    async def test_test_command_mirrors_the_pipeline(self, router, store, matcher):
        await store.subscribe(OWNER, CHAT_A.id)
        await store.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "казино")
        await store.add_rule(OWNER, CHAT_A.id, RuleKind.EXCLUDE, "тест")
        actions = await msg(router, OWNER, "/test заходи в казино")
        assert "✅" in texts(actions)[0]
        actions = await msg(router, OWNER, "/test казино тест")
        assert "🚫" in texts(actions)[0]
        actions = await msg(router, OWNER, "/test ничего")
        assert "—" in texts(actions)[0]

    async def test_test_without_arguments_asks_for_text(self, router, store):
        await store.subscribe(OWNER, CHAT_A.id)
        await store.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "казино")
        actions = await msg(router, OWNER, "/test")
        assert actions[0].key_or_text == "bot.test.prompt"
        actions = await msg(router, OWNER, "тут казино")
        assert "✅" in texts(actions)[0]

    async def test_test_without_chats(self, router):
        actions = await msg(router, OWNER, "/test что-то")
        assert texts(actions)[0] == t("ru", "bot.test.no_chats")


class TestSettings:
    async def test_digest_threshold_validation(self, router, store):
        await cb(router, OWNER, "set:digest")
        await msg(router, OWNER, "abc")
        assert (await store.get_user(OWNER)).digest_threshold == 20
        await msg(router, OWNER, "1")
        assert (await store.get_user(OWNER)).digest_threshold == 20
        await msg(router, OWNER, "7")
        assert (await store.get_user(OWNER)).digest_threshold == 7
        await cb(router, OWNER, "set:digest")
        await msg(router, OWNER, "0")
        assert (await store.get_user(OWNER)).digest_threshold == 0

    async def test_settings_view_shows_current_values(self, router):
        actions = await cb(router, OWNER, "set:main")
        assert "Europe/Kyiv" in texts(actions)[0]


class TestUnknown:
    async def test_unknown_callback(self, router):
        actions = await cb(router, OWNER, "nope:1")
        assert actions[0].key_or_text == "bot.err.unknown_action"

    async def test_unknown_command(self, router):
        actions = await msg(router, OWNER, "/nonsense")
        assert actions[0].key_or_text == "bot.err.not_waiting"

    async def test_plain_text_without_pending_state(self, router):
        actions = await msg(router, OWNER, "просто текст")
        assert actions[0].key_or_text == "bot.err.not_waiting"


class TestMenus:
    def test_callback_data_fits_in_64_bytes(self):
        for chat_id in (-1001234567890, -55):
            data = menus.check_callback(f"ig:{chat_id}:{-1001234567890}")
            assert len(data.encode()) <= 64
        with pytest.raises(ValueError, match="too long"):
            menus.check_callback("x" * 65)

    def test_pagination_bounds(self):
        page = menus.paginate(list(range(25)), 99, 10)
        assert (page.index, page.total, list(page.items)) == (2, 3, [20, 21, 22, 23, 24])
        assert page.has_prev and not page.has_next
        empty = menus.paginate([], 0, 10)
        assert (empty.index, empty.total, list(empty.items)) == (0, 1, [])

    def test_owner_menu_has_extra_entries(self):
        owner_rows = menus.main_menu(is_owner=True)
        mod_rows = menus.main_menu(is_owner=False)
        assert len(owner_rows) == len(mod_rows) + 1
        assert any("users" in cb for row in owner_rows for _, cb in row)


class TestMenuRendering:
    def test_shared_menu_marks_state(self):
        from ddiver.storage import ChatRow

        rows = [
            ChatRow(CHAT_A, available=True, shared=True, access_hash=None),
            ChatRow(CHAT_B, available=True, shared=False, access_hash=None),
        ]
        keyboard = menus.shared_menu(rows, 0)
        labels = [menus.unwrap(label) for row in keyboard for label, _ in row]
        assert any(label.startswith("🗂") for label in labels)
        assert any(label.startswith("🔒") for label in labels)
        data = [cb for row in keyboard for _, cb in row]
        assert f"share:{CHAT_A.id}:0" in data  # toggling a shared chat turns it off
        assert f"share:{CHAT_B.id}:1" in data

    def test_chats_menu_flags_unavailable_chats(self):
        from ddiver.storage import ChatRow

        rows = [ChatRow(CHAT_A, available=False, shared=True, access_hash=None)]
        keyboard = menus.chats_menu(rows, {CHAT_A.id}, 0)
        label = menus.unwrap(keyboard[0][0][0])
        assert label.startswith("✅") and label.endswith("⚠️")

    def test_chats_menu_paginates(self):
        from ddiver.storage import ChatRow

        rows = [
            ChatRow(ChatRef(-1000 - i, f"Chat {i}", None, "supergroup"), True, True, None)
            for i in range(20)
        ]
        first = menus.chats_menu(rows, set(), 0)
        nav = [cb for row in first for _, cb in row if cb.startswith("chats:")]
        assert "chats:1" in nav and "chats:0" not in nav
        middle = menus.chats_menu(rows, set(), 1)
        nav = [cb for row in middle for _, cb in row if cb.startswith("chats:")]
        assert {"chats:0", "chats:2"} <= set(nav)

    def test_rules_menu_navigation_keeps_chat_and_kind(self):
        from ddiver.storage import Rule

        rules = [
            Rule(i, OWNER, CHAT_A.id, RuleKind.INCLUDE, f"rule{i}", None, 0) for i in range(25)
        ]
        keyboard = menus.rules_menu(rules, ["🔤"] * 25, CHAT_A.id, "include", 1)
        data = [cb for row in keyboard for _, cb in row]
        assert f"rules:{CHAT_A.id}:include:0" in data
        assert f"rules:{CHAT_A.id}:include:2" in data
        assert f"radd:{CHAT_A.id}:include" in data
        assert f"rules:{CHAT_A.id}:exclude:0" in data

    def test_rules_menu_truncates_long_patterns_and_marks_disabled(self):
        from ddiver.storage import Rule

        long_rule = Rule(1, OWNER, 0, RuleKind.INCLUDE, "x" * 120, "timeout", 0)
        label = menus.unwrap(menus.rules_menu([long_rule], ["🧩"], 0, "include", 0)[0][0][0])
        assert label.startswith("❌ ⏸")
        assert label.endswith("…")
        assert len(label) < 80

    def test_ignored_menu_shows_scope(self):
        from ddiver.storage import IgnoredSender

        entries = [
            IgnoredSender(OWNER, ALL_CHATS, 500, None),
            IgnoredSender(OWNER, CHAT_A.id, 600, "spammer"),
        ]
        keyboard = menus.ignored_menu(entries, 0)
        labels = [menus.unwrap(label) for row in keyboard for label, _ in row]
        assert any("🌐" in label for label in labels)
        assert any("📡" in label and "spammer" in label for label in labels)

    def test_users_menu_lists_actions(self):
        keyboard = menus.users_menu([3, 4], [5])
        data = [cb for row in keyboard for _, cb in row]
        assert data[:2] == ["allow:3", "deny:3"]
        assert "revoke:5" in data

    def test_rules_chat_menu_always_offers_all_chats(self):
        from ddiver.storage import ChatRow

        rows = [ChatRow(CHAT_A, True, True, None)]
        keyboard = menus.rules_chat_menu(rows, 0)
        assert keyboard[0][0][1] == f"rules:{ALL_CHATS}:include:0"

**Identifying vacuous test assertion in owner-only test**
**Highlighting multiple vacuous and flawed test cases**
**Noting incomplete assertions in queue tests**
**Assessing user privacy and authorization handling**
**Identifying bot chat subscription limitations**
**Analyzing bot sender filter and reconnect handling**
**Identifying async disconnect hang issue**
**Planning async disconnect fix and hang tests**
**Confirming cursor advance despite handler crash**
**Planning robust per-chat sequencing**
**Identifying unbounded memory and task leaks**
**Detecting listener cursor leak on catchup exceptions**
**Analyzing chat payload and DB cursor handling**
**Planning test file review and key findings**
codex
The green suite is masking several production-only paths: real signal shutdown, fatal-auth behavior, reconnect reconciliation, queue overflow/failure, and migration events are either not exercised or are tested only at a lower layer. I’m validating each candidate against the concrete control flow now and pruning anything merely architectural or speculative.
exec
/bin/zsh -lc "sed -n '1,280p' ddiver/bot/menus.py && sed -n '1,260p' tests/fakes.py && sed -n '1,470p' tests/test_storage.py" in /Users/L/Code/ddiver
 succeeded in 0ms:
"""Keyboards and list pagination, expressed as plain data so handlers stay testable.

A button is ``(label_key_or_text, callback_data)``; the adapter turns it into a Telethon
``Button.inline``. Labels are i18n keys unless they are pre-rendered (chat titles, patterns),
in which case the handler passes the literal text via :func:`literal`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ddiver.i18n import LANG_NAMES, LANGS
from ddiver.storage import ALL_CHATS, ChatRow, IgnoredSender, Rule

CHATS_PER_PAGE = 8
RULES_PER_PAGE = 10
PATTERN_PREVIEW = 60
MAX_CALLBACK_BYTES = 64

QUICK_TIMEZONES = ("Europe/Kyiv", "Europe/Warsaw", "Europe/Berlin", "Europe/London", "UTC")

Button = tuple[str, str]
Keyboard = list[list[Button]]


def literal(text: str) -> str:
    """Mark a button label as already-rendered text rather than an i18n key."""
    return f"\x00{text}"


def is_literal(label: str) -> bool:
    return label.startswith("\x00")


def unwrap(label: str) -> str:
    return label[1:] if is_literal(label) else label


def check_callback(data: str) -> str:
    """Telegram rejects callback data over 64 bytes; fail loudly in tests rather than at runtime."""
    if len(data.encode()) > MAX_CALLBACK_BYTES:
        msg = f"callback data too long: {data!r}"
        raise ValueError(msg)
    return data


@dataclass(frozen=True, slots=True)
class Page:
    items: Sequence[Any]
    index: int
    total: int

    @property
    def has_prev(self) -> bool:
        return self.index > 0

    @property
    def has_next(self) -> bool:
        return self.index + 1 < self.total


def paginate(items: Sequence[Any], index: int, per_page: int) -> Page:
    total = max(1, -(-len(items) // per_page))
    index = max(0, min(index, total - 1))
    start = index * per_page
    return Page(items[start : start + per_page], index, total)


def nav_row(prefix: str, page: Page, *, suffix: str = "") -> list[Button]:
    row: list[Button] = []
    if page.has_prev:
        row.append(("bot.btn.prev", check_callback(f"{prefix}:{page.index - 1}{suffix}")))
    if page.has_next:
        row.append(("bot.btn.next", check_callback(f"{prefix}:{page.index + 1}{suffix}")))
    return row


def main_menu(*, is_owner: bool) -> Keyboard:
    rows: Keyboard = [
        [("bot.menu.chats", "chats:0"), ("bot.menu.rules", "rules:pick")],
        [("bot.menu.ignored", "ign:0"), ("bot.menu.settings", "set:main")],
        [("bot.menu.status", "status"), ("bot.menu.help", "help")],
    ]
    if is_owner:
        rows.append([("bot.menu.shared", "shared:0"), ("bot.menu.users", "users")])
    return rows


def language_menu() -> Keyboard:
    return [[(literal(LANG_NAMES[lang]), f"lang:{lang}")] for lang in LANGS]


def timezone_menu() -> Keyboard:
    rows = [[(literal(tz), check_callback(f"tz:{tz}"))] for tz in QUICK_TIMEZONES]
    rows.append([("bot.btn.cancel", "menu")])
    return rows


def chat_label(chat: ChatRow, *, subscribed: bool) -> str:
    mark = "✅" if subscribed else "☐"
    warn = "" if chat.available else " ⚠️"
    return literal(f"{mark} {chat.ref.title}{warn}")


def chats_menu(chats: Sequence[ChatRow], subscribed: set[int], page_index: int) -> Keyboard:
    page = paginate(chats, page_index, CHATS_PER_PAGE)
    rows: Keyboard = []
    for chat in page.items:
        target = 0 if chat.id in subscribed else 1
        rows.append(
            [(chat_label(chat, subscribed=chat.id in subscribed), f"sub:{chat.id}:{target}")]
        )
    nav = nav_row("chats", page)
    if nav:
        rows.append(nav)
    rows.append([("bot.btn.search", "chats:search"), ("bot.btn.back", "menu")])
    return rows


def shared_menu(chats: Sequence[ChatRow], page_index: int) -> Keyboard:
    page = paginate(chats, page_index, CHATS_PER_PAGE)
    rows: Keyboard = []
    for chat in page.items:
        mark = "🗂" if chat.shared else "🔒"
        target = 0 if chat.shared else 1
        rows.append([(literal(f"{mark} {chat.ref.title}"), f"share:{chat.id}:{target}")])
    nav = nav_row("shared", page)
    if nav:
        rows.append(nav)
    rows.append([("bot.btn.back", "menu")])
    return rows


def rules_chat_menu(chats: Sequence[ChatRow], page_index: int) -> Keyboard:
    page = paginate(chats, page_index, CHATS_PER_PAGE)
    rows: Keyboard = [[("bot.btn.all_chats", f"rules:{ALL_CHATS}:include:0")]]
    for chat in page.items:
        rows.append([(literal(chat.ref.title), f"rules:{chat.id}:include:0")])
    nav = nav_row("rpick", page)
    if nav:
        rows.append(nav)
    rows.append([("bot.btn.back", "menu")])
    return rows


def rule_preview(rule: Rule, icon: str) -> str:
    pattern = rule.pattern
    if len(pattern) > PATTERN_PREVIEW:
        pattern = pattern[: PATTERN_PREVIEW - 1] + "…"
    disabled = "⏸ " if rule.disabled_reason else ""
    return literal(f"❌ {disabled}{icon} {pattern}")


def rules_menu(
    rules: Sequence[Rule],
    icons: Sequence[str],
    chat_id: int,
    kind: str,
    page_index: int,
) -> Keyboard:
    page = paginate(list(zip(rules, icons, strict=True)), page_index, RULES_PER_PAGE)
    rows: Keyboard = [
        [(rule_preview(rule, icon), check_callback(f"rdel:{rule.id}"))] for rule, icon in page.items
    ]
    nav = nav_row("rules", page, suffix=f":{chat_id}:{kind}")
    if nav:
        # the paging callback carries the chat and kind so the handler stays stateless
        nav = [
            (label, check_callback(f"rules:{chat_id}:{kind}:{data.split(':')[1]}"))
            for label, data in nav
        ]
        rows.append(nav)
    other = "exclude" if kind == "include" else "include"
    other_label = "bot.btn.exclude" if kind == "include" else "bot.btn.include"
    rows.append(
        [
            ("bot.btn.add", check_callback(f"radd:{chat_id}:{kind}")),
            (other_label, check_callback(f"rules:{chat_id}:{other}:0")),
        ]
    )
    rows.append([("bot.btn.back", "rules:pick")])
    return rows


def ignored_menu(entries: Sequence[IgnoredSender], page_index: int) -> Keyboard:
    page = paginate(entries, page_index, RULES_PER_PAGE)
    rows: Keyboard = []
    for entry in page.items:
        scope = "🌐" if entry.chat_id == ALL_CHATS else "📡"
        label = literal(f"❌ {scope} {entry.note or entry.sender_id}")
        rows.append([(label, check_callback(f"igdel:{entry.chat_id}:{entry.sender_id}"))])
    nav = nav_row("ign", page)
    if nav:
        rows.append(nav)
    rows.append([("bot.btn.back", "menu")])
    return rows


def settings_menu() -> Keyboard:
    return [
        [("bot.settings.language", "set:lang"), ("bot.settings.timezone", "set:tz")],
        [("bot.settings.digest", "set:digest")],
        [("bot.btn.back", "menu")],
    ]


def notification_buttons(chat_id: int, msg_id: int, sender_id: int | None) -> Keyboard:
    """Buttons under a notification. No nonce: the handler reads the user's own outbox row."""
    row: list[Button] = []
    if sender_id is not None:
        row.append(("notif.btn.ignore", check_callback(f"ig:{chat_id}:{sender_id}")))
    row.append(("notif.btn.exclude", check_callback(f"ex:{chat_id}:{msg_id}")))
    return [row]


def users_menu(pending: Sequence[int], allowed: Sequence[int]) -> Keyboard:
    rows: Keyboard = []
    for user_id in pending:
        rows.append(
            [
                ("bot.btn.allow", check_callback(f"allow:{user_id}")),
                ("bot.btn.deny", check_callback(f"deny:{user_id}")),
            ]
        )
    for user_id in allowed:
        rows.append([("bot.btn.revoke", check_callback(f"revoke:{user_id}"))])
    rows.append([("bot.btn.back", "menu")])
    return rows
"""Offline fakes: Telethon events built without a connection, a fake bot client, a fake clock."""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from telethon import events, utils
from telethon.tl import types as tl

T0 = dt.datetime(2026, 9, 22, 12, 0, 0, tzinfo=dt.UTC)
SELF_ID = 999


class FakeClock:
    """Callable UTC clock that only moves when the test says so."""

    def __init__(self, start: dt.datetime = T0) -> None:
        self.now = start

    def __call__(self) -> dt.datetime:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += dt.timedelta(seconds=seconds)


class VirtualClock(FakeClock):
    """Clock whose ``sleep`` advances time instantly, for timing-sensitive code."""

    def __init__(self, start: dt.datetime = T0) -> None:
        super().__init__(start)
        self.slept: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.tick(seconds)
        await asyncio.sleep(0)

    def monotonic(self) -> float:
        return (self.now - T0).total_seconds()


class _MbCache:
    def get(self, _bare_id: int) -> None:
        return None


class FakeUserClient:
    """The minimum surface Telethon events need, plus recorders for what we call."""

    parse_mode = None

    def __init__(self, self_id: int = SELF_ID) -> None:
        self._self_id = self_id
        self._mb_entity_cache = _MbCache()
        self.entity_calls: list[int] = []
        self.entities: dict[int, Any] = {}

    async def get_entity(self, peer_id: int) -> Any:
        self.entity_calls.append(peer_id)
        try:
            return self.entities[peer_id]
        except KeyError as exc:
            msg = f"Could not find the input entity for {peer_id}"
            raise ValueError(msg) from exc


@dataclass
class SentMessage:
    user_id: int
    text: str
    entities: list[Any] = field(default_factory=list)
    buttons: Any = None
    link_preview: bool = True


class FakeBotClient:
    """Captures ``send_message`` calls.

    ``fail_next`` queues exceptions to raise; ``hold``/``release`` block sending so a test can
    build up a backlog deterministically.
    """

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self.errors: list[BaseException | None] = []
        self._gate = asyncio.Event()
        self._gate.set()

    def fail_next(self, *errors: BaseException | None) -> None:
        self.errors.extend(errors)

    def hold(self) -> None:
        self._gate.clear()

    def release(self) -> None:
        self._gate.set()

    async def send_message(
        self,
        user_id: int,
        text: str,
        *,
        formatting_entities: list[Any] | None = None,
        buttons: Any = None,
        link_preview: bool = True,
    ) -> SentMessage:
        await self._gate.wait()
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        msg = SentMessage(user_id, text, list(formatting_entities or []), buttons, link_preview)
        self.sent.append(msg)
        return msg


def make_channel(
    chat_id: int = -1001234567890,
    title: str = "Test chat",
    username: str | None = None,
    *,
    megagroup: bool = True,
    forum: bool = False,
) -> tl.Channel:
    bare = utils.resolve_id(chat_id)[0]
    return tl.Channel(
        id=bare,
        title=title,
        photo=tl.ChatPhotoEmpty(),
        date=None,
        megagroup=megagroup,
        broadcast=not megagroup,
        username=username,
        forum=forum,
    )


def make_user(
    user_id: int = 42,
    first_name: str | None = "Иван",
    username: str | None = "ivan",
    *,
    bot: bool = False,
    deleted: bool = False,
    min_: bool = True,
) -> tl.User:
    return tl.User(
        id=user_id,
        first_name=first_name,
        username=username,
        bot=bot,
        deleted=deleted,
        min=min_,
        access_hash=None if min_ else 12345,
    )


def make_event(
    text: str = "hello",
    *,
    chat: tl.Channel | tl.Chat | None = None,
    sender: tl.User | tl.Channel | None = None,
    msg_id: int = 77,
    date: dt.datetime = T0,
    out: bool = False,
    topic_id: int | None = None,
    fwd_from_name: str | None = None,
    client: FakeUserClient | None = None,
) -> events.NewMessage.Event:
    """Build a ``NewMessage`` event offline, with entities attached like Telethon does."""
    chat = chat if chat is not None else make_channel()
    sender = sender if sender is not None else make_user()
    reply_to = None
    if topic_id is not None:
        reply_to = tl.MessageReplyHeader(reply_to_msg_id=topic_id, forum_topic=True)
    fwd = None
    if fwd_from_name is not None:
        fwd = tl.MessageFwdHeader(date=date, from_name=fwd_from_name)
    message = tl.Message(
        id=msg_id,
        peer_id=tl.PeerChannel(chat.id) if isinstance(chat, tl.Channel) else tl.PeerChat(chat.id),
        message=text,
        date=date,
        from_id=_peer_of(sender),
        out=out,
        reply_to=reply_to,
        fwd_from=fwd,
    )
    event = events.NewMessage.Event(message)
    event._entities = {
        utils.get_peer_id(chat): chat,
        utils.get_peer_id(sender): sender,
    }
    event._set_client(client or FakeUserClient())
    return event


def _peer_of(entity: tl.User | tl.Channel) -> tl.TypePeer:
    if isinstance(entity, tl.User):
        return tl.PeerUser(entity.id)
    return tl.PeerChannel(entity.id)
import datetime as dt
import sqlite3

import pytest

import ddiver.storage as storage_module
from ddiver.links import ChatRef
from ddiver.rules import RuleKind
from ddiver.storage import (
    ALL_CHATS,
    Cursor,
    DatabaseCorruptError,
    DeliveryStatus,
    DuplicateRuleError,
    Role,
    Storage,
    safe_write,
)
from tests.fakes import T0, FakeClock

OWNER = 1
MOD = 2
STRANGER = 3

CHAT_A = ChatRef(-1001, "Alpha", "alpha", "supergroup")
CHAT_B = ChatRef(-1002, "beta", None, "supergroup", is_forum=True)
BASIC = ChatRef(-55, "Old group", None, "group")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
async def store(tmp_path, clock):
    s = Storage(tmp_path / "db.sqlite3", owner_id=OWNER, clock=clock)
    await s.open()
    yield s
    await s.close()


@pytest.fixture
async def seeded(store):
    await store.ensure_user(OWNER)
    await store.ensure_user(MOD)
    await store.update_user(MOD, role=Role.ALLOWED)
    await store.ensure_user(STRANGER)
    await store.upsert_chat(CHAT_A, access_hash=111)
    await store.upsert_chat(CHAT_B)
    return store


class TestLifecycle:
    async def test_migrations_run_once(self, tmp_path, clock):
        s = Storage(tmp_path / "db.sqlite3", clock=clock)
        await s.open()
        assert await s.schema_version() == 1
        await s.close()
        s2 = Storage(tmp_path / "db.sqlite3", clock=clock)
        await s2.open()
        assert await s2.schema_version() == 1
        await s2.close()

    async def test_open_failure_closes_connection(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage_module, "_MIGRATIONS", ["CREATE TABLE broken ("])
        s = Storage(tmp_path / "db.sqlite3")
        with pytest.raises(Exception, match="syntax error"):
            await s.open()
        with pytest.raises(RuntimeError, match="not open"):
            _ = s.db

    async def test_corrupt_database_is_detected(self, tmp_path):
        path = tmp_path / "db.sqlite3"
        path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
        s = Storage(path)
        with pytest.raises((DatabaseCorruptError, sqlite3.DatabaseError)):
            await s.open()

    async def test_safe_write_swallows_locked_database(self, caplog):
        async def boom():
            raise sqlite3.OperationalError("database is locked")

        async def fine():
            return None

        assert await safe_write(boom(), "cursor flush") is False
        assert "sqlite busy" in caplog.text
        assert await safe_write(fine(), "cursor flush") is True

    async def test_record_boot_counts_recent_boots(self, store, clock):
        assert await store.record_boot() == 1
        assert await store.record_boot() == 2
        clock.tick(11 * 60)
        assert await store.record_boot() == 1


class TestUsers:
    async def test_owner_is_computed_and_always_active(self, store):
        owner = await store.ensure_user(OWNER)
        assert owner.is_owner and owner.active and owner.role is Role.ALLOWED
        await store.update_user(OWNER, role=Role.DENIED)
        assert (await store.get_user(OWNER)).active

    async def test_stranger_defaults(self, store):
        user = await store.ensure_user(STRANGER, lang="ru")
        assert (user.role, user.lang, user.tz, user.onboarded, user.active) == (
            Role.PENDING,
            "ru",
            None,
            False,
            False,
        )

    async def test_ensure_user_is_idempotent(self, store):
        await store.ensure_user(MOD, lang="uk")
        await store.update_user(MOD, role=Role.ALLOWED)
        again = await store.ensure_user(MOD, lang="en")
        assert (again.role, again.lang) == (Role.ALLOWED, "uk")

    async def test_onboarding_flags(self, store):
        await store.ensure_user(MOD)
        user = await store.update_user(MOD, lang="uk")
        assert not user.onboarded
        user = await store.update_user(MOD, tz="Europe/Kyiv")
        assert user.onboarded

    async def test_pause_and_resume_delivery(self, store):
        await store.ensure_user(MOD)
        await store.update_user(MOD, role=Role.ALLOWED)
        await store.pause_delivery(MOD, "blocked")
        user = await store.get_user(MOD)
        assert user.paused and user.pause_reason == "blocked"
        assert user.active  # role is unaffected, so no re-approval is needed
        assert user.delivery_paused_at == T0
        assert await store.resume_delivery(MOD) is True
        assert await store.resume_delivery(MOD) is False
        assert not (await store.get_user(MOD)).paused

    async def test_update_user_validation(self, store):
        await store.ensure_user(MOD)
        with pytest.raises(ValueError, match="unknown user fields"):
            await store.update_user(MOD, hacker="x")
        with pytest.raises(KeyError):
            await store.update_user(999, lang="en")
        assert (await store.update_user(MOD)).id == MOD

    async def test_active_user_ids(self, seeded):
        assert await seeded.active_user_ids() == {OWNER, MOD}
        await seeded.update_user(MOD, role=Role.DENIED)
        assert await seeded.active_user_ids() == {OWNER}

    async def test_list_users_by_role(self, seeded):
        assert [u.id for u in await seeded.list_users(roles={Role.PENDING})] == [STRANGER]
        assert len(await seeded.list_users()) == 3


class TestChatsAndSubscriptions:
    async def test_upsert_refreshes_metadata_but_keeps_flags(self, seeded):
        await seeded.set_chat_shared(CHAT_A.id, True)
        await seeded.set_chat_available(CHAT_A.id, False)
        await seeded.upsert_chat(ChatRef(CHAT_A.id, "Alpha2", "alpha2", "supergroup"))
        chat = await seeded.get_chat(CHAT_A.id)
        assert (chat.ref.title, chat.ref.username) == ("Alpha2", "alpha2")
        assert chat.shared is True and chat.available is False
        assert chat.access_hash == 111  # not clobbered by an upsert without one

    async def test_available_and_shared_report_transitions(self, seeded):
        assert await seeded.set_chat_available(CHAT_A.id, False) is True
        assert await seeded.set_chat_available(CHAT_A.id, False) is False
        assert await seeded.set_chat_shared(CHAT_A.id, True) is True
        assert await seeded.set_chat_shared(CHAT_A.id, True) is False
        assert [c.id for c in await seeded.list_chats(shared_only=True)] == [CHAT_A.id]

    async def test_subscribe_rejects_unknown_and_non_group_chats(self, seeded):
        assert await seeded.subscribe(MOD, -9999) is False  # unknown chat
        assert await seeded.subscribe(MOD, 12345) is False  # a positive id is a private chat
        assert await seeded.subscribe(MOD, CHAT_A.id) is True
        assert await seeded.subscribe(MOD, CHAT_A.id) is False  # idempotent

    async def test_monitored_set_excludes_inactive_paused_and_unavailable(self, seeded):
        await seeded.subscribe(OWNER, CHAT_A.id)
        await seeded.subscribe(MOD, CHAT_B.id)
        await seeded.subscribe(STRANGER, CHAT_B.id)
        assert await seeded.monitored_chat_ids() == {CHAT_A.id, CHAT_B.id}
        await seeded.pause_delivery(MOD, "blocked")
        assert await seeded.monitored_chat_ids() == {CHAT_A.id}
        await seeded.resume_delivery(MOD)
        await seeded.set_chat_available(CHAT_B.id, False)
        assert await seeded.monitored_chat_ids() == {CHAT_A.id}

    async def test_subscribers_of_filters_inactive(self, seeded):
        await seeded.subscribe(MOD, CHAT_A.id)
        await seeded.subscribe(STRANGER, CHAT_A.id)
        assert await seeded.subscribers_of(CHAT_A.id) == [MOD]
        assert await seeded.subscribers_of(CHAT_A.id, active_only=False) == [MOD, STRANGER]

    async def test_unsubscribe_all_on_unshare(self, seeded):
        await seeded.subscribe(OWNER, CHAT_A.id)
        await seeded.subscribe(MOD, CHAT_A.id)
        assert await seeded.unsubscribe_all(CHAT_A.id, except_user=OWNER) == [MOD]
        assert [c.id for c in await seeded.list_subscriptions(OWNER)] == [CHAT_A.id]
        assert await seeded.list_subscriptions(MOD) == []
        assert await seeded.unsubscribe(OWNER, CHAT_A.id) is True
        assert await seeded.unsubscribe(OWNER, CHAT_A.id) is False

    async def test_is_subscribed(self, seeded):
        await seeded.subscribe(MOD, CHAT_A.id)
        assert await seeded.is_subscribed(MOD, CHAT_A.id)
        assert not await seeded.is_subscribed(MOD, CHAT_B.id)

    async def test_subscription_requires_existing_user(self, store):
        await store.upsert_chat(CHAT_A)
        with pytest.raises(Exception, match="FOREIGN KEY"):
            await store.subscribe(404, CHAT_A.id)

    async def test_migrate_basic_group_to_supergroup(self, seeded):
        await seeded.upsert_chat(BASIC)
        await seeded.subscribe(MOD, BASIC.id)
        rule = await seeded.add_rule(MOD, BASIC.id, RuleKind.INCLUDE, "казино")
        await seeded.add_ignored(MOD, BASIC.id, 500)
        await seeded.upsert_cursors([Cursor(BASIC.id, 10, T0)])
        new_ref = ChatRef(-1009999, "Old group", None, "supergroup")
        await seeded.migrate_chat(BASIC.id, new_ref, access_hash=42)
        assert [c.id for c in await seeded.list_subscriptions(MOD)] == [new_ref.id]
        assert (await seeded.get_rule(rule.id, MOD)).chat_id == new_ref.id
        assert (await seeded.list_ignored(MOD))[0].chat_id == new_ref.id
        assert BASIC.id not in await seeded.get_cursors()
        assert (await seeded.get_chat(BASIC.id)).available is False
        assert (await seeded.get_chat(new_ref.id)).access_hash == 42


class TestRules:
    async def test_crud_and_scoping(self, seeded):
        await seeded.subscribe(OWNER, CHAT_A.id)
        await seeded.subscribe(MOD, CHAT_A.id)
        chat_rule = await seeded.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "казино")
        global_rule = await seeded.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "=бан")
        mod_rule = await seeded.add_rule(MOD, ALL_CHATS, RuleKind.INCLUDE, "spam")
        assert global_rule.is_global and not chat_rule.is_global
        assert [r.id for r in await seeded.list_rules(OWNER)] == [global_rule.id, chat_rule.id]
        assert [r.id for r in await seeded.list_rules(OWNER, CHAT_A.id)] == [chat_rule.id]
        assert await seeded.get_rule(mod_rule.id, OWNER) is None
        assert await seeded.delete_rule(mod_rule.id, OWNER) is False
        assert await seeded.delete_rule(mod_rule.id, MOD) is True

    async def test_duplicates_are_case_folded(self, seeded):
        await seeded.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "казино")
        for dup in ("Казино", "КАЗИНО"):
            with pytest.raises(DuplicateRuleError):
                await seeded.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, dup)
        await seeded.add_rule(OWNER, CHAT_A.id, RuleKind.EXCLUDE, "казино")
        await seeded.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "казино")
        await seeded.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "re:Abc")
        await seeded.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "re:abc")
        assert await seeded.count_rules(OWNER) == 5
        assert await seeded.count_rules(OWNER, CHAT_A.id) == 4

    async def test_rules_for_chat_ordering_and_filters(self, seeded):
        await seeded.subscribe(OWNER, CHAT_A.id)
        await seeded.subscribe(MOD, CHAT_A.id)
        await seeded.subscribe(STRANGER, CHAT_A.id)
        g = await seeded.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "g")
        c = await seeded.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "c")
        other_chat = await seeded.add_rule(OWNER, CHAT_B.id, RuleKind.INCLUDE, "other")
        mod_rule = await seeded.add_rule(MOD, ALL_CHATS, RuleKind.INCLUDE, "m")
        stranger_rule = await seeded.add_rule(STRANGER, ALL_CHATS, RuleKind.INCLUDE, "s")
        ids = [r.id for r in await seeded.rules_for_chat(CHAT_A.id)]
        assert ids[:2] == [c.id, g.id]  # chat-specific before global, per user
        assert mod_rule.id in ids
        assert other_chat.id not in ids
        assert stranger_rule.id not in ids  # pending user
        await seeded.pause_delivery(MOD, "blocked")
        assert mod_rule.id not in [r.id for r in await seeded.rules_for_chat(CHAT_A.id)]

    async def test_disable_and_enable(self, seeded):
        await seeded.subscribe(OWNER, CHAT_A.id)
        rule = await seeded.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "казино")
        await seeded.disable_rule(rule.id, "timeout")
        assert await seeded.rules_for_chat(CHAT_A.id) == []
        assert (await seeded.get_rule(rule.id, OWNER)).disabled_reason == "timeout"
        assert await seeded.enable_rule(rule.id, MOD) is False
        assert await seeded.enable_rule(rule.id, OWNER) is True
        assert len(await seeded.rules_for_chat(CHAT_A.id)) == 1

    async def test_flush_rule_hits(self, seeded):
        r1 = await seeded.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "a")
        r2 = await seeded.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "b")
        await seeded.flush_rule_hits({r1.id: 3, r2.id: 1})
        await seeded.flush_rule_hits({})
        assert (await seeded.get_rule(r1.id, OWNER)).hits == 3
        assert (await seeded.get_rule(r2.id, OWNER)).hits == 1

    async def test_deleting_user_cascades(self, seeded):
        await seeded.subscribe(MOD, CHAT_A.id)
        await seeded.add_rule(MOD, CHAT_A.id, RuleKind.INCLUDE, "x")
        await seeded.db.execute("DELETE FROM users WHERE id=?", (MOD,))
        assert await seeded.subscribers_of(CHAT_A.id, active_only=False) == []
        assert await seeded.list_rules(MOD) == []


class TestIgnoredSenders:
    async def test_scoping(self, seeded):
        assert await seeded.add_ignored(OWNER, CHAT_A.id, 500, "spammer") is True
        assert await seeded.add_ignored(OWNER, CHAT_A.id, 500) is False
        await seeded.add_ignored(OWNER, ALL_CHATS, 600)
        await seeded.add_ignored(MOD, CHAT_B.id, -1001234567890)  # anonymous admin, marked id
        assert await seeded.ignored_for_chat(CHAT_A.id) == {OWNER: {500, 600}}
        assert await seeded.ignored_for_chat(CHAT_B.id) == {OWNER: {600}, MOD: {-1001234567890}}
        listed = await seeded.list_ignored(OWNER)
        assert [(i.chat_id, i.sender_id, i.note) for i in listed] == [
            (ALL_CHATS, 600, None),
            (CHAT_A.id, 500, "spammer"),
        ]
        assert [i.sender_id for i in await seeded.list_ignored(OWNER, CHAT_A.id)] == [500]
        assert await seeded.remove_ignored(OWNER, CHAT_A.id, 500) is True
        assert await seeded.remove_ignored(OWNER, CHAT_A.id, 500) is False


class TestCursors:
    async def test_never_move_backwards(self, store):
        await store.upsert_cursors([Cursor(-1001, 100, T0)])
        await store.upsert_cursors([Cursor(-1001, 90, T0), Cursor(-1002, 5, T0)])
        cursors = await store.get_cursors()
        assert cursors[-1001].last_msg_id == 100
        assert cursors[-1002].last_msg_id == 5
        later = T0 + dt.timedelta(seconds=1)
        await store.upsert_cursors([Cursor(-1001, 101, later)])
        cursors = await store.get_cursors()
        assert (cursors[-1001].last_msg_id, cursors[-1001].last_msg_at) == (101, later)
        await store.upsert_cursors([])
        await store.delete_cursor(-1002)
        assert -1002 not in await store.get_cursors()


PAYLOAD = {"text": "казино", "chat_title": "Alpha"}


class TestOutbox:
    async def test_claim_is_idempotent_per_user_and_message(self, store):
        item = await store.claim_delivery(OWNER, -1001, 10, 7, PAYLOAD)
        assert item is not None
        assert item.status is DeliveryStatus.PENDING
        assert item.payload == PAYLOAD
        assert item.key == (OWNER, -1001, 10)
        assert await store.claim_delivery(OWNER, -1001, 10, 8, PAYLOAD) is None
        assert await store.claim_delivery(MOD, -1001, 10, 7, PAYLOAD) is not None

    async def test_pending_are_replayed_in_order(self, store, clock):
        await store.claim_delivery(OWNER, -1001, 10, None, PAYLOAD)
        clock.tick(1)
        await store.claim_delivery(OWNER, -1001, 11, None, PAYLOAD)
        clock.tick(1)
        await store.claim_delivery(MOD, -1002, 5, None, PAYLOAD)
        await store.set_delivery_status(OWNER, -1001, 10, DeliveryStatus.SENT)
        pending = await store.pending_deliveries()
        assert [p.key for p in pending] == [(OWNER, -1001, 11), (MOD, -1002, 5)]

    async def test_status_transitions_and_attempts(self, store):
        await store.claim_delivery(OWNER, -1001, 10, 7, PAYLOAD)
        await store.set_delivery_status(
            OWNER, -1001, 10, DeliveryStatus.FAILED, error="MessageTooLong", attempts=2
        )
        item = await store.get_delivery(OWNER, -1001, 10)
        assert (item.status, item.attempts) == (DeliveryStatus.FAILED, 2)
        await store.set_delivery_status(OWNER, -1001, 10, DeliveryStatus.SENT)
        item = await store.get_delivery(OWNER, -1001, 10)
        assert item.status is DeliveryStatus.SENT and item.attempts == 2

    async def test_mark_digested_in_batch(self, store):
        keys = []
        for msg_id in range(5):
            await store.claim_delivery(OWNER, -1001, msg_id, None, PAYLOAD)
            keys.append((OWNER, -1001, msg_id))
        await store.mark_digested(keys)
        await store.mark_digested([])
        assert await store.pending_deliveries() == []
        assert await store.delivery_counts(since=T0 - dt.timedelta(hours=1)) == {"digested": 5}

    async def test_release_allows_reclaim(self, store):
        await store.claim_delivery(OWNER, -1001, 10, None, PAYLOAD)
        await store.release_delivery(OWNER, -1001, 10)
        assert await store.claim_delivery(OWNER, -1001, 10, None, PAYLOAD) is not None

    async def test_prune_keeps_pending_and_recent(self, store, clock):
        await store.claim_delivery(OWNER, -1001, 1, None, PAYLOAD)  # stays pending
        await store.claim_delivery(OWNER, -1001, 2, None, PAYLOAD)
        await store.set_delivery_status(OWNER, -1001, 2, DeliveryStatus.SENT)
        clock.tick(8 * 86400)
        await store.claim_delivery(OWNER, -1001, 3, None, PAYLOAD)
        await store.set_delivery_status(OWNER, -1001, 3, DeliveryStatus.SENT)
        assert await store.prune_outbox() == 1
        assert {i.msg_id for i in await store.pending_deliveries()} == {1}
        assert await store.get_delivery(OWNER, -1001, 3) is not None

**Verifying chat storage logic and pagination**
**Reviewing ignore handler and message forwarding logic**
**Planning peer ID retrieval inspection**
exec
/bin/zsh -lc "uv run python - <<'PY'
import inspect
from telethon.client.users import UserMethods
print(inspect.getsource(UserMethods.get_input_entity))
PY" in /Users/L/Code/ddiver
 succeeded in 11ms:
**Analyzing Telethon cache handling issues**
**Planning regex offload to executor**
exec
/bin/zsh -lc "sed -n '1,190p' tests/test_matcher.py && nl -ba ddiver/runtime.py | sed -n '245,470p' && nl -ba ddiver/delivery.py | sed -n '125,385p'" in /Users/L/Code/ddiver
 succeeded in 0ms:
import pytest
import regex

from ddiver.links import ChatRef
from ddiver.matcher import TIMEOUTS_BEFORE_DISABLE, Matcher
from ddiver.rules import CompiledRule, MatchMode, RuleKind
from ddiver.storage import ALL_CHATS, Role, Storage

CHAT_A = ChatRef(-1001, "Alpha", "alpha", "supergroup")
CHAT_B = ChatRef(-1002, "Beta", None, "supergroup")


@pytest.fixture
async def store(tmp_path):
    s = Storage(tmp_path / "db.sqlite3", owner_id=1)
    await s.open()
    await s.ensure_user(1)
    await s.ensure_user(2)
    await s.update_user(2, role=Role.ALLOWED)
    await s.upsert_chat(CHAT_A)
    await s.upsert_chat(CHAT_B)
    await s.subscribe(1, CHAT_A.id)
    await s.subscribe(2, CHAT_A.id)
    await s.subscribe(1, CHAT_B.id)
    yield s
    await s.close()


@pytest.fixture
async def matcher(store):
    m = Matcher(store, timeout=0.2)
    await m.reload()
    return m


async def test_monitored_set_and_fast_path(store, matcher):
    assert matcher.monitored == {CHAT_A.id, CHAT_B.id}
    assert matcher.is_monitored(CHAT_A.id) and not matcher.is_monitored(-5)
    assert await matcher.evaluate(-5, "казино", 10) == []
    assert await matcher.evaluate(CHAT_A.id, "", 10) == []
    assert matcher.stats_evaluated == 0


async def test_one_match_per_user_chat_specific_before_global(store, matcher):
    g = await store.add_rule(1, ALL_CHATS, RuleKind.INCLUDE, "казино")
    c = await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, "=казино")
    await store.add_rule(2, CHAT_A.id, RuleKind.INCLUDE, "re:каз\\w+")
    await matcher.invalidate()
    matches = await matcher.evaluate(CHAT_A.id, "Заходи в КАЗИНО", 10)
    assert {(m.user_id, m.rule.id) for m in matches} == {(1, c.id), (2, matches[1].rule.id)}
    assert "Заходи в КАЗИНО"[matches[0].span.start : matches[0].span.end] == "КАЗИНО"
    # in chat B only user 1 is subscribed and only the global rule applies
    matches_b = await matcher.evaluate(CHAT_B.id, "казино!", 10)
    assert [(m.user_id, m.rule.id) for m in matches_b] == [(1, g.id)]
    assert matcher.stats_matched == 2


async def test_exclusion_beats_inclusion(store, matcher):
    await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, "казино")
    await store.add_rule(1, ALL_CHATS, RuleKind.EXCLUDE, "re:тест")
    await matcher.invalidate()
    assert await matcher.evaluate(CHAT_A.id, "казино тест", 10) == []
    assert len(await matcher.evaluate(CHAT_A.id, "казино", 10)) == 1


async def test_ignored_sender_per_chat_and_global(store, matcher):
    await store.add_rule(1, ALL_CHATS, RuleKind.INCLUDE, "спам")
    await store.add_ignored(1, CHAT_A.id, 500)
    await store.add_ignored(1, ALL_CHATS, 600)
    await matcher.invalidate()
    assert await matcher.evaluate(CHAT_A.id, "спам", 500) == []
    assert await matcher.evaluate(CHAT_B.id, "спам", 500) != []  # chat-scoped ignore
    assert await matcher.evaluate(CHAT_A.id, "спам", 600) == []
    assert await matcher.evaluate(CHAT_B.id, "спам", 600) == []
    assert await matcher.evaluate(CHAT_A.id, "спам", None) != []  # unknown sender is not ignored


async def test_invalidate_single_chat_refreshes_only_that_chat(store, matcher):
    await store.add_rule(1, ALL_CHATS, RuleKind.INCLUDE, "one")
    await matcher.invalidate()
    assert await matcher.evaluate(CHAT_A.id, "one", 1) != []
    assert await matcher.evaluate(CHAT_B.id, "one", 1) != []
    await store.add_rule(1, ALL_CHATS, RuleKind.INCLUDE, "two")
    await matcher.invalidate(CHAT_A.id)
    assert await matcher.evaluate(CHAT_A.id, "two", 1) != []
    assert await matcher.evaluate(CHAT_B.id, "two", 1) == []  # still cached without 'two'


async def test_unsubscribe_removes_chat_from_monitored(store, matcher):
    await store.unsubscribe(1, CHAT_B.id)
    await matcher.invalidate(CHAT_B.id)
    assert not matcher.is_monitored(CHAT_B.id)


async def test_invalid_stored_rule_is_disabled_with_hook(store):
    disabled = []

    async def hook(rule, reason):
        disabled.append((rule.id, reason))

    bad = await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, "re:(unclosed")
    good = await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, "ok")
    m = Matcher(store, timeout=0.2, on_rule_disabled=hook)
    await m.reload()
    matches = await m.evaluate(CHAT_A.id, "ok (unclosed", 1)
    assert [x.rule.id for x in matches] == [good.id]
    assert disabled == [(bad.id, "invalid:rule.err.invalid_regex")]
    row = await store.get_rule(bad.id, 1)
    assert row is not None and row.disabled_reason == "invalid:rule.err.invalid_regex"


async def test_timeout_counts_and_disables_after_threshold(store, monkeypatch):
    disabled = []

    async def hook(rule, reason):
        disabled.append((rule.id, reason))

    slow = await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, "re:slow")
    m = Matcher(store, timeout=0.02, on_rule_disabled=hook)
    await m.reload()
    # Replace compilation with a genuinely catastrophic pattern that validation would reject.
    monkeypatch.setattr(
        "ddiver.matcher.compile_rule",
        lambda raw, timeout: CompiledRule(raw, MatchMode.REGEX, regex.compile("((a|aa)+)+b")),
    )
    for i in range(TIMEOUTS_BEFORE_DISABLE):
        assert await m.evaluate(CHAT_A.id, "a" * 40, 1) == []
        assert m.stats_timeouts == i + 1
    assert disabled == [(slow.id, "timeout")]
    row = await store.get_rule(slow.id, 1)
    assert row is not None and row.disabled_reason == "timeout"
    # after disabling, the rule is gone from the (rebuilt) rule set
    monkeypatch.undo()
    assert await m.evaluate(CHAT_A.id, "slow", 1) == []
   245	            await self.listener.cursors.flush()
   246	        if self.matcher is not None:
   247	            await self.matcher.flush_hits()
   248	        if self.delivery is not None:
   249	            left = await self.delivery.drain()
   250	            if left:
   251	                log.warning("%d notifications stay pending for the next start", left)
   252	        with contextlib.suppress(Exception):
   253	            await self.bot_client.disconnect()
   254	        await self.storage.close()
   255	
   256	    async def run(self) -> int:
   257	        assert self.supervisor is not None  # noqa: S101 - set up in setup()
   258	        await connect_with_timeout(self.bot_client)
   259	        await self.bot_client.start(bot_token=self.settings.bot_token.get_secret_value())
   260	        await connect_with_timeout(self.user_client)
   261	        if not await self.user_client.is_user_authorized():
   262	            log.error("the user session is not authorized; run `ddiver login`")
   263	            return 3
   264	        boots = await self.storage.record_boot()
   265	        assert self.delivery is not None  # noqa: S101
   266	        await self.delivery.replay_pending()
   267	        self._start_background_tasks()
   268	        await boot_delay(boots)
   269	        asyncio.create_task(self._catch_up(), name="catchup-boot")  # noqa: RUF006
   270	        return await self.supervisor.run()
   271	
   272	    def request_stop(self) -> None:
   273	        if self.supervisor is not None:
   274	            self.supervisor.request_stop()
   275	        with contextlib.suppress(Exception):
   276	            self.user_client.disconnect()
   277	
   278	    # -- handlers --------------------------------------------------------------------------
   279	
   280	    def _register_handlers(self) -> None:
   281	        listener = self.listener
   282	        assert listener is not None  # noqa: S101
   283	
   284	        @self.user_client.on(events.NewMessage())
   285	        async def on_message(event: Any) -> None:
   286	            await listener.on_message(event)
   287	
   288	        @self.user_client.on(events.Raw(tl.UpdateChannel))
   289	        async def on_channel_update(update: Any) -> None:
   290	            await self._check_channel_access(update)
   291	
   292	        @self.user_client.on(events.ChatAction())
   293	        async def on_chat_action(event: Any) -> None:
   294	            await self._on_chat_action(event)
   295	
   296	        @self.bot_client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
   297	        async def on_bot_message(event: Any) -> None:
   298	            await self._on_bot_update(event, text=event.raw_text)
   299	
   300	        @self.bot_client.on(events.CallbackQuery(func=lambda e: e.is_private))
   301	        async def on_bot_callback(event: Any) -> None:
   302	            data = event.data.decode("utf-8", "replace") if event.data else ""
   303	            await self._on_bot_update(event, data=data)
   304	
   305	    async def _on_bot_update(
   306	        self, event: Any, *, text: str | None = None, data: str | None = None
   307	    ) -> None:
   308	        assert self.router is not None  # noqa: S101
   309	        try:
   310	            sender = await event.get_sender()
   311	            incoming = Incoming(
   312	                user_id=int(event.sender_id),
   313	                text=text,
   314	                data=data,
   315	                msg_id=getattr(event, "message_id", None),
   316	                lang_code=getattr(sender, "lang_code", None),
   317	                is_private=True,
   318	            )
   319	            actions = await self.router.dispatch(incoming)
   320	            user = await self.storage.get_user(incoming.user_id)
   321	            lang = (user.lang if user else None) or DEFAULT_LANG
   322	            await self.bot.perform(event, actions, lang)
   323	        except Exception:
   324	            log.exception("bot update failed")
   325	
   326	    async def _on_chat_action(self, event: Any) -> None:
   327	        try:
   328	            me = await self.user_client.get_me()
   329	            if event.user_id != me.id:
   330	                return
   331	            chat_id = int(utils.get_peer_id(event.action_message.peer_id))
   332	            if event.user_kicked or event.user_left:
   333	                if await self.storage.set_chat_available(chat_id, False):
   334	                    await self._notify_lost_access(chat_id, "left")
   335	                await self._invalidate(chat_id)
   336	            elif event.user_added or event.user_joined:
   337	                if await self.storage.set_chat_available(chat_id, True):
   338	                    await self._notify_regained_access(chat_id)
   339	                await self._invalidate(chat_id)
   340	        except Exception:
   341	            log.debug("chat action handling failed", exc_info=True)
   342	
   343	    async def _check_channel_access(self, update: Any) -> None:
   344	        """``UpdateChannel`` is what Telegram sends when our membership changed."""
   345	        try:
   346	            bare = int(getattr(update, "channel_id", 0))
   347	            if not bare:
   348	                return
   349	            chat_id = int(utils.get_peer_id(tl.PeerChannel(bare)))
   350	            entity = (getattr(update, "_entities", None) or {}).get(chat_id)
   351	            if entity is None:
   352	                return
   353	            lost = isinstance(entity, tl.ChannelForbidden) or bool(getattr(entity, "left", False))
   354	            if lost:
   355	                if await self.storage.set_chat_available(chat_id, False):
   356	                    await self._notify_lost_access(chat_id, type(entity).__name__)
   357	            elif await self.storage.set_chat_available(chat_id, True):
   358	                await self._notify_regained_access(chat_id)
   359	            await self._invalidate(chat_id)
   360	        except Exception:
   361	            log.debug("channel update handling failed", exc_info=True)
   362	
   363	    async def _invalidate(self, chat_id: int | None = None) -> None:
   364	        if self.matcher is not None:
   365	            await self.matcher.invalidate(chat_id)
   366	
   367	    # -- background tasks ------------------------------------------------------------------
   368	
   369	    def _start_background_tasks(self) -> None:
   370	        listener = self.listener
   371	        assert listener is not None  # noqa: S101
   372	        self._tasks = [
   373	            PeriodicTask("cursor-flush", self._flush_state, CURSOR_FLUSH_SECONDS),
   374	            PeriodicTask("heartbeat", self._heartbeat, HEARTBEAT_SECONDS),
   375	            PeriodicTask("ping", self._ping, PING_SECONDS),
   376	            PeriodicTask("reconcile", self._reconcile, RECONCILE_SECONDS),
   377	            PeriodicTask("summary", self._log_summary, SUMMARY_SECONDS),
   378	            PeriodicTask("prune", self._prune, PRUNE_SECONDS),
   379	        ]
   380	        for task in self._tasks:
   381	            task.start()
   382	
   383	    async def _flush_state(self) -> None:
   384	        if self.listener is not None:
   385	            await self.listener.cursors.flush()
   386	        if self.matcher is not None:
   387	            await safe_write(self.matcher.flush_hits(), "rule hits")
   388	
   389	    async def _heartbeat(self) -> None:
   390	        write_heartbeat(self.settings.heartbeat_path, self.clock())
   391	
   392	    async def _ping(self) -> None:
   393	        try:
   394	            async with asyncio.timeout(PING_TIMEOUT):
   395	                await self.user_client(GetStateRequest())
   396	            self._last_ping_ok = self.clock()
   397	        except (TimeoutError, errors.RPCError, ConnectionError):
   398	            log.warning("listener ping failed")
   399	
   400	    async def _reconcile(self) -> None:
   401	        """Telethon can silently drop a channel gap; re-check the tail of every chat."""
   402	        await self._catch_up()
   403	
   404	    async def _catch_up(self) -> None:
   405	        if self.catchup is None:
   406	            return
   407	        async with self._catchup_lock:
   408	            try:
   409	                result = await self.catchup.run()
   410	            except Exception:
   411	                log.exception("catch-up round failed")
   412	                return
   413	        if result.chats:
   414	            log.info(
   415	                "catch-up: %d chats, %d messages, %d notifications",
   416	                result.chats,
   417	                result.messages,
   418	                result.claimed,
   419	            )
   420	
   421	    async def _log_summary(self) -> None:
   422	        if self.listener is None or self.delivery is None:
   423	            return
   424	        metrics = self.listener.metrics
   425	        delivery = self.delivery.metrics
   426	        log.info(
   427	            "seen=%d matched=%d claimed=%d queued=%d delivered=%d digested=%d failed=%d "
   428	            "floodwait_s=%.0f",
   429	            metrics.seen,
   430	            metrics.matched,
   431	            metrics.claimed,
   432	            self.delivery.total_queued(),
   433	            delivery.delivered,
   434	            delivery.digested,
   435	            delivery.failed,
   436	            delivery.flood_seconds,
   437	        )
   438	        last = self.listener.last_update_at
   439	        if last is not None and self.clock() - last > SILENCE_WARN:
   440	            log.warning("no updates for over 10 minutes; forcing a catch-up")
   441	            with contextlib.suppress(Exception):
   442	                await self.user_client.catch_up()
   443	            await self._catch_up()
   444	
   445	    async def _prune(self) -> None:
   446	        removed = await self.storage.prune_outbox()
   447	        if removed:
   448	            log.info("pruned %d old outbox rows", removed)
   449	
   450	    async def _run_listener(self) -> None:
   451	        if self.user_client.reconnected.is_set():
   452	            self.user_client.reconnected.clear()
   453	            await self._catch_up()
   454	        await self.user_client.run_until_disconnected()
   455	
   456	    # -- helpers ---------------------------------------------------------------------------
   457	
   458	    async def _resolve_entity(self, peer_id: int) -> Any:
   459	        return await self.user_client.get_entity(peer_id)
   460	
   461	    async def _tell(self, user_id: int, text: str) -> None:
   462	        with contextlib.suppress(errors.RPCError, ValueError, ConnectionError):
   463	            await self.bot.send(user_id, text)
   464	
   465	    async def _notify_lost_access(self, chat_id: int, reason: str) -> None:
   466	        log.warning("lost access to chat %s (%s)", chat_id, reason)
   467	        await self._broadcast(chat_id, "bot.chats.lost_access")
   468	
   469	    async def _notify_regained_access(self, chat_id: int) -> None:
   470	        await self._broadcast(chat_id, "bot.chats.regained_access")
   125	        self._user_buckets: dict[int, TokenBucket] = {}
   126	        self._flood_until = 0.0
   127	        self._stopping = False
   128	        self.metrics = Metrics()
   129	
   130	    # -- lifecycle -------------------------------------------------------------------------
   131	
   132	    async def replay_pending(self) -> int:
   133	        """Re-queue outbox rows left behind by a crash or a shutdown. Runs before catch-up."""
   134	        items = await self._storage.pending_deliveries()
   135	        for item in items:
   136	            await self.enqueue(item)
   137	        if items:
   138	            log.info("replayed %d pending notifications from the outbox", len(items))
   139	        return len(items)
   140	
   141	    async def enqueue(self, item: OutboxItem) -> bool:
   142	        """Hand an outbox row to the user's worker. Never raises, never blocks the caller."""
   143	        user = await self._storage.get_user(item.user_id)
   144	        if user is None or not user.active or user.paused:
   145	            self.metrics.dropped_inactive += 1
   146	            return False
   147	        state = self._queue_for(item.user_id)
   148	        try:
   149	            state.queue.put_nowait(item)
   150	        except asyncio.QueueFull:
   151	            state.overflow += 1
   152	            self.metrics.dropped_overflow += 1
   153	            log.warning(
   154	                "delivery queue full for user %s (%d dropped)", item.user_id, state.overflow
   155	            )
   156	            return False
   157	        state.enqueued_at[item.key] = self._clock()
   158	        return True
   159	
   160	    def queue_length(self, user_id: int) -> int:
   161	        state = self._queues.get(user_id)
   162	        return state.queue.qsize() if state else 0
   163	
   164	    def total_queued(self) -> int:
   165	        return sum(s.queue.qsize() for s in self._queues.values())
   166	
   167	    async def drop_user(self, user_id: int) -> None:
   168	        """Forget everything queued for a user (they blocked the bot or lost access)."""
   169	        state = self._queues.pop(user_id, None)
   170	        if state is None:
   171	            return
   172	        if state.worker is not None:
   173	            state.worker.cancel()
   174	            with contextlib.suppress(asyncio.CancelledError):
   175	                await state.worker
   176	        while not state.queue.empty():
   177	            state.queue.get_nowait()
   178	
   179	    async def wait_idle(self, timeout: float = 5.0) -> bool:
   180	        """Wait until every queue is empty and its worker finished the item it was holding."""
   181	
   182	        async def join_all() -> None:
   183	            while True:
   184	                pending = [s.queue.join() for s in list(self._queues.values())]
   185	                if not pending:
   186	                    return
   187	                await asyncio.gather(*pending)
   188	                if all(s.queue.empty() for s in self._queues.values()):
   189	                    return
   190	
   191	        try:
   192	            await asyncio.wait_for(join_all(), timeout)
   193	        except TimeoutError:
   194	            return False
   195	        return True
   196	
   197	    async def drain(self, timeout: float = 15.0) -> int:
   198	        """Give the workers a bounded chance to finish; returns how many items were left."""
   199	        self._stopping = True
   200	        if not await self.wait_idle(timeout):
   201	            log.warning("shutdown: %d notifications left in queues", self.total_queued())
   202	        left = self.total_queued()
   203	        for state in self._queues.values():
   204	            if state.worker is not None:
   205	                state.worker.cancel()
   206	                with contextlib.suppress(asyncio.CancelledError):
   207	                    await state.worker
   208	        return left
   209	
   210	    # -- internals -------------------------------------------------------------------------
   211	
   212	    def _queue_for(self, user_id: int) -> _UserQueue:
   213	        state = self._queues.get(user_id)
   214	        if state is None:
   215	            state = _UserQueue(queue=asyncio.Queue(maxsize=QUEUE_MAXSIZE))
   216	            self._queues[user_id] = state
   217	            self._user_buckets[user_id] = TokenBucket(
   218	                USER_RATE, monotonic=self._monotonic, sleep=self._sleep
   219	            )
   220	        if state.worker is None or state.worker.done():
   221	            if state.worker is not None and state.worker.done():
   222	                self.metrics.worker_restarts += 1
   223	            state.worker = asyncio.create_task(
   224	                self._run_worker(user_id, state), name=f"delivery-{user_id}"
   225	            )
   226	        return state
   227	
   228	    async def _run_worker(self, user_id: int, state: _UserQueue) -> None:
   229	        """The worker loop. No exception may end it — that would silently stop notifications."""
   230	        while True:
   231	            item = await state.queue.get()
   232	            try:
   233	                await self._process(user_id, state, item)
   234	            except asyncio.CancelledError:
   235	                state.queue.task_done()
   236	                raise
   237	            except Exception:
   238	                log.exception("delivery failed for %s; item marked failed", item.key)
   239	                self.metrics.failed += 1
   240	                with contextlib.suppress(Exception):
   241	                    await self._storage.set_delivery_status(
   242	                        *item.key, DeliveryStatus.FAILED, error="unhandled"
   243	                    )
   244	            finally:
   245	                state.enqueued_at.pop(item.key, None)
   246	                state.queue.task_done()
   247	
   248	    async def _process(self, user_id: int, state: _UserQueue, item: OutboxItem) -> None:
   249	        user = await self._storage.get_user(user_id)
   250	        if user is None or not user.active or user.paused:
   251	            self.metrics.dropped_inactive += 1
   252	            return
   253	        lang = user.lang or DEFAULT_LANG
   254	        tz = user.tz or "UTC"
   255	
   256	        batch = self._collect_digest(state, item, user.digest_threshold or self._default_threshold)
   257	        if batch is not None:
   258	            entries = [DigestEntry(Payload.from_json(i.payload), i.key) for i in batch]
   259	            overflow, state.overflow = state.overflow, 0
   260	            rendered = render_digest(
   261	                entries, lang=lang, tz=tz, now=self._clock(), overflow=overflow
   262	            )
   263	            sent = await self._send(user_id, rendered, buttons=None, item=batch[0])
   264	            if sent:
   265	                await self._storage.mark_digested([i.key for i in batch])
   266	                self.metrics.digested += len(batch)
   267	            return
   268	
   269	        payload = Payload.from_json(item.payload)
   270	        rendered = render_notification(payload, lang=lang, tz=tz, now=self._clock())
   271	        buttons = self._buttons(item, lang) if self._buttons else None
   272	        if await self._send(user_id, rendered, buttons=buttons, item=item):
   273	            await self._storage.set_delivery_status(*item.key, DeliveryStatus.SENT)
   274	            self.metrics.delivered += 1
   275	
   276	    def _collect_digest(
   277	        self, state: _UserQueue, item: OutboxItem, threshold: int
   278	    ) -> list[OutboxItem] | None:
   279	        """Decide whether ``item`` starts a digest and, if so, drain the queue into it."""
   280	        if threshold <= 0:
   281	            return None
   282	        backlog = state.queue.qsize() + 1  # the item we just popped counts too
   283	        oldest = min(state.enqueued_at.values(), default=None)
   284	        aged = (
   285	            oldest is not None
   286	            and backlog >= DIGEST_MIN_QUEUE
   287	            and self._clock() - oldest >= DIGEST_AGE
   288	        )
   289	        if backlog < threshold and not aged:
   290	            return None
   291	        batch = [item]
   292	        while len(batch) < DIGEST_MAX_ITEMS and not state.queue.empty():
   293	            extra = state.queue.get_nowait()
   294	            state.enqueued_at.pop(extra.key, None)
   295	            state.queue.task_done()
   296	            batch.append(extra)
   297	        return batch
   298	
   299	    # -- sending ---------------------------------------------------------------------------
   300	
   301	    async def _send(
   302	        self, user_id: int, rendered: Rendered, *, buttons: Any, item: OutboxItem
   303	    ) -> bool:
   304	        attempts = item.attempts
   305	        degraded = False
   306	        while True:
   307	            await self._wait_flood_gate()
   308	            await self._global_bucket.acquire()
   309	            bucket = self._user_buckets.get(user_id)
   310	            if bucket is not None:
   311	                await bucket.acquire()
   312	            attempts += 1
   313	            try:
   314	                await self._bot.send_message(
   315	                    user_id,
   316	                    rendered.text,
   317	                    formatting_entities=None if degraded else rendered.entities,
   318	                    buttons=None if degraded else buttons,
   319	                    link_preview=False,
   320	                )
   321	            except asyncio.CancelledError:
   322	                raise
   323	            except errors.FloodWaitError as exc:
   324	                self._open_flood_gate(exc.seconds, user_id)
   325	                attempts -= 1  # a flood wait is not the user's fault; do not burn an attempt
   326	                continue
   327	            except PAUSE_ERRORS as exc:
   328	                log.info("pausing delivery for %s: %s", user_id, type(exc).__name__)
   329	                await self._storage.pause_delivery(user_id, type(exc).__name__)
   330	                await self._storage.set_delivery_status(
   331	                    *item.key, DeliveryStatus.FAILED, error=type(exc).__name__, attempts=attempts
   332	                )
   333	                await self.drop_user(user_id)
   334	                self.metrics.failed += 1
   335	                return False
   336	            except errors.BadRequestError as exc:
   337	                if not degraded and _is_rendering_error(exc):
   338	                    degraded = True
   339	                    self.metrics.degraded_sends += 1
   340	                    log.warning("retrying plain-text after %s for %s", type(exc).__name__, item.key)
   341	                    continue
   342	                await self._fail(item, exc, attempts)
   343	                return False
   344	            except (*RETRYABLE, ValueError) as exc:
   345	                if isinstance(exc, ValueError) or attempts >= MAX_ATTEMPTS:
   346	                    await self._fail(item, exc, attempts)
   347	                    return False
   348	                delay = min(BACKOFF_CAP, BACKOFF_BASE * 2 ** (attempts - 1))
   349	                log.warning(
   350	                    "send to %s failed (%s); retry %d in %.0fs",
   351	                    user_id,
   352	                    type(exc).__name__,
   353	                    attempts,
   354	                    delay,
   355	                )
   356	                await self._sleep(delay)
   357	                continue
   358	            return True
   359	
   360	    async def _fail(self, item: OutboxItem, exc: BaseException, attempts: int) -> None:
   361	        log.error("giving up on %s: %s", item.key, type(exc).__name__)
   362	        self.metrics.failed += 1
   363	        await self._storage.set_delivery_status(
   364	            *item.key, DeliveryStatus.FAILED, error=type(exc).__name__, attempts=attempts
   365	        )
   366	
   367	    def _open_flood_gate(self, seconds: float, user_id: int) -> None:
   368	        until = self._monotonic() + seconds + 1
   369	        if until > self._flood_until:
   370	            self._flood_until = until
   371	            self.metrics.flood_waits += 1
   372	            self.metrics.flood_seconds += seconds
   373	            log.warning(
   374	                "flood wait %.0fs (triggered by user %s); all workers paused", seconds, user_id
   375	            )
   376	
   377	    async def _wait_flood_gate(self) -> None:
   378	        while True:
   379	            remaining = self._flood_until - self._monotonic()
   380	            if remaining <= 0:
   381	                return
   382	            await self._sleep(remaining)
   383	
   384	
   385	def _is_rendering_error(exc: errors.BadRequestError) -> bool:

**Verifying matcher invalidation and disable behavior**
**Identifying matcher caching with paused users issue**
**Planning matcher invalidation on delivery pause and resume**
**Fixing digest threshold handling bug**
**Analyzing notification delivery and digest threshold issues**
**Identifying digest render bug and cutoff handling**
**Assessing SQLite locking and message ordering**
**Listing high-confidence shutdown issues**
**Extending shutdown issues list with migration concerns**
**Verifying TelegramClient.disconnect behavior**
exec
/bin/zsh -lc "uv run python - <<'PY'
import inspect
from telethon import TelegramClient
print(TelegramClient.disconnect, inspect.iscoroutinefunction(TelegramClient.disconnect))
print(inspect.getsource(TelegramClient.disconnect))
PY" in /Users/L/Code/ddiver
 succeeded in 68ms:
<function TelegramBaseClient.disconnect at 0x10b7d3f60> False
    def disconnect(self: 'TelegramClient'):
        """
        Disconnects from Telegram.

        If the event loop is already running, this method returns a
        coroutine that you should await on your own code; otherwise
        the loop is ran until said coroutine completes.

        Event handlers which are currently running will be cancelled before
        this function returns (in order to properly clean-up their tasks).
        In particular, this means that using ``disconnect`` in a handler
        will cause code after the ``disconnect`` to never run. If this is
        needed, consider spawning a separate task to do the remaining work.

        Example
            .. code-block:: python

                # You don't need to use this if you used "with client"
                await client.disconnect()
        """
        if self.loop.is_running():
            # Disconnect may be called from an event handler, which would
            # cancel itself during itself and never actually complete the
            # disconnection. Shield the task to prevent disconnect itself
            # from being cancelled. See issue #3942 for more details.
            return asyncio.shield(self.loop.create_task(self._disconnect_coro()))
        else:
            try:
                self.loop.run_until_complete(self._disconnect_coro())
            except RuntimeError:
                # Python 3.5.x complains when called from
                # `__aexit__` and there were pending updates with:
                #   "Event loop stopped before Future completed."
                #
                # However, it doesn't really make a lot of sense.
                pass


**Analyzing immediate disconnect scheduling**
**Planning test for catchup task race conditions**
exec
/bin/zsh -lc "uv run python - <<'PY'
import asyncio, tempfile
from pathlib import Path
from telethon import errors
from ddiver.storage import Storage, Role
from ddiver.delivery import Delivery
from tests.fakes import FakeBotClient, FakeClock, T0
from tests.test_delivery import BASE_PAYLOAD, payload

async def main():
 p=Path(tempfile.mkdtemp())/'d.sqlite'
 s=Storage(p,owner_id=1,clock=FakeClock()); await s.open(); await s.ensure_user(2); await s.update_user(2,role=Role.ALLOWED,lang='en',tz='UTC')
 b=FakeBotClient(); d=Delivery(b,s,clock=FakeClock())
 item=await s.claim_delivery(2,BASE_PAYLOAD['chat_id'],1,7,payload(1)); b.fail_next(errors.UserIsBlockedError(request=None))
 await d.enqueue(item)
 await asyncio.sleep(.1)
 tasks=[(t.get_name(),t.done(),repr(t.exception()) if t.done() and not t.cancelled() else '') for t in asyncio.all_tasks() if t is not asyncio.current_task()]
 print('queues',d._queues)
 print('tasks',tasks)
 print('paused',(await s.get_user(2)).paused)
 await s.close()

asyncio.run(main())
PY" in /Users/L/Code/ddiver
 succeeded in 287ms:
queues {}
tasks [('delivery-2', False, '')]
paused True

**Investigating orphan worker behavior**
exec
/bin/zsh -lc "uv run python - <<'PY'
import asyncio,tempfile
from pathlib import Path
from ddiver.storage import Storage,Role
from ddiver.delivery import Delivery
from tests.fakes import FakeBotClient,FakeClock
from tests.test_delivery import BASE_PAYLOAD,payload
async def main():
 s=Storage(Path(tempfile.mkdtemp())/'d',owner_id=1,clock=FakeClock()); await s.open(); await s.ensure_user(2); await s.update_user(2,role=Role.ALLOWED,lang='en',tz='UTC')
 b=FakeBotClient(); b.hold(); d=Delivery(b,s,clock=FakeClock())
 i=await s.claim_delivery(2,BASE_PAYLOAD['chat_id'],1,7,payload(1)); await d.enqueue(i); await asyncio.sleep(0)
 try: print('drain',await d.drain(timeout=.01))
 except BaseException as e: print('error',type(e).__name__,str(e))
 print('pending',[(x.key,x.status.value) for x in await s.pending_deliveries()]); await s.close()
asyncio.run(main())
PY" in /Users/L/Code/ddiver
 succeeded in 121ms:
shutdown: 0 notifications left in queues
error ValueError task_done() called too many times
pending [((2, -1001234567890, 1), 'pending')]

**Configuring digest failure and error handling**
exec
/bin/zsh -lc "uv run python - <<'PY'
import asyncio,tempfile
from pathlib import Path
from telethon import errors
from ddiver.storage import Storage,Role
from ddiver.delivery import Delivery
from tests.fakes import FakeBotClient,FakeClock
from tests.test_delivery import BASE_PAYLOAD,payload
async def main():
 s=Storage(Path(tempfile.mkdtemp())/'d',owner_id=1,clock=FakeClock()); await s.open(); await s.ensure_user(2); await s.update_user(2,role=Role.ALLOWED,lang='en',tz='UTC',digest_threshold=3)
 b=FakeBotClient(); b.hold(); b.fail_next(None, errors.MessageTooLongError(request=None)); d=Delivery(b,s,clock=FakeClock())
 items=[]
 for n in range(1,6):
  i=await s.claim_delivery(2,BASE_PAYLOAD['chat_id'],n,7,payload(n)); items.append(i); await d.enqueue(i)
 b.release(); await d.wait_idle()
 print([(n,(await s.get_delivery(2,BASE_PAYLOAD['chat_id'],n)).status.value) for n in range(1,6)])
 print('sent',len(b.sent),'queue',d.total_queued())
 await d.drain(); await s.close()
asyncio.run(main())
PY" in /Users/L/Code/ddiver
 succeeded in 1155ms:
giving up on (2, -1001234567890, 2): MessageTooLongError
[(1, 'sent'), (2, 'failed'), (3, 'pending'), (4, 'pending'), (5, 'pending')]
sent 1 queue 0

exec
/bin/zsh -lc "uv run python - <<'PY'
import asyncio,tempfile
from pathlib import Path
from ddiver.storage import Storage,Role
from ddiver.delivery import Delivery
from tests.fakes import FakeBotClient,FakeClock
from tests.test_delivery import BASE_PAYLOAD,payload
async def main():
 s=Storage(Path(tempfile.mkdtemp())/'d',owner_id=1,clock=FakeClock()); await s.open(); await s.ensure_user(2); await s.update_user(2,role=Role.ALLOWED,lang='en',tz='UTC',digest_threshold=0)
 b=FakeBotClient(); b.hold(); d=Delivery(b,s,clock=FakeClock(),default_threshold=3)
 for n in range(1,6):
  i=await s.claim_delivery(2,BASE_PAYLOAD['chat_id'],n,7,payload(n)); await d.enqueue(i)
 b.release(); await d.wait_idle(); print('sends',len(b.sent),[m.text.splitlines()[0] for m in b.sent]); await d.drain(); await s.close()
asyncio.run(main())
PY" in /Users/L/Code/ddiver
 succeeded in 1213ms:
sends 2 ['📡 Chat Чат', '📦 4 matches while you were busy']

**Planning rendering limits and encoding checks**
exec
/bin/zsh -lc "uv run python - <<'PY'
from ddiver.formatter import Payload,DigestEntry,render_digest
from tests.fakes import T0
entries=[]
for c in range(12):
 for i in range(5):
  p=Payload(-1000-c,'T'*128,None,'supergroup',i,None,T0,'x'*100,[(0,1)],'r'*300,'include',False,42,'S'*128,None)
  entries.append(DigestEntry(p,(1,-1000-c,i)))
r=render_digest(entries,lang='en',tz='UTC',now=T0)
print(len(r.text),len(r.text.encode('utf-16-le'))//2,len(r.entities),r.text.count('📡'))
PY" in /Users/L/Code/ddiver
 succeeded in 99ms:
5337 5340 15 2

**Analyzing message batching and concurrency issues**
**Designing atomic subscription updates with ownership checks**
**Assessing potential subscription privacy leak**
**Identifying moderator access vulnerability**
**Exposing chat metadata via forged requests**
**Testing user ID assertion logic**
**Reviewing test coverage gaps**
exec
/bin/zsh -lc "nl -ba ddiver/app.py | sed -n '55,135p'; nl -ba ddiver/listener.py | sed -n '105,245p'; nl -ba ddiver/catchup.py | sed -n '65,190p'; nl -ba ddiver/matcher.py | sed -n '75,180p'; nl -ba ddiver/storage.py | sed -n '430,535p'; nl -ba ddiver/bot/handlers.py | sed -n '205,240p'" in /Users/L/Code/ddiver
 succeeded in 0ms:
    55	    Degraded mode matters because the container policy is ``restart: unless-stopped``, which
    56	    ignores exit codes: exiting on a dead session would produce an infinite restart loop that
    57	    re-notifies the owner and re-runs catch-up every few seconds.
    58	    """
    59	
    60	    def __init__(
    61	        self,
    62	        *,
    63	        run_listener: Callable[[], Awaitable[None]],
    64	        notify_owner: Callable[[str], Awaitable[None]] | None = None,
    65	        clock: Clock = utcnow,
    66	        sleep: Sleeper = asyncio.sleep,
    67	        rng: random.Random | None = None,
    68	        max_attempts: int | None = None,
    69	    ) -> None:
    70	        self._run_listener = run_listener
    71	        self._notify_owner = notify_owner
    72	        self._clock = clock
    73	        self._sleep = sleep
    74	        self._rng = rng or random.Random(0)  # jitter for reconnects, not cryptography
    75	        self._max_attempts = max_attempts
    76	        self.state = SupervisorState()
    77	        self._stop = asyncio.Event()
    78	
    79	    def request_stop(self) -> None:
    80	        self._stop.set()
    81	
    82	    @property
    83	    def stopped(self) -> bool:
    84	        return self._stop.is_set()
    85	
    86	    async def run(self) -> int:
    87	        self.state.started_at = self._clock()
    88	        while not self._stop.is_set():
    89	            self.state.attempts += 1
    90	            try:
    91	                await self._run_listener()
    92	            except asyncio.CancelledError:
    93	                raise
    94	            except Exception as exc:
    95	                if is_fatal_auth_error(exc):
    96	                    await self._enter_degraded(type(exc).__name__)
    97	                    return EXIT_OK
    98	                if is_session_file_error(exc):
    99	                    await self._enter_degraded(f"session file: {exc}")
   100	                    return EXIT_OK
   101	                log.warning("listener stopped with %s", type(exc).__name__, exc_info=True)
   102	            else:
   103	                if self._stop.is_set():
   104	                    break
   105	                log.info("listener disconnected; reconnecting")
   106	            if self._max_attempts is not None and self.state.attempts >= self._max_attempts:
   107	                return EXIT_ERROR
   108	            if self._stop.is_set():
   109	                break
   110	            await self._backoff()
   111	        return EXIT_OK
   112	
   113	    async def _backoff(self) -> None:
   114	        delay = min(BACKOFF_CAP, BACKOFF_BASE * 2 ** (self.state.attempts - 1))
   115	        delay += self._rng.uniform(0, delay / 4)
   116	        self.state.backoffs.append(delay)
   117	        log.info("reconnecting in %.0fs", delay)
   118	        await self._sleep(delay)
   119	
   120	    async def _enter_degraded(self, reason: str) -> None:
   121	        self.state.degraded = True
   122	        self.state.degraded_reason = reason
   123	        log.error("listener needs a new login (%s); entering degraded mode", reason)
   124	        await self.notify_owner_if_due(force=True)
   125	
   126	    async def notify_owner_if_due(self, *, force: bool = False) -> bool:
   127	        """Tell the owner about degraded mode, at most once every six hours."""
   128	        if not self.state.degraded or self._notify_owner is None:
   129	            return False
   130	        now = self._clock()
   131	        last = self.state.owner_notified_at
   132	        if not force and last is not None and now - last < OWNER_REMINDER:
   133	            return False
   134	        self.state.owner_notified_at = now
   135	        with contextlib.suppress(Exception):
   105	    return bool(message.message)
   106	
   107	
   108	class Listener:
   109	    def __init__(
   110	        self,
   111	        storage: Storage,
   112	        matcher: Matcher,
   113	        senders: SenderCache,
   114	        *,
   115	        deliver: DeliverHook,
   116	        clock: Clock = utcnow,
   117	        ignore_own: bool = True,
   118	    ) -> None:
   119	        self._storage = storage
   120	        self._matcher = matcher
   121	        self._senders = senders
   122	        self._deliver = deliver
   123	        self._clock = clock
   124	        self._ignore_own = ignore_own
   125	        self.cursors = CursorTracker(storage)
   126	        self.metrics = ListenerMetrics()
   127	        self.last_update_at: dt.datetime | None = None
   128	        self._chat_cache: dict[int, ChatRef] = {}
   129	
   130	    async def on_message(self, event: Any) -> None:
   131	        """Entry point for ``events.NewMessage``. Never raises: one bad message must not
   132	        stop the listener."""
   133	        try:
   134	            await self.handle(event.message, chat=getattr(event, "chat", None))
   135	        except Exception:
   136	            log.exception("listener failed on a message")
   137	
   138	    async def handle(
   139	        self,
   140	        message: tl.Message,
   141	        *,
   142	        chat: Any = None,
   143	        catchup: bool = False,
   144	        catchup_skipped: int = 0,
   145	    ) -> list[OutboxItem]:
   146	        self.metrics.seen += 1
   147	        self.last_update_at = self._clock()
   148	        if not should_consider(message, ignore_own=self._ignore_own):
   149	            if getattr(message, "out", False):
   150	                self.metrics.skipped_own += 1
   151	            return []
   152	
   153	        chat_id = _chat_id_of(message)
   154	        if chat_id is None or chat_id >= 0:  # private chats are never monitored
   155	            self.metrics.skipped_private += 1
   156	            return []
   157	        if not self._matcher.is_monitored(chat_id):
   158	            return []
   159	
   160	        await self._refresh_chat(chat_id, chat if chat is not None else message.chat)
   161	
   162	        moment = message.date or self._clock()
   163	        self.metrics.considered += 1
   164	        prepared = prepare_text(message.message)
   165	        matches = await self._matcher.evaluate(chat_id, prepared.shadow, _sender_id_of(message))
   166	        if not matches:
   167	            self.cursors.note(chat_id, message.id, moment)
   168	            return []
   169	
   170	        sender = await sender_of(message, self._senders)
   171	        if sender is not None and sender.is_bot:
   172	            self.metrics.skipped_bot += 1
   173	            self.cursors.note(chat_id, message.id, moment)
   174	            return []
   175	
   176	        self.metrics.matched += 1
   177	        claimed: list[OutboxItem] = []
   178	        for match in matches:
   179	            item = await self._claim(
   180	                message,
   181	                chat_id,
   182	                match,
   183	                prepared,
   184	                sender,
   185	                catchup=catchup,
   186	                catchup_skipped=catchup_skipped,
   187	            )
   188	            if item is not None:
   189	                claimed.append(item)
   190	        # The cursor may only advance once every match for this message is durable.
   191	        self.cursors.note(chat_id, message.id, moment)
   192	        for item in claimed:
   193	            await self._deliver(item)
   194	        return claimed
   195	
   196	    async def _claim(
   197	        self,
   198	        message: tl.Message,
   199	        chat_id: int,
   200	        match: Match,
   201	        prepared: Any,
   202	        sender: SenderInfo | None,
   203	        *,
   204	        catchup: bool,
   205	        catchup_skipped: int,
   206	    ) -> OutboxItem | None:
   207	        chat = self._chat_cache.get(chat_id) or ChatRef(chat_id, str(chat_id), None, "supergroup")
   208	        spans = [prepared.to_original(s) for s in match.spans]
   209	        payload = Payload(
   210	            chat_id=chat_id,
   211	            chat_title=chat.title,
   212	            chat_username=chat.username,
   213	            chat_kind=chat.kind,
   214	            msg_id=message.id,
   215	            topic_id=topic_id_of(message),
   216	            date=message.date or self._clock(),
   217	            text=message.message or "",
   218	            spans=[(s.start, s.end) for s in spans],
   219	            rule_pattern=match.rule.pattern,
   220	            rule_kind=match.rule.kind.value,
   221	            rule_is_global=match.rule.is_global,
   222	            sender_id=sender.id if sender else None,
   223	            sender_name=sender.display_name if sender else "",
   224	            sender_username=sender.username if sender else None,
   225	            sender_is_channel=bool(sender and sender.is_channel),
   226	            sender_is_deleted=bool(sender and sender.is_deleted),
   227	            is_forwarded=message.fwd_from is not None,
   228	            catchup=catchup,
   229	            catchup_skipped=catchup_skipped,
   230	        )
   231	        item = await self._storage.claim_delivery(
   232	            match.user_id, chat_id, message.id, match.rule.id, payload.to_json()
   233	        )
   234	        if item is None:
   235	            self.metrics.duplicates += 1
   236	            return None
   237	        self.metrics.claimed += 1
   238	        self._matcher.note_hit(match.rule.id)
   239	        return item
   240	
   241	    async def _refresh_chat(self, chat_id: int, entity: Any) -> None:
   242	        ref = chat_ref_of(entity)
   243	        if ref is None:
   244	            return
   245	        cached = self._chat_cache.get(chat_id)
    65	        self._storage = storage
    66	        self._listener = listener
    67	        self._history = history
    68	        self._clock = clock
    69	        self._sleep = sleep
    70	        self._hours = hours
    71	        self._per_chat = per_chat
    72	        self._total = total
    73	        self._on_unavailable = on_unavailable
    74	
    75	    async def run(self, chat_ids: set[int] | None = None) -> CatchupResult:
    76	        result = CatchupResult()
    77	        if self._hours <= 0 or self._per_chat <= 0:
    78	            return result
    79	        cursors = await self._storage.get_cursors()
    80	        monitored = chat_ids if chat_ids is not None else await self._storage.monitored_chat_ids()
    81	        cutoff = self._clock() - dt.timedelta(hours=self._hours)
    82	
    83	        for chat_id in sorted(monitored):
    84	            cursor = cursors.get(chat_id)
    85	            if cursor is None:
    86	                # A chat we have never seen starts "now": no backfill on first subscribe.
    87	                continue
    88	            chat = await self._storage.get_chat(chat_id)
    89	            if chat is not None and not chat.available:
    90	                result.skipped += 1
    91	                continue
    92	            if result.messages >= self._total:
    93	                log.warning(
    94	                    "catch-up budget reached; %d chats not checked", len(monitored) - result.chats
    95	                )
    96	                break
    97	            await self._catch_up_chat(chat_id, cursor.last_msg_id, cutoff, result)
    98	            await self._sleep(INTER_CHAT_PAUSE)
    99	        return result
   100	
   101	    async def _catch_up_chat(
   102	        self, chat_id: int, min_id: int, cutoff: dt.datetime, result: CatchupResult
   103	    ) -> None:
   104	        self._listener.cursors.block(chat_id)
   105	        try:
   106	            buffered, capped, newest = await self._fetch(chat_id, min_id, cutoff, result)
   107	        except errors.FloodWaitError as exc:
   108	            result.flood_waits += 1
   109	            log.warning(
   110	                "catch-up flood wait %ss on chat %s; skipping it this round", exc.seconds, chat_id
   111	            )
   112	            self._listener.cursors.unblock(chat_id)
   113	            return
   114	        except (errors.ChannelPrivateError, errors.ChatForbiddenError, ValueError) as exc:
   115	            reason = type(exc).__name__
   116	            log.warning("catch-up cannot read chat %s (%s)", chat_id, reason)
   117	            result.unavailable.append(chat_id)
   118	            if await self._storage.set_chat_available(chat_id, False) and self._on_unavailable:
   119	                await self._on_unavailable(chat_id, reason)
   120	            self._listener.cursors.unblock(chat_id)
   121	            return
   122	        except Exception:
   123	            log.exception("catch-up failed for chat %s", chat_id)
   124	            self._listener.cursors.unblock(chat_id)
   125	            return
   126	
   127	        result.chats += 1
   128	        skipped = self._per_chat if capped else 0
   129	        if capped:
   130	            result.capped_chats.append(chat_id)
   131	        for index, message in enumerate(reversed(buffered)):
   132	            claimed = await self._listener.handle(
   133	                message,
   134	                chat=message.chat,
   135	                catchup=True,
   136	                catchup_skipped=skipped if index == 0 else 0,
   137	            )
   138	            result.claimed += len(claimed)
   139	            if index % YIELD_EVERY == 0:
   140	                await asyncio.sleep(0)
   141	        if newest is not None:
   142	            # Advance even when everything was filtered out, so the next round starts here.
   143	            self._listener.cursors.note(chat_id, newest[0], newest[1])
   144	        self._listener.cursors.unblock(chat_id)
   145	        await self._listener.cursors.flush()
   146	
   147	    async def _fetch(
   148	        self, chat_id: int, min_id: int, cutoff: dt.datetime, result: CatchupResult
   149	    ) -> tuple[list[tl.Message], bool, tuple[int, dt.datetime] | None]:
   150	        buffered: list[tl.Message] = []
   151	        newest: tuple[int, dt.datetime] | None = None
   152	        fetched = 0
   153	        async for message in self._history(chat_id, min_id, self._per_chat):
   154	            fetched += 1
   155	            result.messages += 1
   156	            date = getattr(message, "date", None) or self._clock()
   157	            if newest is None:
   158	                newest = (message.id, date)
   159	            if date < cutoff:
   160	                return buffered, False, newest
   161	            if should_consider(message, ignore_own=True):
   162	                buffered.append(message)
   163	            if fetched % YIELD_EVERY == 0:
   164	                await asyncio.sleep(0)
   165	        return buffered, fetched >= self._per_chat, newest
   166	
   167	
   168	def telethon_history(client: Any) -> History:
   169	    """Adapt a Telethon client to the ``History`` callable used by :class:`Catchup`."""
   170	
   171	    def history(chat_id: int, min_id: int, limit: int) -> AsyncIterator[tl.Message]:
   172	        messages: AsyncIterator[tl.Message] = client.iter_messages(
   173	            chat_id, min_id=min_id, limit=limit
   174	        )
   175	        return messages
   176	
   177	    return history
    75	
    76	    async def flush_hits(self) -> None:
    77	        if not self._hits:
    78	            return
    79	        hits, self._hits = dict(self._hits), Counter()
    80	        await self._storage.flush_rule_hits(hits)
    81	
    82	    # -- monitored set ---------------------------------------------------------------------
    83	
    84	    @property
    85	    def monitored(self) -> frozenset[int]:
    86	        return frozenset(self._monitored)
    87	
    88	    def is_monitored(self, chat_id: int) -> bool:
    89	        return chat_id in self._monitored
    90	
    91	    async def reload(self) -> None:
    92	        """Reload the monitored set from storage and drop every cached rule set."""
    93	        self._monitored = await self._storage.monitored_chat_ids()
    94	        self._cache.clear()
    95	
    96	    async def invalidate(self, chat_id: int | None = None) -> None:
    97	        """Drop cached rules for one chat (or all) and refresh the monitored set."""
    98	        if chat_id is None:
    99	            self._cache.clear()
   100	        else:
   101	            self._cache.pop(chat_id, None)
   102	        self._monitored = await self._storage.monitored_chat_ids()
   103	
   104	    # -- evaluation ------------------------------------------------------------------------
   105	
   106	    async def evaluate(self, chat_id: int, text: str, sender_id: int | None) -> list[Match]:
   107	        """Return at most one :class:`Match` per subscribed user for ``text`` in ``chat_id``.
   108	
   109	        Per user: ignored sender → no match; any exclusion hit → no match; otherwise the first
   110	        include rule (chat-specific before global) that matches. A rule that times out counts
   111	        as "no match" and is disabled after :data:`TIMEOUTS_BEFORE_DISABLE` timeouts.
   112	        """
   113	        if not text or chat_id not in self._monitored:
   114	            return []
   115	        self.stats_evaluated += 1
   116	        ruleset = await self._get(chat_id)
   117	        matches: list[Match] = []
   118	        for user in ruleset.users:
   119	            if sender_id is not None and sender_id in user.ignored:
   120	                continue
   121	            if await self._any_excluded(user, text):
   122	                continue
   123	            for rule, compiled in user.includes:
   124	                spans = await self._search_all(rule, compiled, text)
   125	                if spans:
   126	                    matches.append(Match(user.user_id, rule, spans))
   127	                    break
   128	        if matches:
   129	            self.stats_matched += 1
   130	        return matches
   131	
   132	    async def _any_excluded(self, user: _UserRules, text: str) -> bool:
   133	        for rule, compiled in user.excludes:
   134	            if await self._search_all(rule, compiled, text):
   135	                return True
   136	        return False
   137	
   138	    async def _search_all(self, rule: Rule, compiled: CompiledRule, text: str) -> list[Span]:
   139	        try:
   140	            return compiled.search_all(text, self._timeout)
   141	        except RuleTimeout:
   142	            self.stats_timeouts += 1
   143	            count = self._timeouts.get(rule.id, 0) + 1
   144	            self._timeouts[rule.id] = count
   145	            log.warning("rule %s timed out (%d/%d)", rule.id, count, TIMEOUTS_BEFORE_DISABLE)
   146	            if count >= TIMEOUTS_BEFORE_DISABLE:
   147	                await self._disable(rule, DISABLED_TIMEOUT)
   148	            return []
   149	
   150	    async def _disable(self, rule: Rule, reason: str) -> None:
   151	        await self._storage.disable_rule(rule.id, reason)
   152	        self._timeouts.pop(rule.id, None)
   153	        self._cache.clear()  # the rule may be global, so every chat could be affected
   154	        log.warning("rule %s (%r) disabled: %s", rule.id, rule.pattern, reason)
   155	        if self._on_rule_disabled is not None:
   156	            await self._on_rule_disabled(rule, reason)
   157	
   158	    # -- loading ---------------------------------------------------------------------------
   159	
   160	    async def _get(self, chat_id: int) -> _ChatRuleSet:
   161	        cached = self._cache.get(chat_id)
   162	        if cached is not None:
   163	            return cached
   164	        ruleset = await self._load(chat_id)
   165	        self._cache[chat_id] = ruleset
   166	        return ruleset
   167	
   168	    async def _load(self, chat_id: int) -> _ChatRuleSet:
   169	        rules = await self._storage.rules_for_chat(chat_id)
   170	        ignored = await self._storage.ignored_for_chat(chat_id)
   171	        by_user: dict[int, _UserRules] = {}
   172	        for rule in rules:  # ordered by user, chat-specific before global
   173	            bucket = by_user.setdefault(rule.user_id, _UserRules(rule.user_id))
   174	            try:
   175	                compiled = compile_rule(rule.pattern, timeout=self._timeout)
   176	            except RuleError as exc:
   177	                await self._disable(rule, f"{DISABLED_INVALID}:{exc.key}")
   178	                continue
   179	            target = bucket.includes if rule.kind is RuleKind.INCLUDE else bucket.excludes
   180	            target.append((rule, compiled))
   430	                ref.kind,
   431	                int(ref.is_forum),
   432	                access_hash,
   433	                self._now(),
   434	            ),
   435	        )
   436	
   437	    async def get_chat(self, chat_id: int) -> ChatRow | None:
   438	        cur = await self.db.execute("SELECT * FROM chats WHERE id=?", (chat_id,))
   439	        row = await cur.fetchone()
   440	        return self._chat(row) if row else None
   441	
   442	    async def list_chats(self, *, shared_only: bool = False) -> list[ChatRow]:
   443	        sql = "SELECT * FROM chats"
   444	        if shared_only:
   445	            sql += " WHERE shared=1"
   446	        sql += " ORDER BY title COLLATE NOCASE"
   447	        cur = await self.db.execute(sql)
   448	        return [self._chat(r) for r in await cur.fetchall()]
   449	
   450	    async def set_chat_available(self, chat_id: int, available: bool) -> bool:
   451	        """``True`` when the flag actually changed, so callers notify only on transitions."""
   452	        cur = await self.db.execute(
   453	            "UPDATE chats SET available=?, updated_at=? WHERE id=? AND available<>?",
   454	            (int(available), self._now(), chat_id, int(available)),
   455	        )
   456	        return cur.rowcount == 1
   457	
   458	    async def set_chat_shared(self, chat_id: int, shared: bool) -> bool:
   459	        cur = await self.db.execute(
   460	            "UPDATE chats SET shared=?, shared_at=?, updated_at=? WHERE id=? AND shared<>?",
   461	            (int(shared), self._now() if shared else None, self._now(), chat_id, int(shared)),
   462	        )
   463	        return cur.rowcount == 1
   464	
   465	    async def migrate_chat(self, old_id: int, new_ref: ChatRef, access_hash: int | None) -> None:
   466	        """Move subscriptions, rules and ignores from a basic group to its supergroup."""
   467	        await self.upsert_chat(new_ref, access_hash=access_hash)
   468	        now = self._now()
   469	        await self.db.execute(
   470	            """INSERT OR IGNORE INTO subscriptions (user_id, chat_id, created_at)
   471	               SELECT user_id, ?, ? FROM subscriptions WHERE chat_id=?""",
   472	            (new_ref.id, now, old_id),
   473	        )
   474	        await self.db.execute("DELETE FROM subscriptions WHERE chat_id=?", (old_id,))
   475	        await self.db.execute("UPDATE rules SET chat_id=? WHERE chat_id=?", (new_ref.id, old_id))
   476	        await self.db.execute(
   477	            "UPDATE ignored_senders SET chat_id=? WHERE chat_id=?", (new_ref.id, old_id)
   478	        )
   479	        await self.db.execute("DELETE FROM chat_cursor WHERE chat_id=?", (old_id,))
   480	        await self.db.execute(
   481	            "UPDATE chats SET available=0, updated_at=? WHERE id=?", (now, old_id)
   482	        )
   483	
   484	    # -- subscriptions ---------------------------------------------------------------------
   485	
   486	    async def subscribe(self, user_id: int, chat_id: int) -> bool:
   487	        """Subscribe, but only to a known chat of a group-like kind (callback data is untrusted)."""
   488	        chat = await self.get_chat(chat_id)
   489	        if chat is None or chat.ref.kind not in CHAT_KINDS:
   490	            return False
   491	        cur = await self.db.execute(
   492	            "INSERT OR IGNORE INTO subscriptions (user_id, chat_id, created_at) VALUES (?,?,?)",
   493	            (user_id, chat_id, self._now()),
   494	        )
   495	        return cur.rowcount == 1
   496	
   497	    async def unsubscribe(self, user_id: int, chat_id: int) -> bool:
   498	        cur = await self.db.execute(
   499	            "DELETE FROM subscriptions WHERE user_id=? AND chat_id=?", (user_id, chat_id)
   500	        )
   501	        return cur.rowcount == 1
   502	
   503	    async def unsubscribe_all(self, chat_id: int, *, except_user: int | None = None) -> list[int]:
   504	        """Drop every subscription for a chat (used when the owner unshares it)."""
   505	        sql = "SELECT user_id FROM subscriptions WHERE chat_id=?"
   506	        params: list[Any] = [chat_id]
   507	        if except_user is not None:
   508	            sql += " AND user_id<>?"
   509	            params.append(except_user)
   510	        cur = await self.db.execute(sql, params)
   511	        affected = [int(r["user_id"]) for r in await cur.fetchall()]
   512	        if affected:
   513	            marks = ",".join("?" * len(affected))
   514	            await self.db.execute(
   515	                f"DELETE FROM subscriptions WHERE chat_id=? AND user_id IN ({marks})",  # noqa: S608
   516	                (chat_id, *affected),
   517	            )
   518	        return affected
   519	
   520	    async def is_subscribed(self, user_id: int, chat_id: int) -> bool:
   521	        cur = await self.db.execute(
   522	            "SELECT 1 FROM subscriptions WHERE user_id=? AND chat_id=?", (user_id, chat_id)
   523	        )
   524	        return await cur.fetchone() is not None
   525	
   526	    async def list_subscriptions(self, user_id: int) -> list[ChatRow]:
   527	        cur = await self.db.execute(
   528	            """SELECT c.* FROM chats c JOIN subscriptions s ON s.chat_id=c.id
   529	               WHERE s.user_id=? ORDER BY c.title COLLATE NOCASE""",
   530	            (user_id,),
   531	        )
   532	        return [self._chat(r) for r in await cur.fetchall()]
   533	
   534	    async def subscribers_of(self, chat_id: int, *, active_only: bool = True) -> list[int]:
   535	        cur = await self.db.execute(
   205	            return [Reply("bot.chats.search_empty")]  # stay in search mode so they can retype
   206	        router.clear_pending(ctx.user_id)
   207	        subscribed = {c.id for c in await storage.list_subscriptions(ctx.user_id)}
   208	        return [
   209	            Reply(
   210	                "bot.chats.title",
   211	                {"n": len(subscribed)},
   212	                buttons=menus.chats_menu(rows, subscribed, 0),
   213	            )
   214	        ]
   215	
   216	    @router.callback("sub")
   217	    async def subscribe(ctx: Context) -> list[Action]:
   218	        _, raw_chat, raw_state = (ctx.incoming.data or "").split(":")
   219	        chat_id, wanted = _int(raw_chat), raw_state == "1"
   220	        chat = await storage.get_chat(chat_id)
   221	        if chat is None or (not ctx.is_owner and not chat.shared):
   222	            return [Toast("bot.err.not_found")]
   223	        if wanted:
   224	            await storage.subscribe(ctx.user_id, chat_id)
   225	        else:
   226	            await storage.unsubscribe(ctx.user_id, chat_id)
   227	        await invalidate(chat_id)
   228	        rows = await visible_chats(ctx.user)
   229	        subscribed = {c.id for c in await storage.list_subscriptions(ctx.user_id)}
   230	        key = "bot.chats.subscribed" if wanted else "bot.chats.unsubscribed"
   231	        return [
   232	            Toast(t(ctx.lang, key, chat=chat.ref.title), literal=True),
   233	            Edit(
   234	                "bot.chats.title",
   235	                {"n": len(subscribed)},
   236	                buttons=menus.chats_menu(rows, subscribed, 0),
   237	            ),
   238	        ]
   239	
   240	    # -- shared chats (owner only) ---------------------------------------------------------

**Planning line tests**
exec
/bin/zsh -lc "nl -ba tests/test_bot.py | sed -n '105,225p'; nl -ba tests/test_delivery.py | sed -n '245,335p'; nl -ba tests/test_app.py | sed -n '55,105p'; nl -ba tests/test_storage.py | sed -n '205,235p'; nl -ba ddiver/health.py; nl -ba ddiver/formatter.py | sed -n '180,255p'; nl -ba ddiver/runtime.py | sed -n '215,275p'" in /Users/L/Code/ddiver
 succeeded in 0ms:
   105	        assert await msg(router, STRANGER, "/start") == []
   106	        assert await cb(router, STRANGER, "menu") == []
   107	
   108	    async def test_group_messages_are_ignored(self, router):
   109	        assert await msg(router, OWNER, "/status", is_private=False) == []
   110	
   111	    async def test_owner_is_never_gated(self, router, store):
   112	        await store.update_user(OWNER, role=Role.DENIED)
   113	        assert kinds(await msg(router, OWNER, "/menu")) == ["Reply"]
   114	
   115	    async def test_incoming_update_clears_a_delivery_pause(self, router, store):
   116	        await store.pause_delivery(MOD, "blocked")
   117	        await msg(router, MOD, "/menu")
   118	        assert not (await store.get_user(MOD)).paused
   119	
   120	
   121	class TestOwnerOnly:
   122	    @pytest.mark.parametrize("data", ["allow:3", "deny:3", "revoke:1", "shared:0", "share:-1001:1"])
   123	    async def test_moderators_cannot_run_owner_actions(self, router, store, data):
   124	        actions = await cb(router, MOD, data)
   125	        assert kinds(actions) == ["Toast"]
   126	        assert actions[0].key_or_text == "bot.err.owner_only"
   127	        assert (await store.get_user(STRANGER)) is None or True
   128	
   129	    async def test_owner_can_allow_and_revoke(self, router, store):
   130	        await store.ensure_user(STRANGER)
   131	        actions = await cb(router, OWNER, "allow:3")
   132	        assert (await store.get_user(STRANGER)).role is Role.ALLOWED
   133	        assert any(isinstance(a, Send) and a.user_id == STRANGER for a in actions)
   134	        await cb(router, OWNER, "revoke:3")
   135	        assert (await store.get_user(STRANGER)).role is Role.DENIED
   136	
   137	    async def test_owner_cannot_revoke_themselves(self, router, store):
   138	        actions = await cb(router, OWNER, "revoke:1")
   139	        assert actions[0].key_or_text == "bot.err.owner_only"
   140	        assert (await store.get_user(OWNER)).active
   141	
   142	
   143	class TestOnboarding:
   144	    async def test_language_then_timezone_then_menu(self, router, store):
   145	        await store.update_user(MOD, lang=None, tz=None)
   146	        actions = await msg(router, MOD, "/start")
   147	        assert "lang:uk" in callbacks(actions)
   148	        await cb(router, MOD, "lang:uk")
   149	        user = await store.get_user(MOD)
   150	        assert user.lang == "uk" and user.tz is None
   151	        actions = await cb(router, MOD, "tz:Europe/Kyiv")
   152	        assert (await store.get_user(MOD)).tz == "Europe/Kyiv"
   153	        assert any("Europe/Kyiv" in text for text in texts(actions, "uk"))
   154	
   155	    async def test_typed_timezone_with_suggestions(self, router, store):
   156	        await store.update_user(MOD, tz=None)
   157	        await cb(router, MOD, "set:tz")
   158	        actions = await msg(router, MOD, "Europe")
   159	        assert texts(actions, "en")[0] == t("en", "bot.start.tz_candidates")
   160	        actions = await msg(router, MOD, "kyiv")
   161	        assert (await store.get_user(MOD)).tz == "Europe/Kyiv"
   162	
   163	    async def test_unknown_timezone_is_reported(self, router, store):
   164	        await cb(router, MOD, "set:tz")
   165	        actions = await msg(router, MOD, "Mars/Olympus")
   166	        assert texts(actions, "en") == [t("en", "bot.start.tz_unknown")]
   167	
   168	    async def test_fixed_offset_warns_about_dst(self, router, store):
   169	        await cb(router, MOD, "set:tz")
   170	        actions = await msg(router, MOD, "+3")
   171	        assert any("daylight" in text for text in texts(actions, "en"))
   172	        assert (await store.get_user(MOD)).tz == "Etc/GMT-3"
   173	
   174	
   175	class TestChatsAndSharing:
   176	    async def test_moderators_only_see_shared_chats(self, router, store):
   177	        actions = await cb(router, MOD, "chats:0")
   178	        assert texts(actions, "en") == [t("en", "bot.chats.none_shared")]
   179	        await store.set_chat_shared(CHAT_A.id, True)
   180	        actions = await cb(router, MOD, "chats:0")
   181	        assert any("Модерация" in menus.unwrap(label) for label, _ in actions[0].buttons[0])
   182	        assert all("Приватный" not in str(row) for row in actions[0].buttons)
   183	
   184	    async def test_owner_sees_every_chat(self, router):
   185	        actions = await cb(router, OWNER, "chats:0")
   186	        labels = [menus.unwrap(label) for row in actions[0].buttons for label, _ in row]
   187	        assert any("Приватный" in label for label in labels)
   188	
   189	    async def test_subscribe_toggle_is_idempotent(self, router, store, matcher):
   190	        await store.set_chat_shared(CHAT_A.id, True)
   191	        await cb(router, MOD, f"sub:{CHAT_A.id}:1")
   192	        await cb(router, MOD, f"sub:{CHAT_A.id}:1")
   193	        assert [c.id for c in await store.list_subscriptions(MOD)] == [CHAT_A.id]
   194	        assert matcher.is_monitored(CHAT_A.id)
   195	        await cb(router, MOD, f"sub:{CHAT_A.id}:0")
   196	        assert await store.list_subscriptions(MOD) == []
   197	        assert not matcher.is_monitored(CHAT_A.id)
   198	
   199	    async def test_moderator_cannot_subscribe_to_an_unshared_chat(self, router, store):
   200	        actions = await cb(router, MOD, f"sub:{CHAT_B.id}:1")
   201	        assert actions[0].key_or_text == "bot.err.not_found"
   202	        assert await store.list_subscriptions(MOD) == []
   203	
   204	    async def test_forged_private_chat_id_is_rejected(self, router, store):
   205	        actions = await cb(router, MOD, "sub:777000:1")
   206	        assert actions[0].key_or_text == "bot.err.not_found"
   207	        assert await store.list_subscriptions(MOD) == []
   208	
   209	    async def test_unsharing_drops_moderator_subscriptions_and_notifies(self, router, store):
   210	        await store.set_chat_shared(CHAT_A.id, True)
   211	        await cb(router, MOD, f"sub:{CHAT_A.id}:1")
   212	        await cb(router, OWNER, f"share:{CHAT_A.id}:0")
   213	        assert await store.list_subscriptions(MOD) == []
   214	        notices = [a for a in await cb(router, OWNER, "shared:0") if isinstance(a, Send)]
   215	        assert notices == []  # the notice was sent by the share callback, not the listing
   216	
   217	    async def test_chat_search_matches_title_and_username(self, router, store):
   218	        await store.set_chat_shared(CHAT_A.id, True)
   219	        await cb(router, MOD, "chats:search")
   220	        actions = await msg(router, MOD, "@modchat")
   221	        labels = [menus.unwrap(label) for row in actions[0].buttons for label, _ in row]
   222	        assert any("Модерация" in label for label in labels)
   223	        await cb(router, MOD, "chats:search")
   224	        actions = await msg(router, MOD, "nothing here")
   225	        assert texts(actions, "en") == [t("en", "bot.chats.search_empty")]
   245	
   246	
   247	class TestDigest:
   248	    async def test_threshold_collapses_the_queue_into_one_message(self, delivery, bot, store):
   249	        await store.update_user(MOD, digest_threshold=5)
   250	        bot.hold()  # build a backlog, as a burst of matches would
   251	        items = [await claim(store, MOD, i) for i in range(1, 8)]
   252	        for item in items:
   253	            await delivery.enqueue(item)
   254	        bot.release()
   255	        await settle(delivery)
   256	        # the worker was already sending the first item when the burst arrived, so the rest
   257	        # collapse into one digest
   258	        assert len(bot.sent) == 2
   259	        assert "6 совпадений" in bot.sent[1].text
   260	        rows = [await store.get_delivery(*i.key) for i in items]
   261	        assert rows[0].status is DeliveryStatus.SENT
   262	        assert all(r.status is DeliveryStatus.DIGESTED for r in rows[1:])
   263	        assert delivery.metrics.digested == 6
   264	        assert delivery.metrics.delivered == 1
   265	
   266	    async def test_below_threshold_sends_individually(self, delivery, bot, store):
   267	        await store.update_user(MOD, digest_threshold=10)
   268	        bot.hold()
   269	        for i in range(1, 4):
   270	            await delivery.enqueue(await claim(store, MOD, i))
   271	        bot.release()
   272	        await settle(delivery)
   273	        assert len(bot.sent) == 3
   274	
   275	    async def test_age_triggers_a_digest_below_the_threshold(self, delivery, bot, store, clock):
   276	        await store.update_user(MOD, digest_threshold=50)
   277	        bot.hold()
   278	        for i in range(1, 7):
   279	            await delivery.enqueue(await claim(store, MOD, i))
   280	        clock.tick(31)  # the oldest queued item is now older than DIGEST_AGE
   281	        bot.release()
   282	        await settle(delivery)
   283	        assert len(bot.sent) == 2
   284	        assert "5 совпадений" in bot.sent[1].text  # threshold not reached, age triggered it
   285	        assert delivery.metrics.digested == 5
   286	
   287	    async def test_digest_threshold_zero_disables_digests(self, delivery, bot, store):
   288	        await store.update_user(MOD, digest_threshold=0)
   289	        for i in range(1, 6):
   290	            await delivery.enqueue(await claim(store, MOD, i))
   291	        await settle(delivery)
   292	        assert len(bot.sent) == 5
   293	
   294	
   295	class TestQueueLimits:
   296	    async def test_overflow_is_counted_and_reported_in_the_next_digest(
   297	        self, delivery, bot, store, monkeypatch
   298	    ):
   299	        monkeypatch.setattr("ddiver.delivery.QUEUE_MAXSIZE", 3)
   300	        await store.update_user(MOD, digest_threshold=3)
   301	        bot.hold()  # fill the queue without letting the worker drain it
   302	        accepted = 0
   303	        for i in range(1, 8):
   304	            if await delivery.enqueue(await claim(store, MOD, i)):
   305	                accepted += 1
   306	        assert accepted == 4  # three queued plus the one the worker is already sending
   307	        assert delivery.metrics.dropped_overflow == 3
   308	        bot.release()
   309	        await settle(delivery)
   310	        assert "отброшено" in bot.sent[-1].text
   311	
   312	    async def test_default_queue_size_is_bounded(self):
   313	        assert QUEUE_MAXSIZE == 2000
   314	
   315	
   316	class TestShutdown:
   317	    async def test_drain_reports_what_is_left(self, delivery, bot, store, sleeps):
   318	        await store.update_user(MOD, digest_threshold=0)
   319	        for i in range(1, 4):
   320	            await delivery.enqueue(await claim(store, MOD, i))
   321	        left = await delivery.drain(timeout=5)
   322	        assert left == 0
   323	        assert len(bot.sent) == 3
   324	
   325	    async def test_undelivered_items_stay_pending_for_the_next_start(self, delivery, store, bot):
   326	        item = await claim(store, MOD, 1)
   327	        await delivery.enqueue(item)
   328	        await delivery.drop_user(MOD)  # simulates a hard stop before the worker ran
   329	        assert bot.sent == []
   330	        assert [p.key for p in await store.pending_deliveries()] == [item.key]
   331	
   332	
   333	class TestRateLimiting:
   334	    async def test_per_user_bucket_spaces_messages_out(self, delivery, bot, store, sleeps):
   335	        await store.update_user(MOD, digest_threshold=0)
    55	        notify_owner=notify,
    56	        clock=clock,
    57	        sleep=sleeps,
    58	        rng=random.Random(1),
    59	        max_attempts=max_attempts,
    60	    )
    61	
    62	
    63	class TestFatalClassification:
    64	    @pytest.mark.parametrize(
    65	        "exc",
    66	        [
    67	            errors.SessionRevokedError(request=None),
    68	            errors.AuthKeyUnregisteredError(request=None),
    69	            errors.UserDeactivatedBanError(request=None),
    70	            errors.AuthKeyDuplicatedError(request=None),  # two processes on one session
    71	            common_errors.AuthKeyNotFound(),  # not an RPCError at all
    72	            errors.PhoneNumberBannedError(request=None),
    73	        ],
    74	    )
    75	    def test_fatal(self, exc):
    76	        assert is_fatal_auth_error(exc)
    77	
    78	    @pytest.mark.parametrize(
    79	        "exc",
    80	        [
    81	            errors.SessionPasswordNeededError(request=None),  # only means "2FA needed"
    82	            errors.FloodWaitError(request=None, capture=5),
    83	            ConnectionError("reset"),
    84	            TimeoutError(),
    85	        ],
    86	    )
    87	    def test_not_fatal(self, exc):
    88	        assert not is_fatal_auth_error(exc)
    89	
    90	    def test_session_file_errors(self):
    91	        assert is_session_file_error(sqlite3.OperationalError("database is locked"))
    92	        assert is_session_file_error(sqlite3.DatabaseError("malformed"))
    93	        assert not is_session_file_error(ValueError("nope"))
    94	
    95	
    96	class TestSupervisor:
    97	    async def test_fatal_auth_parks_in_degraded_mode_without_exiting(self, clock, sleeps):
    98	        notes = []
    99	
   100	        async def notify(reason):
   101	            notes.append(reason)
   102	
   103	        sup = make_supervisor(
   104	            [errors.SessionRevokedError(request=None)], clock, sleeps, notify=notify
   105	        )
   205	        assert await seeded.unsubscribe(OWNER, CHAT_A.id) is False
   206	
   207	    async def test_is_subscribed(self, seeded):
   208	        await seeded.subscribe(MOD, CHAT_A.id)
   209	        assert await seeded.is_subscribed(MOD, CHAT_A.id)
   210	        assert not await seeded.is_subscribed(MOD, CHAT_B.id)
   211	
   212	    async def test_subscription_requires_existing_user(self, store):
   213	        await store.upsert_chat(CHAT_A)
   214	        with pytest.raises(Exception, match="FOREIGN KEY"):
   215	            await store.subscribe(404, CHAT_A.id)
   216	
   217	    async def test_migrate_basic_group_to_supergroup(self, seeded):
   218	        await seeded.upsert_chat(BASIC)
   219	        await seeded.subscribe(MOD, BASIC.id)
   220	        rule = await seeded.add_rule(MOD, BASIC.id, RuleKind.INCLUDE, "казино")
   221	        await seeded.add_ignored(MOD, BASIC.id, 500)
   222	        await seeded.upsert_cursors([Cursor(BASIC.id, 10, T0)])
   223	        new_ref = ChatRef(-1009999, "Old group", None, "supergroup")
   224	        await seeded.migrate_chat(BASIC.id, new_ref, access_hash=42)
   225	        assert [c.id for c in await seeded.list_subscriptions(MOD)] == [new_ref.id]
   226	        assert (await seeded.get_rule(rule.id, MOD)).chat_id == new_ref.id
   227	        assert (await seeded.list_ignored(MOD))[0].chat_id == new_ref.id
   228	        assert BASIC.id not in await seeded.get_cursors()
   229	        assert (await seeded.get_chat(BASIC.id)).available is False
   230	        assert (await seeded.get_chat(new_ref.id)).access_hash == 42
   231	
   232	
   233	class TestRules:
   234	    async def test_crud_and_scoping(self, seeded):
   235	        await seeded.subscribe(OWNER, CHAT_A.id)
     1	"""Heartbeat file used by ``ddiver health`` / Docker HEALTHCHECK."""
     2	
     3	from __future__ import annotations
     4	
     5	import datetime as dt
     6	from pathlib import Path
     7	
     8	from ddiver.timeutil import utcnow
     9	
    10	MAX_AGE_SECONDS = 120
    11	
    12	
    13	def write_heartbeat(path: Path, now: dt.datetime | None = None) -> None:
    14	    moment = now or utcnow()
    15	    path.parent.mkdir(parents=True, exist_ok=True)
    16	    tmp = path.with_suffix(".tmp")
    17	    tmp.write_text(moment.isoformat())
    18	    tmp.replace(path)
    19	
    20	
    21	def heartbeat_age(path: Path, now: dt.datetime | None = None) -> float | None:
    22	    """Seconds since the last heartbeat, or ``None`` if the file is missing/unreadable."""
    23	    try:
    24	        stamp = dt.datetime.fromisoformat(path.read_text().strip())
    25	    except (OSError, ValueError):
    26	        return None
    27	    if stamp.tzinfo is None:
    28	        stamp = stamp.replace(tzinfo=dt.UTC)
    29	    return ((now or utcnow()) - stamp).total_seconds()
    30	
    31	
    32	def is_healthy(
    33	    path: Path, max_age: float = MAX_AGE_SECONDS, now: dt.datetime | None = None
    34	) -> bool:
    35	    age = heartbeat_age(path, now)
    36	    return age is not None and 0 <= age <= max_age
   180	    view = window(payload.text, spans, budget)
   181	    if view.cut_before:
   182	        rt.add("…")
   183	    rt.add_with_bold_spans(view.text, view.spans)
   184	    if view.cut_after:
   185	        rt.add("…").newline().add(t(lang, "notif.truncated", n=view.cut_after))
   186	
   187	    text, entities = rt.build()
   188	    return Rendered(text, entities)
   189	
   190	
   191	@dataclass(frozen=True, slots=True)
   192	class DigestEntry:
   193	    payload: Payload
   194	    key: tuple[int, int, int]
   195	
   196	
   197	def render_digest(
   198	    entries: list[DigestEntry],
   199	    *,
   200	    lang: str,
   201	    tz: str,
   202	    now: dt.datetime,
   203	    overflow: int = 0,
   204	) -> Rendered:
   205	    """One message summarising many matches, grouped by chat, newest chats first by count."""
   206	    del tz, now  # only absolute grouping is shown; per-item times stay in the linked messages
   207	    by_chat: dict[int, list[Payload]] = {}
   208	    for entry in entries:
   209	        by_chat.setdefault(entry.payload.chat_id, []).append(entry.payload)
   210	    ordered = sorted(by_chat.items(), key=lambda kv: (-len(kv[1]), kv[1][0].chat_title))
   211	
   212	    rt = RichText().add(t(lang, "digest.title", n=len(entries)), bold=True).newline(2)
   213	    shown_chats = 0
   214	    for chat_id, payloads in ordered:
   215	        if shown_chats >= DIGEST_CHATS or rt.utf16_length > DIGEST_BUDGET:
   216	            break
   217	        first = payloads[0]
   218	        chat = first.chat_ref
   219	        head_link = chat_link(chat) or message_link(chat, first.msg_id, first.topic_id)
   220	        rt.add("📡 ")
   221	        rt.add(first.chat_title or str(chat_id), bold=True, url=head_link)
   222	        rt.add(f" — {len(payloads)}").newline()
   223	        for payload in payloads[:DIGEST_ITEMS_PER_CHAT]:
   224	            link = message_link(chat, payload.msg_id, payload.topic_id)
   225	            snippet = _snippet(payload)
   226	            line = t(
   227	                lang,
   228	                "digest.item",
   229	                rule=payload.rule_pattern,
   230	                sender=sender_label(payload, lang) or "?",
   231	                snippet=snippet,
   232	            )
   233	            rt.add("  • ")
   234	            rt.add(line, url=link) if link else rt.add(line)
   235	            rt.newline()
   236	        if len(payloads) > DIGEST_ITEMS_PER_CHAT:
   237	            rt.add("  ").add(
   238	                t(lang, "digest.more_items", n=len(payloads) - DIGEST_ITEMS_PER_CHAT)
   239	            ).newline()
   240	        shown_chats += 1
   241	
   242	    if shown_chats < len(ordered):
   243	        remaining = ordered[shown_chats:]
   244	        rt.newline().add(
   245	            t(
   246	                lang,
   247	                "digest.more_chats",
   248	                n=len(remaining),
   249	                m=sum(len(p) for _, p in remaining),
   250	            )
   251	        )
   252	    if overflow:
   253	        rt.newline().add(t(lang, "digest.overflow", n=overflow))
   254	
   255	    text, entities = rt.build()
   215	        await self.listener.prime_chat_cache()
   216	        self.catchup = Catchup(
   217	            self.storage,
   218	            self.listener,
   219	            telethon_history(self.user_client),
   220	            clock=self.clock,
   221	            hours=self.settings.catchup_hours,
   222	            per_chat=self.settings.catchup_max_per_chat,
   223	            on_unavailable=self._notify_lost_access,
   224	        )
   225	        self.router = build_router(
   226	            self.storage,
   227	            matcher=self.matcher,
   228	            status=self._status,
   229	            regex_timeout=self.settings.regex_timeout,
   230	            clock=self.clock,
   231	        )
   232	        self.supervisor = Supervisor(
   233	            run_listener=self._run_listener,
   234	            notify_owner=self._notify_degraded,
   235	            clock=self.clock,
   236	        )
   237	        self._register_handlers()
   238	
   239	    async def close(self) -> None:
   240	        for task in self._tasks:
   241	            await task.stop()
   242	        with contextlib.suppress(Exception):
   243	            await self.user_client.disconnect()
   244	        if self.listener is not None:
   245	            await self.listener.cursors.flush()
   246	        if self.matcher is not None:
   247	            await self.matcher.flush_hits()
   248	        if self.delivery is not None:
   249	            left = await self.delivery.drain()
   250	            if left:
   251	                log.warning("%d notifications stay pending for the next start", left)
   252	        with contextlib.suppress(Exception):
   253	            await self.bot_client.disconnect()
   254	        await self.storage.close()
   255	
   256	    async def run(self) -> int:
   257	        assert self.supervisor is not None  # noqa: S101 - set up in setup()
   258	        await connect_with_timeout(self.bot_client)
   259	        await self.bot_client.start(bot_token=self.settings.bot_token.get_secret_value())
   260	        await connect_with_timeout(self.user_client)
   261	        if not await self.user_client.is_user_authorized():
   262	            log.error("the user session is not authorized; run `ddiver login`")
   263	            return 3
   264	        boots = await self.storage.record_boot()
   265	        assert self.delivery is not None  # noqa: S101
   266	        await self.delivery.replay_pending()
   267	        self._start_background_tasks()
   268	        await boot_delay(boots)
   269	        asyncio.create_task(self._catch_up(), name="catchup-boot")  # noqa: RUF006
   270	        return await self.supervisor.run()
   271	
   272	    def request_stop(self) -> None:
   273	        if self.supervisor is not None:
   274	            self.supervisor.request_stop()
   275	        with contextlib.suppress(Exception):

**Requesting exact test app rest**
exec
/bin/zsh -lc "nl -ba tests/test_app.py | sed -n '96,125p'; nl -ba ddiver/delivery.py | sed -n '228,370p'; nl -ba ddiver/bot/handlers.py | sed -n '340,430p'; nl -ba ddiver/bot/router.py | sed -n '155,215p'; nl -ba ddiver/runtime.py | sed -n '280,370p'" in /Users/L/Code/ddiver
 succeeded in 0ms:
    96	class TestSupervisor:
    97	    async def test_fatal_auth_parks_in_degraded_mode_without_exiting(self, clock, sleeps):
    98	        notes = []
    99	
   100	        async def notify(reason):
   101	            notes.append(reason)
   102	
   103	        sup = make_supervisor(
   104	            [errors.SessionRevokedError(request=None)], clock, sleeps, notify=notify
   105	        )
   106	        assert await sup.run() == EXIT_OK  # never a restart loop under `restart: unless-stopped`
   107	        assert sup.state.degraded and sup.state.degraded_reason == "SessionRevokedError"
   108	        assert notes == ["SessionRevokedError"]
   109	        assert sleeps.calls == []  # no backoff, no reconnect attempts
   110	
   111	    async def test_owner_reminder_is_rate_limited(self, clock, sleeps):
   112	        notes = []
   113	
   114	        async def notify(reason):
   115	            notes.append(reason)
   116	
   117	        sup = make_supervisor(
   118	            [errors.AuthKeyDuplicatedError(request=None)], clock, sleeps, notify=notify
   119	        )
   120	        await sup.run()
   121	        assert await sup.notify_owner_if_due() is False
   122	        clock.tick(6 * 3600 + 1)
   123	        assert await sup.notify_owner_if_due() is True
   124	        assert len(notes) == 2
   125	
   228	    async def _run_worker(self, user_id: int, state: _UserQueue) -> None:
   229	        """The worker loop. No exception may end it — that would silently stop notifications."""
   230	        while True:
   231	            item = await state.queue.get()
   232	            try:
   233	                await self._process(user_id, state, item)
   234	            except asyncio.CancelledError:
   235	                state.queue.task_done()
   236	                raise
   237	            except Exception:
   238	                log.exception("delivery failed for %s; item marked failed", item.key)
   239	                self.metrics.failed += 1
   240	                with contextlib.suppress(Exception):
   241	                    await self._storage.set_delivery_status(
   242	                        *item.key, DeliveryStatus.FAILED, error="unhandled"
   243	                    )
   244	            finally:
   245	                state.enqueued_at.pop(item.key, None)
   246	                state.queue.task_done()
   247	
   248	    async def _process(self, user_id: int, state: _UserQueue, item: OutboxItem) -> None:
   249	        user = await self._storage.get_user(user_id)
   250	        if user is None or not user.active or user.paused:
   251	            self.metrics.dropped_inactive += 1
   252	            return
   253	        lang = user.lang or DEFAULT_LANG
   254	        tz = user.tz or "UTC"
   255	
   256	        batch = self._collect_digest(state, item, user.digest_threshold or self._default_threshold)
   257	        if batch is not None:
   258	            entries = [DigestEntry(Payload.from_json(i.payload), i.key) for i in batch]
   259	            overflow, state.overflow = state.overflow, 0
   260	            rendered = render_digest(
   261	                entries, lang=lang, tz=tz, now=self._clock(), overflow=overflow
   262	            )
   263	            sent = await self._send(user_id, rendered, buttons=None, item=batch[0])
   264	            if sent:
   265	                await self._storage.mark_digested([i.key for i in batch])
   266	                self.metrics.digested += len(batch)
   267	            return
   268	
   269	        payload = Payload.from_json(item.payload)
   270	        rendered = render_notification(payload, lang=lang, tz=tz, now=self._clock())
   271	        buttons = self._buttons(item, lang) if self._buttons else None
   272	        if await self._send(user_id, rendered, buttons=buttons, item=item):
   273	            await self._storage.set_delivery_status(*item.key, DeliveryStatus.SENT)
   274	            self.metrics.delivered += 1
   275	
   276	    def _collect_digest(
   277	        self, state: _UserQueue, item: OutboxItem, threshold: int
   278	    ) -> list[OutboxItem] | None:
   279	        """Decide whether ``item`` starts a digest and, if so, drain the queue into it."""
   280	        if threshold <= 0:
   281	            return None
   282	        backlog = state.queue.qsize() + 1  # the item we just popped counts too
   283	        oldest = min(state.enqueued_at.values(), default=None)
   284	        aged = (
   285	            oldest is not None
   286	            and backlog >= DIGEST_MIN_QUEUE
   287	            and self._clock() - oldest >= DIGEST_AGE
   288	        )
   289	        if backlog < threshold and not aged:
   290	            return None
   291	        batch = [item]
   292	        while len(batch) < DIGEST_MAX_ITEMS and not state.queue.empty():
   293	            extra = state.queue.get_nowait()
   294	            state.enqueued_at.pop(extra.key, None)
   295	            state.queue.task_done()
   296	            batch.append(extra)
   297	        return batch
   298	
   299	    # -- sending ---------------------------------------------------------------------------
   300	
   301	    async def _send(
   302	        self, user_id: int, rendered: Rendered, *, buttons: Any, item: OutboxItem
   303	    ) -> bool:
   304	        attempts = item.attempts
   305	        degraded = False
   306	        while True:
   307	            await self._wait_flood_gate()
   308	            await self._global_bucket.acquire()
   309	            bucket = self._user_buckets.get(user_id)
   310	            if bucket is not None:
   311	                await bucket.acquire()
   312	            attempts += 1
   313	            try:
   314	                await self._bot.send_message(
   315	                    user_id,
   316	                    rendered.text,
   317	                    formatting_entities=None if degraded else rendered.entities,
   318	                    buttons=None if degraded else buttons,
   319	                    link_preview=False,
   320	                )
   321	            except asyncio.CancelledError:
   322	                raise
   323	            except errors.FloodWaitError as exc:
   324	                self._open_flood_gate(exc.seconds, user_id)
   325	                attempts -= 1  # a flood wait is not the user's fault; do not burn an attempt
   326	                continue
   327	            except PAUSE_ERRORS as exc:
   328	                log.info("pausing delivery for %s: %s", user_id, type(exc).__name__)
   329	                await self._storage.pause_delivery(user_id, type(exc).__name__)
   330	                await self._storage.set_delivery_status(
   331	                    *item.key, DeliveryStatus.FAILED, error=type(exc).__name__, attempts=attempts
   332	                )
   333	                await self.drop_user(user_id)
   334	                self.metrics.failed += 1
   335	                return False
   336	            except errors.BadRequestError as exc:
   337	                if not degraded and _is_rendering_error(exc):
   338	                    degraded = True
   339	                    self.metrics.degraded_sends += 1
   340	                    log.warning("retrying plain-text after %s for %s", type(exc).__name__, item.key)
   341	                    continue
   342	                await self._fail(item, exc, attempts)
   343	                return False
   344	            except (*RETRYABLE, ValueError) as exc:
   345	                if isinstance(exc, ValueError) or attempts >= MAX_ATTEMPTS:
   346	                    await self._fail(item, exc, attempts)
   347	                    return False
   348	                delay = min(BACKOFF_CAP, BACKOFF_BASE * 2 ** (attempts - 1))
   349	                log.warning(
   350	                    "send to %s failed (%s); retry %d in %.0fs",
   351	                    user_id,
   352	                    type(exc).__name__,
   353	                    attempts,
   354	                    delay,
   355	                )
   356	                await self._sleep(delay)
   357	                continue
   358	            return True
   359	
   360	    async def _fail(self, item: OutboxItem, exc: BaseException, attempts: int) -> None:
   361	        log.error("giving up on %s: %s", item.key, type(exc).__name__)
   362	        self.metrics.failed += 1
   363	        await self._storage.set_delivery_status(
   364	            *item.key, DeliveryStatus.FAILED, error=type(exc).__name__, attempts=attempts
   365	        )
   366	
   367	    def _open_flood_gate(self, seconds: float, user_id: int) -> None:
   368	        until = self._monotonic() + seconds + 1
   369	        if until > self._flood_until:
   370	            self._flood_until = until
   340	            Toast(t(ctx.lang, "bot.users.revoked_toast", user_id=target), literal=True),
   341	            *await _users_view(ctx),
   342	        ]
   343	
   344	    # -- rules -----------------------------------------------------------------------------
   345	
   346	    def icon_for(pattern: str, lang: str) -> str:
   347	        mode, _ = parse_pattern(pattern)
   348	        return t(lang, KIND_ICONS[mode])
   349	
   350	    async def rules_view(ctx: Context, chat_id: int, kind: RuleKind, page: int) -> list[Action]:
   351	        rules = await storage.list_rules(ctx.user_id, chat_id, kind)
   352	        icons = [icon_for(r.pattern, ctx.lang) for r in rules]
   353	        if chat_id == ALL_CHATS:
   354	            title = ctx.t("bot.rules.title_global")
   355	        else:
   356	            chat = await storage.get_chat(chat_id)
   357	            title = ctx.t("bot.rules.title", chat=chat.ref.title if chat else chat_id)
   358	        body = title if rules else f"{title}\n{ctx.t('bot.rules.empty')}"
   359	        return [
   360	            Edit(
   361	                body,
   362	                literal=True,
   363	                buttons=menus.rules_menu(rules, icons, chat_id, kind.value, page),
   364	            )
   365	        ]
   366	
   367	    @router.callback("rules")
   368	    async def rules(ctx: Context) -> list[Action]:
   369	        parts = (ctx.incoming.data or "rules:pick").split(":")
   370	        if parts[1] == "pick":
   371	            chats = await storage.list_subscriptions(ctx.user_id)
   372	            return [Edit("bot.rules.pick_chat", buttons=menus.rules_chat_menu(chats, 0))]
   373	        chat_id = _int(parts[1])
   374	        kind = RuleKind(parts[2]) if len(parts) > 2 else RuleKind.INCLUDE
   375	        page = _int(parts[3]) if len(parts) > 3 else 0
   376	        return await rules_view(ctx, chat_id, kind, page)
   377	
   378	    @router.callback("rpick")
   379	    async def rules_pick_page(ctx: Context) -> list[Action]:
   380	        page = _int((ctx.incoming.data or "rpick:0").split(":", 1)[1])
   381	        chats = await storage.list_subscriptions(ctx.user_id)
   382	        return [Edit("bot.rules.pick_chat", buttons=menus.rules_chat_menu(chats, page))]
   383	
   384	    @router.callback("radd")
   385	    async def rules_add_prompt(ctx: Context) -> list[Action]:
   386	        _, raw_chat, raw_kind = (ctx.incoming.data or "").split(":")
   387	        router.set_pending(ctx.user_id, "rules_add", {"chat_id": _int(raw_chat), "kind": raw_kind})
   388	        return [Reply("bot.rules.prompt")]
   389	
   390	    @router.pending("rules_add")
   391	    async def rules_add(ctx: Context) -> list[Action]:
   392	        payload = router.pending_payload(ctx.user_id)
   393	        chat_id = int(payload.get("chat_id", ALL_CHATS))
   394	        kind = RuleKind(payload.get("kind", RuleKind.INCLUDE.value))
   395	        router.clear_pending(ctx.user_id)
   396	        lines = parse_rule_lines(ctx.incoming.text or "")
   397	        if len(lines) > MAX_LINES_PER_MESSAGE:
   398	            return [Reply("bot.rules.too_many_lines", {"limit": MAX_LINES_PER_MESSAGE})]
   399	        report = await _add_rules(ctx, chat_id, kind, lines)
   400	        return [Reply(report, literal=True), *await rules_view(ctx, chat_id, kind, 0)]
   401	
   402	    async def _add_rules(ctx: Context, chat_id: int, kind: RuleKind, lines: Sequence[str]) -> str:
   403	        added = duplicates = 0
   404	        problems: list[str] = []
   405	        total_user = await storage.count_rules(ctx.user_id)
   406	        total_chat = await storage.count_rules(ctx.user_id, chat_id)
   407	        for number, line in enumerate(lines, start=1):
   408	            if total_chat >= MAX_RULES_PER_CHAT:
   409	                problems.append(ctx.t("bot.rules.limit_chat", limit=MAX_RULES_PER_CHAT))
   410	                break
   411	            if total_user >= MAX_RULES_PER_USER:
   412	                problems.append(ctx.t("bot.rules.limit_user", limit=MAX_RULES_PER_USER))
   413	                break
   414	            try:
   415	                compiled = compile_rule(line, timeout=regex_timeout)
   416	            except RuleError as exc:
   417	                problems.append(
   418	                    ctx.t(
   419	                        "bot.rules.line_invalid",
   420	                        line=number,
   421	                        reason=t(ctx.lang, exc.key, **exc.params),
   422	                    )
   423	                )
   424	                continue
   425	            try:
   426	                await storage.add_rule(ctx.user_id, chat_id, kind, compiled.pattern)
   427	            except DuplicateRuleError:
   428	                duplicates += 1
   429	                problems.append(ctx.t("bot.rules.line_duplicate", line=number))
   430	                continue
   155	
   156	        return register
   157	
   158	    def pending(self, kind: str) -> Callable[[Handler], Handler]:
   159	        def register(handler: Handler) -> Handler:
   160	            self._pending_handlers[kind] = handler
   161	            return handler
   162	
   163	        return register
   164	
   165	    def fallback(self, handler: Handler) -> Handler:
   166	        self._fallback = handler
   167	        return handler
   168	
   169	    # -- pending input ---------------------------------------------------------------------
   170	
   171	    def set_pending(self, user_id: int, kind: str, payload: dict[str, Any] | None = None) -> None:
   172	        self._pending[user_id] = PendingInput(
   173	            kind, dict(payload or {}), self._clock() + PENDING_TTL
   174	        )
   175	
   176	    def get_pending(self, user_id: int) -> PendingInput | None:
   177	        state = self._pending.get(user_id)
   178	        if state is None:
   179	            return None
   180	        if self._clock() >= state.expires_at:
   181	            del self._pending[user_id]
   182	            return None
   183	        return state
   184	
   185	    def clear_pending(self, user_id: int) -> None:
   186	        self._pending.pop(user_id, None)
   187	
   188	    # -- dispatch --------------------------------------------------------------------------
   189	
   190	    async def dispatch(self, incoming: Incoming) -> list[Action]:
   191	        if not incoming.is_private:
   192	            return []  # the bot only works in DMs; group commands are ignored
   193	        user = await self._storage.ensure_user(
   194	            incoming.user_id, lang=_seed_lang(incoming.lang_code)
   195	        )
   196	        if not user.is_owner and user.role is not Role.ALLOWED:
   197	            return await self._gate(incoming, user)
   198	        if user.paused and await self._storage.resume_delivery(user.id):
   199	            log.info("delivery resumed for %s after an incoming update", user.id)
   200	            user = await self._storage.ensure_user(user.id)
   201	
   202	        ctx = Context(incoming, user, self._storage, self)
   203	        if incoming.data is not None:
   204	            return await self._dispatch_callback(ctx)
   205	        return await self._dispatch_message(ctx)
   206	
   207	    async def _dispatch_callback(self, ctx: Context) -> list[Action]:
   208	        data = ctx.incoming.data or ""
   209	        prefix = data.split(":", 1)[0]
   210	        entry = self._callbacks.get(prefix)
   211	        if entry is None:
   212	            return [Toast("bot.err.unknown_action")]
   213	        handler, owner_only = entry
   214	        if owner_only and not ctx.is_owner:
   215	            log.warning("user %s tried an owner-only action %r", ctx.user_id, prefix)
   280	    def _register_handlers(self) -> None:
   281	        listener = self.listener
   282	        assert listener is not None  # noqa: S101
   283	
   284	        @self.user_client.on(events.NewMessage())
   285	        async def on_message(event: Any) -> None:
   286	            await listener.on_message(event)
   287	
   288	        @self.user_client.on(events.Raw(tl.UpdateChannel))
   289	        async def on_channel_update(update: Any) -> None:
   290	            await self._check_channel_access(update)
   291	
   292	        @self.user_client.on(events.ChatAction())
   293	        async def on_chat_action(event: Any) -> None:
   294	            await self._on_chat_action(event)
   295	
   296	        @self.bot_client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
   297	        async def on_bot_message(event: Any) -> None:
   298	            await self._on_bot_update(event, text=event.raw_text)
   299	
   300	        @self.bot_client.on(events.CallbackQuery(func=lambda e: e.is_private))
   301	        async def on_bot_callback(event: Any) -> None:
   302	            data = event.data.decode("utf-8", "replace") if event.data else ""
   303	            await self._on_bot_update(event, data=data)
   304	
   305	    async def _on_bot_update(
   306	        self, event: Any, *, text: str | None = None, data: str | None = None
   307	    ) -> None:
   308	        assert self.router is not None  # noqa: S101
   309	        try:
   310	            sender = await event.get_sender()
   311	            incoming = Incoming(
   312	                user_id=int(event.sender_id),
   313	                text=text,
   314	                data=data,
   315	                msg_id=getattr(event, "message_id", None),
   316	                lang_code=getattr(sender, "lang_code", None),
   317	                is_private=True,
   318	            )
   319	            actions = await self.router.dispatch(incoming)
   320	            user = await self.storage.get_user(incoming.user_id)
   321	            lang = (user.lang if user else None) or DEFAULT_LANG
   322	            await self.bot.perform(event, actions, lang)
   323	        except Exception:
   324	            log.exception("bot update failed")
   325	
   326	    async def _on_chat_action(self, event: Any) -> None:
   327	        try:
   328	            me = await self.user_client.get_me()
   329	            if event.user_id != me.id:
   330	                return
   331	            chat_id = int(utils.get_peer_id(event.action_message.peer_id))
   332	            if event.user_kicked or event.user_left:
   333	                if await self.storage.set_chat_available(chat_id, False):
   334	                    await self._notify_lost_access(chat_id, "left")
   335	                await self._invalidate(chat_id)
   336	            elif event.user_added or event.user_joined:
   337	                if await self.storage.set_chat_available(chat_id, True):
   338	                    await self._notify_regained_access(chat_id)
   339	                await self._invalidate(chat_id)
   340	        except Exception:
   341	            log.debug("chat action handling failed", exc_info=True)
   342	
   343	    async def _check_channel_access(self, update: Any) -> None:
   344	        """``UpdateChannel`` is what Telegram sends when our membership changed."""
   345	        try:
   346	            bare = int(getattr(update, "channel_id", 0))
   347	            if not bare:
   348	                return
   349	            chat_id = int(utils.get_peer_id(tl.PeerChannel(bare)))
   350	            entity = (getattr(update, "_entities", None) or {}).get(chat_id)
   351	            if entity is None:
   352	                return
   353	            lost = isinstance(entity, tl.ChannelForbidden) or bool(getattr(entity, "left", False))
   354	            if lost:
   355	                if await self.storage.set_chat_available(chat_id, False):
   356	                    await self._notify_lost_access(chat_id, type(entity).__name__)
   357	            elif await self.storage.set_chat_available(chat_id, True):
   358	                await self._notify_regained_access(chat_id)
   359	            await self._invalidate(chat_id)
   360	        except Exception:
   361	            log.debug("channel update handling failed", exc_info=True)
   362	
   363	    async def _invalidate(self, chat_id: int | None = None) -> None:
   364	        if self.matcher is not None:
   365	            await self.matcher.invalidate(chat_id)
   366	
   367	    # -- background tasks ------------------------------------------------------------------
   368	
   369	    def _start_background_tasks(self) -> None:
   370	        listener = self.listener

**Identifying handler registration lines**
exec
/bin/zsh -lc "nl -ba ddiver/listener.py | sed -n '88,108p'; nl -ba ddiver/senders.py | sed -n '105,175p'; nl -ba ddiver/storage.py | sed -n '535,655p'; nl -ba ddiver/storage.py | sed -n '770,805p'; nl -ba tests/test_catchup.py | sed -n '190,245p'" in /Users/L/Code/ddiver
 succeeded in 0ms:
    88	def chat_ref_of(entity: Any) -> ChatRef | None:
    89	    """Chat metadata from an update entity, or ``None`` for private chats and unknown types."""
    90	    if entity is None or isinstance(entity, tl.User):
    91	        return None
    92	    if isinstance(entity, (tl.Chat, tl.Channel, tl.ChatForbidden, tl.ChannelForbidden)):
    93	        return ChatRef.from_entity(entity)
    94	    return None
    95	
    96	
    97	def should_consider(message: tl.Message, *, ignore_own: bool = True) -> bool:
    98	    """Shared by the live handler and catch-up so both paths filter identically."""
    99	    if not isinstance(message, tl.Message):  # MessageService / MessageEmpty
   100	        return False
   101	    if getattr(message, "action", None) is not None:
   102	        return False
   103	    if ignore_own and getattr(message, "out", False):
   104	        return False
   105	    return bool(message.message)
   106	
   107	
   108	class Listener:
   105	        self._resolver = resolver
   106	        self._clock = clock
   107	        self._sleep = sleep
   108	        self._ttl = ttl
   109	        self._size = size
   110	        self._min_interval = min_interval
   111	        self._entries: OrderedDict[int, tuple[dt.datetime, SenderInfo]] = OrderedDict()
   112	        self._lock = asyncio.Lock()
   113	        self._next_allowed: dt.datetime | None = None
   114	        self.stats_hits = 0
   115	        self.stats_resolves = 0
   116	        self.stats_failures = 0
   117	        self.stats_throttled = 0
   118	
   119	    def get(self, peer_id: int) -> SenderInfo | None:
   120	        entry = self._entries.get(peer_id)
   121	        if entry is None:
   122	            return None
   123	        stored_at, info = entry
   124	        if self._clock() - stored_at > self._ttl:
   125	            del self._entries[peer_id]
   126	            return None
   127	        self._entries.move_to_end(peer_id)
   128	        self.stats_hits += 1
   129	        return info
   130	
   131	    def put(self, info: SenderInfo) -> None:
   132	        self._entries[info.id] = (self._clock(), info)
   133	        self._entries.move_to_end(info.id)
   134	        while len(self._entries) > self._size:
   135	            self._entries.popitem(last=False)
   136	
   137	    async def resolve(self, peer_id: int) -> SenderInfo | None:
   138	        """Cache lookup, then at most one resolve per ``min_interval``; never raises."""
   139	        cached = self.get(peer_id)
   140	        if cached is not None:
   141	            return cached
   142	        if self._resolver is None:
   143	            return None
   144	        async with self._lock:
   145	            cached = self.get(peer_id)
   146	            if cached is not None:
   147	                return cached
   148	            now = self._clock()
   149	            if self._next_allowed is not None and now < self._next_allowed:
   150	                self.stats_throttled += 1
   151	                return None
   152	            self._next_allowed = now + dt.timedelta(seconds=self._min_interval)
   153	            self.stats_resolves += 1
   154	            try:
   155	                async with asyncio.timeout(RESOLVE_TIMEOUT):
   156	                    entity = await self._resolver(peer_id)
   157	            except (RPCError, ValueError, TypeError, TimeoutError, ConnectionError) as exc:
   158	                self.stats_failures += 1
   159	                log.info("sender resolve failed for %s: %s", peer_id, type(exc).__name__)
   160	                return None
   161	            except asyncio.CancelledError:
   162	                raise
   163	            except Exception:
   164	                self.stats_failures += 1
   165	                log.warning("unexpected sender resolve error for %s", peer_id, exc_info=True)
   166	                return None
   167	        info = sender_from_entity(entity)
   168	        if info is not None:
   169	            self.put(info)
   170	        return info
   171	
   172	
   173	async def sender_of(message: Any, cache: SenderCache) -> SenderInfo | None:
   174	    """Sender of a Telethon message: entity attached to the update, else cache/resolve.
   175	
   535	        cur = await self.db.execute(
   536	            "SELECT user_id FROM subscriptions WHERE chat_id=? ORDER BY user_id", (chat_id,)
   537	        )
   538	        ids = [int(r["user_id"]) for r in await cur.fetchall()]
   539	        if not active_only:
   540	            return ids
   541	        active = await self.active_user_ids()
   542	        return [uid for uid in ids if uid in active]
   543	
   544	    async def monitored_chat_ids(self) -> set[int]:
   545	        """Chats with at least one active, unpaused subscriber, that we still have access to."""
   546	        active = await self.active_user_ids()
   547	        if not active:
   548	            return set()
   549	        marks = ",".join("?" * len(active))
   550	        cur = await self.db.execute(
   551	            f"""SELECT DISTINCT s.chat_id FROM subscriptions s
   552	                JOIN users u ON u.id=s.user_id
   553	                JOIN chats c ON c.id=s.chat_id
   554	                WHERE s.user_id IN ({marks}) AND u.delivery_paused_at IS NULL
   555	                  AND c.available=1 AND c.kind IN ('group','supergroup','channel')""",  # noqa: S608
   556	            tuple(sorted(active)),
   557	        )
   558	        return {int(r["chat_id"]) for r in await cur.fetchall()}
   559	
   560	    # -- rules -----------------------------------------------------------------------------
   561	
   562	    @staticmethod
   563	    def _rule(row: aiosqlite.Row) -> Rule:
   564	        return Rule(
   565	            id=row["id"],
   566	            user_id=row["user_id"],
   567	            chat_id=row["chat_id"],
   568	            kind=RuleKind(row["kind"]),
   569	            pattern=row["pattern"],
   570	            disabled_reason=row["disabled_reason"],
   571	            hits=row["hits"],
   572	        )
   573	
   574	    async def add_rule(self, user_id: int, chat_id: int, kind: RuleKind, pattern: str) -> Rule:
   575	        try:
   576	            cur = await self.db.execute(
   577	                """INSERT INTO rules (user_id, chat_id, kind, pattern, pattern_key, created_at)
   578	                   VALUES (?,?,?,?,?,?)""",
   579	                (user_id, chat_id, kind.value, pattern, pattern_key(pattern), self._now()),
   580	            )
   581	        except aiosqlite.IntegrityError as exc:
   582	            raise DuplicateRuleError(pattern) from exc
   583	        rule = await self.get_rule(int(cur.lastrowid or 0), user_id)
   584	        if rule is None:  # pragma: no cover - impossible after the insert
   585	            raise KeyError(pattern)
   586	        return rule
   587	
   588	    async def get_rule(self, rule_id: int, user_id: int) -> Rule | None:
   589	        cur = await self.db.execute(
   590	            "SELECT * FROM rules WHERE id=? AND user_id=?", (rule_id, user_id)
   591	        )
   592	        row = await cur.fetchone()
   593	        return self._rule(row) if row else None
   594	
   595	    async def delete_rule(self, rule_id: int, user_id: int) -> bool:
   596	        cur = await self.db.execute(
   597	            "DELETE FROM rules WHERE id=? AND user_id=?", (rule_id, user_id)
   598	        )
   599	        return cur.rowcount == 1
   600	
   601	    async def count_rules(self, user_id: int, chat_id: int | None = None) -> int:
   602	        sql = "SELECT COUNT(*) AS n FROM rules WHERE user_id=?"
   603	        params: list[Any] = [user_id]
   604	        if chat_id is not None:
   605	            sql += " AND chat_id=?"
   606	            params.append(chat_id)
   607	        cur = await self.db.execute(sql, params)
   608	        row = await cur.fetchone()
   609	        return int(row["n"]) if row else 0
   610	
   611	    async def list_rules(
   612	        self, user_id: int, chat_id: int | None = None, kind: RuleKind | None = None
   613	    ) -> list[Rule]:
   614	        sql = "SELECT * FROM rules WHERE user_id=?"
   615	        params: list[Any] = [user_id]
   616	        if chat_id is not None:
   617	            sql += " AND chat_id=?"
   618	            params.append(chat_id)
   619	        if kind is not None:
   620	            sql += " AND kind=?"
   621	            params.append(kind.value)
   622	        sql += " ORDER BY (chat_id=0) DESC, chat_id, kind DESC, id"
   623	        cur = await self.db.execute(sql, params)
   624	        return [self._rule(r) for r in await cur.fetchall()]
   625	
   626	    async def rules_for_chat(self, chat_id: int) -> list[Rule]:
   627	        """Enabled rules applying to ``chat_id``: chat-specific ones plus the global rules of
   628	        every active, unpaused subscriber. Chat-specific rules come first."""
   629	        active = await self.active_user_ids()
   630	        if not active:
   631	            return []
   632	        marks = ",".join("?" * len(active))
   633	        cur = await self.db.execute(
   634	            f"""SELECT r.* FROM rules r
   635	                JOIN users u ON u.id=r.user_id
   636	                JOIN subscriptions s ON s.user_id=r.user_id AND s.chat_id=?
   637	                WHERE (r.chat_id=? OR r.chat_id=0) AND r.disabled_reason IS NULL
   638	                  AND r.user_id IN ({marks}) AND u.delivery_paused_at IS NULL
   639	                ORDER BY r.user_id, (r.chat_id=0), r.kind, r.id""",  # noqa: S608
   640	            (chat_id, chat_id, *sorted(active)),
   641	        )
   642	        return [self._rule(r) for r in await cur.fetchall()]
   643	
   644	    async def disable_rule(self, rule_id: int, reason: str) -> None:
   645	        await self.db.execute("UPDATE rules SET disabled_reason=? WHERE id=?", (reason, rule_id))
   646	
   647	    async def enable_rule(self, rule_id: int, user_id: int) -> bool:
   648	        cur = await self.db.execute(
   649	            "UPDATE rules SET disabled_reason=NULL WHERE id=? AND user_id=?", (rule_id, user_id)
   650	        )
   651	        return cur.rowcount == 1
   652	
   653	    async def flush_rule_hits(self, hits: dict[int, int]) -> None:
   654	        if not hits:
   655	            return
   770	                DeliveryStatus.PENDING.value,
   771	                json.dumps(payload, ensure_ascii=False),
   772	                self._now(),
   773	            ),
   774	        )
   775	        if cur.rowcount != 1:
   776	            return None
   777	        return await self.get_delivery(user_id, chat_id, msg_id)
   778	
   779	    async def get_delivery(self, user_id: int, chat_id: int, msg_id: int) -> OutboxItem | None:
   780	        cur = await self.db.execute(
   781	            "SELECT * FROM outbox WHERE user_id=? AND chat_id=? AND msg_id=?",
   782	            (user_id, chat_id, msg_id),
   783	        )
   784	        row = await cur.fetchone()
   785	        return self._outbox(row) if row else None
   786	
   787	    async def pending_deliveries(self, limit: int = 5000) -> list[OutboxItem]:
   788	        cur = await self.db.execute(
   789	            "SELECT * FROM outbox WHERE status='pending' ORDER BY created_at, msg_id LIMIT ?",
   790	            (limit,),
   791	        )
   792	        return [self._outbox(r) for r in await cur.fetchall()]
   793	
   794	    async def set_delivery_status(
   795	        self,
   796	        user_id: int,
   797	        chat_id: int,
   798	        msg_id: int,
   799	        status: DeliveryStatus,
   800	        *,
   801	        error: str | None = None,
   802	        attempts: int | None = None,
   803	    ) -> None:
   804	        sent_at = self._now() if status in (DeliveryStatus.SENT, DeliveryStatus.DIGESTED) else None
   805	        await self.db.execute(
   190	            [Cursor(CHAT_ID, 1, T0), Cursor(OTHER_ID, 1, T0)],
   191	        )
   192	        history = FakeHistory({OTHER_ID: history_of(OTHER, [(5, "казино")])})
   193	        history.errors[CHAT_ID] = errors.FloodWaitError(request=None, capture=30)
   194	        catchup = Catchup(store, listener, history, clock=clock, sleep=sleeps)
   195	        result = await catchup.run({CHAT_ID, OTHER_ID})
   196	        assert result.flood_waits == 1
   197	        assert result.claimed == 1
   198	        assert [i.chat_id for i in listener.delivered] == [OTHER_ID]
   199	        assert (await store.get_chat(CHAT_ID)).available is True  # a flood wait is temporary
   200	
   201	    async def test_cursor_is_withheld_until_the_chat_finished(self, store, listener, clock):
   202	        await store.upsert_cursors([Cursor(CHAT_ID, 1, T0)])
   203	        history = FakeHistory({CHAT_ID: history_of(CHAT, [(9, "казино")])})
   204	        seen_blocked = []
   205	
   206	        async def sleep(_seconds):
   207	            seen_blocked.append(set(listener.cursors.blocked))
   208	
   209	        catchup = Catchup(store, listener, history, clock=clock, sleep=sleep)
   210	
   211	        original = listener.handle
   212	
   213	        async def spy(message, **kwargs):
   214	            seen_blocked.append(set(listener.cursors.blocked))
   215	            return await original(message, **kwargs)
   216	
   217	        listener.handle = spy
   218	        await catchup.run({CHAT_ID})
   219	        assert seen_blocked[0] == {CHAT_ID}  # blocked while replaying
   220	        assert listener.cursors.blocked == set()  # released afterwards
   221	
   222	    async def test_total_budget_stops_the_round(self, store, listener, clock, sleeps):
   223	        await store.upsert_cursors([Cursor(CHAT_ID, 0, T0), Cursor(OTHER_ID, 0, T0)])
   224	        history = FakeHistory(
   225	            {
   226	                CHAT_ID: history_of(CHAT, [(i, "казино") for i in range(10, 0, -1)], step=1),
   227	                OTHER_ID: history_of(OTHER, [(i, "казино") for i in range(10, 0, -1)], step=1),
   228	            }
   229	        )
   230	        catchup = Catchup(store, listener, history, clock=clock, sleep=sleeps, total=5)
   231	        result = await catchup.run({CHAT_ID, OTHER_ID})
   232	        assert result.chats == 1
   233	        assert len(history.calls) == 1  # the second chat is left for the next round
   234	
   235	    async def test_service_and_own_messages_are_filtered(self, store, listener, clock, sleeps):
   236	        await store.upsert_cursors([Cursor(CHAT_ID, 0, T0)])
   237	        own = make_event("казино", chat=CHAT, msg_id=3, out=True).message
   238	        service = tl.MessageService(
   239	            id=2,
   240	            peer_id=tl.PeerChannel(1234567890),
   241	            date=T0,
   242	            action=tl.MessageActionChatJoinedByLink(inviter_id=1),
   243	        )
   244	        normal = make_event("казино", chat=CHAT, msg_id=1).message
   245	        history = FakeHistory({CHAT_ID: [own, service, normal]})

**Identifying design invariant contradiction**
**Analyzing notification handling bottleneck**
**Planning resolver prefetch outside handler**
**Planning durable dispatcher fix**
**Evaluating startup retry and task leak issues**
**Identifying worker cancellation bookkeeping issues**
**Fixing router resume to properly invalidate matcher and replay delivery**
**Planning per-subscriber cursor reset and catchup filtering**
**Identifying notification duplication and potential information leak**
**Identifying privacy risks in moderator callbacks**
**Assessing test suite defects and gaps**
exec
/bin/zsh -lc 'uv run pytest --collect-only -q | tail -n 2 && uv run ruff check ddiver tests && uv run mypy ddiver' in /Users/L/Code/ddiver
 exited 1 in 466ms:
tests/test_timeutil.py: 39

PLC0415 `import` should be at the top-level of a file
   --> tests/test_bot.py:473:9
    |
471 | class TestMenuRendering:
472 |     def test_shared_menu_marks_state(self):
473 |         from ddiver.storage import ChatRow
    |         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
474 |
475 |         rows = [
    |

PLC0415 `import` should be at the top-level of a file
   --> tests/test_bot.py:488:9
    |
487 |     def test_chats_menu_flags_unavailable_chats(self):
488 |         from ddiver.storage import ChatRow
    |         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
489 |
490 |         rows = [ChatRow(CHAT_A, available=False, shared=True, access_hash=None)]
    |

PLC0415 `import` should be at the top-level of a file
   --> tests/test_bot.py:496:9
    |
495 |     def test_chats_menu_paginates(self):
496 |         from ddiver.storage import ChatRow
    |         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
497 |
498 |         rows = [
    |

PLC0415 `import` should be at the top-level of a file
   --> tests/test_bot.py:510:9
    |
509 |     def test_rules_menu_navigation_keeps_chat_and_kind(self):
510 |         from ddiver.storage import Rule
    |         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
511 |
512 |         rules = [
    |

PLC0415 `import` should be at the top-level of a file
   --> tests/test_bot.py:523:9
    |
522 |     def test_rules_menu_truncates_long_patterns_and_marks_disabled(self):
523 |         from ddiver.storage import Rule
    |         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
524 |
525 |         long_rule = Rule(1, OWNER, 0, RuleKind.INCLUDE, "x" * 120, "timeout", 0)
    |

PLC0415 `import` should be at the top-level of a file
   --> tests/test_bot.py:532:9
    |
531 |     def test_ignored_menu_shows_scope(self):
532 |         from ddiver.storage import IgnoredSender
    |         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
533 |
534 |         entries = [
    |

PLC0415 `import` should be at the top-level of a file
   --> tests/test_bot.py:550:9
    |
549 |     def test_rules_chat_menu_always_offers_all_chats(self):
550 |         from ddiver.storage import ChatRow
    |         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
551 |
552 |         rows = [ChatRow(CHAT_A, True, True, None)]
    |

Found 7 errors.

**Clarifying test command behavior**
**Planning cursor reconciliation fix**
**Assessing catchup access hash handling**
**Analyzing NewMessage migration behavior**
exec
/bin/zsh -lc "uv run python - <<'PY'
import inspect
from telethon.events import NewMessage, ChatAction
print(inspect.getsource(NewMessage.build))
print('ChatAction build migrate refs:')
s=inspect.getsource(ChatAction.build)
for i,l in enumerate(s.splitlines(),1):
 if 'Migrate' in l or 'migrat' in l: print(i,l)
PY" in /Users/L/Code/ddiver
 succeeded in 39ms:
    @classmethod
    def build(cls, update, others=None, self_id=None):
        if isinstance(update,
                      (types.UpdateNewMessage, types.UpdateNewChannelMessage)):
            if not isinstance(update.message, types.Message):
                return  # We don't care about MessageService's here
            event = cls.Event(update.message)
        elif isinstance(update, types.UpdateShortMessage):
            event = cls.Event(types.Message(
                out=update.out,
                mentioned=update.mentioned,
                media_unread=update.media_unread,
                silent=update.silent,
                id=update.id,
                peer_id=types.PeerUser(update.user_id),
                from_id=types.PeerUser(self_id if update.out else update.user_id),
                message=update.message,
                date=update.date,
                fwd_from=update.fwd_from,
                via_bot_id=update.via_bot_id,
                reply_to=update.reply_to,
                entities=update.entities,
                ttl_period=update.ttl_period
            ))
        elif isinstance(update, types.UpdateShortChatMessage):
            event = cls.Event(types.Message(
                out=update.out,
                mentioned=update.mentioned,
                media_unread=update.media_unread,
                silent=update.silent,
                id=update.id,
                from_id=types.PeerUser(self_id if update.out else update.from_id),
                peer_id=types.PeerChat(update.chat_id),
                message=update.message,
                date=update.date,
                fwd_from=update.fwd_from,
                via_bot_id=update.via_bot_id,
                reply_to=update.reply_to,
                entities=update.entities,
                ttl_period=update.ttl_period
            ))
        else:
            return

        return event

ChatAction build migrate refs:

**Diagnosing missing UpdateNewMessage handler**
**Fixing concurrent Outbox claim exceptions**
**Formulating test report and severity**
**Identifying concurrent loss and overflow issues**
**Consolidating test scenarios and defect lines**
exec
/bin/zsh -lc "nl -ba ddiver/runtime.py | sed -n '170,205p'; nl -ba ddiver/senders.py | sed -n '137,190p'; nl -ba ddiver/clients.py | sed -n '20,50p'; nl -ba ddiver/storage.py | sed -n '750,795p'; nl -ba tests/test_delivery.py | sed -n '150,195p'; nl -ba tests/test_health.py; git status --short" in /Users/L/Code/ddiver
 succeeded in 0ms:
   170	        self.listener: Listener | None = None
   171	        self.delivery: Delivery | None = None
   172	        self.catchup: Catchup | None = None
   173	        self.router: Router | None = None
   174	        self.supervisor: Supervisor | None = None
   175	        self.senders = SenderCache(self._resolve_entity, clock=clock)
   176	        self._tasks: list[PeriodicTask] = []
   177	        self._last_ping_ok: dt.datetime | None = None
   178	        self._catchup_lock = asyncio.Lock()
   179	
   180	    # -- lifecycle -------------------------------------------------------------------------
   181	
   182	    async def setup(self) -> None:
   183	        await self.storage.open()
   184	        await self.storage.ensure_user(self.settings.owner_id)
   185	
   186	        async def on_rule_disabled(rule: Rule, reason: str) -> None:
   187	            user = await self.storage.get_user(rule.user_id)
   188	            lang = (user.lang if user else None) or DEFAULT_LANG
   189	            await self._tell(
   190	                rule.user_id, t(lang, "bot.rules.disabled", pattern=rule.pattern, reason=reason)
   191	            )
   192	
   193	        self.matcher = Matcher(
   194	            self.storage,
   195	            timeout=self.settings.regex_timeout,
   196	            on_rule_disabled=on_rule_disabled,
   197	        )
   198	        await self.matcher.reload()
   199	
   200	        self.delivery = Delivery(
   201	            self.bot,
   202	            self.storage,
   203	            clock=self.clock,
   204	            buttons=notification_buttons_factory(None),
   205	            default_threshold=self.settings.digest_threshold,
   137	    async def resolve(self, peer_id: int) -> SenderInfo | None:
   138	        """Cache lookup, then at most one resolve per ``min_interval``; never raises."""
   139	        cached = self.get(peer_id)
   140	        if cached is not None:
   141	            return cached
   142	        if self._resolver is None:
   143	            return None
   144	        async with self._lock:
   145	            cached = self.get(peer_id)
   146	            if cached is not None:
   147	                return cached
   148	            now = self._clock()
   149	            if self._next_allowed is not None and now < self._next_allowed:
   150	                self.stats_throttled += 1
   151	                return None
   152	            self._next_allowed = now + dt.timedelta(seconds=self._min_interval)
   153	            self.stats_resolves += 1
   154	            try:
   155	                async with asyncio.timeout(RESOLVE_TIMEOUT):
   156	                    entity = await self._resolver(peer_id)
   157	            except (RPCError, ValueError, TypeError, TimeoutError, ConnectionError) as exc:
   158	                self.stats_failures += 1
   159	                log.info("sender resolve failed for %s: %s", peer_id, type(exc).__name__)
   160	                return None
   161	            except asyncio.CancelledError:
   162	                raise
   163	            except Exception:
   164	                self.stats_failures += 1
   165	                log.warning("unexpected sender resolve error for %s", peer_id, exc_info=True)
   166	                return None
   167	        info = sender_from_entity(entity)
   168	        if info is not None:
   169	            self.put(info)
   170	        return info
   171	
   172	
   173	async def sender_of(message: Any, cache: SenderCache) -> SenderInfo | None:
   174	    """Sender of a Telethon message: entity attached to the update, else cache/resolve.
   175	
   176	    Returns ``None`` only when the sender is unknown and cannot be resolved cheaply.
   177	    """
   178	    info = sender_from_entity(getattr(message, "sender", None))
   179	    if info is not None:
   180	        cache.put(info)
   181	        return info
   182	    sender_id = getattr(message, "sender_id", None)
   183	    if sender_id is None:
   184	        return None
   185	    return await cache.resolve(int(sender_id))
   186	
   187	
   188	def suppress_rpc() -> contextlib.AbstractContextManager[None]:
   189	    return contextlib.suppress(RPCError, ValueError)
    20	BOT_FLOOD_THRESHOLD = 0  # delivery owns the retry policy, so it must see the error
    21	ENTITY_CACHE_LIMIT = 20_000
    22	
    23	
    24	class ListenerClient(TelegramClient):  # type: ignore[misc]
    25	    """Telethon client that tells us when it reconnected.
    26	
    27	    Telethon requests ``getChannelDifference`` with a limit of 100 for user accounts and, on
    28	    ``ChannelDifferenceTooLong``, jumps ``pts`` and returns no updates at all. In a busy chat
    29	    that silently loses everything beyond 100 messages per gap, so after every reconnect we
    30	    re-run the cursor-based catch-up.
    31	    """
    32	
    33	    def __init__(self, *args: Any, **kwargs: Any) -> None:
    34	        super().__init__(*args, **kwargs)
    35	        self.reconnected = asyncio.Event()
    36	        self.reconnects = 0
    37	
    38	    async def _handle_auto_reconnect(self) -> None:
    39	        try:
    40	            await super()._handle_auto_reconnect()
    41	        except Exception:  # the callback runs detached; an exception here would vanish
    42	            log.warning("auto-reconnect handler failed", exc_info=True)
    43	        self.reconnects += 1
    44	        self.reconnected.set()
    45	        log.info("listener reconnected (%d since start); scheduling a catch-up", self.reconnects)
    46	
    47	
    48	def is_fatal_auth_error(exc: BaseException) -> bool:
    49	    """Errors that mean the session is dead and only ``ddiver login`` can fix it.
    50	
   750	
   751	    async def claim_delivery(
   752	        self,
   753	        user_id: int,
   754	        chat_id: int,
   755	        msg_id: int,
   756	        rule_id: int | None,
   757	        payload: dict[str, Any],
   758	    ) -> OutboxItem | None:
   759	        """Record intent to deliver. ``None`` when this (user, chat, msg) was already claimed —
   760	        the dedupe between live updates, catch-up and restarts."""
   761	        cur = await self.db.execute(
   762	            """INSERT OR IGNORE INTO outbox
   763	                 (user_id, chat_id, msg_id, rule_id, status, payload, created_at)
   764	               VALUES (?,?,?,?,?,?,?)""",
   765	            (
   766	                user_id,
   767	                chat_id,
   768	                msg_id,
   769	                rule_id,
   770	                DeliveryStatus.PENDING.value,
   771	                json.dumps(payload, ensure_ascii=False),
   772	                self._now(),
   773	            ),
   774	        )
   775	        if cur.rowcount != 1:
   776	            return None
   777	        return await self.get_delivery(user_id, chat_id, msg_id)
   778	
   779	    async def get_delivery(self, user_id: int, chat_id: int, msg_id: int) -> OutboxItem | None:
   780	        cur = await self.db.execute(
   781	            "SELECT * FROM outbox WHERE user_id=? AND chat_id=? AND msg_id=?",
   782	            (user_id, chat_id, msg_id),
   783	        )
   784	        row = await cur.fetchone()
   785	        return self._outbox(row) if row else None
   786	
   787	    async def pending_deliveries(self, limit: int = 5000) -> list[OutboxItem]:
   788	        cur = await self.db.execute(
   789	            "SELECT * FROM outbox WHERE status='pending' ORDER BY created_at, msg_id LIMIT ?",
   790	            (limit,),
   791	        )
   792	        return [self._outbox(r) for r in await cur.fetchall()]
   793	
   794	    async def set_delivery_status(
   795	        self,
   150	    async def test_flood_wait_retries_the_same_item_and_pauses_everyone(
   151	        self, delivery, bot, store, sleeps
   152	    ):
   153	        bot.fail_next(errors.FloodWaitError(request=None, capture=30))
   154	        await delivery.enqueue(await claim(store, MOD, 1))
   155	        await delivery.enqueue(await claim(store, OWNER, 2))
   156	        await settle(delivery)
   157	        assert len(bot.sent) == 2
   158	        assert delivery.metrics.flood_waits == 1
   159	        assert delivery.metrics.flood_seconds == 30
   160	        assert any(s >= 30 for s in sleeps.calls)  # the shared gate, not a per-user sleep
   161	        assert delivery.metrics.delivered == 2
   162	
   163	    async def test_repeated_flood_waits_never_fail_the_item(self, delivery, bot, store):
   164	        bot.fail_next(*[errors.FloodWaitError(request=None, capture=5) for _ in range(8)])
   165	        item = await claim(store, MOD, 1)
   166	        await delivery.enqueue(item)
   167	        await settle(delivery)
   168	        assert len(bot.sent) == 1
   169	        row = await store.get_delivery(*item.key)
   170	        assert row.status is DeliveryStatus.SENT
   171	
   172	
   173	class TestErrors:
   174	    async def test_blocked_user_is_paused_and_queue_dropped(self, delivery, bot, store):
   175	        bot.fail_next(errors.UserIsBlockedError(request=None))
   176	        item = await claim(store, MOD, 1)
   177	        await delivery.enqueue(item)
   178	        await delivery.enqueue(await claim(store, MOD, 2))
   179	        await settle(delivery)
   180	        user = await store.get_user(MOD)
   181	        assert user.paused and user.pause_reason == "UserIsBlockedError"
   182	        assert user.active  # no re-approval needed after they unblock
   183	        assert delivery.queue_length(MOD) == 0
   184	        assert (await store.get_delivery(*item.key)).status is DeliveryStatus.FAILED
   185	
   186	    async def test_entity_error_falls_back_to_plain_text(self, delivery, bot, store):
   187	        bot.fail_next(errors.EntitiesTooLongError(request=None))
   188	        item = await claim(store, MOD, 1)
   189	        await delivery.enqueue(item)
   190	        await settle(delivery)
   191	        assert len(bot.sent) == 1
   192	        assert bot.sent[0].entities == []
   193	        assert bot.sent[0].buttons is None
   194	        assert delivery.metrics.degraded_sends == 1
   195	        assert (await store.get_delivery(*item.key)).status is DeliveryStatus.SENT
     1	import datetime as dt
     2	
     3	from ddiver.health import heartbeat_age, is_healthy, write_heartbeat
     4	
     5	
     6	def test_heartbeat_roundtrip(tmp_path):
     7	    path = tmp_path / "sub" / "heartbeat"
     8	    now = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.UTC)
     9	    write_heartbeat(path, now)
    10	    assert heartbeat_age(path, now + dt.timedelta(seconds=30)) == 30
    11	    assert is_healthy(path, now=now + dt.timedelta(seconds=119))
    12	    assert not is_healthy(path, now=now + dt.timedelta(seconds=121))
    13	
    14	
    15	def test_missing_or_garbage_file(tmp_path):
    16	    path = tmp_path / "heartbeat"
    17	    assert heartbeat_age(path) is None
    18	    assert not is_healthy(path)
    19	    path.write_text("not a date")
    20	    assert not is_healthy(path)
    21	
    22	
    23	def test_future_stamp_is_unhealthy(tmp_path):
    24	    path = tmp_path / "heartbeat"
    25	    now = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.UTC)
    26	    write_heartbeat(path, now + dt.timedelta(minutes=5))
    27	    assert not is_healthy(path, now=now)

**Planning defect categorization and heartbeat fix**
**Designing authorization and subscription checks**
**Fixing test action capture and assertions**
**Analyzing private DM subscription logic**
**Evaluating private DM test effectiveness**
**Distinguishing batch failure and size issues**
**Confirming task orphaning and queue issues**
**Identifying count from tail output**
exec
/bin/zsh -lc 'uv run pytest --collect-only | tail -n 3' in /Users/L/Code/ddiver
 succeeded in 538ms:
tests/test_timeutil.py::test_relative_rejects_naive_now

372 tests collected in 0.34s

**Planning final report formatting**
**Prioritizing defects by severity and dependencies**
codex
The full suite passes—372 tests—but I found the following real defects.

## Notification loss, duplication, and misrouting

1. **A transient outbox write failure can permanently skip a message for one or more users.**  
   Files: [listener.py:130](/Users/L/Code/ddiver/ddiver/listener.py:130), [listener.py:178](/Users/L/Code/ddiver/ddiver/listener.py:178), [storage.py:751](/Users/L/Code/ddiver/ddiver/storage.py:751), [clients.py:91](/Users/L/Code/ddiver/ddiver/clients.py:91)

   Failure scenario: Telethon dispatches updates concurrently because `sequential_updates=False`. Message 100 matches users A and B. A’s outbox row is inserted, but B’s insert raises `sqlite3.OperationalError`; `on_message` catches it, and message 100 never reaches the cursor or delivery loop. Concurrent message 101 succeeds, advances the in-memory cursor to 101, and the periodic flush persists it. Catch-up now starts after 101, so B’s notification for message 100 is permanently lost. A’s already-inserted row is also not enqueued until a restart.

   Concrete fix: serialize processing per chat, and do not allow message N+1 to advance that chat’s cursor until all claims for N have succeeded or been durably recorded for retry. Wrap all claims for one message in a transaction or durable ingress record, retry `OperationalError`, and enqueue any successfully created rows even if a later user claim fails. Add a concurrent-handler test that injects a failure between two users’ claims and then processes a higher message ID.

2. **Queue overflow leaves durable notifications pending forever during the current process.**  
   Files: [delivery.py:132](/Users/L/Code/ddiver/ddiver/delivery.py:132), [delivery.py:141](/Users/L/Code/ddiver/ddiver/delivery.py:141), [storage.py:787](/Users/L/Code/ddiver/ddiver/storage.py:787)

   Failure scenario: a user’s 2,000-slot queue fills. `enqueue()` returns `False`, but the corresponding outbox row remains `pending`. Nothing scans pending rows after startup, so that notification is never delivered unless the process restarts. Startup replay has the same defect: if 5,000 rows belong to one user, most overflow the queue and remain stranded; rows beyond the hard `LIMIT 5000` are not even examined. A long-running process can therefore silently accumulate an unbounded pending outbox.

   Concrete fix: introduce a persistent outbox dispatcher that repeatedly claims bounded batches as queue capacity becomes available. Track an explicit `queued`/lease state or an in-memory key set so scanning cannot enqueue duplicates. Page until all pending rows have either been queued or intentionally deferred; do not describe queue overflow as “dropped” unless the affected rows are included in a successfully delivered digest and atomically marked digested.

3. **Blocking the bot strands pending rows, leaks a worker task, and can grow the outbox without bound.**  
   Files: [delivery.py:167](/Users/L/Code/ddiver/ddiver/delivery.py:167), [delivery.py:327](/Users/L/Code/ddiver/ddiver/delivery.py:327), [bot/router.py:190](/Users/L/Code/ddiver/ddiver/bot/router.py:190), [matcher.py:96](/Users/L/Code/ddiver/ddiver/matcher.py:96)

   Failure scenario: Telegram raises `UserIsBlockedError`. The worker calls `drop_user()` from inside itself. `drop_user()` removes the queue, cancels the current task, and awaits that same task. The resulting exception is caught by the worker’s broad handler, after which the worker loops back to `queue.get()` on an orphaned queue and remains alive forever. I reproduced this: `_queues` was empty while an unfinished `delivery-2` task remained.

   The queued rows that were removed stay `pending`. The matcher is not invalidated after `pause_delivery()`, so its cached rules continue matching the paused user and create more pending rows; `enqueue()` rejects every one. When the user sends a message and the router clears the pause, it neither invalidates the matcher nor replays that user’s pending rows. Depending on whether some unrelated invalidation occurred while paused, the user either keeps matching with an ever-growing backlog or remains absent from the matcher after resuming.

   Concrete fix: never cancel/await a worker from that same worker. Have `_process()` return a “stop worker” outcome, drain the queue safely, and let `_run_worker()` exit normally. Add pause/resume hooks: pausing must invalidate the matcher; resuming must invalidate it again and trigger a bounded replay for that user. Decide explicitly whether queued pre-block rows are retried or marked failed, and update every row accordingly.

4. **A failed digest marks only its first item; all other drained items become permanently unqueued pending rows.**  
   Files: [delivery.py:256](/Users/L/Code/ddiver/ddiver/delivery.py:256), [delivery.py:291](/Users/L/Code/ddiver/ddiver/delivery.py:291), [delivery.py:342](/Users/L/Code/ddiver/ddiver/delivery.py:342)

   Failure scenario: five rows form a digest. Sending the digest raises `MessageTooLongError` or exhausts transient retries. `_send()` receives only `batch[0]`, so only that row is marked failed. The other four were removed from the queue and had `task_done()` called, but remain `pending` in SQLite with no mechanism to enqueue them again. I reproduced statuses `sent, failed, pending, pending, pending` with an empty queue.

   Concrete fix: make `_send()` operate on a batch identity or return a structured result without changing one row. On permanent failure, atomically mark every batch row failed or requeue every row. On pause errors, apply the same policy to the entire batch. Add a test that fails the digest RPC and asserts the status of every member and the subsequent retry behavior.

5. **Digest threshold `0` does not disable digests.**  
   Files: [delivery.py:256](/Users/L/Code/ddiver/ddiver/delivery.py:256), [tests/test_delivery.py:287](/Users/L/Code/ddiver/tests/test_delivery.py:287)

   Failure scenario: a user sets `digest_threshold=0`. The expression `user.digest_threshold or self._default_threshold` substitutes the configured default because zero is falsey. A burst is therefore collapsed into a digest despite the user explicitly disabling digests. With a default threshold of 3 and a five-item held burst, I reproduced two sends: one notification and one four-match digest.

   Concrete fix: pass `user.digest_threshold` directly; use `None`, not zero, to represent “inherit default” if inheritance is required. The test must hold the bot and enqueue more items than the default threshold; its current sequential sends never create enough backlog, so it passes without exercising the decision.

6. **The digest length budget is not enforced and can produce invalid Telegram messages.**  
   Files: [formatter.py:214](/Users/L/Code/ddiver/ddiver/formatter.py:214), [delivery.py:336](/Users/L/Code/ddiver/ddiver/delivery.py:336)

   Failure scenario: the loop checks the current length only before adding a whole chat. One chat can then append five entries containing 300-character rule patterns, long sender names, and snippets. With valid field limits, two such chat sections rendered to 5,340 UTF-16 units in my check, above Telegram’s 4,096-unit limit. Telegram raises `MessageTooLongError`; it is not included in `_is_rendering_error()`, and even a plain-text resend would still be too long. This then triggers the stranded-batch defect above.

   Concrete fix: budget every heading and item before appending it, truncate rule/sender/snippet fields by UTF-16 units, reserve space for “more” and overflow lines, and perform a final `<=4096` assertion/truncation. Add a maximal-field digest test using UTF-16 length, not Python `len()`.

7. **Unsubscribing and later resubscribing can deliver messages from the period when the user was not subscribed.**  
   Files: [storage.py:497](/Users/L/Code/ddiver/ddiver/storage.py:497), [listener.py:157](/Users/L/Code/ddiver/ddiver/listener.py:157), [catchup.py:79](/Users/L/Code/ddiver/ddiver/catchup.py:79)

   Failure scenario: the last active subscriber leaves a chat when the stored cursor is 100. Messages 101–200 arrive while no one is subscribed, so the listener ignores them and does not advance the cursor. The user resubscribes, and reconciliation runs before a newer live update is flushed. Catch-up reads from 100 and applies the user’s current rules to messages 101–200, sending notifications for messages that predate the subscription.

   Concrete fix: preserve per-user subscription boundaries and suppress catch-up matches whose dates precede `subscriptions.created_at`, or advance/reset the chat cursor when monitoring transitions from zero active subscribers to one. Add tests for unsubscribe → history gap → resubscribe, including the case where another user remains subscribed.

## Reconnect, catch-up, and chat lifecycle

8. **The reconnect event is set but never consumed while the listener is running, so reconnect gaps can be lost permanently.**  
   Files: [clients.py:38](/Users/L/Code/ddiver/ddiver/clients.py:38), [runtime.py:450](/Users/L/Code/ddiver/ddiver/runtime.py:450)

   Failure scenario: the stored cursor is 100, the connection drops, and messages 101–300 are missed. `_handle_auto_reconnect()` sets `reconnected`, but `_run_listener()` checks it only before entering `run_until_disconnected()`, which remains active across automatic reconnects. Message 301 then arrives live and the cursor is flushed to 301. The 15-minute periodic catch-up starts at 301 and can never see 101–300.

   Concrete fix: run a tracked background task that waits on `user_client.reconnected` and immediately reconciles while live cursor advancement for affected chats is blocked. Alternatively, make the reconnect callback schedule a serialized reconciliation directly. The event must be consumed during `run_until_disconnected()`, not only after it returns. Add a test that sets the event while `run_until_disconnected()` remains pending and then delivers a higher live ID.

9. **Basic-group-to-supergroup migration is never wired to a real Telethon event.**  
   Files: [listener.py:97](/Users/L/Code/ddiver/ddiver/listener.py:97), [runtime.py:284](/Users/L/Code/ddiver/ddiver/runtime.py:284), [storage.py:465](/Users/L/Code/ddiver/ddiver/storage.py:465), [tests/test_storage.py:217](/Users/L/Code/ddiver/tests/test_storage.py:217)

   Failure scenario: Telegram emits a `MessageService` containing `MessageActionChatMigrateTo`. Telethon 1.45’s `events.NewMessage.build()` explicitly does not construct a NewMessage event for `MessageService`, and neither registered raw/channel nor `ChatAction` handler handles migration. `Storage.migrate_chat()` is therefore never called. Subscriptions and rules remain on the old basic-group ID; messages in the new supergroup fail `matcher.is_monitored()` and produce no notifications.

   Concrete fix: register a raw handler for `UpdateNewMessage`/`UpdateNewChannelMessage`, recognize both migration action directions, obtain the new entity/access hash from the update’s attached entities, call `migrate_chat()` transactionally, invalidate both chat IDs, update the listener cache, and notify subscribers. The existing test only calls the storage method directly; add a runtime-level test built from the raw Telethon update.

10. **Once a chat is marked unavailable, the advertised recovery paths do not exist.**  
    Files: [storage.py:544](/Users/L/Code/ddiver/ddiver/storage.py:544), [listener.py:153](/Users/L/Code/ddiver/ddiver/listener.py:153), [catchup.py:80](/Users/L/Code/ddiver/ddiver/catchup.py:80)

    Failure scenario: catch-up marks a chat `available=0`. The account is later re-added while the bot is offline, so no `ChatAction` update is observed. On restart, `monitored_chat_ids()` excludes the chat, catch-up iterates only monitored chats, and a later live message is rejected at `is_monitored()` before the listener can refresh metadata or mark it available. The chat remains disabled forever.

    Concrete fix: periodically probe unavailable subscribed chats using their stored input peers, independently of the monitored set. Also treat an attached live group/channel message as proof of access before the monitored fast-path rejection: set `available=1`, invalidate, and then process it.

11. **Catch-up ignores the stored channel access hash and passes a bare marked integer to Telethon.**  
    Files: [catchup.py:168](/Users/L/Code/ddiver/ddiver/catchup.py:168), [runtime.py:216](/Users/L/Code/ddiver/ddiver/runtime.py:216)

    Failure scenario: SQLite contains `chats.access_hash`, but the Telethon session’s entity cache no longer contains that channel—for example, the session entity cache was recreated while the application database was retained. `iter_messages(-100…)` invokes Telethon 1.45 input-entity resolution. Without a cached entity it eventually tries a channel lookup with access hash zero and can raise `ValueError`. Catch-up classifies that as lost access and marks the still-accessible chat unavailable, disabling all its notifications.

    Concrete fix: have catch-up load the `ChatRow` and pass `InputPeerChannel(bare_id, access_hash)` for channels/supergroups, or `InputPeerChat` for basic groups. Treat a missing access hash separately from an actual `ChannelPrivateError`; do not mark the chat unavailable merely because local entity resolution failed. Add a test whose fake rejects integer peers but accepts the constructed `InputPeerChannel`.

12. **The initial catch-up task is untracked and can outlive application shutdown.**  
    Files: [runtime.py:239](/Users/L/Code/ddiver/ddiver/runtime.py:239), [runtime.py:269](/Users/L/Code/ddiver/ddiver/runtime.py:269)

    Failure scenario: shutdown begins while `catchup-boot` is iterating history or sleeping in Telethon. `close()` stops only `PeriodicTask` instances, disconnects clients, drains delivery, and closes SQLite, but never cancels or awaits `catchup-boot`. The task can subsequently continue into listener/storage operations against a closed database, or remain alive in a retry/flood-wait path. Any exception becomes “Task exception was never retrieved.”

    Concrete fix: store the boot catch-up task, cancel and await it before flushing cursors and closing the database, or put all application-owned tasks in an `asyncio.TaskGroup`. Ensure `_catch_up_chat()` unblocks cursors in a `finally` block.

## Event-loop hangs and Telethon hot-path misuse

13. **All regular-expression matching runs synchronously on the event-loop thread.**  
    Files: [matcher.py:106](/Users/L/Code/ddiver/ddiver/matcher.py:106), [matcher.py:138](/Users/L/Code/ddiver/ddiver/matcher.py:138)

    Failure scenario: an allow-listed hostile moderator installs hundreds of regex rules that each consume the allowed 20 ms on a chosen input. `evaluate()` awaits `_search_all()`, but `_search_all()` performs the synchronous `regex.finditer()` call directly; there is no thread handoff. Five hundred rules can block the only event loop for roughly ten seconds per message. With Telethon’s concurrent update dispatch, incoming update tasks accumulate during a burst, delaying delivery, bot controls, heartbeat, and cursor flush and causing unbounded task/memory growth.

    Concrete fix: offload the entire regex batch for a message to a bounded dedicated executor as specified by the design, with a bounded queue and an overall per-message budget. Do not call `to_thread` once per rule. Add an event-loop responsiveness test with more than the offload threshold and a ticker coroutine that must continue running.

14. **The listener performs user-session entity RPCs inside matched-message handlers.**  
    Files: [runtime.py:175](/Users/L/Code/ddiver/ddiver/runtime.py:175), [senders.py:137](/Users/L/Code/ddiver/ddiver/senders.py:137), [senders.py:173](/Users/L/Code/ddiver/ddiver/senders.py:173), [listener.py:170](/Users/L/Code/ddiver/ddiver/listener.py:170)

    Failure scenario: a matched update lacks an attached sender entity. `sender_of()` calls the configured resolver, which is `user_client.get_entity()`, under a five-second timeout. A burst of different uncached senders queues handler tasks behind the resolver lock and performs one listener-session RPC after another. If resolution is throttled or fails, the sender becomes `None`; a bot sender is then not recognized as a bot and can generate a notification even though bot senders must be dropped.

    Concrete fix: remove the network resolver from the live listener path. Use only `message.sender`/attached update entities and a previously populated cache. If background enrichment is desired, enqueue it outside the message handler and do not advance delivery based on unresolved bot status; either conservatively skip unknown senders or persist an unresolved item for bounded later classification.

## Delivery/task lifecycle

15. **Cancelling a busy delivery worker calls `task_done()` twice and can make shutdown fail.**  
    Files: [delivery.py:197](/Users/L/Code/ddiver/ddiver/delivery.py:197), [delivery.py:228](/Users/L/Code/ddiver/ddiver/delivery.py:228)

    Failure scenario: the bot RPC is blocked when shutdown’s `wait_idle()` times out. `drain()` cancels the worker. The `CancelledError` branch calls `state.queue.task_done()`, then `finally` calls it again, raising `ValueError: task_done() called too many times`. `drain()` suppresses only `CancelledError`, so the exception escapes `Application.close()` and can prevent the bot disconnect and database close. I reproduced this with a held fake bot and a short drain timeout.

    Concrete fix: call `task_done()` exactly once, exclusively in `finally`. Preserve the currently held row as pending when cancelled. Expand the shutdown test to hold the bot until timeout and assert that `drain()` returns normally and the row remains pending.

16. **Fatal authentication does not enter degraded mode; it exits the whole application.**  
    Files: [app.py:86](/Users/L/Code/ddiver/ddiver/app.py:86), [runtime.py:256](/Users/L/Code/ddiver/ddiver/runtime.py:256), [tests/test_app.py:97](/Users/L/Code/ddiver/tests/test_app.py:97)

    Failure scenario: the user session raises `SessionRevokedError`. The supervisor sets `state.degraded`, sends one owner notification, and immediately returns `EXIT_OK`. `Application.run()` returns, `cmd_run` closes the bot, delivery, and database, and Docker’s `restart: unless-stopped` starts the container again regardless of the zero exit code. This produces exactly the restart/re-notification loop the design says degraded mode prevents; the bot UI and workers are not kept alive.

    Concrete fix: after entering degraded state, remain in a stop-aware wait loop while the bot client, delivery workers, heartbeat, and periodic six-hour owner reminder continue running. Exit only on an explicit shutdown signal. The current test is asserting the wrong behavior: `await sup.run() == EXIT_OK` proves that it exited. Rewrite it to start `run()` as a task, assert the task remains pending after fatal auth, advance six hours to verify the reminder, then request stop and await `EXIT_OK`.

17. **The health check stays green when the listener is disconnected or its ping has failed indefinitely.**  
    Files: [runtime.py:389](/Users/L/Code/ddiver/ddiver/runtime.py:389), [runtime.py:392](/Users/L/Code/ddiver/ddiver/runtime.py:392), [health.py:13](/Users/L/Code/ddiver/ddiver/health.py:13), [tests/test_health.py:6](/Users/L/Code/ddiver/tests/test_health.py:6)

    Failure scenario: the user client enters an infinite reconnect loop or every `GetStateRequest` ping fails. The independent heartbeat task continues writing the current timestamp, and `cmd health` checks only that timestamp. `_last_ping_ok` is assigned but never written or evaluated. Docker therefore reports healthy indefinitely while the listener cannot receive messages.

    Concrete fix: atomically write structured heartbeat data containing heartbeat time, last successful ping, transport/reconnecting state, last update, degraded status, and queue length. `cmd health` must reject stale pings and excessive update silence while allowing explicitly degraded mode according to the documented policy. Tests must cover a fresh file whose `ping_ok_at` is stale or whose transport is reconnecting.

## Multi-user security and privacy

18. **A moderator can retain a subscription to a chat while the owner unshares it.**  
    Files: [bot/handlers.py:216](/Users/L/Code/ddiver/ddiver/bot/handlers.py:216), [storage.py:486](/Users/L/Code/ddiver/ddiver/storage.py:486), [storage.py:503](/Users/L/Code/ddiver/ddiver/storage.py:503)

    Failure scenario: callbacks run concurrently. A moderator’s forged `sub:<chat>:1` handler reads `shared=True` and pauses. The owner sets `shared=False`; `unsubscribe_all()` selects no moderator row. The moderator resumes. `Storage.subscribe()` rereads the now-unshared chat but checks only its kind, not `shared`, and inserts the subscription after the owner’s deletion. The hostile moderator is now subscribed to a chat the owner did not share.

    Concrete fix: enforce authorization atomically in the storage insert: `INSERT … SELECT … FROM chats WHERE id=? AND kind IN (…) AND (shared=1 OR ?=OWNER_ID)`. Make unshare plus deletion one transaction. Do not rely on the handler’s earlier read. Add a deterministic interleaving test with barriers around the owner’s unshare and moderator’s insert.

19. **Forged rule callbacks reveal unshared chat metadata.**  
    Files: [bot/handlers.py:350](/Users/L/Code/ddiver/ddiver/bot/handlers.py:350), [bot/handlers.py:367](/Users/L/Code/ddiver/ddiver/bot/handlers.py:367), [bot/handlers.py:384](/Users/L/Code/ddiver/ddiver/bot/handlers.py:384)

    Failure scenario: the owner has an unshared private family supergroup in `chats`. An allow-listed moderator who knows or obtains its marked ID sends `rules:<id>:include:0`. `rules_view()` scopes the rule query to the moderator, but then calls unrestricted `get_chat()` and returns the hidden chat’s title. `radd:<id>:include` also accepts the hidden ID and creates chat-specific rules without verifying subscription or sharing. Thus the “moderators never see the owner’s dialog list” boundary is bypassed by forged callback data.

    Concrete fix: centralize a `can_access_chat(actor, chat_id)` check and require either owner status or both `shared=1` and an actor-owned subscription for all chat-scoped rule, ignore, and custom-exclusion callbacks. Return the same generic not-found response for inaccessible and nonexistent IDs. For notification actions, bind the action to the actor’s own outbox row; the ignore callback currently lacks a message ID, so include one or use an opaque action record.

## Tests that currently pass vacuously or assert the wrong behavior

20. **The owner-only mutation assertion is literally always true.**  
    File: [tests/test_bot.py:121](/Users/L/Code/ddiver/tests/test_bot.py:121)

    Failure scenario: `assert (await store.get_user(STRANGER)) is None or True` passes for every possible database state. A regression in which a moderator’s `allow:`, `deny:`, `revoke:`, or `share:` callback mutates state and then returns the expected toast would pass this test.

    Concrete fix: snapshot the relevant user role, chat sharing flag, subscriptions, rules, and ignores before each parameterized callback, then assert exact equality afterward. Use separate expected-state checks for each payload rather than one unrelated stranger lookup.

21. **The unsharing test does not test that a notice was sent.**  
    File: [tests/test_bot.py:209](/Users/L/Code/ddiver/tests/test_bot.py:209)

    Failure scenario: the test discards the actions returned by `share:<chat>:0`, then calls `shared:0` and asserts that the listing action contains no `Send`. Removing the notification loop from the unshare handler would still pass.

    Concrete fix: retain the unshare callback’s returned actions and assert exactly one `Send` targeting the dropped moderator with the expected localized chat title. Keep the subscription-deletion assertion separately.

22. **The queue-overflow test treats stranded pending rows as successfully handled.**  
    File: [tests/test_delivery.py:295](/Users/L/Code/ddiver/tests/test_delivery.py:295)

    Failure scenario: the test verifies the overflow counter and the digest text but never checks the three overflowed outbox rows. They remain `pending` with an empty queue, which is the loss/delay defect described above, yet the test passes.

    Concrete fix: after settling, assert the status of every claimed row and assert that no pending row is left without a queued/leased retry. If overflow is represented only as a count in a delivered digest, atomically mark the corresponding rows digested and assert that mapping.

23. **The blocked-user test misses both the orphan worker and the unrecoverable queued row.**  
    File: [tests/test_delivery.py:173](/Users/L/Code/ddiver/tests/test_delivery.py:173)

    Failure scenario: the test enqueues two rows but asserts only the first row’s failed status and `queue_length()==0`. Because `drop_user()` removes the queue dictionary entry, that length is zero even while an orphaned `delivery-<uid>` task is waiting forever and the second row remains pending with no queue.

    Concrete fix: assert the second row’s explicit status/retry ownership, enumerate named tasks to ensure no worker remains after pause, then simulate an inbound user update and verify that pending delivery is replayed or intentionally finalized.
tokens used
200,707
The full suite passes—372 tests—but I found the following real defects.

## Notification loss, duplication, and misrouting

1. **A transient outbox write failure can permanently skip a message for one or more users.**  
   Files: [listener.py:130](/Users/L/Code/ddiver/ddiver/listener.py:130), [listener.py:178](/Users/L/Code/ddiver/ddiver/listener.py:178), [storage.py:751](/Users/L/Code/ddiver/ddiver/storage.py:751), [clients.py:91](/Users/L/Code/ddiver/ddiver/clients.py:91)

   Failure scenario: Telethon dispatches updates concurrently because `sequential_updates=False`. Message 100 matches users A and B. A’s outbox row is inserted, but B’s insert raises `sqlite3.OperationalError`; `on_message` catches it, and message 100 never reaches the cursor or delivery loop. Concurrent message 101 succeeds, advances the in-memory cursor to 101, and the periodic flush persists it. Catch-up now starts after 101, so B’s notification for message 100 is permanently lost. A’s already-inserted row is also not enqueued until a restart.

   Concrete fix: serialize processing per chat, and do not allow message N+1 to advance that chat’s cursor until all claims for N have succeeded or been durably recorded for retry. Wrap all claims for one message in a transaction or durable ingress record, retry `OperationalError`, and enqueue any successfully created rows even if a later user claim fails. Add a concurrent-handler test that injects a failure between two users’ claims and then processes a higher message ID.

2. **Queue overflow leaves durable notifications pending forever during the current process.**  
   Files: [delivery.py:132](/Users/L/Code/ddiver/ddiver/delivery.py:132), [delivery.py:141](/Users/L/Code/ddiver/ddiver/delivery.py:141), [storage.py:787](/Users/L/Code/ddiver/ddiver/storage.py:787)

   Failure scenario: a user’s 2,000-slot queue fills. `enqueue()` returns `False`, but the corresponding outbox row remains `pending`. Nothing scans pending rows after startup, so that notification is never delivered unless the process restarts. Startup replay has the same defect: if 5,000 rows belong to one user, most overflow the queue and remain stranded; rows beyond the hard `LIMIT 5000` are not even examined. A long-running process can therefore silently accumulate an unbounded pending outbox.

   Concrete fix: introduce a persistent outbox dispatcher that repeatedly claims bounded batches as queue capacity becomes available. Track an explicit `queued`/lease state or an in-memory key set so scanning cannot enqueue duplicates. Page until all pending rows have either been queued or intentionally deferred; do not describe queue overflow as “dropped” unless the affected rows are included in a successfully delivered digest and atomically marked digested.

3. **Blocking the bot strands pending rows, leaks a worker task, and can grow the outbox without bound.**  
   Files: [delivery.py:167](/Users/L/Code/ddiver/ddiver/delivery.py:167), [delivery.py:327](/Users/L/Code/ddiver/ddiver/delivery.py:327), [bot/router.py:190](/Users/L/Code/ddiver/ddiver/bot/router.py:190), [matcher.py:96](/Users/L/Code/ddiver/ddiver/matcher.py:96)

   Failure scenario: Telegram raises `UserIsBlockedError`. The worker calls `drop_user()` from inside itself. `drop_user()` removes the queue, cancels the current task, and awaits that same task. The resulting exception is caught by the worker’s broad handler, after which the worker loops back to `queue.get()` on an orphaned queue and remains alive forever. I reproduced this: `_queues` was empty while an unfinished `delivery-2` task remained.

   The queued rows that were removed stay `pending`. The matcher is not invalidated after `pause_delivery()`, so its cached rules continue matching the paused user and create more pending rows; `enqueue()` rejects every one. When the user sends a message and the router clears the pause, it neither invalidates the matcher nor replays that user’s pending rows. Depending on whether some unrelated invalidation occurred while paused, the user either keeps matching with an ever-growing backlog or remains absent from the matcher after resuming.

   Concrete fix: never cancel/await a worker from that same worker. Have `_process()` return a “stop worker” outcome, drain the queue safely, and let `_run_worker()` exit normally. Add pause/resume hooks: pausing must invalidate the matcher; resuming must invalidate it again and trigger a bounded replay for that user. Decide explicitly whether queued pre-block rows are retried or marked failed, and update every row accordingly.

4. **A failed digest marks only its first item; all other drained items become permanently unqueued pending rows.**  
   Files: [delivery.py:256](/Users/L/Code/ddiver/ddiver/delivery.py:256), [delivery.py:291](/Users/L/Code/ddiver/ddiver/delivery.py:291), [delivery.py:342](/Users/L/Code/ddiver/ddiver/delivery.py:342)

   Failure scenario: five rows form a digest. Sending the digest raises `MessageTooLongError` or exhausts transient retries. `_send()` receives only `batch[0]`, so only that row is marked failed. The other four were removed from the queue and had `task_done()` called, but remain `pending` in SQLite with no mechanism to enqueue them again. I reproduced statuses `sent, failed, pending, pending, pending` with an empty queue.

   Concrete fix: make `_send()` operate on a batch identity or return a structured result without changing one row. On permanent failure, atomically mark every batch row failed or requeue every row. On pause errors, apply the same policy to the entire batch. Add a test that fails the digest RPC and asserts the status of every member and the subsequent retry behavior.

5. **Digest threshold `0` does not disable digests.**  
   Files: [delivery.py:256](/Users/L/Code/ddiver/ddiver/delivery.py:256), [tests/test_delivery.py:287](/Users/L/Code/ddiver/tests/test_delivery.py:287)

   Failure scenario: a user sets `digest_threshold=0`. The expression `user.digest_threshold or self._default_threshold` substitutes the configured default because zero is falsey. A burst is therefore collapsed into a digest despite the user explicitly disabling digests. With a default threshold of 3 and a five-item held burst, I reproduced two sends: one notification and one four-match digest.

   Concrete fix: pass `user.digest_threshold` directly; use `None`, not zero, to represent “inherit default” if inheritance is required. The test must hold the bot and enqueue more items than the default threshold; its current sequential sends never create enough backlog, so it passes without exercising the decision.

6. **The digest length budget is not enforced and can produce invalid Telegram messages.**  
   Files: [formatter.py:214](/Users/L/Code/ddiver/ddiver/formatter.py:214), [delivery.py:336](/Users/L/Code/ddiver/ddiver/delivery.py:336)

   Failure scenario: the loop checks the current length only before adding a whole chat. One chat can then append five entries containing 300-character rule patterns, long sender names, and snippets. With valid field limits, two such chat sections rendered to 5,340 UTF-16 units in my check, above Telegram’s 4,096-unit limit. Telegram raises `MessageTooLongError`; it is not included in `_is_rendering_error()`, and even a plain-text resend would still be too long. This then triggers the stranded-batch defect above.

   Concrete fix: budget every heading and item before appending it, truncate rule/sender/snippet fields by UTF-16 units, reserve space for “more” and overflow lines, and perform a final `<=4096` assertion/truncation. Add a maximal-field digest test using UTF-16 length, not Python `len()`.

7. **Unsubscribing and later resubscribing can deliver messages from the period when the user was not subscribed.**  
   Files: [storage.py:497](/Users/L/Code/ddiver/ddiver/storage.py:497), [listener.py:157](/Users/L/Code/ddiver/ddiver/listener.py:157), [catchup.py:79](/Users/L/Code/ddiver/ddiver/catchup.py:79)

   Failure scenario: the last active subscriber leaves a chat when the stored cursor is 100. Messages 101–200 arrive while no one is subscribed, so the listener ignores them and does not advance the cursor. The user resubscribes, and reconciliation runs before a newer live update is flushed. Catch-up reads from 100 and applies the user’s current rules to messages 101–200, sending notifications for messages that predate the subscription.

   Concrete fix: preserve per-user subscription boundaries and suppress catch-up matches whose dates precede `subscriptions.created_at`, or advance/reset the chat cursor when monitoring transitions from zero active subscribers to one. Add tests for unsubscribe → history gap → resubscribe, including the case where another user remains subscribed.

## Reconnect, catch-up, and chat lifecycle

8. **The reconnect event is set but never consumed while the listener is running, so reconnect gaps can be lost permanently.**  
   Files: [clients.py:38](/Users/L/Code/ddiver/ddiver/clients.py:38), [runtime.py:450](/Users/L/Code/ddiver/ddiver/runtime.py:450)

   Failure scenario: the stored cursor is 100, the connection drops, and messages 101–300 are missed. `_handle_auto_reconnect()` sets `reconnected`, but `_run_listener()` checks it only before entering `run_until_disconnected()`, which remains active across automatic reconnects. Message 301 then arrives live and the cursor is flushed to 301. The 15-minute periodic catch-up starts at 301 and can never see 101–300.

   Concrete fix: run a tracked background task that waits on `user_client.reconnected` and immediately reconciles while live cursor advancement for affected chats is blocked. Alternatively, make the reconnect callback schedule a serialized reconciliation directly. The event must be consumed during `run_until_disconnected()`, not only after it returns. Add a test that sets the event while `run_until_disconnected()` remains pending and then delivers a higher live ID.

9. **Basic-group-to-supergroup migration is never wired to a real Telethon event.**  
   Files: [listener.py:97](/Users/L/Code/ddiver/ddiver/listener.py:97), [runtime.py:284](/Users/L/Code/ddiver/ddiver/runtime.py:284), [storage.py:465](/Users/L/Code/ddiver/ddiver/storage.py:465), [tests/test_storage.py:217](/Users/L/Code/ddiver/tests/test_storage.py:217)

   Failure scenario: Telegram emits a `MessageService` containing `MessageActionChatMigrateTo`. Telethon 1.45’s `events.NewMessage.build()` explicitly does not construct a NewMessage event for `MessageService`, and neither registered raw/channel nor `ChatAction` handler handles migration. `Storage.migrate_chat()` is therefore never called. Subscriptions and rules remain on the old basic-group ID; messages in the new supergroup fail `matcher.is_monitored()` and produce no notifications.

   Concrete fix: register a raw handler for `UpdateNewMessage`/`UpdateNewChannelMessage`, recognize both migration action directions, obtain the new entity/access hash from the update’s attached entities, call `migrate_chat()` transactionally, invalidate both chat IDs, update the listener cache, and notify subscribers. The existing test only calls the storage method directly; add a runtime-level test built from the raw Telethon update.

10. **Once a chat is marked unavailable, the advertised recovery paths do not exist.**  
    Files: [storage.py:544](/Users/L/Code/ddiver/ddiver/storage.py:544), [listener.py:153](/Users/L/Code/ddiver/ddiver/listener.py:153), [catchup.py:80](/Users/L/Code/ddiver/ddiver/catchup.py:80)

    Failure scenario: catch-up marks a chat `available=0`. The account is later re-added while the bot is offline, so no `ChatAction` update is observed. On restart, `monitored_chat_ids()` excludes the chat, catch-up iterates only monitored chats, and a later live message is rejected at `is_monitored()` before the listener can refresh metadata or mark it available. The chat remains disabled forever.

    Concrete fix: periodically probe unavailable subscribed chats using their stored input peers, independently of the monitored set. Also treat an attached live group/channel message as proof of access before the monitored fast-path rejection: set `available=1`, invalidate, and then process it.

11. **Catch-up ignores the stored channel access hash and passes a bare marked integer to Telethon.**  
    Files: [catchup.py:168](/Users/L/Code/ddiver/ddiver/catchup.py:168), [runtime.py:216](/Users/L/Code/ddiver/ddiver/runtime.py:216)

    Failure scenario: SQLite contains `chats.access_hash`, but the Telethon session’s entity cache no longer contains that channel—for example, the session entity cache was recreated while the application database was retained. `iter_messages(-100…)` invokes Telethon 1.45 input-entity resolution. Without a cached entity it eventually tries a channel lookup with access hash zero and can raise `ValueError`. Catch-up classifies that as lost access and marks the still-accessible chat unavailable, disabling all its notifications.

    Concrete fix: have catch-up load the `ChatRow` and pass `InputPeerChannel(bare_id, access_hash)` for channels/supergroups, or `InputPeerChat` for basic groups. Treat a missing access hash separately from an actual `ChannelPrivateError`; do not mark the chat unavailable merely because local entity resolution failed. Add a test whose fake rejects integer peers but accepts the constructed `InputPeerChannel`.

12. **The initial catch-up task is untracked and can outlive application shutdown.**  
    Files: [runtime.py:239](/Users/L/Code/ddiver/ddiver/runtime.py:239), [runtime.py:269](/Users/L/Code/ddiver/ddiver/runtime.py:269)

    Failure scenario: shutdown begins while `catchup-boot` is iterating history or sleeping in Telethon. `close()` stops only `PeriodicTask` instances, disconnects clients, drains delivery, and closes SQLite, but never cancels or awaits `catchup-boot`. The task can subsequently continue into listener/storage operations against a closed database, or remain alive in a retry/flood-wait path. Any exception becomes “Task exception was never retrieved.”

    Concrete fix: store the boot catch-up task, cancel and await it before flushing cursors and closing the database, or put all application-owned tasks in an `asyncio.TaskGroup`. Ensure `_catch_up_chat()` unblocks cursors in a `finally` block.

## Event-loop hangs and Telethon hot-path misuse

13. **All regular-expression matching runs synchronously on the event-loop thread.**  
    Files: [matcher.py:106](/Users/L/Code/ddiver/ddiver/matcher.py:106), [matcher.py:138](/Users/L/Code/ddiver/ddiver/matcher.py:138)

    Failure scenario: an allow-listed hostile moderator installs hundreds of regex rules that each consume the allowed 20 ms on a chosen input. `evaluate()` awaits `_search_all()`, but `_search_all()` performs the synchronous `regex.finditer()` call directly; there is no thread handoff. Five hundred rules can block the only event loop for roughly ten seconds per message. With Telethon’s concurrent update dispatch, incoming update tasks accumulate during a burst, delaying delivery, bot controls, heartbeat, and cursor flush and causing unbounded task/memory growth.

    Concrete fix: offload the entire regex batch for a message to a bounded dedicated executor as specified by the design, with a bounded queue and an overall per-message budget. Do not call `to_thread` once per rule. Add an event-loop responsiveness test with more than the offload threshold and a ticker coroutine that must continue running.

14. **The listener performs user-session entity RPCs inside matched-message handlers.**  
    Files: [runtime.py:175](/Users/L/Code/ddiver/ddiver/runtime.py:175), [senders.py:137](/Users/L/Code/ddiver/ddiver/senders.py:137), [senders.py:173](/Users/L/Code/ddiver/ddiver/senders.py:173), [listener.py:170](/Users/L/Code/ddiver/ddiver/listener.py:170)

    Failure scenario: a matched update lacks an attached sender entity. `sender_of()` calls the configured resolver, which is `user_client.get_entity()`, under a five-second timeout. A burst of different uncached senders queues handler tasks behind the resolver lock and performs one listener-session RPC after another. If resolution is throttled or fails, the sender becomes `None`; a bot sender is then not recognized as a bot and can generate a notification even though bot senders must be dropped.

    Concrete fix: remove the network resolver from the live listener path. Use only `message.sender`/attached update entities and a previously populated cache. If background enrichment is desired, enqueue it outside the message handler and do not advance delivery based on unresolved bot status; either conservatively skip unknown senders or persist an unresolved item for bounded later classification.

## Delivery/task lifecycle

15. **Cancelling a busy delivery worker calls `task_done()` twice and can make shutdown fail.**  
    Files: [delivery.py:197](/Users/L/Code/ddiver/ddiver/delivery.py:197), [delivery.py:228](/Users/L/Code/ddiver/ddiver/delivery.py:228)

    Failure scenario: the bot RPC is blocked when shutdown’s `wait_idle()` times out. `drain()` cancels the worker. The `CancelledError` branch calls `state.queue.task_done()`, then `finally` calls it again, raising `ValueError: task_done() called too many times`. `drain()` suppresses only `CancelledError`, so the exception escapes `Application.close()` and can prevent the bot disconnect and database close. I reproduced this with a held fake bot and a short drain timeout.

    Concrete fix: call `task_done()` exactly once, exclusively in `finally`. Preserve the currently held row as pending when cancelled. Expand the shutdown test to hold the bot until timeout and assert that `drain()` returns normally and the row remains pending.

16. **Fatal authentication does not enter degraded mode; it exits the whole application.**  
    Files: [app.py:86](/Users/L/Code/ddiver/ddiver/app.py:86), [runtime.py:256](/Users/L/Code/ddiver/ddiver/runtime.py:256), [tests/test_app.py:97](/Users/L/Code/ddiver/tests/test_app.py:97)

    Failure scenario: the user session raises `SessionRevokedError`. The supervisor sets `state.degraded`, sends one owner notification, and immediately returns `EXIT_OK`. `Application.run()` returns, `cmd_run` closes the bot, delivery, and database, and Docker’s `restart: unless-stopped` starts the container again regardless of the zero exit code. This produces exactly the restart/re-notification loop the design says degraded mode prevents; the bot UI and workers are not kept alive.

    Concrete fix: after entering degraded state, remain in a stop-aware wait loop while the bot client, delivery workers, heartbeat, and periodic six-hour owner reminder continue running. Exit only on an explicit shutdown signal. The current test is asserting the wrong behavior: `await sup.run() == EXIT_OK` proves that it exited. Rewrite it to start `run()` as a task, assert the task remains pending after fatal auth, advance six hours to verify the reminder, then request stop and await `EXIT_OK`.

17. **The health check stays green when the listener is disconnected or its ping has failed indefinitely.**  
    Files: [runtime.py:389](/Users/L/Code/ddiver/ddiver/runtime.py:389), [runtime.py:392](/Users/L/Code/ddiver/ddiver/runtime.py:392), [health.py:13](/Users/L/Code/ddiver/ddiver/health.py:13), [tests/test_health.py:6](/Users/L/Code/ddiver/tests/test_health.py:6)

    Failure scenario: the user client enters an infinite reconnect loop or every `GetStateRequest` ping fails. The independent heartbeat task continues writing the current timestamp, and `cmd health` checks only that timestamp. `_last_ping_ok` is assigned but never written or evaluated. Docker therefore reports healthy indefinitely while the listener cannot receive messages.

    Concrete fix: atomically write structured heartbeat data containing heartbeat time, last successful ping, transport/reconnecting state, last update, degraded status, and queue length. `cmd health` must reject stale pings and excessive update silence while allowing explicitly degraded mode according to the documented policy. Tests must cover a fresh file whose `ping_ok_at` is stale or whose transport is reconnecting.

## Multi-user security and privacy

18. **A moderator can retain a subscription to a chat while the owner unshares it.**  
    Files: [bot/handlers.py:216](/Users/L/Code/ddiver/ddiver/bot/handlers.py:216), [storage.py:486](/Users/L/Code/ddiver/ddiver/storage.py:486), [storage.py:503](/Users/L/Code/ddiver/ddiver/storage.py:503)

    Failure scenario: callbacks run concurrently. A moderator’s forged `sub:<chat>:1` handler reads `shared=True` and pauses. The owner sets `shared=False`; `unsubscribe_all()` selects no moderator row. The moderator resumes. `Storage.subscribe()` rereads the now-unshared chat but checks only its kind, not `shared`, and inserts the subscription after the owner’s deletion. The hostile moderator is now subscribed to a chat the owner did not share.

    Concrete fix: enforce authorization atomically in the storage insert: `INSERT … SELECT … FROM chats WHERE id=? AND kind IN (…) AND (shared=1 OR ?=OWNER_ID)`. Make unshare plus deletion one transaction. Do not rely on the handler’s earlier read. Add a deterministic interleaving test with barriers around the owner’s unshare and moderator’s insert.

19. **Forged rule callbacks reveal unshared chat metadata.**  
    Files: [bot/handlers.py:350](/Users/L/Code/ddiver/ddiver/bot/handlers.py:350), [bot/handlers.py:367](/Users/L/Code/ddiver/ddiver/bot/handlers.py:367), [bot/handlers.py:384](/Users/L/Code/ddiver/ddiver/bot/handlers.py:384)

    Failure scenario: the owner has an unshared private family supergroup in `chats`. An allow-listed moderator who knows or obtains its marked ID sends `rules:<id>:include:0`. `rules_view()` scopes the rule query to the moderator, but then calls unrestricted `get_chat()` and returns the hidden chat’s title. `radd:<id>:include` also accepts the hidden ID and creates chat-specific rules without verifying subscription or sharing. Thus the “moderators never see the owner’s dialog list” boundary is bypassed by forged callback data.

    Concrete fix: centralize a `can_access_chat(actor, chat_id)` check and require either owner status or both `shared=1` and an actor-owned subscription for all chat-scoped rule, ignore, and custom-exclusion callbacks. Return the same generic not-found response for inaccessible and nonexistent IDs. For notification actions, bind the action to the actor’s own outbox row; the ignore callback currently lacks a message ID, so include one or use an opaque action record.

## Tests that currently pass vacuously or assert the wrong behavior

20. **The owner-only mutation assertion is literally always true.**  
    File: [tests/test_bot.py:121](/Users/L/Code/ddiver/tests/test_bot.py:121)

    Failure scenario: `assert (await store.get_user(STRANGER)) is None or True` passes for every possible database state. A regression in which a moderator’s `allow:`, `deny:`, `revoke:`, or `share:` callback mutates state and then returns the expected toast would pass this test.

    Concrete fix: snapshot the relevant user role, chat sharing flag, subscriptions, rules, and ignores before each parameterized callback, then assert exact equality afterward. Use separate expected-state checks for each payload rather than one unrelated stranger lookup.

21. **The unsharing test does not test that a notice was sent.**  
    File: [tests/test_bot.py:209](/Users/L/Code/ddiver/tests/test_bot.py:209)

    Failure scenario: the test discards the actions returned by `share:<chat>:0`, then calls `shared:0` and asserts that the listing action contains no `Send`. Removing the notification loop from the unshare handler would still pass.

    Concrete fix: retain the unshare callback’s returned actions and assert exactly one `Send` targeting the dropped moderator with the expected localized chat title. Keep the subscription-deletion assertion separately.

22. **The queue-overflow test treats stranded pending rows as successfully handled.**  
    File: [tests/test_delivery.py:295](/Users/L/Code/ddiver/tests/test_delivery.py:295)

    Failure scenario: the test verifies the overflow counter and the digest text but never checks the three overflowed outbox rows. They remain `pending` with an empty queue, which is the loss/delay defect described above, yet the test passes.

    Concrete fix: after settling, assert the status of every claimed row and assert that no pending row is left without a queued/leased retry. If overflow is represented only as a count in a delivered digest, atomically mark the corresponding rows digested and assert that mapping.

23. **The blocked-user test misses both the orphan worker and the unrecoverable queued row.**  
    File: [tests/test_delivery.py:173](/Users/L/Code/ddiver/tests/test_delivery.py:173)

    Failure scenario: the test enqueues two rows but asserts only the first row’s failed status and `queue_length()==0`. Because `drop_user()` removes the queue dictionary entry, that length is zero even while an orphaned `delivery-<uid>` task is waiting forever and the second row remains pending with no queue.

    Concrete fix: assert the second row’s explicit status/retry ownership, enumerate named tasks to ensure no worker remains after pause, then simulate an inbound user update and verify that pending delivery is replayed or intentionally finalized.

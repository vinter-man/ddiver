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
- On fatal auth the process does **not** exit into a restart loop. It enters **degraded mode**
  and *stays in it*: the listener stops, the bot client and workers keep running, `/status` says
  "listener needs re-login", the owner is notified once and then every 6 h, and `ddiver health`
  reports degraded with exit 0 so Docker does not restart it. Returning from `run()` would not
  be enough — `restart: unless-stopped` ignores the exit code.
- A run that lasted at least a minute resets the backoff; a listener that fails immediately five
  times in a row is reported as unhealthy (`/status` and the heartbeat) while retrying.
- Every retry reconnects first: `run_until_disconnected()` always disconnects in its `finally`,
  so re-entering it on the same client would raise "Cannot send requests while disconnected"
  forever.
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
- Matching is a pure function of (rule set, text, sender). Above 20 rules for a chat the whole
  evaluation moves to a worker thread (`regex` releases the GIL — verified: 8.4 M loop iterations
  during a 1 s match in a thread), and a 500 ms per-message budget stops a pathological rule set
  from holding either the loop or the thread.

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
  pending rows are re-queued before catch-up runs, and a rescan every 5 minutes picks up rows
  that could not be queued earlier (a full queue, a paused user) — without it they would wait
  for the next restart. Semantics are at-least-once: a crash between the send and the status
  update re-sends. Rows are pruned after 7 days in batches.
- Nothing is ever left in a state nobody owns: an overflowed row is marked `failed` immediately,
  a failed digest marks its whole batch, and dropping a user's queue leaves the rows `pending`
  for the rescan. `drop_user` never cancels the worker it was called from.

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
- Catch-up holds back the cursor of *every* chat in the round, not only the one being replayed,
  so a live message elsewhere cannot flush a cursor past a gap that has not been examined yet.
  Per chat:
  `iter_messages(peer, min_id=cursor, limit=CATCHUP_MAX_PER_CHAT)` — **newest first**, the
  iterator stops natively at `id <= min_id`; break when `date < now - CATCHUP_HOURS`; buffer and
  then process in chronological order with `catchup=True`. If the cap was hit, the first
  notification for that chat says "N older messages were not checked". `yield` every 20 messages;
  per-chat `try/except` for `FloodWaitError` (requeue after `e.seconds`), `ChannelPrivateError`,
  `ChatForbiddenError` and `ValueError` (missing entity); global cap per round.
  `InputPeerChannel(id, access_hash)` is built from the `chats` row, so a fresh session does not
  break the backfill; a `ValueError` from local entity resolution is treated as transient, not as
  lost access. Chats with no cursor start "now" (no backfill on first subscribe), and a
  subscriber never receives a message older than their own subscription.
- Reconnects: `DdiverClient` overrides `_handle_auto_reconnect` to call `super()` and then set a
  reconcile event. This is necessary because Telethon requests `getChannelDifference` with
  limit 100 for user accounts and, on `ChannelDifferenceTooLong`, jumps `pts` and returns **no**
  updates — at 100k msgs/day a busy chat loses everything beyond 100 messages per gap with no
  signal (verified in `_updates/messagebox.py`). A periodic reconciliation (every 15 min)
  compares each chat's head id with the cursor and backfills the difference.
- Access loss is detected live: `events.Raw(UpdateChannel)` (primary) and `events.ChatAction`
  for self kick/leave (matched against *every* affected user id, not just the first) →
  `available=0`, subscribers notified once on the 1→0 transition. A background task re-probes
  unavailable chats every 6 h and flips them back when they can be read again, so one transient
  error never disables a chat permanently.
- Basic group → supergroup migration (`MessageActionChatMigrateTo`) arrives as a *service*
  message, which `events.NewMessage` never fires for, so it is handled through a raw
  `UpdateNewMessage`/`UpdateNewChannelMessage` handler: subscriptions, chat rules and ignores
  are moved to the new marked id, the old chat is marked unavailable, subscribers told.

## Bot UI

Authorisation: every chat-scoped callback goes through one `accessible_chat` gate — the owner
reaches any known chat, a moderator only a chat that is shared *and* that they subscribe to, and
everything else gets the same "not found" as a chat id that does not exist. One catch-all
`NewMessage` handler and one catch-all `CallbackQuery` handler,
both limited to private chats, both running `authorize(event.sender_id)` first. Active =
owner or `role='allowed'`. A stranger gets one reply and the owner one card (`notified_owner_at`
throttles to once per 24 h); denied users get silence; unauthorised callbacks are only answered.
`allow:`/`deny:`/`revoke:` additionally require the sender to be the owner.

Sharing model: moderators never see the owner's dialog list. The owner has a "🗂 Shared chats"
menu that toggles `chats.shared`; moderators' picker and search operate on shared rows only, as
a DB substring search — they never trigger `get_entity`/`ResolveUsername` on the user session.
Unsharing deletes their subscriptions for that chat and tells them.

Flows: `/start` → language (seeded from `User.lang_code`) if `lang IS NULL` → timezone if
`tz IS NULL` (quick buttons, and the prompt arms the pending-input state so the free text it
invites is actually accepted; suggestions and a
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
  `reconnecting`, `degraded`, `queue`) via a temp file + `replace`. `is_connected()` stays true
  during Telethon's infinite reconnect loop (verified), so the real signals are the transport
  flag and a 60 s `GetStateRequest` ping under `wait_for(15)`. `ddiver health` fails when the
  file is stale, the transport is down or the last successful ping is older than 3 minutes — and
  reports healthy in degraded mode, because restarting cannot fix a session that needs a login. If no update arrives for 10
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

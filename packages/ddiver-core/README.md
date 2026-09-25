# ddiver-core

**Telegram delivery and everything before it.** Rules and matching, storage, the bot UI,
delivery and rendering, and the process around them, taken out of
[ddiver](../../README.md) so that a second app can reuse them. ddiver is the first app on it: a
Telethon user session that listens in Telegram chats. ddiver-fb, which watches Facebook groups,
is meant to be the second.

Three things this package is, stated plainly because the name could suggest otherwise:

- **It depends on Telethon, for the bot side.** Notifications and the configuration UI go
  through a Telegram bot, driven by Telethon: `botclient` builds the bot's client,
  `botruntime.BotAdapter` turns the router's actions into Bot API calls, `richtext` builds
  Telethon `MessageEntity` objects, `links` derives `t.me` links from Telegram ids, and
  `tgerrors` classifies Telethon's errors.
- **It contains no user-session I/O.** Nothing here signs in as a person, opens, reads or writes
  a user session, reads a chat's history or listens to a chat. Where the core needs something
  only a user session knows, it asks through a port the app backs (below). In ddiver, that I/O
  lives in `ddiver.listener`, `ddiver.catchup`, `ddiver.clients`, `ddiver.login` and
  `ddiver.runtime`.
- **It is not transport-agnostic.** It delivers to Telegram and nowhere else. An app whose
  *source* is something other than Telegram still delivers through a Telegram bot; that is the
  only reuse it is built for.

## Modules

```
ddiver_core/
  config.py      Settings every app has: bot token, api_id/api_hash (the bot signs in over
                 MTProto), owner, data dir, logging, metrics, ping, digest threshold, regex timeout
  process.py     one process per data dir, a 0700 data dir, JSON log lines, config errors
  app.py         Supervisor (restart policy, degraded mode), PeriodicTask, boot-rate guard
  health.py      heartbeat file behind `<app> health` and the Docker HEALTHCHECK
  watchdog.py    dead-man's switch ping (PING_URL)
  metrics.py     hand-rolled Prometheus exposition
  botclient.py   the bot's Telethon client, and a connect that cannot hang
  botruntime.py  BotAdapter: router actions and buttons as Telethon calls
  tgerrors.py    Telethon errors after which restarting cannot help
  storage.py     aiosqlite, WAL, one connection; the core schema on user_version, app tables on
                 named migration tracks
  rules.py       rule syntax (word, =word, re:, from:) and compiled matching
  matcher.py     per-chat rule sets for every subscribed user, invalidation, timeouts
  senders.py     sender identity from update entities; SenderCache over an EntityResolver
  links.py       t.me links for chats, messages and senders
  richtext.py    text plus MessageEntity objects with UTF-16 offsets
  formatter.py   Payload (the outbox snapshot) and the notification and digest renderers
  delivery.py    outbox consumption: per-user queues, buckets, the shared flood gate, digests
  portable.py    /export and /import, tagged with the app's format
  timeutil.py    time zones, timestamps, relative time
  i18n/          en/ru/uk catalogs, t(), and the overlays an app lays over them
  bot/           router (authorisation, pending input), menus, handlers
```

Nothing in `ddiver_core` imports `ddiver`.

## Ports an app backs

- **`senders.EntityResolver`**: given a sender id (and the chat it wrote in), return the
  Telethon entity, so a notification can name somebody the update did not describe. **ddiver
  backs it with its user session** (`Application._resolve_entity`, `get_entity` and, for a
  basic group, the member list), and only the delivery worker uses it, rate-limited and cached;
  the listener's `SenderCache` has no resolver, because a message handler must never make an
  RPC. An app with no Telegram senders passes `senders=None` to `Delivery`, and `Delivery` never
  asks the resolver about a payload whose source is not Telegram.
- **`delivery.BotPort`**: what `Delivery` sends through. In production that is `BotAdapter`
  over the bot client; in tests, `FakeBotClient`.
- The source itself. The core has no listener: an app finds messages, matches them with
  `Matcher`, writes a `formatter.Payload` to the outbox with `Storage.claim_delivery` *before*
  it moves past the message, and hands the row to `Delivery`. ddiver's listener and catch-up
  are that code for Telegram.

## What an app may change

With every default, the core behaves exactly as ddiver did before the extraction.

- **Payload links and source.** A payload says where it came from (`source`, `"tg"` by
  default) and may carry its own `url`, `chat_url` and `sender_url`, plus `chat_private` and
  `late`. Telegram links are derived from ids only for a Telegram source; any other source gets
  the links it carries or none. A carried link that is not a plain `http(s)` address, contains
  whitespace or an invisible character, or is longer than 2048 characters is dropped. The new
  fields are written to the outbox only when they differ from their defaults.
- **Strings.** `i18n.register_overlay(lang, catalog)` lays an app's wording over the base
  catalog of a language and adds keys of its own, checked when it is registered.
- **Tables.** `Storage(..., tracks={"fb": [...]})` versions an app's tables on a named track in
  `schema_tracks`, applied after the core's migrations. `PRAGMA user_version` stays the core's.
  A database opened without tracks, which is ddiver's, never gets the ledger table.
- **Exports.** `build_router(..., export_format="ddiver-fb.config")` tags /export with the app
  and makes /import refuse another app's file. The default is `ddiver.config`.
- **Bot UI.** `build_router(..., hooks=UiHooks(...))` hides the Telegram-only and the weekly
  settings, adds an owner-only refresh button to the chat lists, and replaces /status. Commands
  of the app's own go on the returned router, with `Router.command(name, owner_only=True)` for an
  operator command.
- **Settings.** An app subclasses `config.Settings` for its own fields; ddiver's subclass adds
  the user session and catch-up.

## Tests

The core's own tests are in the ddiver repository, under `tests/core`. The fakes they share with
the apps' tests (`FakeClock`, `FakeBotClient`, `SentMessage`, `T0`) are in the separate
workspace package `ddiver-core-testing` (`import ddiver_core_testing`), a dev dependency that is
never installed in an image.

"""The composition root: builds both clients, registers their handlers, owns the tasks.

Other modules import Telethon *types* freely — a message is a message. What lives only here
is the wiring: constructing the two clients, subscribing the event handlers, and owning every
background task. Each layer below declares the narrow Telegram surface it needs as a port
(``BotPort``, ``History``, ``EntityResolver``), which this module satisfies with a real client
and the tests satisfy with a fake. That is why everything except this file, ``clients.py``,
``login.py`` and the CLI can be exercised without a connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import os
from typing import Any

from telethon import TelegramClient, errors, events, utils
from telethon.tl import types as tl
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.functions.updates import GetStateRequest

from ddiver.catchup import Catchup, telethon_history
from ddiver.clients import make_bot_client, make_user_client
from ddiver.config import Settings
from ddiver.listener import CURSOR_FLUSH_SECONDS, Listener, chat_ref_of
from ddiver_core import portable
from ddiver_core.app import PeriodicTask, Supervisor, boot_delay, format_uptime
from ddiver_core.bot.handlers import StatusInfo, build_router
from ddiver_core.bot.router import Incoming, Router, Toast
from ddiver_core.botclient import connect_with_timeout
from ddiver_core.botruntime import BotAdapter, notification_buttons_factory, self_id_of
from ddiver_core.delivery import Delivery
from ddiver_core.formatter import render_weekly
from ddiver_core.health import Heartbeat, write_heartbeat
from ddiver_core.i18n import DEFAULT_LANG, LANGS, t
from ddiver_core.links import ChatRef
from ddiver_core.matcher import Matcher
from ddiver_core.metrics import MetricsServer, MetricsSource
from ddiver_core.senders import SenderCache
from ddiver_core.storage import Rule, Storage, User, safe_write
from ddiver_core.timeutil import Clock, to_zone, utcnow
from ddiver_core.watchdog import Watchdog

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 30.0
PING_SECONDS = 60.0
PING_TIMEOUT = 15.0
RECONCILE_SECONDS = 15 * 60
SUMMARY_SECONDS = 60.0
PRUNE_SECONDS = 24 * 3600
REPROBE_SECONDS = 6 * 3600
RESCAN_SECONDS = 300
WEEKLY_SECONDS = 3600
"""How often to look for somebody whose week is up. The report itself is weekly; this is the
resolution at which "ten in the morning, their time" can be honoured."""

WEEKLY_HOUR = 10
"""Local hour to send at. Not a weekday: a box that was down on Monday morning would then owe
the report until the following Monday, and the point of it is that it arrives."""

WEEKLY_WINDOW = dt.timedelta(days=7)
WEEKLY_MAX_WINDOW = dt.timedelta(days=14)
"""How far back one report may reach when the last one was long ago. `stats_daily` is pruned at
ten weeks, so a wider window would start reporting on rows that are no longer all there."""
SILENCE_WARN = dt.timedelta(minutes=10)
SILENCE_RETRY = dt.timedelta(minutes=30)


def _local_time(moment: dt.datetime, tz: str | None) -> dt.datetime:
    """The moment where this person is. A tz we cannot read falls back to UTC rather than
    raising: one bad row must not stop everybody else's report."""
    if not tz:
        return moment.astimezone(dt.UTC)
    try:
        return to_zone(moment, tz)
    except Exception:  # noqa: BLE001 - zoneinfo raises several unrelated types
        return moment.astimezone(dt.UTC)


class Application:
    """Everything wired together, with a lifecycle the CLI drives."""

    def __init__(self, settings: Settings, *, clock: Clock = utcnow) -> None:
        self.settings = settings
        self.clock = clock
        self.storage = Storage(
            settings.db_path,
            owner_id=settings.owner_id,
            clock=clock,
            digest_threshold=settings.digest_threshold,
        )
        self.user_client = make_user_client(settings)
        self.bot_client = make_bot_client(settings)
        self.bot = BotAdapter(self.bot_client)
        self.matcher: Matcher | None = None
        self.listener: Listener | None = None
        self.delivery: Delivery | None = None
        self.catchup: Catchup | None = None
        self.router: Router | None = None
        self.supervisor: Supervisor | None = None
        self.watchdog = Watchdog(
            settings.ping_url,
            timeout=settings.ping_timeout,
            min_interval=settings.ping_interval,
            clock=clock,
        )
        self.metrics_server: MetricsServer | None = None
        # No resolver: the listener must never make an RPC from a message handler. Entities
        # attached to updates carry everything a notification needs most of the time.
        self.senders = SenderCache(None, clock=clock)
        # Delivery runs off the ingestion path, so it may resolve the names the listener could
        # not — rate-limited to one lookup per second and cached for an hour.
        self.delivery_senders = SenderCache(self._resolve_entity, clock=clock)
        self._tasks: list[PeriodicTask] = []
        self._owned: list[asyncio.Task[Any]] = []
        self._stop_task: asyncio.Task[None] | None = None
        self._last_ping_ok: dt.datetime | None = None
        self._last_update_at: dt.datetime | None = None
        self._silence_probe_at: dt.datetime | None = None
        self._members_loaded: set[int] = set()
        self._lifetimes = 0
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
            buttons=notification_buttons_factory(),
            senders=self.delivery_senders,
            on_user_state_changed=self._invalidate,
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
            on_recovered=self._notify_regained_access,
            on_available=self._invalidate,
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
        """Shut down in an order the next start can pick up from."""
        if self.metrics_server is not None:
            await self.metrics_server.stop()
            self.metrics_server = None
        for task in self._tasks:
            await task.stop()
        for owned in self._owned:
            owned.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await owned
        self._owned.clear()
        # Stop ingesting before flushing: a message arriving after the cursor is written
        # would be skipped on the next start rather than merely delayed.
        with contextlib.suppress(Exception):
            await self.user_client.disconnect()
        # Everything from here on is best-effort, and each step is isolated from the next.
        # `flush_hits` is a bare `executemany`: on a full disk it used to unwind `close()`
        # itself, skipping the delivery drain, the bot disconnect and `storage.close()` — and
        # since aiosqlite's worker thread is not a daemon, the process then hung at exit with
        # the container still reported healthy. Counting rule hits is the least important
        # thing here; it must not be the thing that decides whether the rest runs.
        if self.listener is not None:
            with contextlib.suppress(Exception):
                await self.listener.cursors.flush()
        if self.listener is not None:
            with contextlib.suppress(Exception):
                await self.listener.flush_stats()
        if self.matcher is not None:
            try:
                await self.matcher.flush_hits()
            except Exception:
                log.exception("rule-hit counters were not written")
        if self.delivery is not None:
            try:
                left = await self.delivery.drain()
            except Exception:
                log.exception("delivery drain failed during shutdown")
            else:
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
        log.info("ddiver starting, build %s", os.environ.get("DDIVER_COMMIT", "unknown")[:12])
        await self._publish_commands()
        boots = await self.storage.record_boot()
        assert self.delivery is not None  # noqa: S101
        await self.delivery.replay_pending()
        await self._start_metrics()
        self._start_background_tasks()
        self._spawn(self._boot_catch_up(boots), "catchup-boot")
        self._spawn(self._watch_reconnects(), "reconnect-watch")
        return await self.supervisor.run()

    async def _publish_commands(self) -> None:
        """Register the command list so Telegram offers it when a moderator types "/".

        Purely cosmetic, and Telegram remembers it, so a failure here must never keep the
        bot from starting.
        """
        commands = ("start", "menu", "test", "status", "export", "import", "help", "cancel")
        for lang in (*LANGS, ""):
            try:
                await self.bot_client(
                    SetBotCommandsRequest(
                        scope=tl.BotCommandScopeDefault(),
                        lang_code=lang,
                        commands=[
                            tl.BotCommand(
                                command=name,
                                description=t(lang or DEFAULT_LANG, f"bot.cmd.{name}"),
                            )
                            for name in commands
                        ],
                    )
                )
            except (errors.RPCError, ConnectionError, TimeoutError) as exc:
                log.warning("could not publish the command list for %r: %s", lang, exc)

    async def _start_metrics(self) -> None:
        """Serve /metrics, unless the port is 0 — which is the default and means "no server"."""
        if not self.settings.metrics_port:
            return
        supervisor = self.supervisor
        source = MetricsSource(
            listener=self.listener,
            delivery=self.delivery,
            matcher=self.matcher,
            supervisor=supervisor,
            watchdog=self.watchdog,
            transport=self._transport_state,
            uptime=lambda: (
                (self.clock() - supervisor.state.started_at).total_seconds()
                if supervisor is not None and supervisor.state.started_at is not None
                else 0.0
            ),
        )
        server = MetricsServer(source, self.settings.metrics_host, self.settings.metrics_port)
        try:
            await server.start()
        except OSError as exc:
            # A busy port must not keep the listener off the air: observability is a
            # convenience, monitoring the chats is the job.
            log.warning("could not serve metrics on port %d: %s", self.settings.metrics_port, exc)
            return
        self.metrics_server = server

    def _transport_state(self) -> bool | None:
        sender = getattr(self.user_client, "_sender", None)
        if sender is None:
            return None
        with contextlib.suppress(Exception):  # private API; never break a scrape over it
            return bool(sender._transport_connected())  # noqa: SLF001
        return None

    async def _boot_catch_up(self, boots: int) -> None:
        """Catch up once the process has proved it is not crash-looping.

        Only the backfill waits. Listening, delivery and the heartbeat start immediately: a
        silent heartbeat would make an orchestrator's health check restart the container,
        which is the very loop this delay exists to damp.
        """
        assert self.supervisor is not None  # noqa: S101
        await boot_delay(boots, stop=self.supervisor.stop_event)
        if self.supervisor.stop_event.is_set():
            return
        await self._catch_up()

    def _spawn(self, coro: Any, name: str) -> asyncio.Task[Any]:
        """Start a task we own, so shutdown can cancel it instead of leaving it running."""
        task = asyncio.create_task(coro, name=name)
        self._owned.append(task)
        return task

    def request_stop(self) -> None:
        """Called from a signal handler, so nothing here may await.

        Disconnecting is what breaks ``run_until_disconnected`` out of its wait. Telethon's
        ``disconnect()`` only runs the loop itself when the loop is *not* already running;
        under a running loop it returns a coroutine, so calling it plainly would leave the
        listener attached and leak a coroutine that is never awaited. Hand it to the loop
        instead, and keep the task referenced so it cannot be collected mid-flight.
        """
        if self.supervisor is not None:
            self.supervisor.request_stop()
        with contextlib.suppress(RuntimeError):  # no running loop: nothing to disconnect from
            self._stop_task = asyncio.get_running_loop().create_task(self._disconnect_listener())

    async def _disconnect_listener(self) -> None:
        with contextlib.suppress(Exception):
            await self.user_client.disconnect()

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

        @self.user_client.on(events.Raw((tl.UpdateNewMessage, tl.UpdateNewChannelMessage)))
        async def on_raw_message(update: Any) -> None:
            # events.NewMessage never fires for MessageService, and a group upgrading to a
            # supergroup is exactly that: without this the chat keeps its dead id forever.
            await self._on_service_message(getattr(update, "message", None), update)

        @self.bot_client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
        async def on_bot_message(event: Any) -> None:
            document, name = await self._attached_document(event)
            await self._on_bot_update(
                event, text=event.raw_text, document=document, document_name=name
            )

        @self.bot_client.on(events.CallbackQuery(func=lambda e: e.is_private))
        async def on_bot_callback(event: Any) -> None:
            data = event.data.decode("utf-8", "replace") if event.data else ""
            await self._on_bot_update(event, data=data)

    async def _attached_document(self, event: Any) -> tuple[bytes | None, str | None]:
        """Download a small attached file, or return nothing at all.

        Bounded before the download: a moderator who attaches a video must not make the bot
        pull it into memory to find out it is not a configuration.
        """
        document = getattr(getattr(event, "message", None), "document", None)
        if document is None or int(getattr(document, "size", 0) or 0) > portable.MAX_BYTES:
            return None, None
        # Bounded by who, not only by how big. The allow-list gate lives in the router, which
        # runs after this — so without the check here a stranger, or somebody the owner
        # explicitly denied, could make the bot pull a megabyte and only then be answered with
        # silence, once per message and with no limit on how many at a time. Only /import ever
        # reads the result, and the gate already keeps everyone else away from that.
        user = await self.storage.get_user(int(event.sender_id))
        if user is None or not user.active:
            return None, None
        name = None
        for attribute in getattr(document, "attributes", None) or []:
            name = getattr(attribute, "file_name", None) or name
        try:
            content = await self.bot_client.download_media(event.message, file=bytes)
        except (errors.RPCError, ConnectionError, TimeoutError, ValueError):
            log.warning("could not download an attached file", exc_info=True)
            return None, None
        return (content if isinstance(content, bytes) else None), name

    async def _on_bot_update(
        self,
        event: Any,
        *,
        text: str | None = None,
        data: str | None = None,
        document: bytes | None = None,
        document_name: str | None = None,
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
                document=document,
                document_name=document_name,
            )
            actions = await self.router.dispatch(incoming)
            user = await self.storage.get_user(incoming.user_id)
            lang = (user.lang if user else None) or DEFAULT_LANG
            await self.bot.perform(event, actions, lang)
            if data is not None and not any(isinstance(a, Toast) for a in actions):
                # Telegram keeps the button spinning until the query is answered.
                with contextlib.suppress(Exception):
                    await event.answer()
        except Exception:
            log.exception("bot update failed")

    async def _on_chat_action(self, event: Any) -> None:
        try:
            me_id = self_id_of(self.user_client)  # no RPC: invariant 1
            affected = list(getattr(event, "user_ids", None) or [])
            if event.user_id is not None:
                affected.append(event.user_id)
            if me_id is None or me_id not in affected:
                return  # somebody else joined or left; not our membership
            chat_id = int(event.chat_id)
            if event.user_kicked or event.user_left:
                if await self.storage.set_chat_available(chat_id, False):
                    await self._notify_lost_access(chat_id, "left")
            elif event.user_added or event.user_joined:
                if await self.storage.set_chat_available(chat_id, True):
                    await self._notify_regained_access(chat_id)
            else:
                return
            await self._invalidate(chat_id)
        except Exception:
            log.debug("chat action handling failed", exc_info=True)

    async def _on_service_message(self, message: Any, update: Any) -> None:
        action = getattr(message, "action", None)
        if not isinstance(action, tl.MessageActionChatMigrateTo):
            return
        try:
            old_id = int(utils.get_peer_id(message.peer_id))
            new_id = int(utils.get_peer_id(tl.PeerChannel(action.channel_id)))
            entity = (getattr(update, "_entities", None) or {}).get(new_id)
            ref = chat_ref_of(entity) or ChatRef(new_id, str(new_id), None, "supergroup")
            old = await self.storage.get_chat(old_id)
            if old is None:
                return
            log.info("chat %s migrated to supergroup %s", old_id, new_id)
            await self.storage.migrate_chat(old_id, ref, getattr(entity, "access_hash", None))
            await self._invalidate(None)
            if self.listener is not None:
                await self.listener.prime_chat_cache()
            for user_id in await self.storage.subscribers_of(new_id):
                user = await self.storage.get_user(user_id)
                lang = (user.lang if user else None) or DEFAULT_LANG
                await self._tell(user_id, t(lang, "bot.chats.migrated", chat=ref.title))
        except Exception:
            log.exception("could not migrate a chat")

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
            PeriodicTask("ping", self._ping, PING_SECONDS, first_delay=PING_SECONDS),
            # The first ping waits a full interval: at t=0 the listener may still be
            # connecting, and the heartbeat already reports the transport meanwhile.
            PeriodicTask(
                "reconcile",
                self._reconcile,
                RECONCILE_SECONDS,
                first_delay=RECONCILE_SECONDS,
            ),  # the boot catch-up covers t=0, and running both would double the work
            PeriodicTask("summary", self._log_summary, SUMMARY_SECONDS),
            PeriodicTask("prune", self._prune, PRUNE_SECONDS),
            PeriodicTask("reprobe", self._reprobe, REPROBE_SECONDS),
            PeriodicTask("outbox-rescan", self._rescan_outbox, RESCAN_SECONDS),
            PeriodicTask("weekly", self._weekly, WEEKLY_SECONDS, first_delay=WEEKLY_SECONDS),
        ]
        for task in self._tasks:
            task.start()

    async def _flush_state(self) -> None:
        if self.listener is not None:
            await self.listener.cursors.flush()
            await safe_write(self.listener.flush_stats(), "weekly counters")
        if self.matcher is not None:
            await safe_write(self.matcher.flush_hits(), "rule hits")

    async def _heartbeat(self) -> None:
        if self.watchdog.enabled:
            await self.watchdog.ping()
        sender = getattr(self.user_client, "_sender", None)
        transport = self._transport_state()
        reconnecting: bool | None = None
        with contextlib.suppress(Exception):  # private API: never let it break the heartbeat
            reconnecting = bool(getattr(sender, "_reconnecting", False)) if sender else None
        listener = self.listener
        supervisor = self.supervisor
        write_heartbeat(
            self.settings.heartbeat_path,
            self.clock(),
            Heartbeat(
                ts="",  # filled in by write_heartbeat
                ping_ok_at=self._last_ping_ok.isoformat() if self._last_ping_ok else None,
                last_update_at=(
                    listener.last_update_at.isoformat()
                    if listener and listener.last_update_at
                    else None
                ),
                transport=transport,
                reconnecting=reconnecting,
                degraded=bool(supervisor and supervisor.state.degraded),
                queue=self.delivery.total_queued() if self.delivery else 0,
            ),
        )

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
        if result.capped_chats:
            # The one thing in a catch-up round that is silent data loss by design: these
            # chats had more history than one round will read, and what is below the boundary
            # is never looked at again. The notification says so too, but only to whoever the
            # first match belonged to.
            log.warning(
                "catch-up hit the per-chat limit on %s; older messages there were not checked",
                ", ".join(str(chat_id) for chat_id in result.capped_chats),
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
        if last is None or self.clock() - last <= SILENCE_WARN:
            return
        if (
            self._silence_probe_at is not None
            and self.clock() - self._silence_probe_at < SILENCE_RETRY
        ):
            return  # already probed recently; a quiet night is not an emergency
        self._silence_probe_at = self.clock()
        log.warning("no updates for over 10 minutes; forcing a catch-up")
        with contextlib.suppress(Exception):
            await self.user_client.catch_up()
        await self._catch_up()

    async def _reprobe(self) -> None:
        """Give chats we lost access to another chance; one bad round must not be permanent."""
        if self.catchup is None:
            return
        async with self._catchup_lock:
            result = await self.catchup.reprobe_unavailable()
        if result.recovered:
            log.info("regained access to %d chats", len(result.recovered))

    async def _rescan_outbox(self) -> None:
        """Pick up rows that could not be queued earlier (a full queue, a paused user)."""
        if self.delivery is not None:
            await self.delivery.replay_pending(quiet=True)

    async def _weekly(self) -> None:
        """Send each moderator their week, at ten in the morning where they are.

        Runs hourly and asks the question per user rather than firing on a global schedule,
        because everybody here is in a different timezone and a report that lands at four in
        the morning is a reason to turn the bot off.
        """
        now = self.clock()
        for user_id in sorted(await self.storage.active_user_ids()):
            user = await self.storage.get_user(user_id)
            if user is None or user.paused:
                continue
            if not (user.weekly_report or user.auto_backup):
                continue
            local = _local_time(now, user.tz)
            if local.hour != WEEKLY_HOUR:
                continue
            if user.weekly_sent_at is None:
                # First sight of this person: start the clock rather than report on a week
                # that was half over before anything was being counted.
                await self.storage.update_user(user_id, weekly_sent_at=now)
                continue
            if now - user.weekly_sent_at < WEEKLY_WINDOW:
                continue
            try:
                await self._send_weekly(user, now)
            except Exception:
                log.exception("weekly report failed for %s", user_id)
                continue
            await self.storage.update_user(user_id, weekly_sent_at=now)

    async def _send_weekly(self, user: User, now: dt.datetime) -> None:
        lang = user.lang or DEFAULT_LANG
        if user.weekly_report:
            # From the last report rather than a flat seven days back: the tick is hourly and
            # the hour is local, so a restart or a slow morning shifts it — and a window
            # measured from *now* would leave the day in between counted by no report at all.
            # Clamped, because after a long outage "your week" should not silently mean a month.
            since = max(user.weekly_sent_at or now - WEEKLY_WINDOW, now - WEEKLY_MAX_WINDOW)
            span = now - since
            report = render_weekly(
                lang=lang,
                total=await self.storage.count_stats(user.id, since, now),
                previous=await self.storage.count_stats(user.id, since - span, since),
                chats=await self.storage.top_chats(user.id, since, now),
                rules=await self.storage.top_rules(user.id, since, now),
            )
            await self.bot.send(user.id, report.text, formatting_entities=report.entities)
        if user.auto_backup:
            # Its own guard: the report above has already been delivered by now, and letting a
            # failed *attachment* abandon the whole tick meant the timestamp was never written
            # and the identical report went out again the next morning, and every morning after.
            # A backup that misses one week is a backup `/export` still hands over on demand.
            try:
                await self._send_backup(user, now, lang)
            except Exception:
                log.exception("weekly backup was not sent to %s", user.id)

    async def _send_backup(self, user: User, now: dt.datetime, lang: str) -> None:
        config = await portable.export_config(self.storage, user.id, now=now)
        body = portable.dumps(config).encode()
        # At the stored caps an export is 1.3 MB and `/import` refuses anything over 1 MB, so
        # the caption cannot promise a restore it cannot deliver. The file still goes out —
        # the data in it is the point — but it says which of the two it is.
        key = "weekly.backup" if len(body) <= portable.MAX_BYTES else "weekly.backup_too_big"
        caption = t(
            lang,
            key,
            chats=len(config["chats"]),
            rules=len(config["rules"]),
            ignored=len(config["ignored"]),
        )
        await self.bot.send_document(user.id, f"ddiver-{now:%Y-%m-%d}.json", body, caption)

    async def _prune(self) -> None:
        removed = await self.storage.prune_outbox()
        await self.storage.prune_stats()
        if removed:
            log.info("pruned %d old outbox rows", removed)

    async def _run_listener(self) -> None:
        """One listener lifetime: connect if needed, then stay until Telethon disconnects.

        ``run_until_disconnected()`` always disconnects the client in its ``finally``, so
        without reconnecting here every retry after the first would raise "Cannot send requests
        while disconnected" and the supervisor would spin on a dead client forever.
        """
        if not self.user_client.is_connected():
            await connect_with_timeout(self.user_client)
        if self._lifetimes and not self._stalled_restart():
            # A supervisor-driven restart means the client was fully down: Telethon's own
            # recovery only replays 100 messages per channel and jumps the rest, so the gap is
            # ours to close. The boot round already covered the first lifetime.
            self.user_client.reconnected.clear()
            await self._catch_up()
        self._lifetimes += 1
        await self.user_client.run_until_disconnected()

    def _stalled_restart(self) -> bool:
        """True while the listener is failing immediately, so a crash loop cannot hammer
        GetHistory once per retry."""
        return bool(self.supervisor and self.supervisor.state.stalled)

    async def _watch_reconnects(self) -> None:
        """Catch up after every automatic reconnect.

        Telethon reconnects underneath ``run_until_disconnected()``, and for a user account it
        asks for at most 100 missed messages per channel: on ``ChannelDifferenceTooLong`` it
        jumps ``pts`` and returns nothing at all. Waiting for the outer loop would mean the gap
        is only noticed when the client disconnects for good, by which time the cursor has
        already moved past it.
        """
        while True:
            await self.user_client.reconnected.wait()
            self.user_client.reconnected.clear()
            log.info("reconnected; running a catch-up round")
            try:
                await self._catch_up()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("catch-up after a reconnect failed")

    # -- helpers ---------------------------------------------------------------------------

    async def _resolve_entity(self, peer_id: int, chat_id: int | None = None) -> Any:
        """Resolve a sender for the delivery worker (never called from a message handler).

        A plain ``get_entity`` on a bare user id only works when the session already knows that
        user's access hash. Basic groups never send one — their updates carry no entities at
        all — so for those we load the member list once and try again.
        """
        try:
            return await self.user_client.get_entity(peer_id)
        except (ValueError, errors.RPCError):
            if chat_id is None:
                raise
            chat = await self.storage.get_chat(chat_id)
            if chat is None or not chat.ref.is_basic_group:
                raise
            if chat_id not in self._members_loaded:
                self._members_loaded.add(chat_id)
                log.info("loading members of %s to name its senders", chat_id)
                await self.user_client.get_participants(chat_id)
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
        return StatusInfo(
            listener_ok=supervisor.state.listener_ok if supervisor else True,
            uptime=format_uptime(supervisor.state.started_at if supervisor else None, now),
            monitored=len(self.matcher.monitored) if self.matcher else 0,
            unavailable=sum(1 for c in chats if not c.available),
            seen=listener.metrics.seen if listener else 0,
            matched=listener.metrics.matched if listener else 0,
            delivered=delivery.metrics.delivered if delivery else 0,
            digested=delivery.metrics.digested if delivery else 0,
            failed=delivery.metrics.failed if delivery else 0,
            queue=delivery.total_queued() if delivery else 0,
            last_update_at=listener.last_update_at if listener else None,
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

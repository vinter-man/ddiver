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
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from telethon import errors
from telethon.tl import types as tl

from ddiver.formatter import DigestEntry, Payload, Rendered, render_digest, render_notification
from ddiver.i18n import DEFAULT_LANG
from ddiver.senders import SenderCache
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
    stopping: bool = False
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
        senders: SenderCache | None = None,
    ) -> None:
        self._bot = bot
        self._storage = storage
        self._clock = clock
        self._monotonic = monotonic or (lambda: asyncio.get_running_loop().time())
        self._sleep = sleep
        self._buttons = buttons
        self._default_threshold = default_threshold
        self._senders = senders
        self._queues: dict[int, _UserQueue] = {}
        self._global_bucket = TokenBucket(GLOBAL_RATE, monotonic=self._monotonic, sleep=sleep)
        self._user_buckets: dict[int, TokenBucket] = {}
        self._flood_until = 0.0
        self._stopping = False
        self.metrics = Metrics()

    # -- lifecycle -------------------------------------------------------------------------

    async def replay_pending(self, *, quiet: bool = False) -> int:
        """Re-queue outbox rows nothing is holding any more.

        Runs at startup (after a crash or a shutdown) and periodically, so a row that could not
        be queued earlier — the queue was full, the user was paused — is not stranded until the
        next restart. Rows already in memory are skipped, so a rescan cannot duplicate them.
        """
        queued = {key for state in self._queues.values() for key in state.enqueued_at}
        items = [i for i in await self._storage.pending_deliveries() if i.key not in queued]
        requeued = 0
        for item in items:
            if await self.enqueue(item):
                requeued += 1
        if requeued and not quiet:
            log.info("replayed %d pending notifications from the outbox", requeued)
        return requeued

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
            # Record it, otherwise the row stays pending with nothing holding it and is only
            # noticed at the next restart.
            await self._storage.set_delivery_status(
                *item.key, DeliveryStatus.FAILED, error="queue_full"
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
        """Forget everything queued for a user (they blocked the bot or lost access).

        Safe to call from inside that user's own worker, which is what the ``UserIsBlocked``
        path does: the worker is asked to stop instead of being cancelled, because cancelling
        and awaiting yourself leaves an orphan task waiting on a queue nobody feeds again.
        Queued rows stay ``pending`` so a later rescan can pick them up.
        """
        state = self._queues.pop(user_id, None)
        self._user_buckets.pop(user_id, None)
        if state is None:
            return
        state.stopping = True
        self._release_queue(state)
        current = asyncio.current_task()
        if state.worker is not None and state.worker is not current:
            state.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await state.worker

    @staticmethod
    def _is_stopping(state: _UserQueue) -> bool:
        """Read through a call: the flag is set from another task while we await."""
        return state.stopping

    @staticmethod
    def _release_queue(state: _UserQueue) -> int:
        """Empty a queue, accounting for every item so ``queue.join()`` cannot hang."""
        released = 0
        while not state.queue.empty():
            item = state.queue.get_nowait()
            state.enqueued_at.pop(item.key, None)
            state.queue.task_done()
            released += 1
        return released

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
            if self._is_stopping(state):  # dropped while we were waiting for work
                state.queue.task_done()
                return
            try:
                await self._process(user_id, state, item)
            except asyncio.CancelledError:
                raise  # task_done() runs in the finally, exactly once
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
            if self._is_stopping(state):
                # the item we just handled dropped this user (they blocked the bot), so the
                # queue is gone: exit instead of waiting on it forever
                return

    async def _process(self, user_id: int, state: _UserQueue, item: OutboxItem) -> None:
        user = await self._storage.get_user(user_id)
        if user is None or not user.active or user.paused:
            self.metrics.dropped_inactive += 1
            return
        lang = user.lang or DEFAULT_LANG
        tz = user.tz or "UTC"

        # 0 means "never digest"; the column default already carries the configured default,
        # so `or self._default_threshold` would silently override the user's explicit choice.
        batch = self._collect_digest(state, item, user.digest_threshold)
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
            else:
                # _send only marked batch[0]; the rest were taken off the queue, so record them
                # too instead of leaving rows nothing will ever pick up again.
                for extra in batch[1:]:
                    await self._storage.set_delivery_status(
                        *extra.key, DeliveryStatus.FAILED, error="digest_failed"
                    )
                    self.metrics.failed += 1
            return

        payload = await self._with_sender_name(Payload.from_json(item.payload))
        rendered = render_notification(payload, lang=lang, tz=tz, now=self._clock())
        buttons = self._buttons(item, lang) if self._buttons else None
        if await self._send(user_id, rendered, buttons=buttons, item=item):
            await self._storage.set_delivery_status(*item.key, DeliveryStatus.SENT)
            self.metrics.delivered += 1

    async def _with_sender_name(self, payload: Payload) -> Payload:
        """Fill in a missing sender name.

        Basic groups deliver updates with no entities, so the listener — which is forbidden
        from making RPCs — can only record the id. Here we are already off the ingestion path
        and behind a rate limit, so one cached lookup is affordable.
        """
        if payload.sender_name or payload.sender_id is None or self._senders is None:
            return payload
        info = await self._senders.resolve(payload.sender_id)
        if info is None or not info.display_name:
            return payload
        return replace(
            payload,
            sender_name=info.display_name,
            sender_username=info.username or payload.sender_username,
            sender_is_deleted=info.is_deleted,
            sender_is_channel=info.is_channel,
        )

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

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
import enum
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from telethon import errors
from telethon.tl import types as tl

from ddiver_core.formatter import DigestEntry, Payload, Rendered, render_digest, render_notification
from ddiver_core.i18n import DEFAULT_LANG
from ddiver_core.senders import SenderCache
from ddiver_core.storage import DeliveryStatus, OutboxItem, Storage
from ddiver_core.timeutil import Clock, utcnow

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


class SendOutcome(enum.Enum):
    """Why a send stopped, because the three answers need three different things done.

    A pause is not a failure: the rows have to survive it, or blocking the bot for a minute
    costs a moderator every notification that was waiting behind the first one.
    """

    SENT = "sent"
    FAILED = "failed"  # deterministic; the row has already been marked
    PAUSED = "paused"  # the moderator blocked the bot; every row stays pending


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
    overflow_noted: bool = False
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
    """One bounded queue and one worker per moderator, behind a bot-wide flood gate.

    Separate queues so a moderator watching a busy chat cannot starve the others; one shared
    gate because a ``FloodWaitError`` on ``SendMessageRequest`` applies to the bot as a whole,
    not to the recipient.
    """

    def __init__(
        self,
        bot: BotPort,
        storage: Storage,
        *,
        clock: Clock = utcnow,
        monotonic: Callable[[], float] | None = None,
        sleep: Sleeper = asyncio.sleep,
        buttons: ButtonFactory | None = None,
        senders: SenderCache | None = None,
        on_user_state_changed: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self._bot = bot
        self._storage = storage
        self._clock = clock
        self._monotonic = monotonic or (lambda: asyncio.get_running_loop().time())
        self._sleep = sleep
        self._buttons = buttons
        self._senders = senders
        self._on_user_state_changed = on_user_state_changed
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
        next restart. Rows already queued are skipped; a row the listener is claiming at this
        very moment is not in memory yet, so at-least-once is what this promises, which is what
        the module docstring says the sender is built for.
        """
        queued = {key for state in self._queues.values() for key in state.enqueued_at}
        items = [i for i in await self._storage.pending_deliveries() if i.key not in queued]
        requeued = 0
        for item in items:
            # A full queue means this user is already holding more than they can be sent; the
            # rows stay pending and the next rescan comes back for them. Asking anyway would
            # cost a `get_user` per row to learn what the queue already knows.
            state = self._queues.get(item.user_id)
            if state is not None and state.queue.full():
                continue
            if await self.enqueue(item):
                requeued += 1
        if requeued and not quiet:
            log.info("replayed %d pending notifications from the outbox", requeued)
        return requeued

    async def enqueue(self, item: OutboxItem) -> bool:
        """Hand an outbox row to the user's worker. Never raises, never blocks the caller."""
        user = await self._storage.get_user(item.user_id)
        if user is None or not user.active or user.paused:
            # Leave the row pending: the user may unblock the bot, and then the rescan should
            # still find it. `pending_deliveries` skips paused users, so these rows cannot fill
            # the replay window, and the prune removes them once they are too old to matter.
            self.metrics.dropped_inactive += 1
            return False
        state = self._queue_for(item.user_id)
        try:
            state.queue.put_nowait(item)
        except asyncio.QueueFull:
            state.overflow += 1
            self.metrics.dropped_overflow += 1
            if not state.overflow_noted:
                # Once per episode: a backlog larger than the queue would otherwise write one
                # line per row, every rescan, for as long as it takes to drain.
                log.warning("delivery queue full for user %s; rows stay pending", item.user_id)
                state.overflow_noted = True
            # The row is left pending on purpose. Marking it failed used to be the answer,
            # from before there was a rescan — but nothing re-reads a failed row, so a backlog
            # larger than the queue was destroyed by the restart the outbox exists to survive.
            return False
        state.enqueued_at[item.key] = self._clock()
        state.overflow_noted = False
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
        """Deliberately re-read on every check.

        ``drop_user`` flips this flag from another task while the worker is parked on the
        queue, so it must never be hoisted into a local across an ``await``. The call also
        keeps mypy from narrowing the attribute to ``False`` for the rest of the branch.
        """
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

        # 0 means "never digest", so this is read as-is rather than `or` something: the
        # configured default is applied once, when the row is created, and after that the
        # column is the person's own answer.
        batch = self._collect_digest(state, item, user.digest_threshold)
        if batch is not None:
            entries = [DigestEntry(Payload.from_json(i.payload), i.key) for i in batch]
            overflow, state.overflow = state.overflow, 0
            rendered = render_digest(
                entries, lang=lang, tz=tz, now=self._clock(), overflow=overflow
            )
            # The send is inside the try on purpose. An error `_send` does not classify
            # escapes to the worker's catch-all, and the rest of the batch would then keep
            # its keys in `enqueued_at` — which is how the periodic rescan tells "in flight"
            # from "stranded", so those rows would stay pending and unreachable for the life
            # of the process, never counted as failed and never seen by the alert.
            try:
                outcome = await self._send(user_id, rendered, buttons=None, item=batch[0])
                await self._finish_digest(batch, outcome)
            finally:
                for extra in batch[1:]:
                    state.enqueued_at.pop(extra.key, None)
            return

        payload = await self._with_sender_name(Payload.from_json(item.payload), item.key)
        rendered = render_notification(payload, lang=lang, tz=tz, now=self._clock())
        buttons = self._buttons(item, lang) if self._buttons else None
        if await self._send(user_id, rendered, buttons=buttons, item=item) is SendOutcome.SENT:
            await self._storage.set_delivery_status(*item.key, DeliveryStatus.SENT)
            self.metrics.delivered += 1

    async def _finish_digest(self, batch: list[OutboxItem], outcome: SendOutcome) -> None:
        if outcome is SendOutcome.SENT:
            await self._storage.mark_digested([i.key for i in batch])
            self.metrics.digested += len(batch)
        elif outcome is SendOutcome.FAILED:
            # Deterministic: this batch would fail the same way on every retry, so the rows
            # are recorded rather than left for a rescan to pick up forever. `_send` marked
            # batch[0]; the rest were taken off the queue and would otherwise be rows that
            # nothing ever picks up again.
            for extra in batch[1:]:
                await self._storage.set_delivery_status(
                    *extra.key, DeliveryStatus.FAILED, error="digest_failed"
                )
                self.metrics.failed += 1
        # SendOutcome.PAUSED: nothing to record. Every row in the batch is still pending and
        # `pending_deliveries` skips a paused user, so the backlog comes back whole on the
        # rescan after they unblock the bot — which is what the outbox is for.

    async def _with_sender_name(self, payload: Payload, key: tuple[int, int, int]) -> Payload:
        """Fill in a missing sender name.

        Basic groups deliver updates with no entities, so the listener — which is forbidden
        from making RPCs — can only record the id. Here we are already off the ingestion path
        and behind a rate limit, so one cached lookup is affordable.
        """
        if payload.sender_name or payload.sender_id is None or self._senders is None:
            return payload
        info = await self._senders.resolve(payload.sender_id, payload.chat_id)
        if info is None or not info.display_name:
            return payload
        enriched = replace(
            payload,
            sender_name=info.display_name,
            sender_username=info.username or payload.sender_username,
            sender_is_deleted=info.is_deleted,
            sender_is_channel=info.is_channel,
        )
        # Keep it: a retry then needs no second lookup, and the stored row shows what was sent.
        with contextlib.suppress(Exception):
            await self._storage.update_payload(key, enriched.to_json())
        return enriched

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
            # Deliberately keep the key in enqueued_at: the row stays `pending` until the
            # digest is actually sent, and the periodic rescan uses that map to tell "nobody
            # is holding this" from "this is in flight".
            state.queue.task_done()
            batch.append(extra)
        return batch

    # -- sending ---------------------------------------------------------------------------

    async def _send(
        self, user_id: int, rendered: Rendered, *, buttons: Any, item: OutboxItem
    ) -> SendOutcome:
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
                if self._on_user_state_changed is not None:
                    # stop the listener from claiming rows nobody can receive
                    await self._on_user_state_changed()
                await self.drop_user(user_id)
                # The row stays pending, and so does the rest of a digest batch. Blocking the
                # bot is not a delivery failure, it is a pause: `pending_deliveries` skips a
                # paused user, so nothing retries against them and nothing loops, and the
                # rescan after `resume_delivery` hands the whole backlog back. Writing them
                # `failed` destroyed up to DIGEST_MAX_ITEMS notifications per block, because
                # nothing anywhere re-reads a failed row.
                self.metrics.dropped_inactive += 1
                return SendOutcome.PAUSED
            except errors.BadRequestError as exc:
                if not degraded and _is_rendering_error(exc):
                    degraded = True
                    self.metrics.degraded_sends += 1
                    log.warning("retrying plain-text after %s for %s", type(exc).__name__, item.key)
                    continue
                await self._fail(item, exc, attempts)
                return SendOutcome.FAILED
            except (*RETRYABLE, ValueError) as exc:
                if isinstance(exc, ValueError) or attempts >= MAX_ATTEMPTS:
                    await self._fail(item, exc, attempts)
                    return SendOutcome.FAILED
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
            return SendOutcome.SENT

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

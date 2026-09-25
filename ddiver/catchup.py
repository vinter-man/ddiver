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
from ddiver.storage import ChatRow, Storage
from ddiver_core.timeutil import Clock, utcnow

log = logging.getLogger(__name__)

YIELD_EVERY = 20
INTER_CHAT_PAUSE = 0.5
REPROBE_AFTER = dt.timedelta(hours=6)

History = Callable[["ChatRow", int, int], AsyncIterator[tl.Message]]
"""``history(chat, min_id, limit)`` → that chat's messages, newest first."""

Sleeper = Callable[[float], Awaitable[None]]


@dataclass(slots=True)
class CatchupResult:
    chats: int = 0
    messages: int = 0
    claimed: int = 0
    capped_chats: list[int] = field(default_factory=list)
    unavailable: list[int] = field(default_factory=list)
    recovered: list[int] = field(default_factory=list)
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
        on_recovered: Callable[[int], Awaitable[Any]] | None = None,
        on_available: Callable[[int], Awaitable[Any]] | None = None,
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
        self._on_recovered = on_recovered
        self._on_available = on_available

    async def run(self, chat_ids: set[int] | None = None) -> CatchupResult:
        result = CatchupResult()
        if self._hours <= 0 or self._per_chat <= 0:
            return result
        cursors = await self._storage.get_cursors()
        monitored = chat_ids if chat_ids is not None else await self._storage.monitored_chat_ids()
        cutoff = self._clock() - dt.timedelta(hours=self._hours)

        # Hold back the cursor for every chat in the round, not just the one being replayed:
        # a live message in a chat still queued would otherwise flush a cursor past the gap,
        # and a crash mid-round would skip it for good.
        todo = sorted(monitored)
        unfinished = set(todo)
        for chat_id in todo:
            self._listener.cursors.block(chat_id)
        try:
            for chat_id in todo:
                cursor = cursors.get(chat_id)
                if cursor is None:
                    # A chat we have never seen starts "now": no backfill on first subscribe.
                    unfinished.discard(chat_id)
                    continue
                chat = await self._storage.get_chat(chat_id)
                if chat is None or not chat.available:
                    result.skipped += 1
                    unfinished.discard(chat_id)
                    continue
                if result.messages >= self._total:
                    log.warning(
                        "catch-up budget reached; %d chats not checked",
                        len(monitored) - result.chats,
                    )
                    break
                if await self._catch_up_chat(chat, cursor.last_msg_id, cutoff, result):
                    unfinished.discard(chat_id)
                await self._sleep(INTER_CHAT_PAUSE)
        finally:
            # A chat this round did not actually read (budget, cancellation, or a fetch that
            # failed) may have gathered a live cursor while it was blocked. Writing that would
            # jump the stored cursor over the very gap the round was supposed to examine, so
            # drop it: the live messages are already durable in the outbox, and re-fetching
            # them is deduplicated.
            #
            # "Did not read" has to include the failures, which is what this used to get wrong.
            # `_fetch_guarded` turns every fetch error into "skip this chat", so a flood wait
            # or an unresolvable peer returned normally, the chat was struck off here anyway,
            # and the live cursor went out over the unexamined history — permanently, because
            # `upsert_cursors` only ever moves forward. A boot round right after downtime is
            # both when the gap is largest and when a flood wait is likeliest.
            for chat_id in unfinished:
                self._listener.cursors.drop(chat_id)
            for chat_id in todo:
                self._listener.cursors.unblock(chat_id)
            await self._listener.cursors.flush()
        return result

    async def reprobe_unavailable(self) -> CatchupResult:
        """Retry chats we lost access to. One transient error must not disable a chat forever."""
        result = CatchupResult()
        cutoff = self._clock() - dt.timedelta(hours=self._hours)
        for chat in await self._storage.probe_candidates(REPROBE_AFTER):
            cursors = await self._storage.get_cursors()
            cursor = cursors.get(chat.id)
            self._listener.cursors.block(chat.id)
            try:
                await self._catch_up_chat(
                    chat, cursor.last_msg_id if cursor else 0, cutoff, result, probing=True
                )
            finally:
                self._listener.cursors.unblock(chat.id)
            await self._sleep(INTER_CHAT_PAUSE)
        return result

    async def _catch_up_chat(
        self,
        chat: ChatRow,
        min_id: int,
        cutoff: dt.datetime,
        result: CatchupResult,
        *,
        probing: bool = False,
    ) -> bool:
        """True when this chat was actually read, which is what lets its cursor move."""
        fetched = await self._fetch_guarded(chat, min_id, cutoff, result)
        if fetched is None:
            return False
        buffered, capped, newest = fetched

        if probing:
            result.recovered.append(chat.id)
            changed = await self._storage.set_chat_available(chat.id, True)
            # The matcher's monitored set still excludes this chat, so refresh it *before*
            # replaying — otherwise every replayed message is dropped and the cursor advances
            # past the backlog we just recovered.
            if self._on_available is not None:
                await self._on_available(chat.id)
            if changed and self._on_recovered:
                await self._on_recovered(chat.id)

        result.chats += 1
        if capped:
            result.capped_chats.append(chat.id)
        failed_at = await self._replay(chat.id, buffered, capped, result)
        if newest is not None:
            # Advance even when everything was filtered out, so the next round starts here —
            # but never past a message the replay could not handle, or that message would be
            # below the cursor next time and lost for good.
            highest = newest[0] if failed_at is None else min(newest[0], failed_at - 1)
            self._listener.cursors.note(chat.id, highest, newest[1])
            if failed_at is not None:
                self._listener.cursors.cap(chat.id, failed_at - 1)
        return True

    async def _fetch_guarded(
        self, chat: ChatRow, min_id: int, cutoff: dt.datetime, result: CatchupResult
    ) -> tuple[list[tl.Message], bool, tuple[int, dt.datetime] | None] | None:
        """Fetch one chat's backlog, turning every failure into "skip this chat this round"."""
        try:
            return await self._fetch(chat, min_id, cutoff, result)
        except errors.FloodWaitError as exc:
            result.flood_waits += 1
            log.warning(
                "catch-up flood wait %ss on chat %s; skipping it this round", exc.seconds, chat.id
            )
        except ValueError:
            # Telethon could not build an input peer (fresh session, cache miss). That is a
            # local problem, not a lost membership: touch the row so the re-probe backs off,
            # but do not tell everybody the chat is gone.
            log.warning("catch-up cannot resolve chat %s locally; will retry", chat.id)
            await self._storage.upsert_chat(chat.ref, access_hash=chat.access_hash)
        except (errors.ChannelPrivateError, errors.ChatForbiddenError) as exc:
            await self._mark_lost(chat, type(exc).__name__, result)
        except Exception:
            log.exception("catch-up failed for chat %s", chat.id)
        return None

    async def _mark_lost(self, chat: ChatRow, reason: str, result: CatchupResult) -> None:
        log.warning("catch-up cannot read chat %s (%s)", chat.id, reason)
        result.unavailable.append(chat.id)
        if await self._storage.set_chat_available(chat.id, False) and self._on_unavailable:
            await self._on_unavailable(chat.id, reason)
        else:
            # already marked: refresh updated_at so the re-probe schedule moves forward
            await self._storage.upsert_chat(chat.ref, access_hash=chat.access_hash)

    async def _replay(
        self, chat_id: int, buffered: list[tl.Message], capped: bool, result: CatchupResult
    ) -> int | None:
        """Replay in chronological order; returns the lowest id we could not handle.

        When the fetch hit the per-chat cap, the oldest message here is a boundary: nothing
        below it was looked at. That has to be said to the person, and it used to be attached
        to the oldest *buffered* message — which is not a notification unless it happens to
        match, so in the ordinary case where it does not, nobody was told anything. The flag
        rides along until a notification actually goes out.
        """
        failed_at: int | None = None
        carry = capped
        for index, message in enumerate(reversed(buffered)):
            try:
                claimed = await self._listener.handle(
                    message,
                    chat=message.chat,
                    catchup=True,
                    catchup_capped=carry,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad message must not abandon the rest of the round.
                log.exception("catch-up could not replay %s/%s", chat_id, message.id)
                failed_at = message.id if failed_at is None else min(failed_at, message.id)
                continue
            if claimed:
                carry = False
            result.claimed += len(claimed)
            if index % YIELD_EVERY == 0:
                await asyncio.sleep(0)
        return failed_at

    async def _fetch(
        self, chat: ChatRow, min_id: int, cutoff: dt.datetime, result: CatchupResult
    ) -> tuple[list[tl.Message], bool, tuple[int, dt.datetime] | None]:
        buffered: list[tl.Message] = []
        newest: tuple[int, dt.datetime] | None = None
        watches_senders = self._listener.watches_senders(chat.id)
        fetched = 0
        async for message in self._history(chat, min_id, self._per_chat):
            fetched += 1
            result.messages += 1
            date = getattr(message, "date", None) or self._clock()
            if newest is None:
                newest = (message.id, date)
            if date < cutoff:
                return buffered, False, newest
            # The same question the live path asks, with the same answer: a text-less message
            # is worth replaying only where somebody is watching a sender. Buffering every
            # photo in a five-hundred-message round would cost memory for nothing.
            if should_consider(message, ignore_own=True, allow_empty=watches_senders):
                buffered.append(message)
            if fetched % YIELD_EVERY == 0:
                await asyncio.sleep(0)
        return buffered, fetched >= self._per_chat, newest


def input_peer_for(chat: ChatRow) -> Any:
    """Build an input peer from what we stored, so a fresh session still finds the chat.

    Telethon resolves a bare marked id through its own session cache; when that cache was
    rebuilt (a re-login) the lookup fails even though we know the access hash.
    """
    if chat.ref.kind == "group":
        return tl.InputPeerChat(chat.ref.bare_id)
    if chat.access_hash is not None:
        return tl.InputPeerChannel(chat.ref.bare_id, chat.access_hash)
    return chat.id


def telethon_history(client: Any) -> History:
    """Adapt a Telethon client to the ``History`` callable used by :class:`Catchup`."""

    def history(chat: ChatRow, min_id: int, limit: int) -> AsyncIterator[tl.Message]:
        messages: AsyncIterator[tl.Message] = client.iter_messages(
            input_peer_for(chat), min_id=min_id, limit=limit
        )
        return messages

    return history

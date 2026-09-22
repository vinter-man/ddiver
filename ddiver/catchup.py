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

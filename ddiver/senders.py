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
    async def __call__(self, peer_id: int, chat_id: int | None = None) -> Any: ...


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

    async def resolve(self, peer_id: int, chat_id: int | None = None) -> SenderInfo | None:
        """Cache lookup, then at most one resolve per ``min_interval``; never raises.

        ``chat_id`` tells the resolver where the sender was seen, which is what makes a basic
        group's members findable at all.
        """
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
                    entity = await self._resolver(peer_id, chat_id)
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
    """Sender of a Telethon message, without ever making a network call of its own.

    Basic groups arrive as ``UpdateShortChatMessage``, which carries no entities at all, so
    there is often nothing to build a name from. The id is still known, and that is what the
    notification's ignore button and the copyable id line need — the display name is filled in
    later, in the delivery worker, where an RPC is affordable.
    """
    info = sender_from_entity(getattr(message, "sender", None))
    if info is not None:
        cache.put(info)
        return info
    sender_id = getattr(message, "sender_id", None)
    if sender_id is None:
        return None
    cached = cache.get(int(sender_id))
    if cached is not None:
        return cached
    return SenderInfo(id=int(sender_id))


def suppress_rpc() -> contextlib.AbstractContextManager[None]:
    return contextlib.suppress(RPCError, ValueError)

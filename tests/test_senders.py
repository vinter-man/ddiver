import asyncio
import datetime as dt

import pytest
from telethon.errors import FloodWaitError
from telethon.tl import types as tl

from ddiver.senders import SenderCache, active_username, sender_from_entity, sender_of
from tests.fakes import FakeClock, make_channel, make_event, make_user


def test_sender_from_user_entity():
    info = sender_from_entity(make_user(42, "Иван", "ivan"))
    assert (info.id, info.username, info.is_bot, info.is_deleted) == (42, "ivan", False, False)
    assert info.display_name == "Иван"


def test_sender_display_name_variants():
    assert sender_from_entity(make_user(1, None, "nick")).display_name == "@nick"
    assert sender_from_entity(make_user(1, None, None)).display_name == ""
    full = tl.User(id=1, first_name="Иван", last_name="Петров")
    assert sender_from_entity(full).display_name == "Иван Петров"


def test_sender_from_channel_entity_is_marked_and_flagged():
    info = sender_from_entity(make_channel(-1001234567890, "Anon", "pub"))
    assert (info.id, info.title, info.is_channel, info.username) == (
        -1001234567890,
        "Anon",
        True,
        "pub",
    )
    assert info.display_name == "Anon"


def test_deleted_and_bot_flags():
    assert sender_from_entity(make_user(7, None, None, deleted=True)).is_deleted
    assert sender_from_entity(make_user(8, "Bot", "b", bot=True)).is_bot


def test_collectible_username_is_used():
    chan = tl.Channel(
        id=5,
        title="C",
        photo=tl.ChatPhotoEmpty(),
        date=None,
        usernames=[
            tl.Username(username="inactive", active=False),
            tl.Username(username="collectible", active=True),
        ],
    )
    assert active_username(chan) == "collectible"
    user = tl.User(id=6, usernames=[tl.Username(username="fragment", active=True)])
    assert sender_from_entity(user).username == "fragment"
    assert active_username(tl.User(id=7)) is None


def test_sender_from_none():
    assert sender_from_entity(None) is None


async def test_sender_of_uses_attached_entity_without_resolving():
    calls = []

    async def resolver(peer_id):
        calls.append(peer_id)

    cache = SenderCache(resolver)
    event = make_event(sender=make_user(42, "Иван", "ivan"))
    info = await sender_of(event.message, cache)
    assert info.id == 42 and info.username == "ivan"
    assert calls == []
    assert cache.get(42) is not None  # warmed for later messages


async def test_sender_of_never_resolves_on_the_hot_path():
    """The listener must not make RPCs; an id-only SenderInfo is the honest answer."""
    calls = []

    async def resolver(peer_id):
        calls.append(peer_id)
        return make_user(peer_id, "Resolved", "res", min_=False)

    clock = FakeClock()
    cache = SenderCache(resolver, clock=clock)
    message = make_event().message
    message._sender = None
    message._entities = {}
    info = await sender_of(message, cache)
    assert info is not None
    assert info.id == 42
    assert info.display_name == ""  # the delivery worker fills this in later
    assert calls == []  # nothing was resolved from the handler


async def test_sender_of_uses_a_name_the_cache_already_knows():
    cache = SenderCache(None, clock=FakeClock())
    cache.put(sender_from_entity(make_user(42, "Иван", "ivan", min_=False)))
    message = make_event().message
    message._sender = None
    message._entities = {}
    info = await sender_of(message, cache)
    assert info.display_name == "Иван"


async def test_resolver_is_rate_limited():
    async def resolver(peer_id):
        return make_user(peer_id, "R", None, min_=False)

    clock = FakeClock()
    cache = SenderCache(resolver, clock=clock, min_interval=1.0)
    assert await cache.resolve(1) is not None
    assert await cache.resolve(2) is None  # throttled, not an error
    assert cache.stats_throttled == 1
    clock.tick(1.5)
    assert await cache.resolve(2) is not None


async def test_resolver_errors_are_swallowed():
    async def flood(peer_id):
        raise FloodWaitError(request=None, capture=30)

    clock = FakeClock()
    cache = SenderCache(flood, clock=clock, min_interval=0)
    assert await cache.resolve(5) is None
    assert cache.stats_failures == 1

    async def missing(peer_id):
        msg = "Could not find the input entity"
        raise ValueError(msg)

    cache2 = SenderCache(missing, clock=clock, min_interval=0)
    assert await cache2.resolve(6) is None


async def test_resolver_timeout_is_swallowed():
    async def hang(peer_id):
        await asyncio.sleep(10)

    cache = SenderCache(hang, min_interval=0)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("ddiver.senders.RESOLVE_TIMEOUT", 0.01)
        assert await cache.resolve(9) is None
    assert cache.stats_failures == 1


async def test_cache_ttl_and_lru_eviction():
    clock = FakeClock()
    cache = SenderCache(None, clock=clock, ttl=dt.timedelta(minutes=10), size=2)
    cache.put(sender_from_entity(make_user(1, "A", None)))
    cache.put(sender_from_entity(make_user(2, "B", None)))
    assert cache.get(1) is not None  # refreshes LRU position
    cache.put(sender_from_entity(make_user(3, "C", None)))
    assert cache.get(2) is None  # evicted as least recently used
    assert cache.get(1) is not None and cache.get(3) is not None
    clock.tick(11 * 60)
    assert cache.get(1) is None  # expired


async def test_sender_of_without_sender_id():
    cache = SenderCache(None)
    message = make_event().message
    message._sender = None
    message._entities = {}
    message.from_id = None
    message.peer_id = tl.PeerChannel(1234567890)
    message.post = False
    message._sender_id = None
    assert await sender_of(message, cache) is None


async def test_no_resolver_returns_none():
    cache = SenderCache(None)
    assert await cache.resolve(123) is None

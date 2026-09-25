"""Delivery for a source with no Telegram name resolution (``senders=None``).

ddiver-fb's items arrive with the author's name already known, or with none at all (an
anonymous participant); there is nobody on Telegram to ask. Delivery must send them as they
are, keep the outbox rows it was given, and never hand a Facebook id to a Telegram lookup even
if an app passed a sender cache by mistake.
"""

import asyncio

import pytest
from telethon.tl import types as tl

from ddiver_core.botruntime import notification_buttons_factory
from ddiver_core.delivery import Delivery
from ddiver_core.formatter import Payload
from ddiver_core.senders import SenderCache
from ddiver_core.storage import DeliveryStatus, OutboxItem, Role, Storage
from ddiver_core_testing import T0, FakeBotClient, FakeClock

OWNER = 1
MOD = 2
GROUP = 1234567890
AUTHOR = 100000000000001
POST = 9876543210987654
POST_URL = f"https://www.facebook.com/groups/{GROUP}/posts/{POST}/"
GROUP_URL = f"https://www.facebook.com/groups/{GROUP}/"


def fb_payload(post: int = POST, **overrides: object) -> dict[str, object]:
    payload = Payload(
        chat_id=GROUP,
        chat_title="Оренда Варна",
        chat_username=None,
        chat_kind="group",
        msg_id=post,
        topic_id=None,
        date=T0,
        text="Здам квартиру біля моря",
        spans=[(5, 13)],
        rule_pattern="квартир",
        rule_kind="include",
        rule_is_global=True,
        sender_id=AUTHOR,
        sender_name="Olena K.",
        sender_username=None,
        source="fb",
        url=f"https://www.facebook.com/groups/{GROUP}/posts/{post}/",
        chat_url=GROUP_URL,
        sender_url="https://www.facebook.com/profile.php?id=100000000000001",
    )
    return {**payload.to_json(), **overrides}


class Sleeps:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture
async def store(tmp_path, clock):
    s = Storage(tmp_path / "fb.sqlite3", owner_id=OWNER, clock=clock)
    await s.open()
    await s.ensure_user(OWNER)
    await s.update_user(OWNER, lang="en", tz="UTC")
    await s.ensure_user(MOD)
    await s.update_user(MOD, role=Role.ALLOWED, lang="uk", tz="Europe/Kyiv")
    yield s
    await s.close()


@pytest.fixture
def bot() -> FakeBotClient:
    return FakeBotClient()


def make_delivery(bot: FakeBotClient, store: Storage, clock: FakeClock, **kw: object) -> Delivery:
    sleeps = Sleeps()
    return Delivery(
        bot,
        store,
        clock=clock,
        monotonic=sleeps.monotonic,
        sleep=sleeps.sleep,
        **kw,
    )


async def claim(store: Storage, user_id: int, payload: dict[str, object]) -> OutboxItem:
    item = await store.claim_delivery(user_id, GROUP, int(str(payload["msg_id"])), 7, payload)
    assert item is not None
    return item


def link_urls(entities: list[object]) -> list[str]:
    return [e.url for e in entities if isinstance(e, tl.MessageEntityTextUrl)]


class TestWithoutSenders:
    async def test_a_post_is_sent_with_its_own_links_and_marked_sent(self, bot, store, clock):
        delivery = make_delivery(bot, store, clock, senders=None)
        item = await claim(store, MOD, fb_payload())
        assert await delivery.enqueue(item)
        assert await delivery.wait_idle()

        [sent] = bot.sent
        assert sent.user_id == MOD
        assert "Olena K." in sent.text
        assert POST_URL in link_urls(sent.entities)
        assert all("t.me" not in url for url in link_urls(sent.entities))
        assert (await store.get_delivery(*item.key)).status is DeliveryStatus.SENT
        assert delivery.metrics.delivered == 1

    async def test_a_nameless_author_is_sent_by_id_and_the_row_is_left_alone(
        self, bot, store, clock
    ):
        delivery = make_delivery(bot, store, clock, senders=None)
        payload = fb_payload(sender_name="", sender_url=None)
        item = await claim(store, OWNER, payload)
        await delivery.enqueue(item)
        assert await delivery.wait_idle()

        assert f"User {AUTHOR}" in bot.sent[0].text
        stored = await store.get_delivery(*item.key)
        assert stored.payload == payload  # no name was invented and written back
        assert stored.status is DeliveryStatus.SENT

    async def test_an_anonymous_participant_gets_no_ignore_button(self, bot, store, clock):
        delivery = make_delivery(
            bot, store, clock, senders=None, buttons=notification_buttons_factory()
        )
        payload = fb_payload(sender_id=None, sender_name="", sender_url=None)
        await delivery.enqueue(await claim(store, OWNER, payload))
        assert await delivery.wait_idle()

        [sent] = bot.sent
        assert "Unknown sender" in sent.text
        [[only]] = sent.buttons
        assert only.type.data == b"rdrop:7"

    async def test_the_buttons_carry_the_group_and_the_author(self, bot, store, clock):
        delivery = make_delivery(
            bot, store, clock, senders=None, buttons=notification_buttons_factory()
        )
        await delivery.enqueue(await claim(store, OWNER, fb_payload()))
        assert await delivery.wait_idle()

        [row] = bot.sent[0].buttons
        assert [button.type.data for button in row] == [f"ig:{GROUP}:{AUTHOR}".encode(), b"rdrop:7"]

    async def test_a_backlog_becomes_one_digest_with_the_posts_links(self, bot, store, clock):
        await store.update_user(OWNER, digest_threshold=3)
        delivery = make_delivery(bot, store, clock, senders=None)
        bot.hold()  # let the queue fill before the worker sends anything
        items = [await claim(store, OWNER, fb_payload(POST + n)) for n in range(4)]
        for item in items:
            await delivery.enqueue(item)
        bot.release()
        assert await delivery.wait_idle()

        texts = [sent.text for sent in bot.sent]
        digest = bot.sent[-1]
        assert "matches while you were busy" in digest.text, texts
        assert GROUP_URL in link_urls(digest.entities)
        assert all("t.me" not in url for sent in bot.sent for url in link_urls(sent.entities))
        statuses = {(await store.get_delivery(*i.key)).status for i in items}
        assert statuses <= {DeliveryStatus.SENT, DeliveryStatus.DIGESTED}
        assert DeliveryStatus.DIGESTED in statuses


class TestASenderCacheIsNeverAskedAboutAnotherSource:
    @pytest.fixture
    def asked(self) -> list[int]:
        return []

    @pytest.fixture
    def cache(self, clock, asked) -> SenderCache:
        async def resolver(peer_id: int, chat_id: int | None = None) -> object:
            asked.append(peer_id)
            return tl.User(id=peer_id, first_name="A stranger on Telegram")

        return SenderCache(resolver, clock=clock, min_interval=0)

    async def test_a_facebook_author_is_not_looked_up_on_telegram(
        self, bot, store, clock, cache, asked
    ):
        delivery = make_delivery(bot, store, clock, senders=cache)
        payload = fb_payload(sender_name="")
        item = await claim(store, OWNER, payload)
        await delivery.enqueue(item)
        assert await delivery.wait_idle()

        assert asked == []
        assert "A stranger on Telegram" not in bot.sent[0].text
        assert (await store.get_delivery(*item.key)).payload == payload

    async def test_a_telegram_sender_still_is(self, bot, store, clock, cache, asked):
        """The guard is by source: ddiver's own lookups are what they were."""
        delivery = make_delivery(bot, store, clock, senders=cache)
        telegram = {
            key: value
            for key, value in fb_payload(sender_name="").items()
            if key not in {"source", "url", "chat_url", "sender_url"}
        }
        telegram.update(chat_id=-1001234567890, chat_kind="supergroup")
        item = await store.claim_delivery(OWNER, -1001234567890, POST, 7, telegram)
        assert item is not None
        await delivery.enqueue(item)
        assert await delivery.wait_idle()

        assert asked == [AUTHOR]
        assert "A stranger on Telegram" in bot.sent[0].text

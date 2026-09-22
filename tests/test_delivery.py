import asyncio
import datetime as dt

import pytest
from telethon import errors
from telethon.tl import types as tl

from ddiver.delivery import QUEUE_MAXSIZE, Delivery
from ddiver.storage import DeliveryStatus, Role, Storage
from tests.fakes import T0, FakeBotClient, FakeClock

OWNER = 1
MOD = 2

BASE_PAYLOAD = {
    "chat_id": -1001234567890,
    "chat_title": "Чат",
    "chat_username": None,
    "chat_kind": "supergroup",
    "msg_id": 1,
    "topic_id": None,
    "date": T0.isoformat(),
    "text": "казино",
    "spans": [[0, 6]],
    "rule_pattern": "казино",
    "rule_kind": "include",
    "rule_is_global": False,
    "sender_id": 42,
    "sender_name": "Иван",
    "sender_username": "ivan",
}


def payload(msg_id: int = 1, **over):
    return {**BASE_PAYLOAD, "msg_id": msg_id, **over}


class Sleeps:
    """Records sleeps and advances the monotonic clock without real waiting."""

    def __init__(self) -> None:
        self.now = 0.0
        self.calls: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def sleeps():
    return Sleeps()


@pytest.fixture
async def store(tmp_path, clock):
    s = Storage(tmp_path / "db.sqlite3", owner_id=OWNER, clock=clock)
    await s.open()
    await s.ensure_user(OWNER)
    await s.ensure_user(MOD)
    await s.update_user(MOD, role=Role.ALLOWED, lang="ru", tz="Europe/Kyiv")
    await s.update_user(OWNER, lang="en", tz="UTC")
    yield s
    await s.close()


@pytest.fixture
def bot():
    return FakeBotClient()


@pytest.fixture
def delivery(bot, store, clock, sleeps):
    return Delivery(
        bot, store, clock=clock, monotonic=sleeps.monotonic, sleep=sleeps.sleep, buttons=None
    )


async def claim(store, user_id: int, msg_id: int, **over):
    item = await store.claim_delivery(
        user_id, BASE_PAYLOAD["chat_id"], msg_id, 7, payload(msg_id, **over)
    )
    assert item is not None
    return item


async def settle(delivery, *, timeout: float = 5.0):
    """Let the worker tasks run until every queue is drained."""
    assert await delivery.wait_idle(timeout) is True


class TestHappyPath:
    async def test_single_notification_is_rendered_and_marked_sent(
        self, delivery, bot, store, clock
    ):
        item = await claim(store, MOD, 1)
        assert await delivery.enqueue(item) is True
        clock.tick(12)
        await settle(delivery)
        assert len(bot.sent) == 1
        sent = bot.sent[0]
        assert sent.user_id == MOD
        assert "казино" in sent.text
        assert "12 сек назад" in sent.text  # rendered at send time, in the user's language
        assert sent.link_preview is False
        assert any(isinstance(e, tl.MessageEntityBold) for e in sent.entities)
        row = await store.get_delivery(*item.key)
        assert row.status is DeliveryStatus.SENT
        assert delivery.metrics.delivered == 1

    async def test_replay_pending_requeues_outbox_rows(self, delivery, bot, store):
        await claim(store, MOD, 1)
        await claim(store, MOD, 2)
        await claim(store, OWNER, 3)
        assert await delivery.replay_pending() == 3
        await settle(delivery)
        assert {m.user_id for m in bot.sent} == {MOD, OWNER}
        assert len(bot.sent) == 3
        assert await store.pending_deliveries() == []

    async def test_users_are_independent(self, delivery, bot, store):
        await delivery.enqueue(await claim(store, MOD, 1))
        await delivery.enqueue(await claim(store, OWNER, 2))
        await settle(delivery)
        langs = {m.user_id: m.text for m in bot.sent}
        assert "сек назад" in langs[MOD]  # ru
        assert "s ago" in langs[OWNER]  # en

    async def test_inactive_and_paused_users_are_skipped(self, delivery, bot, store):
        await store.update_user(MOD, role=Role.DENIED)
        assert await delivery.enqueue(await claim(store, MOD, 1)) is False
        await store.update_user(MOD, role=Role.ALLOWED)
        await store.pause_delivery(MOD, "blocked")
        assert await delivery.enqueue(await claim(store, MOD, 2)) is False
        await settle(delivery)
        assert bot.sent == []
        assert delivery.metrics.dropped_inactive == 2


class TestFlood:
    async def test_flood_wait_retries_the_same_item_and_pauses_everyone(
        self, delivery, bot, store, sleeps
    ):
        bot.fail_next(errors.FloodWaitError(request=None, capture=30))
        await delivery.enqueue(await claim(store, MOD, 1))
        await delivery.enqueue(await claim(store, OWNER, 2))
        await settle(delivery)
        assert len(bot.sent) == 2
        assert delivery.metrics.flood_waits == 1
        assert delivery.metrics.flood_seconds == 30
        assert any(s >= 30 for s in sleeps.calls)  # the shared gate, not a per-user sleep
        assert delivery.metrics.delivered == 2

    async def test_repeated_flood_waits_never_fail_the_item(self, delivery, bot, store):
        bot.fail_next(*[errors.FloodWaitError(request=None, capture=5) for _ in range(8)])
        item = await claim(store, MOD, 1)
        await delivery.enqueue(item)
        await settle(delivery)
        assert len(bot.sent) == 1
        row = await store.get_delivery(*item.key)
        assert row.status is DeliveryStatus.SENT


class TestErrors:
    async def test_blocked_user_is_paused_and_queue_dropped(self, delivery, bot, store):
        bot.fail_next(errors.UserIsBlockedError(request=None))
        item = await claim(store, MOD, 1)
        await delivery.enqueue(item)
        await delivery.enqueue(await claim(store, MOD, 2))
        await settle(delivery)
        user = await store.get_user(MOD)
        assert user.paused and user.pause_reason == "UserIsBlockedError"
        assert user.active  # no re-approval needed after they unblock
        assert delivery.queue_length(MOD) == 0
        assert (await store.get_delivery(*item.key)).status is DeliveryStatus.FAILED

    async def test_entity_error_falls_back_to_plain_text(self, delivery, bot, store):
        bot.fail_next(errors.EntitiesTooLongError(request=None))
        item = await claim(store, MOD, 1)
        await delivery.enqueue(item)
        await settle(delivery)
        assert len(bot.sent) == 1
        assert bot.sent[0].entities == []
        assert bot.sent[0].buttons is None
        assert delivery.metrics.degraded_sends == 1
        assert (await store.get_delivery(*item.key)).status is DeliveryStatus.SENT

    async def test_deterministic_bad_request_is_not_retried(self, delivery, bot, store):
        bot.fail_next(errors.MessageTooLongError(request=None))
        item = await claim(store, MOD, 1)
        await delivery.enqueue(item)
        await settle(delivery)
        assert bot.sent == []
        row = await store.get_delivery(*item.key)
        assert row.status is DeliveryStatus.FAILED
        assert row.attempts == 1
        assert delivery.metrics.failed == 1

    async def test_transient_errors_back_off_then_succeed(self, delivery, bot, store, sleeps):
        bot.fail_next(ConnectionError("reset"), errors.TimedOutError(request=None, message="T"))
        item = await claim(store, MOD, 1)
        await delivery.enqueue(item)
        await settle(delivery)
        assert len(bot.sent) == 1
        assert sleeps.calls[:2] == [1.0, 2.0]
        assert (await store.get_delivery(*item.key)).status is DeliveryStatus.SENT

    async def test_transient_errors_give_up_after_max_attempts(self, delivery, bot, store):
        bot.fail_next(*[ConnectionError("reset") for _ in range(9)])
        item = await claim(store, MOD, 1)
        await delivery.enqueue(item)
        await settle(delivery)
        row = await store.get_delivery(*item.key)
        assert row.status is DeliveryStatus.FAILED
        assert row.attempts == 5
        assert bot.sent == []

    async def test_unresolvable_peer_fails_immediately(self, delivery, bot, store):
        bot.fail_next(ValueError("Could not find the input entity"))
        item = await claim(store, MOD, 1)
        await delivery.enqueue(item)
        await settle(delivery)
        assert (await store.get_delivery(*item.key)).status is DeliveryStatus.FAILED

    async def test_worker_survives_an_unexpected_exception(self, delivery, bot, store):
        bot.fail_next(RuntimeError("boom"))
        first = await claim(store, MOD, 1)
        await delivery.enqueue(first)
        await settle(delivery)
        assert (await store.get_delivery(*first.key)).status is DeliveryStatus.FAILED
        # the same worker keeps serving the next item
        second = await claim(store, MOD, 2)
        await delivery.enqueue(second)
        await settle(delivery)
        assert (await store.get_delivery(*second.key)).status is DeliveryStatus.SENT


class TestDigest:
    async def test_threshold_collapses_the_queue_into_one_message(self, delivery, bot, store):
        await store.update_user(MOD, digest_threshold=5)
        bot.hold()  # build a backlog, as a burst of matches would
        items = [await claim(store, MOD, i) for i in range(1, 8)]
        for item in items:
            await delivery.enqueue(item)
        bot.release()
        await settle(delivery)
        # the worker was already sending the first item when the burst arrived, so the rest
        # collapse into one digest
        assert len(bot.sent) == 2
        assert "6 совпадений" in bot.sent[1].text
        rows = [await store.get_delivery(*i.key) for i in items]
        assert rows[0].status is DeliveryStatus.SENT
        assert all(r.status is DeliveryStatus.DIGESTED for r in rows[1:])
        assert delivery.metrics.digested == 6
        assert delivery.metrics.delivered == 1

    async def test_below_threshold_sends_individually(self, delivery, bot, store):
        await store.update_user(MOD, digest_threshold=10)
        bot.hold()
        for i in range(1, 4):
            await delivery.enqueue(await claim(store, MOD, i))
        bot.release()
        await settle(delivery)
        assert len(bot.sent) == 3

    async def test_age_triggers_a_digest_below_the_threshold(self, delivery, bot, store, clock):
        await store.update_user(MOD, digest_threshold=50)
        bot.hold()
        for i in range(1, 7):
            await delivery.enqueue(await claim(store, MOD, i))
        clock.tick(31)  # the oldest queued item is now older than DIGEST_AGE
        bot.release()
        await settle(delivery)
        assert len(bot.sent) == 2
        assert "5 совпадений" in bot.sent[1].text  # threshold not reached, age triggered it
        assert delivery.metrics.digested == 5

    async def test_digest_threshold_zero_disables_digests(self, delivery, bot, store):
        await store.update_user(MOD, digest_threshold=0)
        for i in range(1, 6):
            await delivery.enqueue(await claim(store, MOD, i))
        await settle(delivery)
        assert len(bot.sent) == 5


class TestQueueLimits:
    async def test_overflow_is_counted_and_reported_in_the_next_digest(
        self, delivery, bot, store, monkeypatch
    ):
        monkeypatch.setattr("ddiver.delivery.QUEUE_MAXSIZE", 3)
        await store.update_user(MOD, digest_threshold=3)
        bot.hold()  # fill the queue without letting the worker drain it
        accepted = 0
        for i in range(1, 8):
            if await delivery.enqueue(await claim(store, MOD, i)):
                accepted += 1
        assert accepted == 4  # three queued plus the one the worker is already sending
        assert delivery.metrics.dropped_overflow == 3
        bot.release()
        await settle(delivery)
        assert "отброшено" in bot.sent[-1].text

    async def test_default_queue_size_is_bounded(self):
        assert QUEUE_MAXSIZE == 2000


class TestShutdown:
    async def test_drain_reports_what_is_left(self, delivery, bot, store, sleeps):
        await store.update_user(MOD, digest_threshold=0)
        for i in range(1, 4):
            await delivery.enqueue(await claim(store, MOD, i))
        left = await delivery.drain(timeout=5)
        assert left == 0
        assert len(bot.sent) == 3

    async def test_undelivered_items_stay_pending_for_the_next_start(self, delivery, store, bot):
        item = await claim(store, MOD, 1)
        await delivery.enqueue(item)
        await delivery.drop_user(MOD)  # simulates a hard stop before the worker ran
        assert bot.sent == []
        assert [p.key for p in await store.pending_deliveries()] == [item.key]


class TestRateLimiting:
    async def test_per_user_bucket_spaces_messages_out(self, delivery, bot, store, sleeps):
        await store.update_user(MOD, digest_threshold=0)
        for i in range(1, 4):
            await delivery.enqueue(await claim(store, MOD, i))
        await settle(delivery)
        assert len(bot.sent) == 3
        assert sum(1 for s in sleeps.calls if s >= 0.9) >= 2  # ~1 msg/s into a private chat


class TestTimeRendering:
    async def test_relative_time_is_computed_when_the_message_is_sent(
        self, delivery, bot, store, clock
    ):
        item = await claim(store, MOD, 1, date=(T0 - dt.timedelta(minutes=3)).isoformat())
        await delivery.enqueue(item)
        clock.tick(120)
        await settle(delivery)
        assert "5 мин назад" in bot.sent[0].text


class TestRecoveryFromStrandedRows:
    async def test_overflowed_rows_are_recorded_not_stranded(
        self, delivery, bot, store, monkeypatch
    ):
        monkeypatch.setattr("ddiver.delivery.QUEUE_MAXSIZE", 2)
        bot.hold()
        items = [await claim(store, MOD, i) for i in range(1, 8)]
        for item in items:
            await delivery.enqueue(item)
        bot.release()
        await settle(delivery)
        statuses = {(await store.get_delivery(*i.key)).status for i in items}
        assert DeliveryStatus.PENDING not in statuses  # nothing left for nobody to pick up
        failed = [
            i for i in items if (await store.get_delivery(*i.key)).status is DeliveryStatus.FAILED
        ]
        assert len(failed) == delivery.metrics.dropped_overflow

    async def test_failed_digest_marks_every_item(self, delivery, bot, store):
        await store.update_user(MOD, digest_threshold=3)
        bot.hold()
        items = [await claim(store, MOD, i) for i in range(1, 7)]
        for item in items:
            await delivery.enqueue(item)
        bot.fail_next(None, errors.MessageTooLongError(request=None))
        bot.release()
        await settle(delivery)
        statuses = [(await store.get_delivery(*i.key)).status for i in items]
        assert DeliveryStatus.PENDING not in statuses
        assert statuses.count(DeliveryStatus.FAILED) == 5  # the whole digest batch
        assert delivery.metrics.failed == 5

    async def test_blocked_user_leaves_no_worker_and_rows_come_back_later(
        self, delivery, bot, store
    ):
        bot.fail_next(errors.UserIsBlockedError(request=None))
        first = await claim(store, MOD, 1)
        second = await claim(store, MOD, 2)
        await delivery.enqueue(first)
        await delivery.enqueue(second)
        await settle(delivery)
        assert delivery.queue_length(MOD) == 0
        workers = [t for t in asyncio.all_tasks() if t.get_name() == f"delivery-{MOD}"]
        assert all(t.done() for t in workers)  # no orphan worker spinning on a dead queue
        assert (await store.get_delivery(*second.key)).status is DeliveryStatus.PENDING
        # once the user unblocks the bot, a rescan picks the row up again
        await store.resume_delivery(MOD)
        assert await delivery.replay_pending() == 1
        await settle(delivery)
        assert (await store.get_delivery(*second.key)).status is DeliveryStatus.SENT

    async def test_rescan_does_not_duplicate_queued_rows(self, delivery, bot, store):
        bot.hold()
        item = await claim(store, MOD, 1)
        await delivery.enqueue(item)
        assert await delivery.replay_pending() == 0  # already in memory
        bot.release()
        await settle(delivery)
        assert len(bot.sent) == 1

    async def test_digest_threshold_zero_is_honoured_under_backlog(self, delivery, bot, store):
        await store.update_user(MOD, digest_threshold=0)
        bot.hold()
        for i in range(1, 30):
            await delivery.enqueue(await claim(store, MOD, i))
        bot.release()
        await settle(delivery)
        assert len(bot.sent) == 29  # every match sent on its own, as the user asked


class TestShutdownUnderLoad:
    async def test_cancelling_a_busy_worker_does_not_break_shutdown(self, delivery, bot, store):
        """task_done() must run exactly once, or drain() raises and close() never finishes."""
        bot.hold()
        item = await claim(store, MOD, 1)
        await delivery.enqueue(item)
        await asyncio.sleep(0)  # let the worker pick it up and block inside send
        left = await delivery.drain(timeout=0.05)
        assert left == 0  # the item was in flight, not queued
        assert (await store.get_delivery(*item.key)).status is DeliveryStatus.PENDING
        bot.release()

    async def test_drain_with_a_queued_backlog_leaves_rows_pending(self, delivery, bot, store):
        bot.hold()
        items = [await claim(store, MOD, i) for i in range(1, 5)]
        for item in items:
            await delivery.enqueue(item)
        await asyncio.sleep(0)
        assert await delivery.drain(timeout=0.05) >= 1
        statuses = [(await store.get_delivery(*i.key)).status for i in items]
        assert DeliveryStatus.PENDING in statuses  # recoverable on the next start
        bot.release()


class TestSenderEnrichment:
    async def test_a_missing_name_is_resolved_at_send_time(self, bot, store, clock, sleeps):
        """Basic groups deliver no entities, so the listener can only record the id."""
        from ddiver.senders import SenderCache
        from tests.fakes import make_user

        resolved = []

        async def resolver(peer_id, chat_id=None):
            resolved.append(peer_id)
            return make_user(peer_id, "Сергей", "serge", min_=False)

        delivery = Delivery(
            bot,
            store,
            clock=clock,
            monotonic=sleeps.monotonic,
            sleep=sleeps.sleep,
            senders=SenderCache(resolver, clock=clock, min_interval=0),
        )
        item = await claim(store, MOD, 1, sender_name="", sender_username=None, sender_id=4242)
        await delivery.enqueue(item)
        await settle(delivery)
        assert resolved == [4242]
        assert "Сергей" in bot.sent[0].text
        assert "Неизвестный" not in bot.sent[0].text

    async def test_a_known_name_is_not_resolved_again(self, bot, store, clock, sleeps):
        from ddiver.senders import SenderCache

        resolved = []

        async def resolver(peer_id, chat_id=None):
            resolved.append(peer_id)

        delivery = Delivery(
            bot,
            store,
            clock=clock,
            monotonic=sleeps.monotonic,
            sleep=sleeps.sleep,
            senders=SenderCache(resolver, clock=clock, min_interval=0),
        )
        await delivery.enqueue(await claim(store, MOD, 1))  # the payload already has a name
        await settle(delivery)
        assert resolved == []

    async def test_a_failed_resolve_still_delivers(self, bot, store, clock, sleeps):
        from ddiver.senders import SenderCache

        async def resolver(peer_id, chat_id=None):
            raise ValueError("Could not find the input entity")

        delivery = Delivery(
            bot,
            store,
            clock=clock,
            monotonic=sleeps.monotonic,
            sleep=sleeps.sleep,
            senders=SenderCache(resolver, clock=clock, min_interval=0),
        )
        item = await claim(store, MOD, 1, sender_name="", sender_username=None, sender_id=4242)
        await delivery.enqueue(item)
        await settle(delivery)
        assert len(bot.sent) == 1
        assert "4242" in bot.sent[0].text  # at least the id, so the ignore button makes sense
        assert (await store.get_delivery(*item.key)).status is DeliveryStatus.SENT


class TestRescanDoesNotDuplicate:
    async def test_a_digest_in_flight_is_not_requeued(self, delivery, bot, store):
        """The rescan must tell "in flight" from "nobody is holding this"."""
        await store.update_user(MOD, digest_threshold=3)
        bot.hold()
        items = [await claim(store, MOD, i) for i in range(1, 8)]
        for item in items:
            await delivery.enqueue(item)
        await asyncio.sleep(0)  # the worker collapses the backlog and blocks inside send
        assert await delivery.replay_pending() == 0
        bot.release()
        await settle(delivery)
        assert len(bot.sent) == 2  # one single message plus one digest, nothing twice
        statuses = [(await store.get_delivery(*i.key)).status for i in items]
        assert statuses.count(DeliveryStatus.DIGESTED) == 6

    async def test_rows_for_a_paused_user_leave_the_replay_window(self, delivery, bot, store):
        await store.pause_delivery(MOD, "blocked")
        item = await claim(store, MOD, 1)
        assert await delivery.enqueue(item) is False
        assert (await store.get_delivery(*item.key)).status is DeliveryStatus.FAILED
        assert await store.pending_deliveries() == []

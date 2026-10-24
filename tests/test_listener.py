import asyncio
import datetime as dt
import sqlite3

import pytest
from telethon.tl import types as tl

from ddiver.listener import CursorTracker, Listener, chat_ref_of, should_consider
from ddiver_core.links import ChatRef
from ddiver_core.matcher import Matcher
from ddiver_core.rules import RuleKind
from ddiver_core.senders import SenderCache
from ddiver_core.storage import ALL_CHATS, Cursor, Role, Storage
from ddiver_core_testing import T0
from tests.fakes import make_channel, make_event, make_user

OWNER = 1
MOD = 2
CHAT_ID = -1001234567890
CHAT = make_channel(CHAT_ID, "Moderated chat")
FORUM = make_channel(-1009876543210, "Forum", forum=True)


@pytest.fixture
async def store(tmp_path, clock):
    s = Storage(tmp_path / "db.sqlite3", owner_id=OWNER, clock=clock)
    await s.open()
    clock.now = T0 - dt.timedelta(days=30)  # subscriptions predate every test message
    await s.ensure_user(OWNER)
    await s.update_user(OWNER, lang="ru", tz="Europe/Kyiv")
    await s.ensure_user(MOD)
    await s.update_user(MOD, role=Role.ALLOWED, lang="en", tz="UTC")
    await s.upsert_chat(ChatRef.from_entity(CHAT))
    await s.upsert_chat(ChatRef.from_entity(FORUM))
    await s.subscribe(OWNER, -1001234567890)
    await s.subscribe(OWNER, -1009876543210)
    await s.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "casino")
    clock.now = T0
    yield s
    await s.close()


@pytest.fixture
async def listener(store, clock):
    matcher = Matcher(store, timeout=0.2)
    await matcher.reload()
    delivered = []

    async def deliver(item):
        delivered.append(item)

    listener = Listener(
        store, matcher, SenderCache(None), deliver=deliver, clock=clock, ignore_own=True
    )
    await listener.prime_chat_cache()
    listener.delivered = delivered
    listener.matcher = matcher
    return listener


class TestTextlessMessages:
    """A message with no words in it: a photo, a sticker, a voice note.

    `should_consider` used to end with `return bool(message.message)`, which dropped these
    before anything could ask who sent them — making `from:` rules a lie about the one thing
    they can do that a keyword cannot. The real-Telegram run caught it; nothing offline had.
    """

    async def test_it_is_dropped_when_nobody_watches_a_sender(self, listener, store):
        event = make_event("", chat=CHAT, sender=make_user(555), msg_id=900)
        assert await listener.handle(event.message, chat=event.chat) == []
        assert listener.metrics.skipped_no_text == 1
        assert listener.metrics.considered == 0

    async def test_it_reaches_the_watcher_of_its_sender(self, listener, store):
        await store.add_rule(OWNER, CHAT_ID, RuleKind.INCLUDE, "from:555")
        await listener._matcher.reload()
        event = make_event("", chat=CHAT, sender=make_user(555), msg_id=901)
        claimed = await listener.handle(event.message, chat=event.chat)
        assert [item.msg_id for item in claimed] == [901]
        assert listener.metrics.skipped_no_text == 0

    async def test_a_global_sender_rule_opens_every_chat(self, listener, store):
        await store.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "from:555")
        await listener._matcher.reload()
        event = make_event("", chat=CHAT, sender=make_user(555), msg_id=902)
        assert len(await listener.handle(event.message, chat=event.chat)) == 1

    async def test_a_disabled_sender_rule_does_not_open_the_gate(self, listener, store):
        rule = await store.add_rule(OWNER, CHAT_ID, RuleKind.INCLUDE, "from:555")
        await store.disable_rule(rule.id, "disabled:invalid:whatever")
        await listener._matcher.reload()
        event = make_event("", chat=CHAT, sender=make_user(555), msg_id=903)
        assert await listener.handle(event.message, chat=event.chat) == []
        assert listener.metrics.skipped_no_text == 1

    async def test_somebody_elses_text_still_gets_through(self, listener, store):
        """The change must not turn the ordinary path into a special case."""
        event = make_event("casino", chat=CHAT, sender=make_user(555), msg_id=904)
        assert len(await listener.handle(event.message, chat=event.chat)) == 1


class TestShouldConsider:
    def test_plain_message(self):
        assert should_consider(make_event("casino").message)

    def test_service_message_is_ignored(self):
        service = tl.MessageService(
            id=1,
            peer_id=tl.PeerChannel(1),
            date=T0,
            action=tl.MessageActionChatJoinedByLink(inviter_id=1),
        )
        assert not should_consider(service)

    def test_media_without_caption_is_ignored(self):
        message = make_event("").message
        message.message = None
        assert not should_consider(message)
        message.message = ""
        assert not should_consider(message)

    def test_outgoing_is_ignored_unless_the_test_flag_is_set(self):
        message = make_event("casino", out=True).message
        assert not should_consider(message)
        assert should_consider(message, ignore_own=False)


class TestHandle:
    async def test_match_claims_an_outbox_row_and_delivers(self, listener, store, clock):
        event = make_event("Come play at the casino", chat=CHAT)
        items = await listener.handle(event.message, chat=event.chat)
        assert len(items) == 1
        item = items[0]
        assert item.user_id == OWNER
        assert item.payload["chat_title"] == "Moderated chat"
        assert item.payload["spans"] == [[17, 23]]
        assert item.payload["sender_name"] == "Alex"
        assert item.payload["sender_username"] == "taras"
        assert item.payload["catchup"] is False
        assert listener.delivered == items
        # the cursor only advances after the row exists
        assert listener.cursors.pending[-1001234567890][0] == event.message.id

    async def test_no_match_still_advances_the_cursor(self, listener):
        event = make_event("nothing interesting", chat=CHAT)
        assert await listener.handle(event.message, chat=event.chat) == []
        assert listener.cursors.pending[-1001234567890][0] == event.message.id

    async def test_unmonitored_chat_is_ignored(self, listener):
        other = make_channel(-1005555555555, "Other")
        event = make_event("casino", chat=other)
        assert await listener.handle(event.message, chat=event.chat) == []
        assert listener.cursors.pending == {}

    async def test_private_chat_is_never_monitored(self, listener, store):
        message = make_event("casino").message
        message.peer_id = tl.PeerUser(4242)
        assert await listener.handle(message) == []
        assert listener.metrics.skipped_private == 1

    async def test_bot_senders_are_dropped_after_matching(self, listener):
        bot_user = make_user(77, "Spam Bot", "spambot", bot=True)
        event = make_event("casino", chat=CHAT, sender=bot_user)
        assert await listener.handle(event.message, chat=event.chat) == []
        assert listener.metrics.skipped_bot == 1
        assert listener.cursors.pending[-1001234567890][0] == event.message.id

    async def test_a_moderator_can_opt_into_bot_messages(self, listener, store):
        """The skip is per moderator, so one opting in must not change it for the others."""
        bot_user = make_user(77, "Spam Bot", "spambot", bot=True)
        await store.update_user(OWNER, include_bots=True)
        await listener.matcher.invalidate(None)

        event = make_event("casino", chat=CHAT, sender=bot_user)
        items = await listener.handle(event.message, chat=event.chat)
        assert [i.user_id for i in items] == [OWNER]
        assert items[0].payload["sender_id"] == 77

    async def test_turning_it_off_again_restores_the_skip(self, listener, store):
        bot_user = make_user(77, "Spam Bot", "spambot", bot=True)
        await store.update_user(OWNER, include_bots=True)
        await listener.matcher.invalidate(None)
        assert await listener.handle(
            make_event("casino", chat=CHAT, sender=bot_user).message, chat=CHAT
        )

        await store.update_user(OWNER, include_bots=False)
        await listener.matcher.invalidate(None)
        event = make_event("casino", chat=CHAT, sender=bot_user, msg_id=987)
        assert await listener.handle(event.message, chat=event.chat) == []
        assert listener.metrics.skipped_bot == 1

    async def test_own_messages_are_skipped(self, listener):
        event = make_event("casino", chat=CHAT, out=True)
        assert await listener.handle(event.message, chat=event.chat) == []
        assert listener.metrics.skipped_own == 1

    async def test_forwarded_and_topic_metadata(self, listener):
        event = make_event("casino", chat=FORUM, topic_id=7, fwd_from_name="Someone")
        items = await listener.handle(event.message, chat=event.chat)
        assert items[0].payload["topic_id"] == 7
        assert items[0].payload["is_forwarded"] is True

    async def test_duplicate_message_is_claimed_once(self, listener):
        event = make_event("casino", chat=CHAT)
        assert len(await listener.handle(event.message, chat=event.chat)) == 1
        assert await listener.handle(event.message, chat=event.chat) == []
        assert listener.metrics.duplicates == 1

    async def test_every_subscriber_gets_their_own_row(self, listener, store, clock):
        clock.now = T0 - dt.timedelta(days=1)  # subscribe before the message we will send
        await store.set_chat_shared(-1001234567890, True)
        await store.subscribe(MOD, -1001234567890)
        clock.now = T0
        await store.add_rule(MOD, ALL_CHATS, RuleKind.INCLUDE, "casino")
        await listener.matcher.invalidate()
        event = make_event("casino", chat=CHAT)
        items = await listener.handle(event.message, chat=event.chat)
        assert {i.user_id for i in items} == {OWNER, MOD}

    async def test_all_occurrences_are_highlighted(self, listener):
        event = make_event("casino and more casino", chat=CHAT)
        items = await listener.handle(event.message, chat=event.chat)
        assert items[0].payload["spans"] == [[0, 6], [16, 22]]

    async def test_zero_width_text_maps_spans_back_to_the_original(self, listener):
        event = make_event("ads ca\u200bsino here", chat=CHAT)
        items = await listener.handle(event.message, chat=event.chat)
        start, end = items[0].payload["spans"][0]
        assert event.message.message[start:end] == "ca\u200bsino"

    async def test_anonymous_admin_sender(self, listener):
        event = make_event("casino", chat=CHAT, sender=CHAT)
        items = await listener.handle(event.message, chat=event.chat)
        assert items[0].payload["sender_id"] == -1001234567890
        assert items[0].payload["sender_is_channel"] is True

    async def test_chat_metadata_is_refreshed_when_it_changes(self, listener, store):
        renamed = make_channel(-1001234567890, "New title", username="newname")
        event = make_event("casino", chat=renamed)
        await listener.handle(event.message, chat=event.chat)
        row = await store.get_chat(-1001234567890)
        assert row.ref.title == "New title"
        assert row.ref.username == "newname"

    async def test_rule_hits_are_counted_and_flushed(self, listener, store):
        rules = await store.list_rules(OWNER)
        for i in range(3):
            event = make_event("casino", chat=CHAT, msg_id=100 + i)
            await listener.handle(event.message, chat=event.chat)
        await listener.matcher.flush_hits()
        assert (await store.get_rule(rules[0].id, OWNER)).hits == 3

    async def test_on_message_never_raises(self, listener, caplog):
        class Broken:
            @property
            def message(self):
                raise RuntimeError("boom")

            chat = None

        await listener.on_message(Broken())
        assert "listener failed" in caplog.text


class TestCursorTracker:
    async def test_keeps_the_highest_id_and_flushes(self, store):
        tracker = CursorTracker(store)
        tracker.note(-1001, 10, T0)
        tracker.note(-1001, 5, T0)
        tracker.note(-1002, 3, T0)
        assert await tracker.flush() == 2
        cursors = await store.get_cursors()
        assert cursors[-1001].last_msg_id == 10
        assert tracker.pending == {}

    async def test_blocked_chats_are_withheld(self, store):
        tracker = CursorTracker(store)
        tracker.note(-1001, 10, T0)
        tracker.note(-1002, 4, T0)
        tracker.block(-1001)
        assert await tracker.flush() == 1
        assert set(await store.get_cursors()) == {-1002}
        assert -1001 in tracker.pending
        tracker.unblock(-1001)
        assert await tracker.flush() == 1
        assert set(await store.get_cursors()) == {-1001, -1002}

    async def test_flush_with_nothing_pending(self, store):
        assert await CursorTracker(store).flush() == 0

    async def test_existing_cursor_is_never_regressed(self, store):
        await store.upsert_cursors([Cursor(-1001, 50, T0)])
        tracker = CursorTracker(store)
        tracker.note(-1001, 20, T0 + dt.timedelta(seconds=5))
        await tracker.flush()
        assert (await store.get_cursors())[-1001].last_msg_id == 50


class TestChatRefOf:
    def test_users_and_none_are_rejected(self):
        assert chat_ref_of(None) is None
        assert chat_ref_of(make_user()) is None

    def test_channel_and_forbidden(self):
        assert chat_ref_of(CHAT).title == "Moderated chat"
        forbidden = tl.ChannelForbidden(id=5, access_hash=0, title="Gone", megagroup=True)
        assert chat_ref_of(forbidden).kind == "supergroup"


class TestSubscriptionBoundary:
    async def test_messages_from_before_a_subscription_are_not_delivered(
        self, listener, store, clock
    ):
        """Someone else's cursor can reach back further than this user's subscription."""
        clock.now = T0
        await store.set_chat_shared(-1001234567890, True)
        await store.subscribe(MOD, -1001234567890)
        await store.add_rule(MOD, ALL_CHATS, RuleKind.INCLUDE, "casino")
        await listener.matcher.invalidate()

        old = make_event("casino", chat=CHAT, msg_id=500, date=T0 - dt.timedelta(hours=2))
        items = await listener.handle(old.message, chat=old.chat)
        assert {i.user_id for i in items} == {OWNER}  # the owner subscribed 30 days ago

        fresh = make_event("casino", chat=CHAT, msg_id=501, date=T0 + dt.timedelta(minutes=1))
        items = await listener.handle(fresh.message, chat=fresh.chat)
        assert {i.user_id for i in items} == {OWNER, MOD}

    async def test_a_busy_database_does_not_advance_the_cursor(self, listener, store, monkeypatch):
        """The row has to exist before the cursor moves, or catch-up cannot find the message."""
        import sqlite3

        async def busy(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(store, "claim_delivery", busy)
        event = make_event("casino", chat=CHAT, msg_id=900)
        with pytest.raises(sqlite3.OperationalError):
            await listener.handle(event.message, chat=event.chat)
        assert listener.cursors.pending == {}


class TestUnmonitoredAreCounted:
    async def test_a_message_from_a_chat_nobody_watches_is_counted(self, listener, store):
        """Otherwise the gap between `seen` and `considered` has no explanation on the graph.

        The Telethon handler is registered without `chats=`, so every dialog the account is
        in delivers here. On an account in three hundred chats watching none, `seen` climbs
        steadily and everything else stays flat — which reads as a bug until this counter
        says where the messages went.
        """
        await store.upsert_chat(ChatRef(-100500, "Chat with no subscription", None, "supergroup"))
        other = make_channel(-100500, "Chat with no subscription")
        message = make_event("anything", chat=other).message

        assert await listener.handle(message) == []
        assert listener.metrics.seen == 1
        assert listener.metrics.skipped_unmonitored == 1
        assert listener.metrics.considered == 0

    async def test_a_watched_chat_is_not_counted_as_skipped(self, listener):
        event = make_event("casino", chat=CHAT)
        await listener.handle(event.message, chat=event.chat)
        assert listener.metrics.skipped_unmonitored == 0
        assert listener.metrics.considered == 1


class TestCursorNeverPassesAnUnfinishedMessage:
    """The cursor is the only thing that decides what catch-up will ever look at again.

    Anything below it is unreachable forever, so it must never move past a message that has
    not finished being examined — whether the next message overtook it, or its own claim
    failed and left it undelivered.
    """

    async def test_a_failed_claim_holds_the_cursor_behind_it(self, listener, store, clock):
        """Reproduces the loss directly: no concurrency needed, just one failure."""
        boom = make_event("casino", chat=CHAT, msg_id=100)

        async def failing_claim(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        store.claim_delivery = failing_claim
        with pytest.raises(sqlite3.OperationalError):
            await listener.handle(boom.message, chat=boom.chat)

        # the very next message must not carry the cursor over the one that failed
        later = make_event("nothing interesting", chat=CHAT, msg_id=101)
        await listener.handle(later.message, chat=later.chat)
        await listener.cursors.flush()

        cursors = await store.get_cursors()
        stored = cursors.get(CHAT_ID)
        assert stored is None or stored.last_msg_id < 100, (
            "message 100 fell below the cursor and can never be fetched again"
        )

    async def test_a_later_message_cannot_overtake_an_earlier_one(self, listener, store):
        """Telethon dispatches each update in its own task; 101 finishes while 100 awaits."""
        started = asyncio.Event()
        may_finish = asyncio.Event()
        real_since = store.subscribed_since

        async def slow_since(chat_id):
            started.set()
            await may_finish.wait()
            return await real_since(chat_id)

        store.subscribed_since = slow_since
        first = make_event("casino", chat=CHAT, msg_id=200)
        slow = asyncio.create_task(listener.handle(first.message, chat=first.chat))
        await started.wait()

        second = make_event("nothing", chat=CHAT, msg_id=201)
        await listener.handle(second.message, chat=second.chat)
        await listener.cursors.flush()

        cursors = await store.get_cursors()
        stored = cursors.get(CHAT_ID)
        assert stored is None or stored.last_msg_id < 200, (
            f"cursor reached {stored.last_msg_id} while 200 was still in flight"
        )

        may_finish.set()
        await slow
        await listener.cursors.flush()
        assert (await store.get_cursors())[CHAT_ID].last_msg_id == 201

    async def test_an_ordinary_run_still_advances(self, listener, store):
        """The guard must not stop the cursor moving when nothing is in flight."""
        for msg_id in (300, 301, 302):
            event = make_event("nothing", chat=CHAT, msg_id=msg_id)
            await listener.handle(event.message, chat=event.chat)
        await listener.cursors.flush()
        assert (await store.get_cursors())[CHAT_ID].last_msg_id == 302
        assert listener.cursors.held_ids(CHAT_ID) == set()

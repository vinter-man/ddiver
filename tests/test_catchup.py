import datetime as dt

import pytest
from telethon import errors
from telethon.tl import types as tl

from ddiver.catchup import Catchup
from ddiver.links import ChatRef
from ddiver.listener import Listener
from ddiver.matcher import Matcher
from ddiver.rules import RuleKind
from ddiver.senders import SenderCache
from ddiver.storage import ALL_CHATS, Cursor, Storage
from tests.fakes import T0, FakeClock, make_channel, make_event

OWNER = 1
CHAT_ID = -1001234567890
OTHER_ID = -1005555555555
CHAT = make_channel(CHAT_ID, "Чат")
OTHER = make_channel(OTHER_ID, "Другой")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
async def store(tmp_path, clock):
    s = Storage(tmp_path / "db.sqlite3", owner_id=OWNER, clock=clock)
    await s.open()
    clock.now = T0 - dt.timedelta(days=30)  # subscriptions predate every test message
    await s.ensure_user(OWNER)
    await s.update_user(OWNER, lang="ru", tz="UTC")
    await s.upsert_chat(ChatRef.from_entity(CHAT))
    await s.upsert_chat(ChatRef.from_entity(OTHER))
    await s.subscribe(OWNER, CHAT_ID)
    await s.subscribe(OWNER, OTHER_ID)
    await s.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "казино")
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

    listener = Listener(store, matcher, SenderCache(None), deliver=deliver, clock=clock)
    await listener.prime_chat_cache()
    listener.delivered = delivered
    listener.matcher = matcher
    return listener


class FakeHistory:
    """Newest-first history, like Telethon's ``iter_messages(min_id=…)``."""

    def __init__(self, messages: dict[int, list[tl.Message]]) -> None:
        self.messages = messages
        self.calls: list[tuple[int, int, int]] = []
        self.peers: list[object] = []
        self.errors: dict[int, BaseException] = {}

    def __call__(self, chat, min_id: int, limit: int):
        chat_id = chat.id
        self.calls.append((chat_id, min_id, limit))
        self.peers.append(chat)
        error = self.errors.get(chat_id)

        async def gen():
            if error is not None:
                raise error
            ordered = sorted(self.messages.get(chat_id, []), key=lambda m: m.id, reverse=True)
            for count, message in enumerate(ordered, start=1):
                if message.id <= min_id:
                    return
                yield message
                if count >= limit:
                    return

        return gen()


def history_of(chat, ids_and_texts, *, base_time=T0, step=60):
    messages = []
    for index, (msg_id, text) in enumerate(ids_and_texts):
        event = make_event(
            text, chat=chat, msg_id=msg_id, date=base_time - dt.timedelta(seconds=step * index)
        )
        messages.append(event.message)
    return messages


@pytest.fixture
def sleeps():
    calls = []

    async def sleep(seconds):
        calls.append(seconds)

    sleep.calls = calls
    return sleep


class TestCatchup:
    async def test_replays_newest_messages_in_chronological_order(
        self, store, listener, clock, sleeps
    ):
        await store.upsert_cursors([Cursor(CHAT_ID, 100, T0 - dt.timedelta(minutes=30))])
        history = FakeHistory(
            {
                CHAT_ID: history_of(
                    CHAT,
                    [(103, "казино три"), (102, "тишина"), (101, "казино один")],
                )
            }
        )
        catchup = Catchup(store, listener, history, clock=clock, sleep=sleeps, hours=24)
        result = await catchup.run({CHAT_ID})
        assert history.calls == [(CHAT_ID, 100, 500)]
        assert result.chats == 1
        assert result.claimed == 2
        assert [i.msg_id for i in listener.delivered] == [101, 103]  # oldest first
        assert all(i.payload["catchup"] is True for i in listener.delivered)
        assert (await store.get_cursors())[CHAT_ID].last_msg_id == 103

    async def test_old_cursor_still_delivers_recent_messages(self, store, listener, clock, sleeps):
        # The cursor is three days old; only messages inside the window must be delivered.
        await store.upsert_cursors([Cursor(CHAT_ID, 10, T0 - dt.timedelta(days=3))])
        messages = history_of(
            CHAT,
            [(30, "казино свежее"), (20, "казино старое")],
            step=0,
        )
        messages[1].date = T0 - dt.timedelta(days=2)
        history = FakeHistory({CHAT_ID: messages})
        catchup = Catchup(store, listener, history, clock=clock, sleep=sleeps, hours=24)
        result = await catchup.run({CHAT_ID})
        assert [i.msg_id for i in listener.delivered] == [30]
        assert result.claimed == 1
        assert (await store.get_cursors())[CHAT_ID].last_msg_id == 30

    async def test_cap_is_reported_on_the_first_notification(self, store, listener, clock, sleeps):
        await store.upsert_cursors([Cursor(CHAT_ID, 0, T0)])
        history = FakeHistory(
            {CHAT_ID: history_of(CHAT, [(i, "казино") for i in range(20, 0, -1)], step=1)}
        )
        catchup = Catchup(store, listener, history, clock=clock, sleep=sleeps, per_chat=5)
        result = await catchup.run({CHAT_ID})
        assert result.capped_chats == [CHAT_ID]
        assert [i.msg_id for i in listener.delivered] == [16, 17, 18, 19, 20]
        assert listener.delivered[0].payload["catchup_skipped"] == 5
        assert listener.delivered[1].payload["catchup_skipped"] == 0

    async def test_chats_without_a_cursor_are_skipped(self, store, listener, clock, sleeps):
        history = FakeHistory({CHAT_ID: history_of(CHAT, [(1, "казино")])})
        result = await catchup_of(store, listener, history, clock, sleeps).run({CHAT_ID})
        assert history.calls == []
        assert result.chats == 0

    async def test_unavailable_chats_are_not_probed(self, store, listener, clock, sleeps):
        await store.upsert_cursors([Cursor(CHAT_ID, 1, T0)])
        await store.set_chat_available(CHAT_ID, False)
        history = FakeHistory({CHAT_ID: history_of(CHAT, [(2, "казино")])})
        result = await catchup_of(store, listener, history, clock, sleeps).run({CHAT_ID})
        assert history.calls == []
        assert result.skipped == 1

    async def test_lost_access_marks_the_chat_and_notifies_once(
        self, store, listener, clock, sleeps
    ):
        notified = []

        async def on_unavailable(chat_id, reason):
            notified.append((chat_id, reason))

        await store.upsert_cursors([Cursor(CHAT_ID, 1, T0)])
        history = FakeHistory({})
        history.errors[CHAT_ID] = errors.ChannelPrivateError(request=None)
        catchup = Catchup(
            store, listener, history, clock=clock, sleep=sleeps, on_unavailable=on_unavailable
        )
        result = await catchup.run({CHAT_ID})
        assert result.unavailable == [CHAT_ID]
        assert notified == [(CHAT_ID, "ChannelPrivateError")]
        assert (await store.get_chat(CHAT_ID)).available is False

    async def test_flood_wait_skips_only_that_chat(self, store, listener, clock, sleeps):
        await store.upsert_cursors(
            [Cursor(CHAT_ID, 1, T0), Cursor(OTHER_ID, 1, T0)],
        )
        history = FakeHistory({OTHER_ID: history_of(OTHER, [(5, "казино")])})
        history.errors[CHAT_ID] = errors.FloodWaitError(request=None, capture=30)
        catchup = Catchup(store, listener, history, clock=clock, sleep=sleeps)
        result = await catchup.run({CHAT_ID, OTHER_ID})
        assert result.flood_waits == 1
        assert result.claimed == 1
        assert [i.chat_id for i in listener.delivered] == [OTHER_ID]
        assert (await store.get_chat(CHAT_ID)).available is True  # a flood wait is temporary

    async def test_cursor_is_withheld_until_the_chat_finished(self, store, listener, clock):
        await store.upsert_cursors([Cursor(CHAT_ID, 1, T0)])
        history = FakeHistory({CHAT_ID: history_of(CHAT, [(9, "казино")])})
        seen_blocked = []

        async def sleep(_seconds):
            seen_blocked.append(set(listener.cursors.blocked))

        catchup = Catchup(store, listener, history, clock=clock, sleep=sleep)

        original = listener.handle

        async def spy(message, **kwargs):
            seen_blocked.append(set(listener.cursors.blocked))
            return await original(message, **kwargs)

        listener.handle = spy
        await catchup.run({CHAT_ID})
        assert seen_blocked[0] == {CHAT_ID}  # blocked while replaying
        assert listener.cursors.blocked == set()  # released afterwards

    async def test_total_budget_stops_the_round(self, store, listener, clock, sleeps):
        await store.upsert_cursors([Cursor(CHAT_ID, 0, T0), Cursor(OTHER_ID, 0, T0)])
        history = FakeHistory(
            {
                CHAT_ID: history_of(CHAT, [(i, "казино") for i in range(10, 0, -1)], step=1),
                OTHER_ID: history_of(OTHER, [(i, "казино") for i in range(10, 0, -1)], step=1),
            }
        )
        catchup = Catchup(store, listener, history, clock=clock, sleep=sleeps, total=5)
        result = await catchup.run({CHAT_ID, OTHER_ID})
        assert result.chats == 1
        assert len(history.calls) == 1  # the second chat is left for the next round

    async def test_service_and_own_messages_are_filtered(self, store, listener, clock, sleeps):
        await store.upsert_cursors([Cursor(CHAT_ID, 0, T0)])
        own = make_event("казино", chat=CHAT, msg_id=3, out=True).message
        service = tl.MessageService(
            id=2,
            peer_id=tl.PeerChannel(1234567890),
            date=T0,
            action=tl.MessageActionChatJoinedByLink(inviter_id=1),
        )
        normal = make_event("казино", chat=CHAT, msg_id=1).message
        history = FakeHistory({CHAT_ID: [own, service, normal]})
        result = await catchup_of(store, listener, history, clock, sleeps).run({CHAT_ID})
        assert result.claimed == 1
        assert [i.msg_id for i in listener.delivered] == [1]
        assert (await store.get_cursors())[CHAT_ID].last_msg_id == 3  # cursor still advances

    async def test_disabled_by_configuration(self, store, listener, clock, sleeps):
        await store.upsert_cursors([Cursor(CHAT_ID, 0, T0)])
        history = FakeHistory({CHAT_ID: history_of(CHAT, [(1, "казино")])})
        catchup = Catchup(store, listener, history, clock=clock, sleep=sleeps, hours=0)
        assert (await catchup.run({CHAT_ID})).chats == 0
        assert history.calls == []

    async def test_dedupe_between_catchup_and_live(self, store, listener, clock, sleeps):
        await store.upsert_cursors([Cursor(CHAT_ID, 0, T0)])
        event = make_event("казино", chat=CHAT, msg_id=7)
        await listener.handle(event.message, chat=event.chat)  # live first
        history = FakeHistory({CHAT_ID: [event.message]})
        result = await catchup_of(store, listener, history, clock, sleeps).run({CHAT_ID})
        assert result.claimed == 0
        assert len(listener.delivered) == 1


def catchup_of(store, listener, history, clock, sleeps) -> Catchup:
    return Catchup(store, listener, history, clock=clock, sleep=sleeps)


class TestRoundSafety:
    async def test_every_chat_in_the_round_holds_its_cursor(self, store, listener, clock):
        """A live message in a chat still queued must not flush a cursor past the gap."""
        await store.upsert_cursors([Cursor(CHAT_ID, 1, T0), Cursor(OTHER_ID, 1, T0)])
        history = FakeHistory({CHAT_ID: history_of(CHAT, [(5, "казино")])})
        blocked_during_replay: set[int] = set()

        original = listener.handle

        async def spy(message, **kwargs):
            blocked_during_replay.update(listener.cursors.blocked)
            return await original(message, **kwargs)

        listener.handle = spy

        async def sleep(_seconds):
            return None

        catchup = Catchup(store, listener, history, clock=clock, sleep=sleep)
        await catchup.run({CHAT_ID, OTHER_ID})
        assert blocked_during_replay == {CHAT_ID, OTHER_ID}
        assert listener.cursors.blocked == set()  # released in the finally

    async def test_a_failing_message_does_not_abandon_the_round(
        self, store, listener, clock, sleeps
    ):
        await store.upsert_cursors([Cursor(CHAT_ID, 0, T0), Cursor(OTHER_ID, 0, T0)])
        history = FakeHistory(
            {
                CHAT_ID: history_of(CHAT, [(2, "казино два"), (1, "казино один")], step=1),
                OTHER_ID: history_of(OTHER, [(3, "казино три")]),
            }
        )
        original = listener.handle
        calls = {"n": 0}

        async def flaky(message, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database is busy")
            return await original(message, **kwargs)

        listener.handle = flaky
        result = await Catchup(store, listener, history, clock=clock, sleep=sleeps).run(
            {CHAT_ID, OTHER_ID}
        )
        # chats are visited in id order, so the failing message is the other chat's; the round
        # carries on and replays everything else
        assert result.chats == 2
        assert [i.msg_id for i in listener.delivered] == [1, 2]

    async def test_unresolvable_peer_is_transient_not_lost_access(
        self, store, listener, clock, sleeps
    ):
        notified = []

        async def on_unavailable(chat_id, reason):
            notified.append((chat_id, reason))

        await store.upsert_cursors([Cursor(CHAT_ID, 1, T0)])
        history = FakeHistory({})
        history.errors[CHAT_ID] = ValueError("Could not find the input entity")
        catchup = Catchup(
            store, listener, history, clock=clock, sleep=sleeps, on_unavailable=on_unavailable
        )
        result = await catchup.run({CHAT_ID})
        assert result.unavailable == []
        assert notified == []
        assert (await store.get_chat(CHAT_ID)).available is True

    async def test_reprobe_restores_a_chat_that_works_again(self, store, listener, clock, sleeps):
        recovered = []

        async def on_recovered(chat_id):
            recovered.append(chat_id)

        await store.upsert_cursors([Cursor(CHAT_ID, 1, T0)])
        await store.set_chat_available(CHAT_ID, False)
        history = FakeHistory({CHAT_ID: history_of(CHAT, [(9, "казино")])})
        catchup = Catchup(
            store, listener, history, clock=clock, sleep=sleeps, on_recovered=on_recovered
        )
        assert await catchup.run({CHAT_ID}) is not None
        assert history.calls == []  # a normal round still skips it

        clock.tick(7 * 3600)
        result = await catchup.reprobe_unavailable()
        assert result.recovered == [CHAT_ID]
        assert recovered == [CHAT_ID]
        assert (await store.get_chat(CHAT_ID)).available is True
        assert [i.msg_id for i in listener.delivered] == [9]

    async def test_reprobe_leaves_a_still_broken_chat_alone(self, store, listener, clock, sleeps):
        await store.upsert_cursors([Cursor(CHAT_ID, 1, T0)])
        await store.set_chat_available(CHAT_ID, False)
        history = FakeHistory({})
        history.errors[CHAT_ID] = errors.ChannelPrivateError(request=None)
        catchup = Catchup(store, listener, history, clock=clock, sleep=sleeps)
        clock.tick(7 * 3600)
        result = await catchup.reprobe_unavailable()
        assert result.recovered == []
        assert (await store.get_chat(CHAT_ID)).available is False


class TestInputPeers:
    def test_built_from_the_stored_access_hash(self):
        from telethon.tl import types as tl_types

        from ddiver.catchup import input_peer_for
        from ddiver.storage import ChatRow

        supergroup = ChatRow(ChatRef(-1001234567890, "S", None, "supergroup"), True, True, 999)
        peer = input_peer_for(supergroup)
        assert isinstance(peer, tl_types.InputPeerChannel)
        assert (peer.channel_id, peer.access_hash) == (1234567890, 999)

        basic = ChatRow(ChatRef(-555, "B", None, "group"), True, True, None)
        assert input_peer_for(basic) == tl_types.InputPeerChat(555)

        unknown = ChatRow(ChatRef(-1009, "U", None, "supergroup"), True, True, None)
        assert input_peer_for(unknown) == -1009  # fall back to Telethon's own resolution


class TestCursorHonesty:
    async def test_a_failed_replay_holds_the_cursor_back(self, store, listener, clock, sleeps):
        """A message we could not handle must be re-fetched, not skipped."""
        await store.upsert_cursors([Cursor(CHAT_ID, 0, T0)])
        history = FakeHistory(
            {
                CHAT_ID: history_of(
                    CHAT, [(3, "казино три"), (2, "казино два"), (1, "казино")], step=1
                )
            }
        )
        original = listener.handle

        async def flaky(message, **kwargs):
            if message.id == 2:
                raise RuntimeError("database is locked")
            return await original(message, **kwargs)

        listener.handle = flaky
        await Catchup(store, listener, history, clock=clock, sleep=sleeps).run({CHAT_ID})
        assert (await store.get_cursors())[CHAT_ID].last_msg_id == 1  # not 3
        assert [i.msg_id for i in listener.delivered] == [1, 3]

    async def test_an_unfinished_chat_keeps_its_stored_cursor(self, store, listener, clock, sleeps):
        """The budget break must not let a live cursor jump over the unexamined gap."""
        await store.upsert_cursors([Cursor(CHAT_ID, 100, T0), Cursor(OTHER_ID, 100, T0)])
        history = FakeHistory(
            {
                CHAT_ID: history_of(CHAT, [(i, "казино") for i in range(200, 100, -1)], step=1),
                OTHER_ID: history_of(OTHER, [(i, "казино") for i in range(200, 100, -1)], step=1),
            }
        )
        catchup = Catchup(store, listener, history, clock=clock, sleep=sleeps, total=5)

        # a live message lands in the chat the round will not reach
        live = make_event("казино", chat=CHAT, msg_id=5000)
        original_run = catchup.run

        async def run_with_live_traffic(chat_ids):
            listener.cursors.note(CHAT_ID, 5000, T0)
            listener.cursors.note(OTHER_ID, 5000, T0)
            return await original_run(chat_ids)

        del live
        await run_with_live_traffic({CHAT_ID, OTHER_ID})
        cursors = await store.get_cursors()
        # the chat the round examined keeps its live cursor; the one it never reached stays put
        examined = [c for c in (CHAT_ID, OTHER_ID) if cursors[c].last_msg_id == 5000]
        skipped = [c for c in (CHAT_ID, OTHER_ID) if cursors[c].last_msg_id == 100]
        assert len(examined) == 1, cursors
        assert len(skipped) == 1, cursors

    async def test_recovery_refreshes_the_monitored_set_before_replaying(
        self, store, listener, clock, sleeps
    ):
        invalidated = []

        async def on_available(chat_id):
            invalidated.append(chat_id)
            await listener.matcher.invalidate(chat_id)

        await store.upsert_cursors([Cursor(CHAT_ID, 1, T0)])
        await store.set_chat_available(CHAT_ID, False)
        await listener.matcher.reload()
        assert not listener.matcher.is_monitored(CHAT_ID)

        history = FakeHistory({CHAT_ID: history_of(CHAT, [(9, "казино")])})
        catchup = Catchup(
            store, listener, history, clock=clock, sleep=sleeps, on_available=on_available
        )
        clock.tick(7 * 3600)
        result = await catchup.reprobe_unavailable()
        assert invalidated == [CHAT_ID]
        assert result.claimed == 1  # the backlog was actually delivered, not dropped
        assert [i.msg_id for i in listener.delivered] == [9]

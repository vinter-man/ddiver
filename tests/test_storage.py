import datetime as dt

import pytest

import ddiver.storage as storage_module
from ddiver.links import ChatRef
from ddiver.rules import RuleKind
from ddiver.storage import (
    ALL_CHATS,
    Cursor,
    DeliveryStatus,
    DuplicateRuleError,
    Role,
    Storage,
)

T0 = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.UTC)


class FakeClock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += dt.timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
async def store(tmp_path, clock):
    s = Storage(tmp_path / "db.sqlite3", clock=clock)
    await s.open()
    yield s
    await s.close()


CHAT_A = ChatRef(-1001, "Alpha", "alpha", "supergroup")
CHAT_B = ChatRef(-1002, "beta", None, "supergroup", is_forum=True)


async def test_migrations_run_once_and_are_idempotent(tmp_path, clock):
    s = Storage(tmp_path / "db.sqlite3", clock=clock)
    await s.open()
    assert await s.schema_version() == 1
    await s.close()
    s2 = Storage(tmp_path / "db.sqlite3", clock=clock)
    await s2.open()
    assert await s2.schema_version() == 1
    await s2.close()


async def test_users_lifecycle(store):
    u = await store.ensure_user(1, role=Role.OWNER)
    assert (u.role, u.lang, u.tz, u.onboarded, u.paused) == (Role.OWNER, "en", "UTC", False, False)
    # ensure_user never downgrades an existing row
    again = await store.ensure_user(1)
    assert again.role is Role.OWNER
    u = await store.update_user(1, lang="uk", tz="Europe/Kyiv", onboarded=True)
    assert (u.lang, u.tz, u.onboarded) == ("uk", "Europe/Kyiv", True)
    with pytest.raises(ValueError, match="unknown user fields"):
        await store.update_user(1, hacker="x")
    with pytest.raises(KeyError):
        await store.update_user(999, lang="en")
    await store.ensure_user(2)
    assert [x.id for x in await store.list_users(roles={Role.PENDING})] == [2]
    assert Role.PENDING.can_use is False and Role.ALLOWED.can_use is True


async def test_chats_and_subscriptions(store):
    await store.ensure_user(1, role=Role.OWNER)
    await store.ensure_user(2, role=Role.ALLOWED)
    await store.ensure_user(3, role=Role.PENDING)
    await store.upsert_chat(CHAT_A)
    await store.upsert_chat(CHAT_B)
    assert await store.subscribe(1, CHAT_A.id) is True
    assert await store.subscribe(1, CHAT_A.id) is False  # idempotent
    await store.subscribe(2, CHAT_B.id)
    await store.subscribe(3, CHAT_B.id)  # pending user must not make a chat monitored
    assert await store.monitored_chat_ids() == {CHAT_A.id, CHAT_B.id}
    assert await store.subscribers_of(CHAT_B.id) == [2, 3]
    assert [c.ref.title for c in await store.list_subscriptions(1)] == ["Alpha"]
    assert await store.is_subscribed(1, CHAT_A.id)
    # title refresh via upsert
    await store.upsert_chat(ChatRef(CHAT_A.id, "Alpha2", "alpha2", "supergroup"))
    chat = await store.get_chat(CHAT_A.id)
    assert chat is not None and chat.ref.title == "Alpha2" and chat.ref.username == "alpha2"
    await store.set_chat_available(CHAT_A.id, False)
    chat = await store.get_chat(CHAT_A.id)
    assert chat is not None and chat.available is False
    assert await store.unsubscribe(1, CHAT_A.id) is True
    assert await store.unsubscribe(1, CHAT_A.id) is False
    # paused users do not keep chats monitored
    await store.update_user(2, paused=True)
    assert await store.monitored_chat_ids() == set()


async def test_subscription_requires_known_chat(store):
    await store.ensure_user(1, role=Role.OWNER)
    with pytest.raises(Exception, match="FOREIGN KEY"):
        await store.subscribe(1, -999)


async def test_rules(store):
    await store.ensure_user(1, role=Role.OWNER)
    await store.ensure_user(2, role=Role.ALLOWED)
    await store.upsert_chat(CHAT_A)
    await store.upsert_chat(CHAT_B)
    await store.subscribe(1, CHAT_A.id)
    await store.subscribe(2, CHAT_A.id)
    r1 = await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, "казино")
    r2 = await store.add_rule(1, ALL_CHATS, RuleKind.INCLUDE, "=бан")
    r3 = await store.add_rule(1, CHAT_A.id, RuleKind.EXCLUDE, "re:test")
    r4 = await store.add_rule(2, CHAT_B.id, RuleKind.INCLUDE, "unsubscribed-chat")
    r5 = await store.add_rule(2, ALL_CHATS, RuleKind.INCLUDE, "global-of-user-2")
    with pytest.raises(DuplicateRuleError):
        await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, "казино")
    # same pattern as exclude is a different rule
    await store.add_rule(1, CHAT_A.id, RuleKind.EXCLUDE, "казино")

    assert r2.is_global and not r1.is_global
    assert [r.id for r in await store.list_rules(1, CHAT_A.id)] == [r1.id, r3.id, r3.id + 1 + 2]
    assert [r.id for r in await store.list_rules(1, kind=RuleKind.EXCLUDE)] == [r3.id, r3.id + 3]

    ids = {r.id for r in await store.rules_for_chat(CHAT_A.id)}
    assert r1.id in ids and r2.id in ids and r3.id in ids and r5.id in ids
    assert r4.id not in ids  # chat B rule
    # user 2 is subscribed to A, so their global rule applies to A; not their chat-B rule
    assert r5.id in ids

    await store.disable_rule(r1.id, "timeout")
    assert r1.id not in {r.id for r in await store.rules_for_chat(CHAT_A.id)}
    rule = await store.get_rule(r1.id)
    assert rule is not None and rule.disabled_reason == "timeout"

    await store.bump_rule_hits(r2.id, 3)
    rule = await store.get_rule(r2.id)
    assert rule is not None and rule.hits == 3

    # deletion is scoped to the owner of the rule
    assert await store.delete_rule(r5.id, user_id=1) is False
    assert await store.delete_rule(r5.id, user_id=2) is True
    assert await store.get_rule(r5.id) is None

    # a paused or pending user's rules stop applying
    await store.update_user(2, paused=True)
    assert all(r.user_id == 1 for r in await store.rules_for_chat(CHAT_A.id))


async def test_ignored_senders(store):
    await store.ensure_user(1, role=Role.OWNER)
    await store.ensure_user(2, role=Role.ALLOWED)
    assert await store.add_ignored(1, CHAT_A.id, 500, "spammer") is True
    assert await store.add_ignored(1, CHAT_A.id, 500) is False
    await store.add_ignored(1, ALL_CHATS, 600)
    await store.add_ignored(2, CHAT_B.id, 700)
    assert await store.ignored_for_chat(CHAT_A.id) == {1: {500, 600}}
    assert await store.ignored_for_chat(CHAT_B.id) == {1: {600}, 2: {700}}
    listed = await store.list_ignored(1)
    assert [(i.chat_id, i.sender_id, i.note) for i in listed] == [
        (ALL_CHATS, 600, None),
        (CHAT_A.id, 500, "spammer"),
    ]
    assert await store.remove_ignored(1, CHAT_A.id, 500) is True
    assert await store.remove_ignored(1, CHAT_A.id, 500) is False


async def test_cursors_never_move_backwards(store):
    await store.upsert_cursors([Cursor(-1001, 100, T0)])
    await store.upsert_cursors([Cursor(-1001, 90, T0), Cursor(-1002, 5, T0)])
    cursors = await store.get_cursors()
    assert cursors[-1001].last_msg_id == 100
    assert cursors[-1002].last_msg_id == 5
    await store.upsert_cursors([Cursor(-1001, 101, T0 + dt.timedelta(seconds=1))])
    cursors = await store.get_cursors()
    assert cursors[-1001].last_msg_id == 101
    assert cursors[-1001].last_msg_at == T0 + dt.timedelta(seconds=1)
    await store.delete_cursor(-1002)
    assert -1002 not in await store.get_cursors()


async def test_deliveries_dedupe_and_prune(store, clock):
    assert await store.claim_delivery(1, -1001, 10, 7) is True
    assert await store.claim_delivery(1, -1001, 10, 8) is False  # same msg for same user
    assert await store.claim_delivery(2, -1001, 10, 7) is True  # other user is independent
    await store.set_delivery_status(1, -1001, 10, DeliveryStatus.SENT)
    counts = await store.delivery_counts(since=T0 - dt.timedelta(hours=1))
    assert counts == {"sent": 1, "queued": 1}
    await store.release_delivery(2, -1001, 10)
    assert await store.claim_delivery(2, -1001, 10, 7) is True
    clock.tick(8 * 86400)
    assert await store.claim_delivery(3, -1001, 11, None) is True
    assert await store.prune_deliveries() == 2
    counts = await store.delivery_counts(since=T0 - dt.timedelta(hours=1))
    assert counts == {"queued": 1}


async def test_deleting_user_cascades(store):
    await store.ensure_user(1, role=Role.OWNER)
    await store.upsert_chat(CHAT_A)
    await store.subscribe(1, CHAT_A.id)
    await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, "x")
    await store.db.execute("DELETE FROM users WHERE id=1")
    assert await store.subscribers_of(CHAT_A.id) == []
    assert await store.list_rules(1) == []


async def test_rules_for_chat_orders_chat_specific_before_global(store):
    await store.ensure_user(1, role=Role.OWNER)
    await store.upsert_chat(CHAT_A)
    await store.subscribe(1, CHAT_A.id)
    g = await store.add_rule(1, ALL_CHATS, RuleKind.INCLUDE, "g")
    c = await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, "c")
    assert [r.id for r in await store.rules_for_chat(CHAT_A.id)] == [c.id, g.id]


async def test_open_failure_closes_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module, "_MIGRATIONS", ["CREATE TABLE broken ("])
    s = Storage(tmp_path / "db.sqlite3")
    with pytest.raises(Exception, match="syntax error"):
        await s.open()
    with pytest.raises(RuntimeError, match="not open"):
        _ = s.db

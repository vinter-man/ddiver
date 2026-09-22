import datetime as dt
import sqlite3

import pytest

import ddiver.storage as storage_module
from ddiver.links import ChatRef
from ddiver.rules import RuleKind
from ddiver.storage import (
    ALL_CHATS,
    Cursor,
    DatabaseCorruptError,
    DeliveryStatus,
    DuplicateRuleError,
    Role,
    Storage,
    safe_write,
)
from tests.fakes import T0, FakeClock

OWNER = 1
MOD = 2
STRANGER = 3

CHAT_A = ChatRef(-1001, "Alpha", "alpha", "supergroup")
CHAT_B = ChatRef(-1002, "beta", None, "supergroup", is_forum=True)
BASIC = ChatRef(-55, "Old group", None, "group")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
async def store(tmp_path, clock):
    s = Storage(tmp_path / "db.sqlite3", owner_id=OWNER, clock=clock)
    await s.open()
    yield s
    await s.close()


@pytest.fixture
async def seeded(store):
    await store.ensure_user(OWNER)
    await store.ensure_user(MOD)
    await store.update_user(MOD, role=Role.ALLOWED)
    await store.ensure_user(STRANGER)
    await store.upsert_chat(CHAT_A, access_hash=111)
    await store.upsert_chat(CHAT_B)
    return store


class TestLifecycle:
    async def test_migrations_run_once(self, tmp_path, clock):
        s = Storage(tmp_path / "db.sqlite3", clock=clock)
        await s.open()
        assert await s.schema_version() == 1
        await s.close()
        s2 = Storage(tmp_path / "db.sqlite3", clock=clock)
        await s2.open()
        assert await s2.schema_version() == 1
        await s2.close()

    async def test_open_failure_closes_connection(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage_module, "_MIGRATIONS", ["CREATE TABLE broken ("])
        s = Storage(tmp_path / "db.sqlite3")
        with pytest.raises(Exception, match="syntax error"):
            await s.open()
        with pytest.raises(RuntimeError, match="not open"):
            _ = s.db

    async def test_corrupt_database_is_detected(self, tmp_path):
        path = tmp_path / "db.sqlite3"
        path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
        s = Storage(path)
        with pytest.raises((DatabaseCorruptError, sqlite3.DatabaseError)):
            await s.open()

    async def test_safe_write_swallows_locked_database(self, caplog):
        async def boom():
            raise sqlite3.OperationalError("database is locked")

        async def fine():
            return None

        assert await safe_write(boom(), "cursor flush") is False
        assert "sqlite busy" in caplog.text
        assert await safe_write(fine(), "cursor flush") is True

    async def test_record_boot_counts_recent_boots(self, store, clock):
        assert await store.record_boot() == 1
        assert await store.record_boot() == 2
        clock.tick(11 * 60)
        assert await store.record_boot() == 1


class TestUsers:
    async def test_owner_is_computed_and_always_active(self, store):
        owner = await store.ensure_user(OWNER)
        assert owner.is_owner and owner.active and owner.role is Role.ALLOWED
        await store.update_user(OWNER, role=Role.DENIED)
        assert (await store.get_user(OWNER)).active

    async def test_stranger_defaults(self, store):
        user = await store.ensure_user(STRANGER, lang="ru")
        assert (user.role, user.lang, user.tz, user.onboarded, user.active) == (
            Role.PENDING,
            "ru",
            None,
            False,
            False,
        )

    async def test_ensure_user_is_idempotent(self, store):
        await store.ensure_user(MOD, lang="uk")
        await store.update_user(MOD, role=Role.ALLOWED)
        again = await store.ensure_user(MOD, lang="en")
        assert (again.role, again.lang) == (Role.ALLOWED, "uk")

    async def test_onboarding_flags(self, store):
        await store.ensure_user(MOD)
        user = await store.update_user(MOD, lang="uk")
        assert not user.onboarded
        user = await store.update_user(MOD, tz="Europe/Kyiv")
        assert user.onboarded

    async def test_pause_and_resume_delivery(self, store):
        await store.ensure_user(MOD)
        await store.update_user(MOD, role=Role.ALLOWED)
        await store.pause_delivery(MOD, "blocked")
        user = await store.get_user(MOD)
        assert user.paused and user.pause_reason == "blocked"
        assert user.active  # role is unaffected, so no re-approval is needed
        assert user.delivery_paused_at == T0
        assert await store.resume_delivery(MOD) is True
        assert await store.resume_delivery(MOD) is False
        assert not (await store.get_user(MOD)).paused

    async def test_update_user_validation(self, store):
        await store.ensure_user(MOD)
        with pytest.raises(ValueError, match="unknown user fields"):
            await store.update_user(MOD, hacker="x")
        with pytest.raises(KeyError):
            await store.update_user(999, lang="en")
        assert (await store.update_user(MOD)).id == MOD

    async def test_active_user_ids(self, seeded):
        assert await seeded.active_user_ids() == {OWNER, MOD}
        await seeded.update_user(MOD, role=Role.DENIED)
        assert await seeded.active_user_ids() == {OWNER}

    async def test_list_users_by_role(self, seeded):
        assert [u.id for u in await seeded.list_users(roles={Role.PENDING})] == [STRANGER]
        assert len(await seeded.list_users()) == 3


class TestChatsAndSubscriptions:
    async def test_upsert_refreshes_metadata_but_keeps_flags(self, seeded):
        await seeded.set_chat_shared(CHAT_A.id, True)
        await seeded.set_chat_available(CHAT_A.id, False)
        await seeded.upsert_chat(ChatRef(CHAT_A.id, "Alpha2", "alpha2", "supergroup"))
        chat = await seeded.get_chat(CHAT_A.id)
        assert (chat.ref.title, chat.ref.username) == ("Alpha2", "alpha2")
        assert chat.shared is True and chat.available is False
        assert chat.access_hash == 111  # not clobbered by an upsert without one

    async def test_available_and_shared_report_transitions(self, seeded):
        assert await seeded.set_chat_available(CHAT_A.id, False) is True
        assert await seeded.set_chat_available(CHAT_A.id, False) is False
        assert await seeded.set_chat_shared(CHAT_A.id, True) is True
        assert await seeded.set_chat_shared(CHAT_A.id, True) is False
        assert [c.id for c in await seeded.list_chats(shared_only=True)] == [CHAT_A.id]

    async def test_subscribe_rejects_unknown_and_non_group_chats(self, seeded):
        assert await seeded.subscribe(MOD, -9999) is False  # unknown chat
        assert await seeded.subscribe(MOD, 12345) is False  # a positive id is a private chat
        assert await seeded.subscribe(MOD, CHAT_A.id) is True
        assert await seeded.subscribe(MOD, CHAT_A.id) is False  # idempotent

    async def test_monitored_set_excludes_inactive_paused_and_unavailable(self, seeded):
        await seeded.subscribe(OWNER, CHAT_A.id)
        await seeded.subscribe(MOD, CHAT_B.id)
        await seeded.subscribe(STRANGER, CHAT_B.id)
        assert await seeded.monitored_chat_ids() == {CHAT_A.id, CHAT_B.id}
        await seeded.pause_delivery(MOD, "blocked")
        assert await seeded.monitored_chat_ids() == {CHAT_A.id}
        await seeded.resume_delivery(MOD)
        await seeded.set_chat_available(CHAT_B.id, False)
        assert await seeded.monitored_chat_ids() == {CHAT_A.id}

    async def test_subscribers_of_filters_inactive(self, seeded):
        await seeded.subscribe(MOD, CHAT_A.id)
        await seeded.subscribe(STRANGER, CHAT_A.id)
        assert await seeded.subscribers_of(CHAT_A.id) == [MOD]
        assert await seeded.subscribers_of(CHAT_A.id, active_only=False) == [MOD, STRANGER]

    async def test_unsubscribe_all_on_unshare(self, seeded):
        await seeded.subscribe(OWNER, CHAT_A.id)
        await seeded.subscribe(MOD, CHAT_A.id)
        assert await seeded.unsubscribe_all(CHAT_A.id, except_user=OWNER) == [MOD]
        assert [c.id for c in await seeded.list_subscriptions(OWNER)] == [CHAT_A.id]
        assert await seeded.list_subscriptions(MOD) == []
        assert await seeded.unsubscribe(OWNER, CHAT_A.id) is True
        assert await seeded.unsubscribe(OWNER, CHAT_A.id) is False

    async def test_is_subscribed(self, seeded):
        await seeded.subscribe(MOD, CHAT_A.id)
        assert await seeded.is_subscribed(MOD, CHAT_A.id)
        assert not await seeded.is_subscribed(MOD, CHAT_B.id)

    async def test_subscription_requires_existing_user(self, store):
        await store.upsert_chat(CHAT_A)
        with pytest.raises(Exception, match="FOREIGN KEY"):
            await store.subscribe(404, CHAT_A.id)

    async def test_migrate_basic_group_to_supergroup(self, seeded):
        await seeded.upsert_chat(BASIC)
        await seeded.subscribe(MOD, BASIC.id)
        rule = await seeded.add_rule(MOD, BASIC.id, RuleKind.INCLUDE, "казино")
        await seeded.add_ignored(MOD, BASIC.id, 500)
        await seeded.upsert_cursors([Cursor(BASIC.id, 10, T0)])
        new_ref = ChatRef(-1009999, "Old group", None, "supergroup")
        await seeded.migrate_chat(BASIC.id, new_ref, access_hash=42)
        assert [c.id for c in await seeded.list_subscriptions(MOD)] == [new_ref.id]
        assert (await seeded.get_rule(rule.id, MOD)).chat_id == new_ref.id
        assert (await seeded.list_ignored(MOD))[0].chat_id == new_ref.id
        assert BASIC.id not in await seeded.get_cursors()
        assert (await seeded.get_chat(BASIC.id)).available is False
        assert (await seeded.get_chat(new_ref.id)).access_hash == 42


class TestRules:
    async def test_crud_and_scoping(self, seeded):
        await seeded.subscribe(OWNER, CHAT_A.id)
        await seeded.subscribe(MOD, CHAT_A.id)
        chat_rule = await seeded.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "казино")
        global_rule = await seeded.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "=бан")
        mod_rule = await seeded.add_rule(MOD, ALL_CHATS, RuleKind.INCLUDE, "spam")
        assert global_rule.is_global and not chat_rule.is_global
        assert [r.id for r in await seeded.list_rules(OWNER)] == [global_rule.id, chat_rule.id]
        assert [r.id for r in await seeded.list_rules(OWNER, CHAT_A.id)] == [chat_rule.id]
        assert await seeded.get_rule(mod_rule.id, OWNER) is None
        assert await seeded.delete_rule(mod_rule.id, OWNER) is False
        assert await seeded.delete_rule(mod_rule.id, MOD) is True

    async def test_duplicates_are_case_folded(self, seeded):
        await seeded.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "казино")
        for dup in ("Казино", "КАЗИНО"):
            with pytest.raises(DuplicateRuleError):
                await seeded.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, dup)
        await seeded.add_rule(OWNER, CHAT_A.id, RuleKind.EXCLUDE, "казино")
        await seeded.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "казино")
        await seeded.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "re:Abc")
        await seeded.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "re:abc")
        assert await seeded.count_rules(OWNER) == 5
        assert await seeded.count_rules(OWNER, CHAT_A.id) == 4

    async def test_rules_for_chat_ordering_and_filters(self, seeded):
        await seeded.subscribe(OWNER, CHAT_A.id)
        await seeded.subscribe(MOD, CHAT_A.id)
        await seeded.subscribe(STRANGER, CHAT_A.id)
        g = await seeded.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "g")
        c = await seeded.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "c")
        other_chat = await seeded.add_rule(OWNER, CHAT_B.id, RuleKind.INCLUDE, "other")
        mod_rule = await seeded.add_rule(MOD, ALL_CHATS, RuleKind.INCLUDE, "m")
        stranger_rule = await seeded.add_rule(STRANGER, ALL_CHATS, RuleKind.INCLUDE, "s")
        ids = [r.id for r in await seeded.rules_for_chat(CHAT_A.id)]
        assert ids[:2] == [c.id, g.id]  # chat-specific before global, per user
        assert mod_rule.id in ids
        assert other_chat.id not in ids
        assert stranger_rule.id not in ids  # pending user
        await seeded.pause_delivery(MOD, "blocked")
        assert mod_rule.id not in [r.id for r in await seeded.rules_for_chat(CHAT_A.id)]

    async def test_disable_and_enable(self, seeded):
        await seeded.subscribe(OWNER, CHAT_A.id)
        rule = await seeded.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "казино")
        await seeded.disable_rule(rule.id, "timeout")
        assert await seeded.rules_for_chat(CHAT_A.id) == []
        assert (await seeded.get_rule(rule.id, OWNER)).disabled_reason == "timeout"
        assert await seeded.enable_rule(rule.id, MOD) is False
        assert await seeded.enable_rule(rule.id, OWNER) is True
        assert len(await seeded.rules_for_chat(CHAT_A.id)) == 1

    async def test_flush_rule_hits(self, seeded):
        r1 = await seeded.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "a")
        r2 = await seeded.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "b")
        await seeded.flush_rule_hits({r1.id: 3, r2.id: 1})
        await seeded.flush_rule_hits({})
        assert (await seeded.get_rule(r1.id, OWNER)).hits == 3
        assert (await seeded.get_rule(r2.id, OWNER)).hits == 1

    async def test_deleting_user_cascades(self, seeded):
        await seeded.subscribe(MOD, CHAT_A.id)
        await seeded.add_rule(MOD, CHAT_A.id, RuleKind.INCLUDE, "x")
        await seeded.db.execute("DELETE FROM users WHERE id=?", (MOD,))
        assert await seeded.subscribers_of(CHAT_A.id, active_only=False) == []
        assert await seeded.list_rules(MOD) == []


class TestIgnoredSenders:
    async def test_scoping(self, seeded):
        assert await seeded.add_ignored(OWNER, CHAT_A.id, 500, "spammer") is True
        assert await seeded.add_ignored(OWNER, CHAT_A.id, 500) is False
        await seeded.add_ignored(OWNER, ALL_CHATS, 600)
        await seeded.add_ignored(MOD, CHAT_B.id, -1001234567890)  # anonymous admin, marked id
        assert await seeded.ignored_for_chat(CHAT_A.id) == {OWNER: {500, 600}}
        assert await seeded.ignored_for_chat(CHAT_B.id) == {OWNER: {600}, MOD: {-1001234567890}}
        listed = await seeded.list_ignored(OWNER)
        assert [(i.chat_id, i.sender_id, i.note) for i in listed] == [
            (ALL_CHATS, 600, None),
            (CHAT_A.id, 500, "spammer"),
        ]
        assert [i.sender_id for i in await seeded.list_ignored(OWNER, CHAT_A.id)] == [500]
        assert await seeded.remove_ignored(OWNER, CHAT_A.id, 500) is True
        assert await seeded.remove_ignored(OWNER, CHAT_A.id, 500) is False


class TestCursors:
    async def test_never_move_backwards(self, store):
        await store.upsert_cursors([Cursor(-1001, 100, T0)])
        await store.upsert_cursors([Cursor(-1001, 90, T0), Cursor(-1002, 5, T0)])
        cursors = await store.get_cursors()
        assert cursors[-1001].last_msg_id == 100
        assert cursors[-1002].last_msg_id == 5
        later = T0 + dt.timedelta(seconds=1)
        await store.upsert_cursors([Cursor(-1001, 101, later)])
        cursors = await store.get_cursors()
        assert (cursors[-1001].last_msg_id, cursors[-1001].last_msg_at) == (101, later)
        await store.upsert_cursors([])
        await store.delete_cursor(-1002)
        assert -1002 not in await store.get_cursors()


PAYLOAD = {"text": "казино", "chat_title": "Alpha"}


class TestOutbox:
    async def test_claim_is_idempotent_per_user_and_message(self, store):
        item = await store.claim_delivery(OWNER, -1001, 10, 7, PAYLOAD)
        assert item is not None
        assert item.status is DeliveryStatus.PENDING
        assert item.payload == PAYLOAD
        assert item.key == (OWNER, -1001, 10)
        assert await store.claim_delivery(OWNER, -1001, 10, 8, PAYLOAD) is None
        assert await store.claim_delivery(MOD, -1001, 10, 7, PAYLOAD) is not None

    async def test_pending_are_replayed_in_order(self, store, clock):
        await store.claim_delivery(OWNER, -1001, 10, None, PAYLOAD)
        clock.tick(1)
        await store.claim_delivery(OWNER, -1001, 11, None, PAYLOAD)
        clock.tick(1)
        await store.claim_delivery(MOD, -1002, 5, None, PAYLOAD)
        await store.set_delivery_status(OWNER, -1001, 10, DeliveryStatus.SENT)
        pending = await store.pending_deliveries()
        assert [p.key for p in pending] == [(OWNER, -1001, 11), (MOD, -1002, 5)]

    async def test_status_transitions_and_attempts(self, store):
        await store.claim_delivery(OWNER, -1001, 10, 7, PAYLOAD)
        await store.set_delivery_status(
            OWNER, -1001, 10, DeliveryStatus.FAILED, error="MessageTooLong", attempts=2
        )
        item = await store.get_delivery(OWNER, -1001, 10)
        assert (item.status, item.attempts) == (DeliveryStatus.FAILED, 2)
        await store.set_delivery_status(OWNER, -1001, 10, DeliveryStatus.SENT)
        item = await store.get_delivery(OWNER, -1001, 10)
        assert item.status is DeliveryStatus.SENT and item.attempts == 2

    async def test_mark_digested_in_batch(self, store):
        keys = []
        for msg_id in range(5):
            await store.claim_delivery(OWNER, -1001, msg_id, None, PAYLOAD)
            keys.append((OWNER, -1001, msg_id))
        await store.mark_digested(keys)
        await store.mark_digested([])
        assert await store.pending_deliveries() == []
        assert await store.delivery_counts(since=T0 - dt.timedelta(hours=1)) == {"digested": 5}

    async def test_release_allows_reclaim(self, store):
        await store.claim_delivery(OWNER, -1001, 10, None, PAYLOAD)
        await store.release_delivery(OWNER, -1001, 10)
        assert await store.claim_delivery(OWNER, -1001, 10, None, PAYLOAD) is not None

    async def test_prune_keeps_pending_and_recent(self, store, clock):
        await store.claim_delivery(OWNER, -1001, 1, None, PAYLOAD)  # stays pending
        await store.claim_delivery(OWNER, -1001, 2, None, PAYLOAD)
        await store.set_delivery_status(OWNER, -1001, 2, DeliveryStatus.SENT)
        clock.tick(8 * 86400)
        await store.claim_delivery(OWNER, -1001, 3, None, PAYLOAD)
        await store.set_delivery_status(OWNER, -1001, 3, DeliveryStatus.SENT)
        assert await store.prune_outbox() == 1
        assert {i.msg_id for i in await store.pending_deliveries()} == {1}
        assert await store.get_delivery(OWNER, -1001, 3) is not None

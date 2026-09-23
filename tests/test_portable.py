"""Config export and import: what travels, what does not, and what a bad file does."""

from __future__ import annotations

import json

import pytest

from ddiver import portable
from ddiver.links import ChatRef
from ddiver.rules import RuleKind
from ddiver.storage import ALL_CHATS, Storage

OWNER = 1
MOD = 2
PUBLIC = ChatRef(-1001, "Публичный", "publicchat", "supergroup")
PRIVATE = ChatRef(-1002, "Приватный", None, "supergroup")


@pytest.fixture
async def store(tmp_path, clock):
    s = Storage(tmp_path / "db.sqlite3", owner_id=OWNER, clock=clock)
    await s.open()
    await s.ensure_user(OWNER)
    await s.ensure_user(MOD)
    for ref in (PUBLIC, PRIVATE):
        await s.upsert_chat(ref)
    yield s
    await s.close()


async def seed(store: Storage) -> None:
    await store.subscribe(OWNER, PUBLIC.id)
    await store.subscribe(OWNER, PRIVATE.id)
    await store.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "такси")
    await store.add_rule(OWNER, PUBLIC.id, RuleKind.INCLUDE, "=подвезу")
    await store.add_rule(OWNER, PUBLIC.id, RuleKind.EXCLUDE, "реклама")
    await store.add_ignored(OWNER, ALL_CHATS, 555, "спамер")


class TestExport:
    async def test_it_carries_chats_rules_and_ignores(self, store, clock):
        await seed(store)
        config = await portable.export_config(store, OWNER, now=clock())
        assert config["format"] == "ddiver.config"
        assert {c["username"] for c in config["chats"]} == {"publicchat", None}
        assert {r["pattern"] for r in config["rules"]} == {"такси", "=подвезу", "реклама"}
        assert config["ignored"] == [{"chat_id": 0, "sender_id": 555, "note": "спамер"}]

    async def test_it_never_carries_a_session_or_a_token(self, store, clock):
        """A config backup that is also an account backup is a trap."""
        await seed(store)
        raw = portable.dumps(await portable.export_config(store, OWNER, now=clock())).lower()
        for secret in ("session", "token", "api_hash", "auth_key", "password"):
            assert secret not in raw

    async def test_it_carries_no_message_text(self, store, clock):
        """The outbox holds other people's words; a config file has no business with them."""
        await seed(store)
        await store.claim_delivery(
            OWNER, PUBLIC.id, 7, 1, {"text": "подвезу недорого", "chat_title": "Публичный"}
        )
        raw = portable.dumps(await portable.export_config(store, OWNER, now=clock()))
        assert "подвезу недорого" not in raw

    async def test_one_users_export_holds_nothing_of_anothers(self, store, clock):
        await seed(store)
        await store.add_rule(MOD, ALL_CHATS, RuleKind.INCLUDE, "секрет модератора")
        raw = portable.dumps(await portable.export_config(store, OWNER, now=clock()))
        assert "секрет модератора" not in raw

    async def test_a_chat_named_only_by_a_rule_still_travels(self, store, clock):
        """A rule for a chat you unsubscribed from would otherwise import as 'chat not found'."""
        await store.add_rule(OWNER, PRIVATE.id, RuleKind.INCLUDE, "такси")
        config = await portable.export_config(store, OWNER, now=clock())
        assert [c["id"] for c in config["chats"]] == [PRIVATE.id]
        assert config["chats"][0]["subscribed"] is False


class TestRoundTrip:
    async def test_export_then_import_into_an_empty_account(self, store, clock):
        await seed(store)
        config = await portable.export_config(store, OWNER, now=clock())

        report = await portable.import_config(store, MOD, config, regex_timeout=0.2, is_owner=False)
        assert report.chats_subscribed == 0  # nothing is shared with MOD yet
        assert report.rules_added == 1  # only the rule for all chats, which needs no chat
        assert report.rules_invalid == [
            "=подвезу: chat not available to you",
            "реклама: chat not available to you",
        ]

        for ref in (PUBLIC, PRIVATE):
            await store.set_chat_shared(ref.id, True)
        report = await portable.import_config(store, MOD, config, regex_timeout=0.2, is_owner=False)
        assert report.chats_subscribed == 2
        assert report.rules_added == 2  # the global one was already taken on the first pass
        assert report.ignored_added == 1
        assert {r.pattern for r in await store.list_rules(MOD)} == {
            "такси",
            "=подвезу",
            "реклама",
        }

    async def test_importing_twice_adds_nothing_the_second_time(self, store, clock):
        await seed(store)
        config = await portable.export_config(store, OWNER, now=clock())
        again = await portable.import_config(store, OWNER, config, regex_timeout=0.2, is_owner=True)
        assert again.rules_added == 0
        assert again.rules_duplicate == 3
        assert len(await store.list_rules(OWNER)) == 3

    async def test_it_adds_rather_than_replaces(self, store, clock):
        """One mistaken tap must not cost somebody every rule they wrote."""
        await seed(store)
        config = await portable.export_config(store, OWNER, now=clock())
        await store.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "написано после экспорта")
        await portable.import_config(store, OWNER, config, regex_timeout=0.2, is_owner=True)
        assert "написано после экспорта" in {r.pattern for r in await store.list_rules(OWNER)}

    async def test_a_public_chat_is_found_by_username_when_the_id_changed(self, store, clock):
        """Ids differ between accounts for private chats; a @username is the stable handle."""
        await seed(store)
        config = await portable.export_config(store, OWNER, now=clock())
        for chat in config["chats"]:
            chat["id"] = chat["id"] - 900_000  # as if exported from another account
        for rule in config["rules"]:
            if rule["chat_id"]:
                rule["chat_id"] -= 900_000

        await store.set_chat_shared(PUBLIC.id, True)
        report = await portable.import_config(store, MOD, config, regex_timeout=0.2, is_owner=False)
        assert report.chats_subscribed == 1  # the public one, matched by @publicchat
        assert "Приватный" in " ".join(report.chats_missing)
        assert "=подвезу" in {r.pattern for r in await store.list_rules(MOD)}

    async def test_the_import_flag_cannot_override_what_storage_decides(self, store, clock):
        """`is_owner=True` shapes the report; `Storage.subscribe` still re-checks sharing."""
        await seed(store)
        config = await portable.export_config(store, OWNER, now=clock())
        report = await portable.import_config(store, MOD, config, regex_timeout=0.2, is_owner=True)
        assert report.chats_subscribed == 0
        assert await store.list_subscriptions(MOD) == []


class TestBadInput:
    def test_not_json(self):
        with pytest.raises(portable.BadExportError, match="not valid JSON"):
            portable.loads("{definitely not json")

    def test_json_but_not_ours(self):
        with pytest.raises(portable.BadExportError, match="not a ddiver export"):
            portable.loads('{"hello": "world"}')

    def test_a_newer_format_is_refused_rather_than_half_read(self):
        raw = json.dumps({"format": portable.FORMAT, "version": portable.VERSION + 1})
        with pytest.raises(portable.BadExportError, match="newer ddiver"):
            portable.loads(raw)

    def test_a_huge_file_is_refused_before_parsing(self):
        with pytest.raises(portable.BadExportError, match="too large"):
            portable.loads(b"x" * (portable.MAX_BYTES + 1))

    def test_not_utf8(self):
        with pytest.raises(portable.BadExportError, match="UTF-8"):
            portable.loads(b"\xff\xfe not text")

    async def test_a_hostile_file_cannot_reach_an_unshared_chat(self, store):
        """The file is attacker-controlled; authorisation still runs through Storage."""
        config = {
            "format": portable.FORMAT,
            "version": 1,
            "chats": [{"id": PRIVATE.id, "username": None, "title": "x", "subscribed": True}],
            "rules": [{"chat_id": PRIVATE.id, "kind": "include", "pattern": "такси"}],
        }
        report = await portable.import_config(store, MOD, config, regex_timeout=0.2, is_owner=False)
        assert report.chats_subscribed == 0
        assert await store.list_subscriptions(MOD) == []
        assert await store.list_rules(MOD) == []
        assert report.rules_invalid == ["такси: chat not available to you"]

    async def test_a_broken_rule_is_reported_and_the_rest_still_load(self, store):
        config = {
            "format": portable.FORMAT,
            "version": 1,
            "rules": [
                {"chat_id": 0, "kind": "include", "pattern": "re:[unclosed"},
                {"chat_id": 0, "kind": "include", "pattern": "такси"},
            ],
        }
        report = await portable.import_config(
            store, OWNER, config, regex_timeout=0.2, is_owner=True
        )
        assert report.rules_added == 1
        assert report.rules_invalid == ["re:[unclosed"]

    async def test_missing_sections_are_simply_absent(self, store):
        report = await portable.import_config(
            store,
            OWNER,
            {"format": portable.FORMAT, "version": 1},
            regex_timeout=0.2,
            is_owner=True,
        )
        assert report.anything_happened is False

    async def test_a_null_section_does_not_crash(self, store):
        config = {"format": portable.FORMAT, "version": 1, "chats": None, "rules": None}
        report = await portable.import_config(
            store, OWNER, config, regex_timeout=0.2, is_owner=True
        )
        assert report.anything_happened is False


def test_dumps_is_readable_utf8():
    """A person should be able to open the backup and see their own words."""
    raw = portable.dumps({"format": portable.FORMAT, "rules": [{"pattern": "такси"}]})
    assert "такси" in raw and "\\u0442" not in raw
    assert raw.count("\n") > 1  # indented, not one long line

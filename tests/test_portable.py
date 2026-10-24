"""Config export and import: what travels, what does not, and what a bad file does."""

from __future__ import annotations

import asyncio
import json

import pytest

from ddiver_core import portable
from ddiver_core.links import ChatRef
from ddiver_core.rules import (
    MAX_IGNORED_PER_USER,
    MAX_RULES_PER_CHAT,
    MAX_RULES_PER_USER,
    RuleKind,
)
from ddiver_core.storage import ALL_CHATS, Storage

OWNER = 1
MOD = 2
PUBLIC = ChatRef(-1001, "Public", "publicchat", "supergroup")
PRIVATE = ChatRef(-1002, "Private", None, "supergroup")


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
    await store.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "taxi")
    await store.add_rule(OWNER, PUBLIC.id, RuleKind.INCLUDE, "=lift offered")
    await store.add_rule(OWNER, PUBLIC.id, RuleKind.EXCLUDE, "ads")
    await store.add_ignored(OWNER, ALL_CHATS, 555, "spammer")


class TestExport:
    async def test_it_carries_chats_rules_and_ignores(self, store, clock):
        await seed(store)
        config = await portable.export_config(store, OWNER, now=clock())
        assert config["format"] == "ddiver.config"
        assert {c["username"] for c in config["chats"]} == {"publicchat", None}
        assert {r["pattern"] for r in config["rules"]} == {"taxi", "=lift offered", "ads"}
        assert config["ignored"] == [{"chat_id": 0, "sender_id": 555, "note": "spammer"}]

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
            OWNER, PUBLIC.id, 7, 1, {"text": "lift offered cheap", "chat_title": "Public"}
        )
        raw = portable.dumps(await portable.export_config(store, OWNER, now=clock()))
        assert "lift offered cheap" not in raw

    async def test_one_users_export_holds_nothing_of_anothers(self, store, clock):
        await seed(store)
        await store.add_rule(MOD, ALL_CHATS, RuleKind.INCLUDE, "moderator secret")
        raw = portable.dumps(await portable.export_config(store, OWNER, now=clock()))
        assert "moderator secret" not in raw

    async def test_a_chat_named_only_by_a_rule_still_travels(self, store, clock):
        """A rule for a chat you unsubscribed from would otherwise import as 'chat not found'."""
        await store.add_rule(OWNER, PRIVATE.id, RuleKind.INCLUDE, "taxi")
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
            "=lift offered: chat not found or not available to you",
            "ads: chat not found or not available to you",
        ]

        for ref in (PUBLIC, PRIVATE):
            await store.set_chat_shared(ref.id, True)
        report = await portable.import_config(store, MOD, config, regex_timeout=0.2, is_owner=False)
        assert report.chats_subscribed == 2
        assert report.rules_added == 2  # the global one was already taken on the first pass
        assert report.ignored_added == 1
        assert {r.pattern for r in await store.list_rules(MOD)} == {
            "taxi",
            "=lift offered",
            "ads",
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
        await store.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "written after export")
        await portable.import_config(store, OWNER, config, regex_timeout=0.2, is_owner=True)
        assert "written after export" in {r.pattern for r in await store.list_rules(OWNER)}

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
        assert "Private" in " ".join(report.chats_missing)
        assert "=lift offered" in {r.pattern for r in await store.list_rules(MOD)}

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
            "rules": [{"chat_id": PRIVATE.id, "kind": "include", "pattern": "taxi"}],
        }
        report = await portable.import_config(store, MOD, config, regex_timeout=0.2, is_owner=False)
        assert report.chats_subscribed == 0
        assert await store.list_subscriptions(MOD) == []
        assert await store.list_rules(MOD) == []
        assert report.rules_invalid == ["taxi: chat not found or not available to you"]

    async def test_a_broken_rule_is_reported_and_the_rest_still_load(self, store):
        config = {
            "format": portable.FORMAT,
            "version": 1,
            "rules": [
                {"chat_id": 0, "kind": "include", "pattern": "re:[unclosed"},
                {"chat_id": 0, "kind": "include", "pattern": "taxi"},
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
    raw = portable.dumps({"format": portable.FORMAT, "rules": [{"pattern": "taxi"}]})
    assert "taxi" in raw and "\\u0442" not in raw
    assert raw.count("\n") > 1  # indented, not one long line


class TestNoChatOracle:
    """An import report must not answer "does the owner have a chat called @x?"."""

    async def test_a_guessed_username_is_not_confirmed(self, store, clock):
        await seed(store)
        config = {
            "format": portable.FORMAT,
            "version": 1,
            "chats": [
                {"id": -5001, "username": "publicchat", "title": "guess-A", "subscribed": True},
                {"id": -5002, "username": "nosuchchat", "title": "guess-B", "subscribed": True},
            ],
        }
        report = await portable.import_config(store, MOD, config, regex_timeout=0.2, is_owner=False)
        # The owner really has @publicchat and really does not have @nosuchchat. A moderator
        # it was never shared with must not be able to tell those two apart.
        assert report.chats_missing == ["guess-A", "guess-B"]
        assert report.chats_subscribed == 0

    async def test_the_report_echoes_what_was_sent_not_what_was_found(self, store, clock):
        """Naming the real chat title would rebuild the oracle out of the other field."""
        await seed(store)
        config = {
            "format": portable.FORMAT,
            "version": 1,
            "chats": [
                {"id": PRIVATE.id, "username": None, "title": "whatever", "subscribed": True}
            ],
        }
        report = await portable.import_config(store, MOD, config, regex_timeout=0.2, is_owner=False)
        assert report.chats_missing == ["whatever"]
        assert PRIVATE.title not in " ".join(report.chats_missing)

    async def test_an_existing_private_chat_and_a_missing_one_read_alike(self, store):
        """The rule report used to say "chat not found" versus "not available to you"."""
        config = {
            "format": portable.FORMAT,
            "version": 1,
            "rules": [
                {"chat_id": PRIVATE.id, "kind": "include", "pattern": "a"},
                {"chat_id": -999999, "kind": "include", "pattern": "b"},
            ],
        }
        report = await portable.import_config(store, MOD, config, regex_timeout=0.2, is_owner=False)
        reasons = {line.split(": ", 1)[1] for line in report.rules_invalid}
        assert len(reasons) == 1, report.rules_invalid

    async def test_the_owner_still_reaches_their_own_private_chats(self, store, clock):
        await seed(store)
        config = await portable.export_config(store, OWNER, now=clock())
        await store.db.execute("DELETE FROM rules")
        report = await portable.import_config(
            store, OWNER, config, regex_timeout=0.2, is_owner=True
        )
        assert report.rules_added == 3
        assert report.rules_invalid == []

    async def test_a_moderator_keeps_a_chat_that_was_unshared_under_them(self, store, clock):
        """They are still subscribed, so the UI still lets them manage it; so must this."""
        await store.set_chat_shared(PUBLIC.id, True)
        await store.subscribe(MOD, PUBLIC.id)
        await store.set_chat_shared(PUBLIC.id, False)
        config = {
            "format": portable.FORMAT,
            "version": 1,
            "chats": [
                {"id": PUBLIC.id, "username": "publicchat", "title": "x", "subscribed": True}
            ],
            "rules": [{"chat_id": PUBLIC.id, "kind": "include", "pattern": "taxi"}],
        }
        report = await portable.import_config(store, MOD, config, regex_timeout=0.2, is_owner=False)
        assert report.rules_added == 1
        assert report.rules_invalid == []


class TestImportLimits:
    """A file must not put somebody past the caps the rules screen enforces."""

    async def test_the_per_chat_cap_applies(self, store):
        await store.set_chat_shared(PUBLIC.id, True)
        await store.subscribe(MOD, PUBLIC.id)
        config = {
            "format": portable.FORMAT,
            "version": 1,
            "rules": [
                {"chat_id": PUBLIC.id, "kind": "include", "pattern": f"word{i}"}
                for i in range(MAX_RULES_PER_CHAT + 25)
            ],
        }
        report = await portable.import_config(store, MOD, config, regex_timeout=0.2, is_owner=False)
        assert report.rules_added == MAX_RULES_PER_CHAT
        assert report.rules_over_limit == 25
        assert await store.count_rules(MOD, PUBLIC.id) == MAX_RULES_PER_CHAT

    async def test_the_total_cap_applies_across_chats(self, store):
        config = {
            "format": portable.FORMAT,
            "version": 1,
            "rules": [
                {"chat_id": 0, "kind": "include", "pattern": f"word{i}"}
                for i in range(MAX_RULES_PER_USER + 10)
            ],
        }
        report = await portable.import_config(
            store, OWNER, config, regex_timeout=0.2, is_owner=True
        )
        assert report.rules_added <= MAX_RULES_PER_USER
        assert report.rules_over_limit >= 10
        assert await store.count_rules(OWNER) <= MAX_RULES_PER_USER

    async def test_rules_already_held_count_towards_the_total(self, store):
        """Spread across chats, so the per-chat cap is not what stops it: the total is."""
        held = MAX_RULES_PER_USER - 2
        for i in range(held):
            chat = (ALL_CHATS, PUBLIC.id, PRIVATE.id)[i % 3]
            await store.add_rule(OWNER, chat, RuleKind.INCLUDE, f"rule{i}")
        assert await store.count_rules(OWNER) == held
        assert await store.count_rules(OWNER, PRIVATE.id) < MAX_RULES_PER_CHAT

        config = {
            "format": portable.FORMAT,
            "version": 1,
            "rules": [
                {"chat_id": PRIVATE.id, "kind": "include", "pattern": f"new{i}"} for i in range(10)
            ],
        }
        report = await portable.import_config(
            store, OWNER, config, regex_timeout=0.2, is_owner=True
        )
        assert report.rules_added == 2
        assert report.rules_over_limit == 8
        assert await store.count_rules(OWNER) == MAX_RULES_PER_USER

    async def test_the_all_chats_bucket_shares_the_per_chat_cap(self, store):
        """The rules screen treats "all chats" as a chat for the cap; an import must agree."""
        config = {
            "format": portable.FORMAT,
            "version": 1,
            "rules": [
                {"chat_id": 0, "kind": "include", "pattern": f"glob{i}"}
                for i in range(MAX_RULES_PER_CHAT + 5)
            ],
        }
        report = await portable.import_config(
            store, OWNER, config, regex_timeout=0.2, is_owner=True
        )
        assert report.rules_added == MAX_RULES_PER_CHAT
        assert report.rules_over_limit == 5

    async def test_a_file_only_ever_compiles_a_bounded_number_of_patterns(self, store, monkeypatch):
        """The stored caps do not stop the loop: a rejected pattern never reaches them.

        Validating one catastrophic regex costs a whole ``regex_timeout``, and a megabyte of
        JSON holds about 26,000 of them, so the work has to be bounded on its own.
        """
        compiled: list[str] = []
        real = asyncio.to_thread

        async def spy(func, *args, **kwargs):
            compiled.append(args[0] if args else "")
            return await real(func, *args, **kwargs)

        monkeypatch.setattr(portable.asyncio, "to_thread", spy)
        extra = 50
        config = {
            "format": portable.FORMAT,
            "version": 1,
            # Rejected, so none of them ever counts towards the stored caps.
            "rules": [
                {"chat_id": 0, "kind": "include", "pattern": f"re:[unclosed{i}"}
                for i in range(portable.MAX_RULE_ENTRIES + extra)
            ],
        }
        report = await portable.import_config(
            store, OWNER, config, regex_timeout=0.2, is_owner=True
        )
        assert len(compiled) == portable.MAX_RULE_ENTRIES
        assert len(report.rules_invalid) == portable.MAX_RULE_ENTRIES
        assert report.rules_over_limit == extra
        assert report.rules_added == 0

    async def test_the_chat_list_is_bounded_too(self, store, monkeypatch):
        """A chat that resolves to nothing never reaches a stored cap either.

        The subscription cap bounds what an import can *keep*; nothing bounded what it would
        *look at*, and every entry costs a lookup against the whole dialog list.
        """
        looked_up = 0
        real = portable._find_chat

        async def spy(*args, **kwargs):
            nonlocal looked_up
            looked_up += 1
            return await real(*args, **kwargs)

        monkeypatch.setattr(portable, "_find_chat", spy)
        config = {
            "format": portable.FORMAT,
            "version": 1,
            "chats": [
                {"id": -900000 - i, "title": f"nope-{i}", "subscribed": True}
                for i in range(portable.MAX_CHAT_ENTRIES + 20)
            ],
        }
        report = await portable.import_config(
            store, OWNER, config, regex_timeout=0.2, is_owner=True
        )
        assert looked_up == portable.MAX_CHAT_ENTRIES
        assert report.chats_over_limit is True
        assert report.chats_subscribed == 0

    async def test_a_normal_sized_file_is_not_called_over_the_limit(self, store):
        """The bound must be invisible to a real backup, which carries a handful of chats."""
        await seed(store)
        config = {
            "format": portable.FORMAT,
            "version": 1,
            "chats": [{"id": PUBLIC.id, "title": PUBLIC.title, "subscribed": True}],
        }
        report = await portable.import_config(
            store, OWNER, config, regex_timeout=0.2, is_owner=True
        )
        assert report.chats_over_limit is False

    async def test_compiling_does_not_run_on_the_event_loop(self, store, monkeypatch):
        """A file of slow patterns must not stall everybody else's matching."""
        offloaded = []
        real = asyncio.to_thread

        async def spy(func, *args, **kwargs):
            offloaded.append(func.__name__)
            return await real(func, *args, **kwargs)

        monkeypatch.setattr(portable.asyncio, "to_thread", spy)
        config = {
            "format": portable.FORMAT,
            "version": 1,
            "rules": [{"chat_id": 0, "kind": "include", "pattern": "taxi"}],
        }
        await portable.import_config(store, OWNER, config, regex_timeout=0.2, is_owner=True)
        assert offloaded == ["compile_rule"]


class TestUnreadableJson:
    async def test_a_number_too_long_for_int_is_a_message_not_a_traceback(self):
        """CPython's scanner raises a bare ValueError past 4300 digits, not JSONDecodeError —
        the same escape route the RecursionError branch was written for."""
        raw = '{"format": "ddiver", "version": 1, "n": ' + "1" * 4400 + "}"
        with pytest.raises(portable.BadExportError):
            portable.loads(raw)


class TestHostileTypes:
    """The file is attacker-controlled JSON; nothing in it may be trusted to be what it looks."""

    @pytest.mark.parametrize(
        "config",
        [
            {"chats": "not a list"},
            {"chats": [None, 42, "x"]},
            {"rules": [None, [], "pattern"]},
            {"ignored": [None, 7]},
            {"rules": [{"chat_id": {"nested": 1}, "pattern": "x"}]},
            {"ignored": [{"sender_id": "abc", "note": {"a": 1}}]},
            {"chats": [{"id": [1, 2], "username": 5, "subscribed": "yes"}]},
        ],
    )
    async def test_it_does_not_crash(self, store, config):
        full = {"format": portable.FORMAT, "version": 1, **config}
        report = await portable.import_config(store, OWNER, full, regex_timeout=0.2, is_owner=True)
        assert isinstance(report, portable.ImportReport)

    @pytest.mark.parametrize("version", ["abc", None, [], {"a": 1}])
    def test_a_junk_version_is_a_message_not_a_traceback(self, version):
        raw = json.dumps({"format": portable.FORMAT, "version": version})
        assert portable.loads(raw)["format"] == portable.FORMAT

    async def test_a_non_string_note_is_stored_as_text(self, store):
        config = {
            "format": portable.FORMAT,
            "version": 1,
            "ignored": [{"chat_id": 0, "sender_id": 77, "note": {"weird": True}}],
        }
        await portable.import_config(store, OWNER, config, regex_timeout=0.2, is_owner=True)
        entry = (await store.list_ignored(OWNER))[0]
        assert isinstance(entry.note, str)


class TestIgnoreListWork:
    """The third import loop, and the one two rounds of bounding both missed."""

    async def test_it_stops_reading_an_endless_ignore_list(self, store, monkeypatch):
        """A row that inserts nothing never moves `held`, so the stored cap never ends it.

        Every one of them is a write against the single connection every other task is
        waiting on — measured at 2.7 s of exclusive database time per megabyte of JSON, with
        nothing stored at the end of it and nothing to stop a resend.
        """
        writes = 0
        real = store.add_ignored

        async def counting(*args, **kwargs):
            nonlocal writes
            writes += 1
            return await real(*args, **kwargs)

        monkeypatch.setattr(store, "add_ignored", counting)
        extra = 30
        config = {
            "format": portable.FORMAT,
            "version": 1,
            # The same sender over and over: every row after the first inserts nothing.
            "ignored": [
                {"chat_id": ALL_CHATS, "sender_id": 777}
                for _ in range(portable.MAX_IGNORE_ENTRIES + extra)
            ],
        }
        report = await portable.import_config(
            store, OWNER, config, regex_timeout=0.2, is_owner=True
        )
        assert writes == portable.MAX_IGNORE_ENTRIES
        assert report.ignored_over_limit == extra

    async def test_a_row_that_inserts_nothing_does_not_spend_the_budget(self, store):
        """Restoring a backup onto a configuration that already has it used to burn the cap
        on duplicates and then refuse the entries that were actually new."""
        await store.add_ignored(OWNER, ALL_CHATS, 1)
        config = {
            "format": portable.FORMAT,
            "version": 1,
            "ignored": [
                {"chat_id": ALL_CHATS, "sender_id": 1},  # already there
                {"chat_id": ALL_CHATS, "sender_id": 2},  # new
            ],
        }
        report = await portable.import_config(
            store, OWNER, config, regex_timeout=0.2, is_owner=True
        )
        assert report.ignored_over_limit == 0
        assert await store.count_ignored(OWNER) == 2

    async def test_an_imported_note_is_cut_to_what_a_button_can_hold(self, store):
        config = {
            "format": portable.FORMAT,
            "version": 1,
            "ignored": [{"chat_id": ALL_CHATS, "sender_id": 5, "note": "x" * 5000}],
        }
        await portable.import_config(store, OWNER, config, regex_timeout=0.2, is_owner=True)
        stored = await store.list_ignored(OWNER)
        assert len(stored[0].note) == portable.MAX_NOTE_LEN


class TestIgnoreListLimit:
    async def test_the_ignore_list_is_capped_too(self, store):
        """Every row is read back into memory for every chat the user watches."""
        config = {
            "format": portable.FORMAT,
            "version": 1,
            "ignored": [
                {"chat_id": 0, "sender_id": 1000 + i, "note": None}
                for i in range(MAX_IGNORED_PER_USER + 40)
            ],
        }
        report = await portable.import_config(
            store, OWNER, config, regex_timeout=0.2, is_owner=True
        )
        assert report.ignored_added == MAX_IGNORED_PER_USER
        assert report.ignored_over_limit == 40
        assert await store.count_ignored(OWNER) == MAX_IGNORED_PER_USER

    async def test_entries_already_held_count(self, store):
        for i in range(MAX_IGNORED_PER_USER - 3):
            await store.add_ignored(OWNER, ALL_CHATS, 9000 + i, None)
        config = {
            "format": portable.FORMAT,
            "version": 1,
            "ignored": [{"chat_id": 0, "sender_id": 1 + i} for i in range(10)],
        }
        report = await portable.import_config(
            store, OWNER, config, regex_timeout=0.2, is_owner=True
        )
        assert report.ignored_added == 3
        assert report.ignored_over_limit == 7

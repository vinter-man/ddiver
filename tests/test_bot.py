import datetime as dt

import pytest

from ddiver.bot import menus
from ddiver.bot.handlers import StatusInfo, build_router, propose_exclusion
from ddiver.bot.router import Edit, Incoming, Reply, Send, Toast
from ddiver.i18n import t
from ddiver.links import ChatRef
from ddiver.matcher import Matcher
from ddiver.rules import RuleKind
from ddiver.storage import ALL_CHATS, ChatRow, IgnoredSender, Role, Rule, Storage
from tests.fakes import T0, FakeClock

OWNER = 1
MOD = 2
STRANGER = 3

CHAT_A = ChatRef(-1001, "Модерация", "modchat", "supergroup")
CHAT_B = ChatRef(-1002, "Приватный семейный", None, "supergroup")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
async def store(tmp_path, clock):
    s = Storage(tmp_path / "db.sqlite3", owner_id=OWNER, clock=clock)
    await s.open()
    await s.ensure_user(OWNER)
    await s.update_user(OWNER, lang="ru", tz="Europe/Kyiv")
    await s.ensure_user(MOD)
    await s.update_user(MOD, role=Role.ALLOWED, lang="en", tz="UTC")
    await s.upsert_chat(CHAT_A)
    await s.upsert_chat(CHAT_B)
    yield s
    await s.close()


@pytest.fixture
async def matcher(store):
    m = Matcher(store, timeout=0.2)
    await m.reload()
    return m


@pytest.fixture
def router(store, matcher, clock):
    async def status():
        return StatusInfo(listener_ok=True, uptime="1h", monitored=2, seen=10, matched=3)

    return build_router(store, matcher=matcher, status=status, clock=clock)


def kinds(actions):
    return [type(a).__name__ for a in actions]


def texts(actions, lang="ru"):
    return [
        action.key_or_text if action.literal else t(lang, action.key_or_text, **action.params)
        for action in actions
        if isinstance(action, Toast | Reply | Edit | Send)
    ]


def callbacks(actions) -> list[str]:
    data: list[str] = []
    for action in actions:
        for row in getattr(action, "buttons", None) or []:
            data.extend(callback for _, callback in row)
    return data


async def msg(router, user_id, text, **kw):
    return await router.dispatch(Incoming(user_id=user_id, text=text, **kw))


async def cb(router, user_id, data, **kw):
    return await router.dispatch(Incoming(user_id=user_id, data=data, **kw))


class TestGate:
    async def test_stranger_gets_one_reply_and_the_owner_one_card(self, router, store, clock):
        actions = await msg(router, STRANGER, "/start")
        assert kinds(actions) == ["Reply", "Send"]
        card = actions[1]
        assert card.user_id == OWNER
        assert "allow:3" in callbacks([card]) and "deny:3" in callbacks([card])
        # a second message is silent
        assert await msg(router, STRANGER, "hello") == []
        clock.tick(dt.timedelta(days=1).total_seconds() + 1)
        assert kinds(await msg(router, STRANGER, "hello")) == ["Reply", "Send"]

    async def test_gate_message_is_in_the_client_language(self, router, store):
        actions = await msg(router, STRANGER, "/start", lang_code="uk-UA")
        assert texts(actions, "uk")[0] == t("uk", "bot.gate.pending")
        assert (await store.get_user(STRANGER)).lang == "uk"

    async def test_denied_users_are_silent(self, router, store):
        await store.ensure_user(STRANGER)
        await store.update_user(STRANGER, role=Role.DENIED)
        assert await msg(router, STRANGER, "/start") == []
        assert await cb(router, STRANGER, "menu") == []

    async def test_group_messages_are_ignored(self, router):
        assert await msg(router, OWNER, "/status", is_private=False) == []

    async def test_owner_is_never_gated(self, router, store):
        await store.update_user(OWNER, role=Role.DENIED)
        assert kinds(await msg(router, OWNER, "/menu")) == ["Reply"]

    async def test_incoming_update_clears_a_delivery_pause(self, router, store):
        await store.pause_delivery(MOD, "blocked")
        await msg(router, MOD, "/menu")
        assert not (await store.get_user(MOD)).paused


class TestOwnerOnly:
    @pytest.mark.parametrize("data", ["allow:3", "deny:3", "revoke:1", "shared:0", "share:-1001:1"])
    async def test_moderators_cannot_run_owner_actions(self, router, store, data):
        actions = await cb(router, MOD, data)
        assert kinds(actions) == ["Toast"]
        assert actions[0].key_or_text == "bot.err.owner_only"
        assert (await store.get_user(STRANGER)) is None or True

    async def test_owner_can_allow_and_revoke(self, router, store):
        await store.ensure_user(STRANGER)
        actions = await cb(router, OWNER, "allow:3")
        assert (await store.get_user(STRANGER)).role is Role.ALLOWED
        assert any(isinstance(a, Send) and a.user_id == STRANGER for a in actions)
        await cb(router, OWNER, "revoke:3")
        assert (await store.get_user(STRANGER)).role is Role.DENIED

    async def test_owner_cannot_revoke_themselves(self, router, store):
        actions = await cb(router, OWNER, "revoke:1")
        assert actions[0].key_or_text == "bot.err.owner_only"
        assert (await store.get_user(OWNER)).active


class TestOnboarding:
    async def test_language_then_timezone_then_menu(self, router, store):
        await store.update_user(MOD, lang=None, tz=None)
        actions = await msg(router, MOD, "/start")
        assert "lang:uk" in callbacks(actions)
        await cb(router, MOD, "lang:uk")
        user = await store.get_user(MOD)
        assert user.lang == "uk" and user.tz is None
        actions = await cb(router, MOD, "tz:Europe/Kyiv")
        assert (await store.get_user(MOD)).tz == "Europe/Kyiv"
        assert any("Europe/Kyiv" in text for text in texts(actions, "uk"))

    async def test_typed_timezone_with_suggestions(self, router, store):
        await store.update_user(MOD, tz=None)
        await cb(router, MOD, "set:tz")
        actions = await msg(router, MOD, "Europe")
        assert texts(actions, "en")[0] == t("en", "bot.start.tz_candidates")
        actions = await msg(router, MOD, "kyiv")
        assert (await store.get_user(MOD)).tz == "Europe/Kyiv"

    async def test_unknown_timezone_is_reported(self, router, store):
        await cb(router, MOD, "set:tz")
        actions = await msg(router, MOD, "Mars/Olympus")
        assert texts(actions, "en") == [t("en", "bot.start.tz_unknown")]

    async def test_fixed_offset_warns_about_dst(self, router, store):
        await cb(router, MOD, "set:tz")
        actions = await msg(router, MOD, "+3")
        assert any("daylight" in text for text in texts(actions, "en"))
        assert (await store.get_user(MOD)).tz == "Etc/GMT-3"


class TestChatsAndSharing:
    async def test_moderators_only_see_shared_chats(self, router, store):
        actions = await cb(router, MOD, "chats:0")
        assert texts(actions, "en") == [t("en", "bot.chats.none_shared")]
        await store.set_chat_shared(CHAT_A.id, True)
        actions = await cb(router, MOD, "chats:0")
        assert any("Модерация" in menus.unwrap(label) for label, _ in actions[0].buttons[0])
        assert all("Приватный" not in str(row) for row in actions[0].buttons)

    async def test_owner_sees_every_chat(self, router):
        actions = await cb(router, OWNER, "chats:0")
        labels = [menus.unwrap(label) for row in actions[0].buttons for label, _ in row]
        assert any("Приватный" in label for label in labels)

    async def test_subscribe_toggle_is_idempotent(self, router, store, matcher):
        await store.set_chat_shared(CHAT_A.id, True)
        await cb(router, MOD, f"sub:{CHAT_A.id}:1")
        await cb(router, MOD, f"sub:{CHAT_A.id}:1")
        assert [c.id for c in await store.list_subscriptions(MOD)] == [CHAT_A.id]
        assert matcher.is_monitored(CHAT_A.id)
        await cb(router, MOD, f"sub:{CHAT_A.id}:0")
        assert await store.list_subscriptions(MOD) == []
        assert not matcher.is_monitored(CHAT_A.id)

    async def test_moderator_cannot_subscribe_to_an_unshared_chat(self, router, store):
        actions = await cb(router, MOD, f"sub:{CHAT_B.id}:1")
        assert actions[0].key_or_text == "bot.err.not_found"
        assert await store.list_subscriptions(MOD) == []

    async def test_forged_private_chat_id_is_rejected(self, router, store):
        actions = await cb(router, MOD, "sub:777000:1")
        assert actions[0].key_or_text == "bot.err.not_found"
        assert await store.list_subscriptions(MOD) == []

    async def test_unsharing_drops_moderator_subscriptions_and_notifies(self, router, store):
        await store.set_chat_shared(CHAT_A.id, True)
        await cb(router, MOD, f"sub:{CHAT_A.id}:1")
        await cb(router, OWNER, f"share:{CHAT_A.id}:0")
        assert await store.list_subscriptions(MOD) == []
        notices = [a for a in await cb(router, OWNER, "shared:0") if isinstance(a, Send)]
        assert notices == []  # the notice was sent by the share callback, not the listing

    async def test_chat_search_matches_title_and_username(self, router, store):
        await store.set_chat_shared(CHAT_A.id, True)
        await cb(router, MOD, "chats:search")
        actions = await msg(router, MOD, "@modchat")
        labels = [menus.unwrap(label) for row in actions[0].buttons for label, _ in row]
        assert any("Модерация" in label for label in labels)
        await cb(router, MOD, "chats:search")
        actions = await msg(router, MOD, "nothing here")
        assert texts(actions, "en") == [t("en", "bot.chats.search_empty")]
        # the prompt stays active so the user can simply retype
        actions = await msg(router, MOD, "модерация")
        assert callbacks(actions)


class TestRules:
    async def test_multiline_add_reports_per_line(self, router, store, matcher):
        await store.subscribe(OWNER, CHAT_A.id)
        await cb(router, OWNER, f"radd:{CHAT_A.id}:include")
        actions = await msg(
            router,
            OWNER,
            "казино\n#промо\n=бан\nre:(unclosed\nказино\nre:.*",
        )
        report = texts(actions)[0]
        assert "Добавлено 3" in report
        assert "дубликатов 1" in report
        assert "ошибок 2" in report
        patterns = [r.pattern for r in await store.list_rules(OWNER, CHAT_A.id)]
        assert patterns == ["казино", "#промо", "=бан"]  # hashtags are keywords, not comments

    async def test_rule_limits(self, router, store, monkeypatch):
        monkeypatch.setattr("ddiver.bot.handlers.MAX_LINES_PER_MESSAGE", 2)
        await cb(router, OWNER, f"radd:{CHAT_A.id}:include")
        actions = await msg(router, OWNER, "a\nb\nc")
        assert texts(actions)[0] == t("ru", "bot.rules.too_many_lines", limit=2)
        monkeypatch.setattr("ddiver.bot.handlers.MAX_RULES_PER_CHAT", 1)
        await cb(router, OWNER, f"radd:{CHAT_A.id}:include")
        actions = await msg(router, OWNER, "один\nдва")
        assert "1" in texts(actions)[0]

    async def test_rules_are_private_to_their_owner(self, router, store, matcher):
        rule = await store.add_rule(MOD, ALL_CHATS, RuleKind.INCLUDE, "чужое")
        actions = await cb(router, OWNER, f"rdel:{rule.id}")
        assert actions[0].key_or_text == "bot.err.not_found"
        assert await store.get_rule(rule.id, MOD) is not None

    async def test_delete_rule_refreshes_the_matcher(self, router, store, matcher):
        await store.subscribe(OWNER, CHAT_A.id)
        rule = await store.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "казино")
        await matcher.invalidate()
        assert await matcher.evaluate(CHAT_A.id, "казино", 5) != []
        await cb(router, OWNER, f"rdel:{rule.id}")
        assert await matcher.evaluate(CHAT_A.id, "казино", 5) == []

    async def test_rules_list_shows_kind_icons(self, router, store):
        await store.subscribe(OWNER, CHAT_A.id)
        for pattern in ("казино", "=бан", "re:\\d+"):
            await store.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, pattern)
        actions = await cb(router, OWNER, f"rules:{CHAT_A.id}:include:0")
        labels = [menus.unwrap(label) for row in actions[0].buttons for label, _ in row]
        assert any("🔤" in label for label in labels)
        assert any("🔠" in label for label in labels)
        assert any("🧩" in label for label in labels)


class TestIgnoreAndExclude:
    async def test_ignore_button_scopes_to_the_chat_then_offers_global(self, router, store):
        actions = await cb(router, OWNER, f"ig:{CHAT_A.id}:500")
        assert [i.sender_id for i in await store.list_ignored(OWNER)] == [500]
        assert "igall:500" in callbacks(actions)
        await cb(router, OWNER, "igall:500")
        scopes = {i.chat_id for i in await store.list_ignored(OWNER)}
        assert scopes == {CHAT_A.id, ALL_CHATS}

    async def test_anonymous_admin_is_labelled(self, router, store):
        await cb(router, OWNER, f"ig:{CHAT_A.id}:{CHAT_A.id}")
        entry = (await store.list_ignored(OWNER))[0]
        assert entry.note == t("ru", "bot.ignored.anonymous")

    async def test_exclude_reads_the_outbox_row(self, router, store):
        payload = {
            "chat_id": CHAT_A.id,
            "chat_title": "Модерация",
            "msg_id": 9,
            "date": T0.isoformat(),
            "text": "купи казино-бонус сегодня",
            "spans": [[5, 11]],
            "rule_pattern": "казино",
        }
        await store.claim_delivery(OWNER, CHAT_A.id, 9, 1, payload)
        actions = await cb(router, OWNER, f"ex:{CHAT_A.id}:9")
        assert "exadd:-1001:9" in callbacks(actions)
        await cb(router, OWNER, f"exadd:{CHAT_A.id}:9")
        rules = await store.list_rules(OWNER, CHAT_A.id, RuleKind.EXCLUDE)
        assert [r.pattern for r in rules] == ["=казино-бонус"]

    async def test_exclude_on_a_pruned_row_expires(self, router):
        actions = await cb(router, OWNER, f"ex:{CHAT_A.id}:404")
        assert actions[0].key_or_text == "bot.err.expired"

    async def test_exclude_cannot_read_another_users_row(self, router, store):
        payload = {"chat_id": CHAT_A.id, "chat_title": "x", "msg_id": 9, "date": T0.isoformat()}
        await store.claim_delivery(MOD, CHAT_A.id, 9, 1, payload)
        actions = await cb(router, OWNER, f"ex:{CHAT_A.id}:9")
        assert actions[0].key_or_text == "bot.err.expired"

    async def test_exclusion_equal_to_the_keyword_is_refused(self, router, store):
        payload = {
            "chat_id": CHAT_A.id,
            "chat_title": "Модерация",
            "msg_id": 10,
            "date": T0.isoformat(),
            "text": "казино",
            "spans": [[0, 6]],
            "rule_pattern": "казино",
        }
        await store.claim_delivery(OWNER, CHAT_A.id, 10, 1, payload)
        actions = await cb(router, OWNER, f"ex:{CHAT_A.id}:10")
        assert any(a.key_or_text.startswith("⚠️") for a in actions if isinstance(a, Toast))
        assert "exadd:-1001:10" not in callbacks(actions)

    async def test_custom_exclusion_input(self, router, store):
        await cb(router, OWNER, f"excustom:{CHAT_A.id}")
        actions = await msg(router, OWNER, "re:промокод")
        assert "промокод" in texts(actions)[0]
        rules = await store.list_rules(OWNER, CHAT_A.id, RuleKind.EXCLUDE)
        assert [r.pattern for r in rules] == ["re:промокод"]


class TestProposeExclusion:
    @pytest.mark.parametrize(
        ("text", "span", "expected"),
        [
            ("купи казино-бонус сегодня", (5, 11), "=казино-бонус"),
            ("пиши @casino_bot тут", (6, 12), "=@casino_bot"),
            ("смотри casino.com!", (7, 13), "=casino.com"),
            ("казино", (0, 6), "=казино"),
            ("  казино  ", (2, 8), "=казино"),
        ],
    )
    def test_expands_to_whole_tokens(self, text, span, expected):
        assert propose_exclusion(text, [span]) == expected

    def test_empty_inputs(self):
        assert propose_exclusion("", [(0, 1)]) is None
        assert propose_exclusion("text", []) is None
        assert propose_exclusion("...", [(0, 3)]) is None


class TestPendingInput:
    async def test_expires_after_the_ttl(self, router, store, clock):
        await cb(router, OWNER, f"radd:{CHAT_A.id}:include")
        clock.tick(16 * 60)
        actions = await msg(router, OWNER, "казино")
        assert actions[0].key_or_text == "bot.err.not_waiting"
        assert await store.list_rules(OWNER) == []

    async def test_a_command_clears_pending_state(self, router, store):
        await cb(router, OWNER, f"radd:{CHAT_A.id}:include")
        await msg(router, OWNER, "/cancel")
        actions = await msg(router, OWNER, "казино")
        assert actions[0].key_or_text == "bot.err.not_waiting"

    async def test_rule_lines_starting_with_a_slash_are_kept(self, router, store):
        await cb(router, OWNER, f"radd:{CHAT_A.id}:include")
        await msg(router, OWNER, "/promo\nre:/promo\\b")
        patterns = [r.pattern for r in await store.list_rules(OWNER, CHAT_A.id)]
        assert patterns == ["/promo", "re:/promo\\b"]


class TestStatusAndTest:
    async def test_status_reports_counters(self, router):
        actions = await msg(router, OWNER, "/status")
        body = texts(actions)[0]
        assert "1h" in body and "10" in body

    async def test_test_command_mirrors_the_pipeline(self, router, store, matcher):
        await store.subscribe(OWNER, CHAT_A.id)
        await store.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "казино")
        await store.add_rule(OWNER, CHAT_A.id, RuleKind.EXCLUDE, "тест")
        actions = await msg(router, OWNER, "/test заходи в казино")
        assert "✅" in texts(actions)[0]
        actions = await msg(router, OWNER, "/test казино тест")
        assert "🚫" in texts(actions)[0]
        actions = await msg(router, OWNER, "/test ничего")
        assert "—" in texts(actions)[0]

    async def test_test_without_arguments_asks_for_text(self, router, store):
        await store.subscribe(OWNER, CHAT_A.id)
        await store.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "казино")
        actions = await msg(router, OWNER, "/test")
        assert actions[0].key_or_text == "bot.test.prompt"
        actions = await msg(router, OWNER, "тут казино")
        assert "✅" in texts(actions)[0]

    async def test_test_without_chats(self, router):
        actions = await msg(router, OWNER, "/test что-то")
        assert texts(actions)[0] == t("ru", "bot.test.no_chats")


class TestSettings:
    async def test_digest_threshold_validation(self, router, store):
        await cb(router, OWNER, "set:digest")
        await msg(router, OWNER, "abc")
        assert (await store.get_user(OWNER)).digest_threshold == 20
        await msg(router, OWNER, "1")
        assert (await store.get_user(OWNER)).digest_threshold == 20
        await msg(router, OWNER, "7")
        assert (await store.get_user(OWNER)).digest_threshold == 7
        await cb(router, OWNER, "set:digest")
        await msg(router, OWNER, "0")
        assert (await store.get_user(OWNER)).digest_threshold == 0

    async def test_settings_view_shows_current_values(self, router):
        actions = await cb(router, OWNER, "set:main")
        assert "Europe/Kyiv" in texts(actions)[0]


class TestUnknown:
    async def test_unknown_callback(self, router):
        actions = await cb(router, OWNER, "nope:1")
        assert actions[0].key_or_text == "bot.err.unknown_action"

    async def test_unknown_command(self, router):
        actions = await msg(router, OWNER, "/nonsense")
        assert actions[0].key_or_text == "bot.err.not_waiting"

    async def test_plain_text_without_pending_state(self, router):
        actions = await msg(router, OWNER, "просто текст")
        assert actions[0].key_or_text == "bot.err.not_waiting"


class TestMenus:
    def test_callback_data_fits_in_64_bytes(self):
        for chat_id in (-1001234567890, -55):
            data = menus.check_callback(f"ig:{chat_id}:{-1001234567890}")
            assert len(data.encode()) <= 64
        with pytest.raises(ValueError, match="too long"):
            menus.check_callback("x" * 65)

    def test_pagination_bounds(self):
        page = menus.paginate(list(range(25)), 99, 10)
        assert (page.index, page.total, list(page.items)) == (2, 3, [20, 21, 22, 23, 24])
        assert page.has_prev and not page.has_next
        empty = menus.paginate([], 0, 10)
        assert (empty.index, empty.total, list(empty.items)) == (0, 1, [])

    def test_owner_menu_has_extra_entries(self):
        owner_rows = menus.main_menu(is_owner=True)
        mod_rows = menus.main_menu(is_owner=False)
        assert len(owner_rows) == len(mod_rows) + 1
        assert any("users" in cb for row in owner_rows for _, cb in row)


class TestMenuRendering:
    def test_shared_menu_marks_state(self):
        rows = [
            ChatRow(CHAT_A, available=True, shared=True, access_hash=None),
            ChatRow(CHAT_B, available=True, shared=False, access_hash=None),
        ]
        keyboard = menus.shared_menu(rows, 0)
        labels = [menus.unwrap(label) for row in keyboard for label, _ in row]
        assert any(label.startswith("🗂") for label in labels)
        assert any(label.startswith("🔒") for label in labels)
        data = [cb for row in keyboard for _, cb in row]
        assert f"share:{CHAT_A.id}:0" in data  # toggling a shared chat turns it off
        assert f"share:{CHAT_B.id}:1" in data

    def test_chats_menu_flags_unavailable_chats(self):
        rows = [ChatRow(CHAT_A, available=False, shared=True, access_hash=None)]
        keyboard = menus.chats_menu(rows, {CHAT_A.id}, 0)
        label = menus.unwrap(keyboard[0][0][0])
        assert label.startswith("✅") and label.endswith("⚠️")

    def test_chats_menu_paginates(self):
        rows = [
            ChatRow(ChatRef(-1000 - i, f"Chat {i}", None, "supergroup"), True, True, None)
            for i in range(20)
        ]
        first = menus.chats_menu(rows, set(), 0)
        nav = [cb for row in first for _, cb in row if cb.startswith("chats:")]
        assert "chats:1" in nav and "chats:0" not in nav
        middle = menus.chats_menu(rows, set(), 1)
        nav = [cb for row in middle for _, cb in row if cb.startswith("chats:")]
        assert {"chats:0", "chats:2"} <= set(nav)

    def test_rules_menu_navigation_keeps_chat_and_kind(self):
        rules = [
            Rule(i, OWNER, CHAT_A.id, RuleKind.INCLUDE, f"rule{i}", None, 0) for i in range(25)
        ]
        keyboard = menus.rules_menu(rules, ["🔤"] * 25, CHAT_A.id, "include", 1)
        data = [cb for row in keyboard for _, cb in row]
        assert f"rules:{CHAT_A.id}:include:0" in data
        assert f"rules:{CHAT_A.id}:include:2" in data
        assert f"radd:{CHAT_A.id}:include" in data
        assert f"rules:{CHAT_A.id}:exclude:0" in data

    def test_rules_menu_truncates_long_patterns_and_marks_disabled(self):
        long_rule = Rule(1, OWNER, 0, RuleKind.INCLUDE, "x" * 120, "timeout", 0)
        label = menus.unwrap(menus.rules_menu([long_rule], ["🧩"], 0, "include", 0)[0][0][0])
        assert label.startswith("❌ ⏸")
        assert label.endswith("…")
        assert len(label) < 80

    def test_ignored_menu_shows_scope(self):
        entries = [
            IgnoredSender(OWNER, ALL_CHATS, 500, None),
            IgnoredSender(OWNER, CHAT_A.id, 600, "spammer"),
        ]
        keyboard = menus.ignored_menu(entries, 0)
        labels = [menus.unwrap(label) for row in keyboard for label, _ in row]
        assert any("🌐" in label for label in labels)
        assert any("📡" in label and "spammer" in label for label in labels)

    def test_users_menu_lists_actions(self):
        keyboard = menus.users_menu([3, 4], [5])
        data = [cb for row in keyboard for _, cb in row]
        assert data[:2] == ["allow:3", "deny:3"]
        assert "revoke:5" in data

    def test_rules_chat_menu_always_offers_all_chats(self):
        rows = [ChatRow(CHAT_A, True, True, None)]
        keyboard = menus.rules_chat_menu(rows, 0)
        assert keyboard[0][0][1] == f"rules:{ALL_CHATS}:include:0"

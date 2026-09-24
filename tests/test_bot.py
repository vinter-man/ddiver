import datetime as dt
import json

import pytest

from ddiver import portable
from ddiver.bot import menus
from ddiver.bot.handlers import StatusInfo, build_router
from ddiver.bot.router import Edit, Incoming, Reply, Send, Toast
from ddiver.i18n import t
from ddiver.links import ChatRef
from ddiver.matcher import Matcher
from ddiver.rules import MAX_IGNORED_PER_USER, RuleKind
from ddiver.storage import ALL_CHATS, ChatRow, IgnoredSender, Role, Rule, Storage

OWNER = 1
MOD = 2
STRANGER = 3

CHAT_A = ChatRef(-1001, "Модерация", "modchat", "supergroup")
CHAT_B = ChatRef(-1002, "Приватный семейный", None, "supergroup")


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


async def snapshot(store):
    """Everything an owner-only action could change, for before/after comparison."""
    users = [(u.id, u.role, u.lang, u.tz) for u in await store.list_users()]
    chats = [(c.id, c.shared, c.available) for c in await store.list_chats()]
    subs = {
        u.id: [c.id for c in await store.list_subscriptions(u.id)] for u in await store.list_users()
    }
    rules = {
        u.id: [r.pattern for r in await store.list_rules(u.id)] for u in await store.list_users()
    }
    return users, chats, subs, rules


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
        await store.ensure_user(STRANGER)
        before = await snapshot(store)
        actions = await cb(router, MOD, data)
        assert kinds(actions) == ["Toast"]
        assert actions[0].key_or_text == "bot.err.owner_only"
        assert await snapshot(store) == before  # nothing was mutated

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

    async def test_dropping_a_chat_stays_on_the_same_page(self, router, store):
        """A moderator paging through hundreds of dialogs must not be sent back to page 1."""
        for i in range(30):
            await store.upsert_chat(ChatRef(-2000 - i, f"Чат {i:02d}", None, "supergroup"))
        actions = await cb(router, OWNER, f"sub:{CHAT_A.id}:0:2")
        nav = [data for row in actions[-1].buttons for _, data in row if data.startswith("chats:")]
        assert {"chats:1", "chats:3"} <= set(nav)  # still looking at page 2

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
        actions = await cb(router, OWNER, f"share:{CHAT_A.id}:0")
        assert await store.list_subscriptions(MOD) == []
        notices = [a for a in actions if isinstance(a, Send)]
        assert [n.user_id for n in notices] == [MOD]
        assert "Модерация" in notices[0].key_or_text  # in the moderator's own language
        assert notices[0].literal

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

    async def test_the_notification_button_removes_the_rule_after_a_confirmation(
        self, router, store
    ):
        """One tap must not silently kill a rule; the confirmation names it and its scope."""
        rule = await store.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "такси")
        prompt = await cb(router, OWNER, f"rdrop:{rule.id}")
        assert f"rdropok:{rule.id}" in callbacks(prompt)
        assert "такси" in texts(prompt)[0]
        assert await store.list_rules(OWNER, CHAT_A.id) == [rule]  # nothing dropped yet

        await cb(router, OWNER, f"rdropok:{rule.id}")
        assert await store.list_rules(OWNER, CHAT_A.id) == []

    async def test_a_global_rule_says_so_before_it_is_dropped(self, router, store):
        rule = await store.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "такси")
        prompt = await cb(router, OWNER, f"rdrop:{rule.id}")
        assert t("ru", "notif.rule_scope.global") in texts(prompt)[0]

    async def test_dropping_someone_elses_rule_is_refused(self, router, store):
        rule = await store.add_rule(MOD, ALL_CHATS, RuleKind.INCLUDE, "такси")
        assert (await cb(router, OWNER, f"rdrop:{rule.id}"))[0].key_or_text == (
            "bot.notif.drop_gone"
        )
        assert (await cb(router, OWNER, f"rdropok:{rule.id}"))[0].key_or_text == (
            "bot.notif.drop_gone"
        )
        assert len(await store.list_rules(MOD)) == 1

    async def test_dropping_a_rule_twice_is_harmless(self, router, store):
        rule = await store.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "такси")
        await cb(router, OWNER, f"rdropok:{rule.id}")
        again = await cb(router, OWNER, f"rdropok:{rule.id}")
        assert again[0].key_or_text == "bot.notif.drop_gone"

    async def test_the_matcher_forgets_a_dropped_rule(self, router, store, matcher):
        rule = await store.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "такси")
        await store.set_chat_shared(CHAT_A.id, True)
        await cb(router, OWNER, f"sub:{CHAT_A.id}:1")
        assert await matcher.evaluate(CHAT_A.id, "нужно такси", 55)
        await cb(router, OWNER, f"rdropok:{rule.id}")
        assert not await matcher.evaluate(CHAT_A.id, "нужно такси", 55)


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


class TestStatus:
    async def test_status_reports_counters(self, router):
        actions = await msg(router, OWNER, "/status")
        body = texts(actions)[0]
        assert "1h" in body and "10" in body


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

    @pytest.mark.parametrize("raw", ["²", "-5", "٣٢١٤٣"])
    async def test_a_number_the_prompt_cannot_use_is_asked_again(self, router, store, raw):
        """`'²'.isdigit()` is True and `int('²')` raises: the handler used to die there."""
        await store.update_user(OWNER, digest_threshold=7)
        await cb(router, OWNER, "set:digest")
        actions = await msg(router, OWNER, raw)
        assert texts(actions), "the user was told nothing"
        assert (await store.get_user(OWNER)).digest_threshold == 7

    async def test_the_digest_prompt_states_the_current_value(self, router, store):
        """Asking for a number without showing the one in force leaves nothing to compare to."""
        actions = await cb(router, OWNER, "set:digest")
        assert actions[-1].params == {"n": 20}
        await msg(router, OWNER, "7")
        again = await cb(router, OWNER, "set:digest")
        assert again[-1].params == {"n": 7}
        rejected = await msg(router, OWNER, "1")  # out of range: the prompt repeats
        assert rejected[-1].params == {"n": 7}
        assert (await store.get_user(OWNER)).digest_threshold == 7

    async def test_settings_view_shows_current_values(self, router):
        actions = await cb(router, OWNER, "set:main")
        assert "Europe/Kyiv" in texts(actions)[0]


class TestUnknown:
    async def test_unknown_callback(self, router):
        actions = await cb(router, OWNER, "nope:1")
        assert actions[0].key_or_text == "bot.err.unknown_action"

    async def test_unknown_command(self, router):
        actions = await msg(router, OWNER, "/nonsense")
        assert actions[0].key_or_text == "bot.err.unknown_command"

    async def test_a_rule_line_starting_with_a_slash_is_still_input(self, router, store):
        """`/promo` is a legitimate keyword while the ➕ prompt is open."""
        await cb(router, OWNER, f"radd:{CHAT_A.id}:include")
        await msg(router, OWNER, "/promo")
        assert [r.pattern for r in await store.list_rules(OWNER, CHAT_A.id)] == ["/promo"]

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

    def test_watched_chats_come_first_so_they_can_be_dropped(self):
        """Alphabetical order buries a watched chat among hundreds of cached dialogs."""
        rows = [
            ChatRow(ChatRef(-1000 - i, f"Chat {i:02d}", None, "supergroup"), True, True, None)
            for i in range(20)
        ]
        watched = {rows[17].id, rows[3].id}
        keyboard = menus.chats_menu(rows, watched, 0)
        labels = [menus.unwrap(row[0][0]) for row in keyboard[:2]]
        assert all(label.startswith("✅") for label in labels)
        assert {"Chat 03", "Chat 17"} == {label.split(" ", 1)[1] for label in labels}

    def test_the_toggle_callback_carries_the_page(self):
        """Otherwise dropping a chat on page 3 bounces the moderator back to page 1."""
        rows = [
            ChatRow(ChatRef(-1000 - i, f"Chat {i:02d}", None, "supergroup"), True, True, None)
            for i in range(20)
        ]
        keyboard = menus.chats_menu(rows, set(), 2)
        toggles = [cb for row in keyboard for _, cb in row if cb.startswith("sub:")]
        assert toggles and all(cb.endswith(":2") for cb in toggles)

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


class TestForgedChatIds:
    """A moderator must not learn anything about a chat the owner did not share."""

    async def test_rules_view_of_an_unshared_chat_is_not_found(self, router, store):
        actions = await cb(router, MOD, f"rules:{CHAT_B.id}:include:0")
        assert actions[0].key_or_text == "bot.err.not_found"
        assert all("Приватный" not in str(a.key_or_text) for a in actions)

    async def test_a_shared_chat_is_reachable_even_before_subscribing(self, router, store):
        """It is already listed in their picker, so its title is not a secret."""
        await store.set_chat_shared(CHAT_B.id, True)
        actions = await cb(router, MOD, f"rules:{CHAT_B.id}:include:0")
        assert "Приватный семейный" in actions[0].key_or_text

    async def test_a_subscription_survives_the_chat_being_unshared(self, router, store):
        """Taking the grant away must not lock a moderator out of leaving the chat."""
        await store.set_chat_shared(CHAT_A.id, True)
        await cb(router, MOD, f"sub:{CHAT_A.id}:1")
        await store.set_chat_shared(CHAT_A.id, False)  # e.g. after a migration reset it
        actions = await cb(router, MOD, f"sub:{CHAT_A.id}:0")
        assert actions[0].key_or_text != "bot.err.not_found"
        assert await store.list_subscriptions(MOD) == []

    async def test_adding_a_rule_to_an_unshared_chat_is_refused(self, router, store):
        actions = await cb(router, MOD, f"radd:{CHAT_B.id}:include")
        assert actions[0].key_or_text == "bot.err.not_found"
        await msg(router, MOD, "казино")  # no pending state was armed
        assert await store.list_rules(MOD) == []

    async def test_ignoring_a_sender_in_an_unshared_chat_is_refused(self, router, store):
        actions = await cb(router, MOD, f"ig:{CHAT_B.id}:500")
        assert actions[0].key_or_text == "bot.err.not_found"
        assert await store.list_ignored(MOD) == []

    async def test_the_owner_still_reaches_their_own_chats(self, router, store):
        actions = await cb(router, OWNER, f"rules:{CHAT_B.id}:include:0")
        assert "Приватный семейный" in actions[0].key_or_text

    async def test_a_subscribed_shared_chat_works_for_moderators(self, router, store):
        await store.set_chat_shared(CHAT_B.id, True)
        await cb(router, MOD, f"sub:{CHAT_B.id}:1")
        actions = await cb(router, MOD, f"rules:{CHAT_B.id}:include:0")
        assert "Приватный семейный" in actions[0].key_or_text


class TestOnboardingTimezoneInput:
    async def test_typed_timezone_during_onboarding_is_accepted(self, router, store):
        """The prompt invites free text, so the pending state has to be armed."""
        await store.update_user(MOD, lang=None, tz=None)
        await msg(router, MOD, "/start")
        await cb(router, MOD, "lang:en")
        actions = await msg(router, MOD, "Tashkent")
        assert (await store.get_user(MOD)).tz == "Asia/Tashkent"
        assert any("Asia/Tashkent" in text for text in texts(actions, "en"))

    async def test_start_alone_also_arms_the_prompt(self, router, store):
        await store.update_user(MOD, tz=None)
        await msg(router, MOD, "/start")
        await msg(router, MOD, "kyiv")
        assert (await store.get_user(MOD)).tz == "Europe/Kyiv"


class TestPauseRecovery:
    async def test_unblocking_the_bot_brings_the_chats_back(self, router, store, matcher):
        """Without refreshing the matcher the moderator silently receives nothing again."""
        await store.set_chat_shared(CHAT_A.id, True)
        await cb(router, MOD, f"sub:{CHAT_A.id}:1")
        await store.add_rule(MOD, ALL_CHATS, RuleKind.INCLUDE, "казино")
        await matcher.invalidate()
        assert matcher.is_monitored(CHAT_A.id)

        await store.pause_delivery(MOD, "UserIsBlockedError")
        await matcher.invalidate()
        assert not matcher.is_monitored(CHAT_A.id)

        await msg(router, MOD, "/menu")  # any update clears the pause
        assert not (await store.get_user(MOD)).paused
        assert matcher.is_monitored(CHAT_A.id)
        assert await matcher.evaluate(CHAT_A.id, "казино", 5) != []


class TestDenyIsReversible:
    async def test_a_denied_user_can_be_allowed_again(self, router, store):
        await store.ensure_user(STRANGER)
        await cb(router, OWNER, "deny:3")
        assert (await store.get_user(STRANGER)).role is Role.DENIED
        actions = await cb(router, OWNER, "users")
        assert "allow:3" in callbacks(actions)  # the Users screen offers the way back
        await cb(router, OWNER, "allow:3")
        assert (await store.get_user(STRANGER)).role is Role.ALLOWED


class TestBotMessagesSetting:
    async def test_the_toggle_flips_the_setting_and_says_so(self, router, store):
        settings = await cb(router, OWNER, "set:main")
        assert t("ru", "bot.settings.bots_no") in settings[0].key_or_text

        on = await cb(router, OWNER, "set:bots")
        assert (await store.get_user(OWNER)).include_bots is True
        assert t("ru", "bot.settings.bots_yes") in on[-1].key_or_text
        assert "set:bots" in callbacks(on)

        off = await cb(router, OWNER, "set:bots")
        assert (await store.get_user(OWNER)).include_bots is False
        assert t("ru", "bot.settings.bots_no") in off[-1].key_or_text

    async def test_the_matcher_is_told_about_the_change(self, router, store, matcher):
        """The preference lives in the per-chat rule cache, so it has to be invalidated."""
        await store.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "такси")
        await store.set_chat_shared(CHAT_A.id, True)
        await cb(router, OWNER, f"sub:{CHAT_A.id}:1")
        assert [m.accepts_bots for m in await matcher.evaluate(CHAT_A.id, "такси", 5)] == [False]

        await cb(router, OWNER, "set:bots")
        assert [m.accepts_bots for m in await matcher.evaluate(CHAT_A.id, "такси", 5)] == [True]


class TestRuleCheck:
    """`/test` runs a text through the rules; it never touches real messages."""

    async def test_it_reports_a_verdict_per_watched_chat(self, router, store):
        await store.set_chat_shared(CHAT_A.id, True)
        await cb(router, OWNER, f"sub:{CHAT_A.id}:1")
        await store.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "такси")

        hit = await msg(router, OWNER, "/test дёшево подвезу, такси недорого")
        assert "✅" in hit[0].key_or_text
        assert "такси" in hit[0].key_or_text

        miss = await msg(router, OWNER, "/test обычное сообщение")
        assert "✅" not in miss[0].key_or_text
        assert "—" in miss[0].key_or_text  # the chat is still listed, with no verdict

    async def test_an_exclusion_wins_and_says_so(self, router, store):
        await store.set_chat_shared(CHAT_A.id, True)
        await cb(router, OWNER, f"sub:{CHAT_A.id}:1")
        await store.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "такси")
        await store.add_rule(OWNER, CHAT_A.id, RuleKind.EXCLUDE, "=такси-сервис")

        actions = await msg(router, OWNER, "/test такси-сервис приедет")
        assert "🚫" in actions[0].key_or_text

    async def test_without_chats_it_says_nothing_would_arrive_but_still_checks(self, router, store):
        """The old wording promised a global check that never ran."""
        await store.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "такси")
        actions = await msg(router, OWNER, "/test нужно такси")
        body = actions[0].key_or_text
        assert "⚠️" in body  # no chats: nothing could be delivered
        assert "✅" in body  # but the global rule is still evaluated
        assert t("ru", "bot.test.global_scope") in body

    async def test_the_bare_command_explains_itself(self, router, store):
        actions = await msg(router, OWNER, "/test")
        assert actions[0].key_or_text == "bot.test.prompt"
        assert actions[0].html is True
        # and the text that follows is then checked against the rules
        await store.set_chat_shared(CHAT_A.id, True)
        await cb(router, OWNER, f"sub:{CHAT_A.id}:1")
        await store.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "такси")
        await msg(router, OWNER, "/test")
        verdict = await msg(router, OWNER, "нужно такси")
        assert "✅" in verdict[0].key_or_text


class TestStatusLanguage:
    async def test_the_relative_time_is_rendered_in_the_readers_language(self, router, store):
        """It used to be pre-rendered in English, inside an otherwise translated card."""
        await store.update_user(OWNER, lang="uk")
        actions = await msg(router, OWNER, "/status")
        assert "тому" in actions[0].key_or_text  # Ukrainian for "ago"

        await store.update_user(OWNER, lang="en")
        actions = await msg(router, OWNER, "/status")
        assert "ago" in actions[0].key_or_text


class TestExportImport:
    async def test_export_hands_back_a_document(self, router, store):
        await store.set_chat_shared(CHAT_A.id, True)
        await cb(router, OWNER, f"sub:{CHAT_A.id}:1")
        await store.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "такси")

        actions = await msg(router, OWNER, "/export")
        doc = actions[0]
        assert doc.filename.startswith("ddiver-") and doc.filename.endswith(".json")
        config = portable.loads(doc.content)
        assert {r["pattern"] for r in config["rules"]} == {"такси"}

    async def test_import_takes_a_pasted_file_and_reports_what_landed(self, router, store):
        config = {
            "format": portable.FORMAT,
            "version": 1,
            "rules": [
                {"chat_id": 0, "kind": "include", "pattern": "такси"},
                {"chat_id": 0, "kind": "include", "pattern": "re:[broken"},
            ],
        }
        await msg(router, OWNER, "/import")
        actions = await msg(router, OWNER, json.dumps(config))
        body = actions[0].key_or_text
        assert "1" in body  # one rule landed
        assert "re:[broken" in body  # and the other is named
        assert {r.pattern for r in await store.list_rules(OWNER)} == {"такси"}

    async def test_import_refuses_a_file_that_is_not_ours(self, router, store):
        await msg(router, OWNER, "/import")
        actions = await msg(router, OWNER, '{"hello": "world"}')
        assert "not a ddiver export" in actions[0].key_or_text
        assert await store.list_rules(OWNER) == []

    async def test_import_tells_the_matcher_about_the_new_rules(self, router, store, matcher):
        await store.set_chat_shared(CHAT_A.id, True)
        await cb(router, OWNER, f"sub:{CHAT_A.id}:1")
        config = {
            "format": portable.FORMAT,
            "version": 1,
            "rules": [{"chat_id": 0, "kind": "include", "pattern": "такси"}],
        }
        await msg(router, OWNER, "/import")
        await msg(router, OWNER, json.dumps(config))
        assert await matcher.evaluate(CHAT_A.id, "нужно такси", 5)

    async def test_a_document_is_preferred_over_the_caption(self, router, store):
        """Telegram puts a caption on an attachment; the file is what was meant."""
        config = {
            "format": portable.FORMAT,
            "version": 1,
            "rules": [{"chat_id": 0, "kind": "include", "pattern": "изфайла"}],
        }
        await msg(router, OWNER, "/import")
        await router.dispatch(
            Incoming(
                user_id=OWNER,
                text="вот держи",
                document=json.dumps(config).encode(),
                document_name="ddiver.json",
            )
        )
        assert {r.pattern for r in await store.list_rules(OWNER)} == {"изфайла"}


class TestMalformedCallbacks:
    """Callback data is whatever the client sends, not what we drew on the button."""

    @pytest.mark.parametrize(
        "data",
        [
            "sub",
            "sub:1",
            "share",
            "share:1",
            "radd",
            "radd:1",
            "ig",
            "ig:1",
            "igdel",
            "igdel:1",
            "lang",
            "tz",
            "chats",
            "shared",
            "allow",
            "deny",
            "revoke",
            "rules",
            "rpick",
            "rdel",
            "ign",
            "igall",
            "rdrop",
            "rdropok",
            "set",
            "rules:1:bogus",
            "rules:notanumber:include:x",
            "sub::",
            "ig:::",
        ],
    )
    async def test_a_truncated_or_bogus_payload_answers_instead_of_raising(
        self, router, store, data
    ):
        """Every one of these used to reach the handler and raise, so the button just span."""
        actions = await cb(router, OWNER, data)
        assert actions, f"{data!r} produced no answer at all"

    async def test_the_moderator_path_is_covered_too(self, router, store):
        for data in ("sub", "ig:1", "rules", "rdrop", "set"):
            assert await cb(router, MOD, data), data


class TestIgnoreListLimit:
    async def test_the_buttons_respect_the_same_cap_as_an_import(self, router, store):
        """The import path capped this and the buttons did not, which is the wrong way round:
        every entry is read back into the matcher for every watched chat."""
        await store.set_chat_shared(CHAT_A.id, True)
        await cb(router, OWNER, f"sub:{CHAT_A.id}:1")
        for i in range(MAX_IGNORED_PER_USER):
            await store.add_ignored(OWNER, ALL_CHATS, 10_000 + i, None)

        actions = await cb(router, OWNER, f"ig:{CHAT_A.id}:777")
        assert "2000" in actions[0].key_or_text
        assert await store.count_ignored(OWNER) == MAX_IGNORED_PER_USER

        actions = await cb(router, OWNER, "igall:778")
        assert "2000" in actions[0].key_or_text
        assert await store.count_ignored(OWNER) == MAX_IGNORED_PER_USER

    async def test_below_the_cap_the_buttons_still_work(self, router, store):
        await store.set_chat_shared(CHAT_A.id, True)
        await cb(router, OWNER, f"sub:{CHAT_A.id}:1")
        await cb(router, OWNER, f"ig:{CHAT_A.id}:777")
        assert await store.count_ignored(OWNER) == 1


class TestPreviewBudget:
    async def test_a_slow_rule_set_does_not_run_forever(self, router, store, monkeypatch):
        """The live matcher stops after half a second per subscriber; /test had no limit at
        all, so a full set of slow patterns kept a worker thread busy for the whole set, for
        every chat, on every invocation."""
        import ddiver.bot.handlers as handlers_module

        monkeypatch.setattr(handlers_module, "PREVIEW_BUDGET", -1.0)
        await store.set_chat_shared(CHAT_A.id, True)
        await cb(router, OWNER, f"sub:{CHAT_A.id}:1")
        await store.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "такси")

        actions = await msg(router, OWNER, "/test нужно такси")
        assert "⏱" in actions[0].key_or_text

    async def test_an_ordinary_set_still_answers(self, router, store):
        await store.set_chat_shared(CHAT_A.id, True)
        await cb(router, OWNER, f"sub:{CHAT_A.id}:1")
        await store.add_rule(OWNER, CHAT_A.id, RuleKind.INCLUDE, "такси")
        actions = await msg(router, OWNER, "/test нужно такси")
        assert "✅" in actions[0].key_or_text

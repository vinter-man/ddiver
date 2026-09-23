"""Tests for the Telethon adapter layer, driven by fake clients (no connection)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from telethon import errors, events
from telethon.tl import types as tl

from ddiver.bot import menus
from ddiver.bot.router import Edit, Reply, Send, Toast
from ddiver.links import ChatRef
from ddiver.runtime import (
    RECONCILE_SECONDS,
    BotAdapter,
    notification_buttons_factory,
    refresh_dialogs,
)
from ddiver.storage import OutboxItem, Storage
from tests.fakes import T0, make_channel, make_user

PAYLOAD = {
    "chat_id": -1001,
    "chat_title": "Чат",
    "msg_id": 7,
    "date": T0.isoformat(),
    "text": "казино",
    "spans": [[0, 6]],
    "rule_pattern": "казино",
    "sender_id": 42,
}


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.error: BaseException | None = None

    async def send_message(self, peer: Any, text: str, **kwargs: Any) -> None:
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        self.sent.append({"peer": peer, "text": text, **kwargs})


class FakeEvent(events.CallbackQuery.Event):
    """A real CallbackQuery event, so the adapter's isinstance dispatch is exercised."""

    def __init__(self, sender_id: int = 1) -> None:
        query = tl.UpdateBotCallbackQuery(
            query_id=1,
            user_id=sender_id,
            peer=tl.PeerUser(sender_id),
            msg_id=7,
            chat_instance=0,
            data=b"menu",
        )
        super().__init__(query, tl.PeerUser(sender_id), 7)
        self.answers: list[tuple[str, bool]] = []
        self.edits: list[str] = []
        self.error: BaseException | None = None

    async def answer(self, text: str = "", *, alert: bool = False, **_kwargs: Any) -> None:
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        self.answers.append((text, alert))

    async def edit(self, text: str, **_kwargs: Any) -> None:
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        self.edits.append(text)


class FakeMessageEvent:
    """A real message event delegates `edit` to its Message, so the adapter must not use it."""

    def __init__(self, sender_id: int = 1) -> None:
        self.sender_id = sender_id
        self.edits: list[str] = []

    async def edit(self, *_args: Any, **_kwargs: Any) -> None:
        # Telethon would try to edit the *user's* message here and fail; the adapter must
        # never take this path.
        self.edits.append("must not happen")

    async def answer(self, *_args: Any, **_kwargs: Any) -> None:
        self.edits.append("must not happen")


class TestBotAdapter:
    async def test_toast_is_answered(self):
        bot, event = FakeBot(), FakeEvent()
        await BotAdapter(bot).perform(event, [Toast("bot.cancelled", alert=True)], "ru")
        assert event.answers == [("Отменено.", True)]
        assert bot.sent == []

    async def test_edit_uses_the_event_and_renders_buttons(self):
        bot, event = FakeBot(), FakeEvent()
        actions = [Edit("bot.menu.title", buttons=[[("bot.menu.chats", "chats:0")]])]
        await BotAdapter(bot).perform(event, actions, "ru")
        assert event.edits == ["Что делаем?"]

    async def test_a_message_event_gets_a_reply_not_an_edit(self):
        """A NewMessage event has .edit, but it edits the *user's* message — never use it."""
        bot, event = FakeBot(), FakeMessageEvent(sender_id=5)
        await BotAdapter(bot).perform(event, [Edit("bot.menu.title")], "en")
        assert event.edits == []
        assert bot.sent[0]["peer"] == 5
        assert bot.sent[0]["text"] == "What would you like to do?"

    async def test_a_message_event_never_gets_a_toast(self):
        bot, event = FakeBot(), FakeMessageEvent(sender_id=5)
        await BotAdapter(bot).perform(event, [Toast("bot.cancelled")], "en")
        assert event.edits == [] and bot.sent == []

    async def test_text_is_sent_literally(self):
        """Telethon defaults to markdown, which would eat __init__ out of a rule pattern."""
        bot, event = FakeBot(), FakeEvent()
        await BotAdapter(bot).perform(event, [Reply("re:__init__ **x**", literal=True)], "en")
        assert bot.sent[0]["text"] == "re:__init__ **x**"
        assert bot.sent[0]["parse_mode"] is None

    async def test_html_is_opt_in_per_action(self):
        """The help text uses <b>/<code>; everything else must stay literal."""
        bot, event = FakeBot(), FakeEvent()
        await BotAdapter(bot).perform(
            event, [Reply("bot.help", html=True), Reply("bot.cancelled")], "en"
        )
        assert bot.sent[0]["parse_mode"] == "html"
        assert bot.sent[1]["parse_mode"] is None

    async def test_an_uneditable_html_action_keeps_its_formatting(self):
        """Edit falls back to Reply for a plain message; the flag must survive the swap."""
        bot, event = FakeBot(), FakeMessageEvent(sender_id=5)
        await BotAdapter(bot).perform(event, [Edit("bot.help", html=True)], "en")
        assert bot.sent[0]["parse_mode"] == "html"

    async def test_reply_goes_to_the_sender_with_no_link_preview(self):
        bot, event = FakeBot(), FakeEvent(sender_id=9)
        await BotAdapter(bot).perform(event, [Reply("bot.help")], "uk")
        assert bot.sent[0]["peer"] == 9
        assert bot.sent[0]["link_preview"] is False
        assert "Синтаксис правил" in bot.sent[0]["text"]

    async def test_send_targets_another_user(self):
        bot, event = FakeBot(), FakeEvent()
        actions = [
            Send(
                777,
                "bot.gate.card",
                {"user_id": 3},
                buttons=[[("bot.btn.allow", "allow:3")]],
            )
        ]
        await BotAdapter(bot).perform(event, actions, "en")
        assert bot.sent[0]["peer"] == 777
        assert "3" in bot.sent[0]["text"]
        assert bot.sent[0]["buttons"][0][0].text == "✅ Allow"

    async def test_literal_text_is_not_translated(self):
        bot, event = FakeBot(), FakeEvent()
        await BotAdapter(bot).perform(event, [Reply("Готово: 5", literal=True)], "ru")
        assert bot.sent[0]["text"] == "Готово: 5"

    async def test_literal_button_labels_are_kept(self):
        bot, event = FakeBot(), FakeEvent()
        actions = [Reply("x", literal=True, buttons=[[(menus.literal("Мой чат"), "sub:1:1")]])]
        await BotAdapter(bot).perform(event, actions, "ru")
        assert bot.sent[0]["buttons"][0][0].text == "Мой чат"

    async def test_late_callback_answers_are_ignored(self, caplog):
        bot, event = FakeBot(), FakeEvent()
        event.error = errors.QueryIdInvalidError(request=None)
        await BotAdapter(bot).perform(event, [Toast("bot.cancelled")], "en")
        assert event.answers == []  # no exception escaped

    async def test_rpc_errors_are_logged_not_raised(self, caplog):
        bot, event = FakeBot(), FakeEvent()
        bot.error = errors.UserIsBlockedError(request=None)
        await BotAdapter(bot).perform(event, [Reply("bot.help")], "en")
        assert "could not perform" in caplog.text

    async def test_message_not_modified_is_silent(self):
        bot, event = FakeBot(), FakeEvent()
        event.error = errors.MessageNotModifiedError(request=None)
        await BotAdapter(bot).perform(event, [Edit("bot.menu.title")], "en")
        assert event.edits == []

    async def test_send_message_port_passes_entities(self):
        bot = FakeBot()
        adapter = BotAdapter(bot)
        entities = [tl.MessageEntityBold(0, 2)]
        await adapter.send_message(5, "hi", formatting_entities=entities, buttons=None)
        assert bot.sent[0]["formatting_entities"] == entities
        assert bot.sent[0]["link_preview"] is False


class TestNotificationButtons:
    def test_both_buttons_when_the_sender_is_known(self):
        item = OutboxItem(1, -1001, 7, 3, "pending", 0, PAYLOAD, T0)
        rows = notification_buttons_factory()(item, "ru")
        labels = [button.text for button in rows[0]]
        assert labels == ["🚫 Игнорировать отправителя", "🗑 Убрать это правило"]
        data = [button.type.data.decode() for button in rows[0]]
        assert data == ["ig:-1001:42", "rdrop:3"]

    def test_only_the_rule_button_when_the_sender_is_unknown(self):
        payload = {**PAYLOAD, "sender_id": None}
        item = OutboxItem(1, -1001, 7, 3, "pending", 0, payload, T0)
        rows = notification_buttons_factory()(item, "en")
        assert [b.type.data.decode() for b in rows[0]] == ["rdrop:3"]

    def test_no_keyboard_at_all_when_the_row_has_neither(self):
        """A catch-up row whose rule was deleted meanwhile has nothing to offer."""
        payload = {**PAYLOAD, "sender_id": None}
        item = OutboxItem(1, -1001, 7, None, "pending", 0, payload, T0)
        assert notification_buttons_factory()(item, "en") == []


class FakeDialogClient:
    def __init__(self, entities: list[Any]) -> None:
        self._entities = entities
        self.saved = False

        class _Session:
            def __init__(self, outer: FakeDialogClient) -> None:
                self._outer = outer

            def save(self) -> None:
                self._outer.saved = True

        self.session = _Session(self)

    def iter_dialogs(self) -> Any:
        entities = self._entities

        async def gen() -> Any:
            for entity in entities:
                yield type("Dialog", (), {"entity": entity})()

        return gen()


class TestRefreshDialogs:
    async def test_stores_group_chats_and_skips_users(self, tmp_path):
        storage = Storage(tmp_path / "db.sqlite3", owner_id=1)
        await storage.open()
        try:
            channel = make_channel(-1001234567890, "Группа", username="grp")
            channel.access_hash = 4242
            client = FakeDialogClient([channel, make_user(5, "Человек", "human")])
            assert await refresh_dialogs(client, storage) == 1
            row = await storage.get_chat(-1001234567890)
            assert row is not None
            assert row.ref == ChatRef(-1001234567890, "Группа", "grp", "supergroup")
            assert row.access_hash == 4242
            assert client.saved is True  # entities committed immediately, not in a minute
        finally:
            await storage.close()

    async def test_survives_a_session_that_cannot_save(self, tmp_path):
        storage = Storage(tmp_path / "db.sqlite3", owner_id=1)
        await storage.open()
        try:
            client = FakeDialogClient([make_channel(-1002, "X")])

            def boom() -> None:
                raise RuntimeError("cannot save")

            client.session.save = boom  # type: ignore[method-assign]
            assert await refresh_dialogs(client, storage) == 1
        finally:
            await storage.close()


@pytest.mark.parametrize(
    ("keyboard", "expected_first"),
    [
        (menus.timezone_menu(), menus.literal("Europe/Kyiv")),
        (menus.settings_menu(), "bot.settings.language"),
        (menus.language_menu(), menus.literal("Українська")),
    ],
)
def test_static_menus(keyboard, expected_first):
    assert keyboard[0][0][0] == expected_first


class FakeStorageClient:
    """Minimal user client for the Application helpers that touch Telethon."""

    def __init__(self, me_id: int = 999) -> None:
        self._me = type("User", (), {"id": me_id, "first_name": "Me"})()
        self._self_id = me_id
        self.handlers: list[tuple[object, object]] = []
        self.reconnected = asyncio.Event()

    def on(self, event):
        def register(func):
            self.handlers.append((event, func))
            return func

        return register

    async def get_me(self):
        return self._me

    def is_connected(self) -> bool:
        return True


class FakeBotForApp(FakeBot):
    """The bot client the Application builds its adapter around."""

    def on(self, event):
        def register(func):
            return func

        return register


@pytest.fixture
async def app(tmp_path, monkeypatch):
    """An Application wired to fakes, without connecting anything."""
    import ddiver.runtime as runtime_module
    from ddiver.config import Settings

    for key, value in {
        "API_ID": "1",
        "API_HASH": "0" * 32,
        "BOT_TOKEN": "123456789:AAExampleTokenWithEnoughLength_xyz",
        "OWNER_ID": "1",
        "DATA_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(runtime_module, "make_user_client", lambda *a, **k: FakeStorageClient())
    monkeypatch.setattr(runtime_module, "make_bot_client", lambda *a, **k: FakeBotForApp())
    built = runtime_module.Application(Settings.load(None))
    await built.setup()
    yield built
    await built.storage.close()


class TestMigration:
    async def test_group_upgrade_moves_subscriptions_and_tells_subscribers(self, app):
        from telethon import utils

        from ddiver.rules import RuleKind

        old_ref = ChatRef(-555, "Старая группа", None, "group")
        await app.storage.upsert_chat(old_ref)
        await app.storage.subscribe(1, -555)
        rule = await app.storage.add_rule(1, -555, RuleKind.INCLUDE, "казино")

        new_channel = make_channel(-1001234567890, "Старая группа")
        new_channel.access_hash = 77
        action = tl.MessageActionChatMigrateTo(channel_id=1234567890)
        service = tl.MessageService(id=9, peer_id=tl.PeerChat(555), date=T0, action=action)
        update = tl.UpdateNewMessage(service, pts=1, pts_count=1)
        update._entities = {utils.get_peer_id(new_channel): new_channel}

        await app._on_service_message(service, update)

        assert [c.id for c in await app.storage.list_subscriptions(1)] == [-1001234567890]
        assert (await app.storage.get_rule(rule.id, 1)).chat_id == -1001234567890
        assert (await app.storage.get_chat(-1001234567890)).access_hash == 77
        assert (await app.storage.get_chat(-555)).available is False
        assert any(
            "supergroup" in m["text"] or "супергруп" in m["text"] for m in app.bot._client.sent
        )

    async def test_other_service_messages_are_ignored(self, app):

        service = tl.MessageService(
            id=1,
            peer_id=tl.PeerChat(555),
            date=T0,
            action=tl.MessageActionChatJoinedByLink(inviter_id=1),
        )
        await app._on_service_message(service, tl.UpdateNewMessage(service, 1, 1))
        await app._on_service_message(None, None)  # must not raise


class TestHeartbeatContents:
    async def test_carries_the_listener_state(self, app):
        from ddiver.health import read_heartbeat

        await app._heartbeat()
        beat = read_heartbeat(app.settings.heartbeat_path)
        assert beat is not None and beat.written_at is not None
        assert beat.queue == 0
        assert beat.degraded is False


class TestSenderResolution:
    async def test_a_basic_group_member_is_found_after_loading_the_member_list(self, app):
        """Basic-group updates carry no entities, so a bare get_entity cannot work."""
        await app.storage.upsert_chat(ChatRef(-555, "Группа", None, "group"))
        calls: list[str] = []
        known: dict[int, object] = {}

        async def get_entity(peer_id):
            calls.append(f"get_entity({peer_id})")
            if peer_id not in known:
                raise ValueError("Could not find the input entity")
            return known[peer_id]

        async def get_participants(chat_id):
            calls.append(f"get_participants({chat_id})")
            known[4242] = make_user(4242, "Сергей", "serge", min_=False)
            return list(known.values())

        app.user_client.get_entity = get_entity
        app.user_client.get_participants = get_participants

        entity = await app._resolve_entity(4242, -555)
        assert entity.first_name == "Сергей"
        assert calls == ["get_entity(4242)", "get_participants(-555)", "get_entity(4242)"]

        # the member list is loaded once per chat, not per message
        calls.clear()
        await app._resolve_entity(4242, -555)
        assert calls == ["get_entity(4242)"]

    async def test_a_supergroup_failure_is_not_retried_with_participants(self, app):
        await app.storage.upsert_chat(ChatRef(-1001, "Супергруппа", None, "supergroup"))

        async def get_entity(peer_id):
            raise ValueError("Could not find the input entity")

        app.user_client.get_entity = get_entity
        with pytest.raises(ValueError, match="input entity"):
            await app._resolve_entity(4242, -1001)


class TestListenerLifetimes:
    async def test_a_restart_runs_a_catch_up_but_the_first_start_does_not(self, app):
        """Telethon replays at most 100 messages per channel and jumps the rest, so a gap
        after a full disconnect is ours to close."""
        rounds: list[int] = []

        async def fake_catch_up():
            rounds.append(1)

        async def run_until_disconnected():
            raise ConnectionError("dropped")

        app._catch_up = fake_catch_up
        app.user_client.run_until_disconnected = run_until_disconnected
        app.user_client.is_connected = lambda: True

        with pytest.raises(ConnectionError):
            await app._run_listener()
        assert rounds == []  # the boot round already covered this

        with pytest.raises(ConnectionError):
            await app._run_listener()
        assert rounds == [1]  # the restart closed the gap

    async def test_a_crash_loop_does_not_hammer_history(self, app):
        rounds: list[int] = []

        async def fake_catch_up():
            rounds.append(1)

        async def run_until_disconnected():
            raise ConnectionError("dropped")

        app._catch_up = fake_catch_up
        app.user_client.run_until_disconnected = run_until_disconnected
        app.user_client.is_connected = lambda: True
        app.supervisor.state.stalled = True

        for _ in range(3):
            with pytest.raises(ConnectionError):
                await app._run_listener()
        assert rounds == []

    async def test_the_background_task_set_is_what_operations_expects(self, app):
        app._start_background_tasks()
        names = {task._name for task in app._tasks}
        assert names == {
            "cursor-flush",
            "heartbeat",
            "ping",
            "reconcile",
            "summary",
            "prune",
            "reprobe",
            "outbox-rescan",
        }
        for task in app._tasks:
            await task.stop()


class TestCallbackAnswers:
    async def test_a_callback_without_a_toast_is_still_answered(self, app):
        """Otherwise Telegram keeps the button spinning for the moderator."""
        answered: list[str] = []

        class Event(FakeEvent):
            async def answer(self, text: str = "", *, alert: bool = False, **_kw: Any) -> None:
                answered.append(text)

            async def get_sender(self):
                return make_user(1, "Owner", None, min_=False)

        event = Event(sender_id=1)
        await app._on_bot_update(event, data="menu")
        assert answered == [""]  # the menu edit carries no toast of its own


class TestChatActionMembership:
    """Losing and regaining access to a chat, in the shapes Telegram actually sends.

    Verified against Telethon 1.45: ``user_ids`` carries the whole batch while ``user_id`` is
    only the first of them, and a kick that arrives as ``UpdateChannelParticipant`` has no
    action message at all.
    """

    @staticmethod
    def _service_event(action: Any, *, peer: Any, me: int) -> Any:
        service = tl.MessageService(
            id=5, peer_id=peer, date=T0, action=action, from_id=tl.PeerUser(111)
        )
        return events.ChatAction.build(tl.UpdateNewMessage(service, 1, 1), None, me)

    async def test_a_re_add_alongside_other_users_is_noticed(self, app):
        """Telegram reports the whole batch; user_id alone is just the first of them."""
        app.user_client._self_id = 999
        await app.storage.upsert_chat(ChatRef(-555, "Группа", None, "group"))
        await app.storage.set_chat_available(-555, False)

        event = self._service_event(
            tl.MessageActionChatAddUser(users=[111, 222, 999]),
            peer=tl.PeerChat(555),
            me=999,
        )
        await app._on_chat_action(event)

        chat = await app.storage.get_chat(-555)
        assert chat is not None
        assert chat.available is True

    async def test_a_kick_without_an_action_message_is_handled(self, app):
        """UpdateChannelParticipant carries no service message; only chat_id is available."""
        app.user_client._self_id = 999
        await app.storage.upsert_chat(ChatRef(-1001234567890, "Чат", None, "supergroup"))
        update = tl.UpdateChannelParticipant(
            channel_id=1234567890,
            date=T0,
            actor_id=1,
            user_id=999,
            prev_participant=tl.ChannelParticipant(user_id=999, date=T0),
            new_participant=None,
            qts=1,
        )
        event = events.ChatAction.build(update, None, 999)
        assert event.action_message is None  # the shape that used to raise
        await app._on_chat_action(event)

        chat = await app.storage.get_chat(-1001234567890)
        assert chat is not None
        assert chat.available is False

    async def test_somebody_elses_membership_change_is_ignored(self, app):
        app.user_client._self_id = 999
        await app.storage.upsert_chat(ChatRef(-555, "Группа", None, "group"))
        event = self._service_event(
            tl.MessageActionChatDeleteUser(user_id=111), peer=tl.PeerChat(555), me=999
        )
        await app._on_chat_action(event)

        chat = await app.storage.get_chat(-555)
        assert chat is not None
        assert chat.available is True


class TestBootStormDelay:
    """The crash-loop damper must slow the backfill down without silencing the process."""

    async def test_the_backfill_waits_but_the_heartbeat_does_not(self, app, monkeypatch):
        import ddiver.runtime as runtime_module

        waited: list[int] = []
        released = asyncio.Event()

        async def fake_boot_delay(boots, stop=None):
            waited.append(boots)
            await released.wait()
            return 300.0

        rounds: list[int] = []

        async def fake_catch_up():
            rounds.append(1)

        monkeypatch.setattr(runtime_module, "boot_delay", fake_boot_delay)
        monkeypatch.setattr(app, "_catch_up", fake_catch_up)

        task = asyncio.create_task(app._boot_catch_up(9))
        await asyncio.sleep(0)
        assert waited == [9]
        assert rounds == []  # the backfill is held back...

        # ...while the heartbeat keeps reporting, which is what keeps an orchestrator
        # from restarting the container mid-delay.
        await app._heartbeat()
        assert app.settings.heartbeat_path.exists()

        released.set()
        await task
        assert rounds == [1]

    async def test_a_stop_during_the_delay_cancels_the_backfill(self, app, monkeypatch):
        import ddiver.runtime as runtime_module

        async def fake_boot_delay(boots, stop=None):
            app.supervisor.stop_event.set()
            return 300.0

        rounds: list[int] = []

        async def fake_catch_up():
            rounds.append(1)

        monkeypatch.setattr(runtime_module, "boot_delay", fake_boot_delay)
        monkeypatch.setattr(app, "_catch_up", fake_catch_up)

        await app._boot_catch_up(9)
        assert rounds == []  # shutting down: no point starting a backfill now

    async def test_reconcile_does_not_double_the_boot_catch_up(self, app):
        """The boot catch-up covers t=0; a reconcile there would only repeat it."""
        app._start_background_tasks()
        reconcile = next(t for t in app._tasks if t._name == "reconcile")
        assert reconcile._first_delay == RECONCILE_SECONDS
        await asyncio.gather(*(t.stop() for t in app._tasks))


class TestCommandList:
    """Telegram only offers a "/" menu for commands the bot has registered."""

    async def test_every_language_gets_the_command_list(self, app, monkeypatch):
        from ddiver.i18n import LANGS

        sent: list[Any] = []

        async def call(request):
            sent.append(request)

        monkeypatch.setattr(type(app.bot_client), "__call__", staticmethod(call), raising=False)
        await app._publish_commands()

        assert [r.lang_code for r in sent] == [*LANGS, ""]
        names = [c.command for c in sent[0].commands]
        assert names == ["start", "menu", "test", "status", "export", "import", "help", "cancel"]
        assert all(c.description for r in sent for c in r.commands)
        russian = next(r for r in sent if r.lang_code == "ru")
        assert russian.commands[0].description == "Начать и открыть меню"

    async def test_a_failure_does_not_stop_the_bot(self, app, monkeypatch):

        async def call(_request):
            raise errors.RPCError("request", "nope", 400)

        monkeypatch.setattr(type(app.bot_client), "__call__", staticmethod(call), raising=False)
        await app._publish_commands()  # must not raise


class TestRequestStop:
    """SIGTERM arrives on a signal handler, which cannot await."""

    async def test_the_listener_is_actually_disconnected(self, app):
        disconnected = []

        async def disconnect():
            disconnected.append(True)

        app.user_client.disconnect = disconnect
        app.request_stop()
        assert app._stop_task is not None
        await app._stop_task
        assert disconnected == [True]  # a bare call would only have built a coroutine

    async def test_it_survives_a_client_that_refuses(self, app):
        async def disconnect():
            raise ConnectionError("already gone")

        app.user_client.disconnect = disconnect
        app.request_stop()
        await app._stop_task  # must not raise: shutdown cannot be blocked by this

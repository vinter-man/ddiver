"""Tests for the Telethon adapter layer, driven by fake clients (no connection)."""

from __future__ import annotations

from typing import Any

import pytest
from telethon import errors
from telethon.tl import types as tl

from ddiver.bot import menus
from ddiver.bot.router import Edit, Reply, Send, Toast
from ddiver.links import ChatRef
from ddiver.runtime import BotAdapter, notification_buttons_factory, payload_of, refresh_dialogs
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


class FakeEvent:
    """A callback-query-like event: it can answer and edit."""

    def __init__(self, sender_id: int = 1) -> None:
        self.sender_id = sender_id
        self.answers: list[tuple[str, bool]] = []
        self.edits: list[str] = []
        self.error: BaseException | None = None

    async def answer(self, text: str = "", *, alert: bool = False) -> None:
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
    """A plain message event: it can neither answer nor edit."""

    def __init__(self, sender_id: int = 1) -> None:
        self.sender_id = sender_id


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

    async def test_edit_falls_back_to_a_reply_when_the_event_cannot_edit(self):
        bot, event = FakeBot(), FakeMessageEvent(sender_id=5)
        await BotAdapter(bot).perform(event, [Edit("bot.menu.title")], "en")
        assert bot.sent[0]["peer"] == 5
        assert bot.sent[0]["text"] == "What would you like to do?"

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
        item = OutboxItem(1, -1001, 7, 3, "pending", 0, PAYLOAD, T0)  # type: ignore[arg-type]
        rows = notification_buttons_factory(None)(item, "ru")
        labels = [button.text for button in rows[0]]
        assert labels == ["🚫 Игнорировать отправителя", "➖ Добавить исключение"]
        data = [button.type.data.decode() for button in rows[0]]
        assert data == ["ig:-1001:42", "ex:-1001:7"]

    def test_only_exclude_when_the_sender_is_unknown(self):
        payload = {**PAYLOAD, "sender_id": None}
        item = OutboxItem(1, -1001, 7, 3, "pending", 0, payload, T0)  # type: ignore[arg-type]
        rows = notification_buttons_factory(None)(item, "en")
        assert [b.type.data.decode() for b in rows[0]] == ["ex:-1001:7"]

    def test_payload_of_parses_the_row(self):
        item = OutboxItem(1, -1001, 7, 3, "pending", 0, PAYLOAD, T0)  # type: ignore[arg-type]
        assert payload_of(item).chat_title == "Чат"


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

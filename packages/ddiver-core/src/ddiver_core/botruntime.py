"""The bot half of the Telegram adapter: router actions and buttons as Telethon calls.

Handlers in ``ddiver_core.bot`` return plain actions; :class:`BotAdapter` performs them with a
real bot client, and ``notification_buttons_factory`` turns an outbox row into the keyboard
under a notification. None of it touches a listener's session, which is why it is shared; the
composition root that owns the clients and the background tasks stays with each app.
"""

from __future__ import annotations

import contextlib
import io
import logging
from typing import Any

from telethon import Button, TelegramClient, errors, events
from telethon.tl import types as tl

from ddiver_core.bot import menus
from ddiver_core.bot.router import Edit, Reply, Send, SendDocument, Toast
from ddiver_core.delivery import ButtonFactory
from ddiver_core.i18n import t
from ddiver_core.storage import OutboxItem

log = logging.getLogger(__name__)


class BotAdapter:
    """Turns :mod:`ddiver_core.bot.router` actions into Telethon calls."""

    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    @staticmethod
    def _buttons(rows: Any, lang: str) -> Any:
        if not rows:
            return None
        return [
            [
                Button.inline(
                    menus.unwrap(label) if menus.is_literal(label) else t(lang, label), data
                )
                for label, data in row
            ]
            for row in rows
        ]

    @staticmethod
    def _text(action: Any, lang: str) -> str:
        if action.literal:
            return str(action.key_or_text)
        return t(lang, action.key_or_text, **action.params)

    async def perform(self, event: Any, actions: list[Any], lang: str) -> None:
        # Only a callback query can be edited or answered. `hasattr(event, "edit")` looks right
        # but is always true: a NewMessage event delegates unknown attributes to its Message,
        # so it would try to edit the moderator's own message as the bot.
        editable = isinstance(event, events.CallbackQuery.Event)
        for action in actions:
            try:
                await self._one(event, action, lang, editable=editable)
            except errors.QueryIdInvalidError:
                log.debug("callback answer arrived too late")
            except (errors.RPCError, ValueError):
                log.warning("could not perform %s", type(action).__name__, exc_info=True)

    async def _one(self, event: Any, action: Any, lang: str, *, editable: bool = False) -> None:
        if isinstance(action, Toast):
            if editable:
                await event.answer(self._text(action, lang), alert=action.alert)
            return
        if isinstance(action, Edit):
            if editable:
                with contextlib.suppress(errors.MessageNotModifiedError):
                    await event.edit(
                        self._text(action, lang),
                        buttons=self._buttons(action.buttons, lang),
                        link_preview=False,
                        parse_mode=_parse_mode(action),
                    )
                return
            action = Reply(
                action.key_or_text, action.params, action.buttons, action.literal, action.html
            )
        if isinstance(action, Reply):
            await self._client.send_message(
                await self._peer(event),
                self._text(action, lang),
                buttons=self._buttons(action.buttons, lang),
                link_preview=False,
                parse_mode=_parse_mode(action),
            )
            return
        if isinstance(action, SendDocument):
            await self._client.send_file(
                await self._peer(event),
                io.BytesIO(action.content),
                attributes=[tl.DocumentAttributeFilename(action.filename)],
                caption=t(lang, action.caption_key, **action.params),
                parse_mode=None,
            )
            return
        if isinstance(action, Send):
            await self._client.send_message(
                action.user_id,
                self._text(action, lang),
                buttons=self._buttons(action.buttons, lang),
                link_preview=False,
                parse_mode=None,
            )

    @staticmethod
    async def _peer(event: Any) -> int:
        return int(event.sender_id)

    async def send_document(self, user_id: int, filename: str, content: bytes, caption: str) -> Any:
        return await self._client.send_file(
            user_id,
            io.BytesIO(content),
            attributes=[tl.DocumentAttributeFilename(filename)],
            caption=caption,
            parse_mode=None,
        )

    async def send(
        self,
        user_id: int,
        text: str,
        *,
        formatting_entities: list[tl.TypeMessageEntity] | None = None,
        buttons: Any = None,
        link_preview: bool = False,
    ) -> Any:
        return await self._client.send_message(
            user_id,
            text,
            formatting_entities=formatting_entities,
            buttons=buttons,
            link_preview=link_preview,
            parse_mode=None,  # our text is literal; markdown would eat __init__, **, ` and ~~
        )

    # Delivery's BotPort protocol
    send_message = send


def self_id_of(client: Any) -> int | None:
    """The account's own id, without an RPC.

    ``get_me()`` always issues ``GetUsers`` unless it is asked for the input peer, and a
    membership change in any of 50 chats must not cost a request.
    """
    return getattr(client, "_self_id", None)


def _parse_mode(action: Any) -> str | None:
    """Markdown is never used: it would eat __init__, ``**`` and backticks in a rule pattern.
    HTML is opt-in per action and only for catalog strings we wrote ourselves.
    """
    return "html" if getattr(action, "html", False) else None


def notification_buttons_factory() -> ButtonFactory:
    """Build the inline keyboard under a notification from its outbox row."""

    def factory(item: OutboxItem, lang: str) -> list[list[Any]]:
        payload = item.payload
        rows = menus.notification_buttons(
            item.chat_id, item.msg_id, payload.get("sender_id"), item.rule_id
        )
        return [[Button.inline(t(lang, label), data) for label, data in row] for row in rows]

    return factory

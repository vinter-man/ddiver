"""Build Telegram links for chats, messages and senders.

Facts this module encodes (verified against Telegram clients):

* Public chats:      ``https://t.me/<username>``, message ``https://t.me/<username>/<msg_id>``
* Private supergroups/channels: message ``https://t.me/c/<bare_id>/<msg_id>`` where ``bare_id``
  is the channel id without the ``-100`` prefix. The chat itself has no standalone link, so we
  point the chat title at the message link.
* Forum topics insert the topic id: ``.../<topic_id>/<msg_id>`` (general topic id is 1).
* Basic (non-super) groups have no message links at all.
* Users: ``https://t.me/<username>`` or, without a username, ``tg://user?id=<id>``.
"""

from __future__ import annotations

from dataclasses import dataclass

from telethon import utils
from telethon.tl import types as tl

GENERAL_TOPIC_ID = 1


@dataclass(frozen=True, slots=True)
class ChatRef:
    """A monitored chat, as the rest of the program passes it around.

    It lives here because every field exists to build a link: the username decides public
    versus private, ``is_forum`` decides whether a topic id belongs in the path, and ``kind``
    decides whether a message link exists at all. ``id`` is the Telethon *marked* id.
    """

    id: int
    title: str
    username: str | None
    kind: str  # 'supergroup' | 'channel' | 'group'
    is_forum: bool = False

    @property
    def bare_id(self) -> int:
        bare: int = utils.resolve_id(self.id)[0]
        return bare

    @property
    def is_basic_group(self) -> bool:
        return self.kind == "group"

    @classmethod
    def from_entity(
        cls, entity: tl.Chat | tl.Channel | tl.ChatForbidden | tl.ChannelForbidden
    ) -> ChatRef:
        marked = utils.get_peer_id(entity)
        title = getattr(entity, "title", None) or str(marked)
        if isinstance(entity, tl.Channel):
            kind = "supergroup" if entity.megagroup else "channel"
            return cls(marked, title, entity.username, kind, bool(entity.forum))
        if isinstance(entity, tl.ChannelForbidden):
            kind = "supergroup" if entity.megagroup else "channel"
            return cls(marked, title, None, kind, False)
        return cls(marked, title, None, "group", False)


def chat_link(chat: ChatRef) -> str | None:
    """Link to the chat itself; private chats have none (callers fall back to the message link)."""
    if chat.username:
        return f"https://t.me/{chat.username}"
    return None


def message_link(chat: ChatRef, msg_id: int, topic_id: int | None = None) -> str | None:
    if chat.is_basic_group:
        return None
    topic = f"/{topic_id}" if topic_id else ""
    if chat.username:
        return f"https://t.me/{chat.username}{topic}/{msg_id}"
    return f"https://t.me/c/{chat.bare_id}{topic}/{msg_id}"


def user_link(user_id: int, username: str | None) -> str:
    if username:
        return f"https://t.me/{username}"
    return f"tg://user?id={user_id}"


def topic_id_of(message: tl.Message) -> int | None:
    """Topic id for messages in forum supergroups; ``None`` outside forums."""
    reply_to = message.reply_to
    if isinstance(reply_to, tl.MessageReplyHeader) and reply_to.forum_topic:
        return reply_to.reply_to_top_id or reply_to.reply_to_msg_id or GENERAL_TOPIC_ID
    return None

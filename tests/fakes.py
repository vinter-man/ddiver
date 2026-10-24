"""Offline Telethon events and a fake user client, built without a connection.

The clock and the bot client fakes are shared with every app on ddiver-core and live in
``ddiver_core_testing``, a dev-only package.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from telethon import events, utils
from telethon.tl import types as tl

from ddiver_core_testing import T0

SELF_ID = 999


class _MbCache:
    def get(self, _bare_id: int) -> None:
        return None


class FakeUserClient:
    """The minimum surface Telethon events need, plus recorders for what we call."""

    parse_mode = None

    def __init__(self, self_id: int = SELF_ID) -> None:
        self._self_id = self_id
        self._mb_entity_cache = _MbCache()
        self.entity_calls: list[int] = []
        self.entities: dict[int, Any] = {}

    async def get_entity(self, peer_id: int) -> Any:
        self.entity_calls.append(peer_id)
        try:
            return self.entities[peer_id]
        except KeyError as exc:
            msg = f"Could not find the input entity for {peer_id}"
            raise ValueError(msg) from exc


def make_channel(
    chat_id: int = -1001234567890,
    title: str = "Test chat",
    username: str | None = None,
    *,
    megagroup: bool = True,
    forum: bool = False,
) -> tl.Channel:
    bare = utils.resolve_id(chat_id)[0]
    return tl.Channel(
        id=bare,
        title=title,
        photo=tl.ChatPhotoEmpty(),
        date=None,
        megagroup=megagroup,
        broadcast=not megagroup,
        username=username,
        forum=forum,
    )


def make_user(
    user_id: int = 42,
    first_name: str | None = "Alex",
    username: str | None = "taras",
    *,
    bot: bool = False,
    deleted: bool = False,
    min_: bool = True,
) -> tl.User:
    return tl.User(
        id=user_id,
        first_name=first_name,
        username=username,
        bot=bot,
        deleted=deleted,
        min=min_,
        access_hash=None if min_ else 12345,
    )


def make_event(
    text: str = "hello",
    *,
    chat: tl.Channel | tl.Chat | None = None,
    sender: tl.User | tl.Channel | None = None,
    msg_id: int = 77,
    date: dt.datetime = T0,
    out: bool = False,
    topic_id: int | None = None,
    fwd_from_name: str | None = None,
    client: FakeUserClient | None = None,
) -> events.NewMessage.Event:
    """Build a ``NewMessage`` event offline, with entities attached like Telethon does."""
    chat = chat if chat is not None else make_channel()
    sender = sender if sender is not None else make_user()
    reply_to = None
    if topic_id is not None:
        reply_to = tl.MessageReplyHeader(reply_to_msg_id=topic_id, forum_topic=True)
    fwd = None
    if fwd_from_name is not None:
        fwd = tl.MessageFwdHeader(date=date, from_name=fwd_from_name)
    message = tl.Message(
        id=msg_id,
        peer_id=tl.PeerChannel(chat.id) if isinstance(chat, tl.Channel) else tl.PeerChat(chat.id),
        message=text,
        date=date,
        from_id=_peer_of(sender),
        out=out,
        reply_to=reply_to,
        fwd_from=fwd,
    )
    event = events.NewMessage.Event(message)
    event._entities = {
        utils.get_peer_id(chat): chat,
        utils.get_peer_id(sender): sender,
    }
    event._set_client(client or FakeUserClient())
    return event


def _peer_of(entity: tl.User | tl.Channel) -> tl.TypePeer:
    if isinstance(entity, tl.User):
        return tl.PeerUser(entity.id)
    return tl.PeerChannel(entity.id)

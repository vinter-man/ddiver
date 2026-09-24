"""The user-session side: decide what to look at, match it, and make the match durable.

Hot-path rules (see DESIGN.md "Invariants"):

* no RPC on the listener session inside a handler — only entities attached to the update;
* a private chat is never monitored, whatever the database says;
* the outbox row is written before the cursor moves past the message.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from telethon import utils
from telethon.tl import types as tl

from ddiver.formatter import Payload
from ddiver.links import ChatRef, topic_id_of
from ddiver.matcher import Match, Matcher
from ddiver.rules import PreparedText, prepare_text
from ddiver.senders import SenderCache, SenderInfo, sender_of
from ddiver.storage import Cursor, OutboxItem, Storage, safe_write
from ddiver.timeutil import Clock, utcnow

log = logging.getLogger(__name__)

CURSOR_FLUSH_SECONDS = 5.0

DeliverHook = Callable[[OutboxItem], Awaitable[Any]]


@dataclass(slots=True)
class ListenerMetrics:
    seen: int = 0
    considered: int = 0
    matched: int = 0
    claimed: int = 0
    duplicates: int = 0
    skipped_bot: int = 0
    skipped_private: int = 0
    skipped_unmonitored: int = 0
    skipped_own: int = 0
    unknown_sender: int = 0


@dataclass(slots=True)
class CursorTracker:
    """Highest message id seen per chat, flushed periodically.

    A chat's flush is withheld until its catch-up finished, so a crash in the middle of a
    backfill cannot leave the cursor past messages that were never examined.
    """

    storage: Storage
    pending: dict[int, tuple[int, dt.datetime]] = field(default_factory=dict)
    blocked: set[int] = field(default_factory=set)
    holds: dict[int, set[int]] = field(default_factory=dict)
    """Message ids being processed, per chat. The cursor may never pass the lowest of them.

    Handling a match awaits storage before the row exists; handling a non-match awaits
    nothing. Telethon dispatches each update in its own task, so message 101 can note its id
    and be flushed while message 100 is still deciding whether it is a match — and a stored
    cursor above 100 makes 100 unreachable, because catch-up only ever starts at the cursor.
    A hold is taken before the first await and released only when that message has actually
    reached ``note``; a message that failed keeps its hold, which is the point.
    """

    def hold(self, chat_id: int, msg_id: int) -> None:
        self.holds.setdefault(chat_id, set()).add(msg_id)

    def release(self, chat_id: int, msg_id: int) -> None:
        """Release a hold. Deliberately not called from a ``finally``.

        A message whose claim raised is not durable anywhere: releasing it would let the next
        message flush the cursor straight past it, which is the loss this exists to prevent.
        The hold stays for the life of the process; the next start re-examines the chat from
        the older cursor and the outbox deduplicates whatever arrives twice.
        """
        held = self.holds.get(chat_id)
        if held is None:
            return
        held.discard(msg_id)
        if not held:
            del self.holds[chat_id]

    def _ceiling(self, chat_id: int) -> int | None:
        """Highest id the cursor may reach for this chat right now."""
        held = self.holds.get(chat_id)
        return min(held) - 1 if held else None

    def note(self, chat_id: int, msg_id: int, moment: dt.datetime) -> None:
        current = self.pending.get(chat_id)
        if current is None or msg_id > current[0]:
            self.pending[chat_id] = (msg_id, moment)

    def drop(self, chat_id: int) -> None:
        """Forget a pending note (a catch-up round did not verify this chat after all)."""
        self.pending.pop(chat_id, None)

    def cap(self, chat_id: int, max_id: int) -> None:
        """Lower a pending note. Replaying a chat notes every message it handled, so a failure
        in the middle has to pull the note back below it — otherwise the messages it skipped
        would fall below the cursor and never be fetched again."""
        current = self.pending.get(chat_id)
        if current is None:
            return
        if max_id <= 0:
            del self.pending[chat_id]
        elif current[0] > max_id:
            self.pending[chat_id] = (max_id, current[1])

    def block(self, chat_id: int) -> None:
        self.blocked.add(chat_id)

    def unblock(self, chat_id: int) -> None:
        self.blocked.discard(chat_id)

    async def flush(self) -> int:
        ready = []
        for chat_id, (msg_id, moment) in self.pending.items():
            if chat_id in self.blocked:
                continue
            ceiling = self._ceiling(chat_id)
            if ceiling is None:
                ready.append(Cursor(chat_id, msg_id, moment))
            elif ceiling > 0:
                # Something earlier is still in flight: write what is safely below it and
                # keep the rest pending, rather than skipping the chat entirely.
                ready.append(Cursor(chat_id, min(msg_id, ceiling), moment))
        if not ready:
            return 0
        if not await safe_write(self.storage.upsert_cursors(ready), "cursor flush"):
            return 0
        for cursor in ready:
            current = self.pending.get(cursor.chat_id)
            if current is not None and current[0] == cursor.last_msg_id:
                del self.pending[cursor.chat_id]
        return len(ready)

    def held_ids(self, chat_id: int) -> set[int]:
        """For tests and the heartbeat: what is still in flight in this chat."""
        return set(self.holds.get(chat_id, ()))


def chat_ref_of(entity: Any) -> ChatRef | None:
    """Chat metadata from an update entity, or ``None`` for private chats and unknown types."""
    if entity is None or isinstance(entity, tl.User):
        return None
    if isinstance(entity, (tl.Chat, tl.Channel, tl.ChatForbidden, tl.ChannelForbidden)):
        return ChatRef.from_entity(entity)
    return None


def should_consider(message: tl.Message, *, ignore_own: bool = True) -> bool:
    """Shared by the live handler and catch-up so both paths filter identically."""
    if not isinstance(message, tl.Message):  # MessageService / MessageEmpty
        return False
    if getattr(message, "action", None) is not None:
        return False
    if ignore_own and getattr(message, "out", False):
        return False
    return bool(message.message)


class Listener:
    def __init__(
        self,
        storage: Storage,
        matcher: Matcher,
        senders: SenderCache,
        *,
        deliver: DeliverHook,
        clock: Clock = utcnow,
        ignore_own: bool = True,
    ) -> None:
        self._storage = storage
        self._matcher = matcher
        self._senders = senders
        self._deliver = deliver
        self._clock = clock
        self._ignore_own = ignore_own
        self.cursors = CursorTracker(storage)
        self.metrics = ListenerMetrics()
        self.last_update_at: dt.datetime | None = None
        self._chat_cache: dict[int, ChatRef] = {}

    async def on_message(self, event: Any) -> None:
        """Entry point for ``events.NewMessage``. Never raises: one bad message must not
        stop the listener."""
        try:
            await self.handle(event.message, chat=getattr(event, "chat", None))
        except Exception:
            log.exception("listener failed on a message")

    async def handle(
        self,
        message: tl.Message,
        *,
        chat: Any = None,
        catchup: bool = False,
        catchup_capped: bool = False,
    ) -> list[OutboxItem]:
        self.metrics.seen += 1
        self.last_update_at = self._clock()
        if not should_consider(message, ignore_own=self._ignore_own):
            if getattr(message, "out", False):
                self.metrics.skipped_own += 1
            return []

        chat_id = _chat_id_of(message)
        if chat_id is None or chat_id >= 0:  # private chats are never monitored
            self.metrics.skipped_private += 1
            return []
        if not self._matcher.is_monitored(chat_id):
            # The handler is registered without `chats=`, so every message in every dialog
            # the account is in arrives here and most of them are in chats nobody watches.
            # Counting them is what makes `seen` legible: without this the difference between
            # `seen` and `considered` is a few hundred messages going somewhere unexplained.
            self.metrics.skipped_unmonitored += 1
            return []

        # From here on this message can await — on the chat cache, on the matcher's thread,
        # on storage — and another message in the same chat can overtake it and move the
        # cursor. Hold its id until it has reached `note` under its own power.
        self.cursors.hold(chat_id, message.id)

        await self._refresh_chat(chat_id, chat if chat is not None else message.chat)

        moment = message.date or self._clock()
        self.metrics.considered += 1
        prepared = prepare_text(message.message)
        matches = await self._matcher.evaluate(chat_id, prepared.shadow, _sender_id_of(message))
        if not matches:
            self.cursors.note(chat_id, message.id, moment)
            self.cursors.release(chat_id, message.id)
            return []

        sender = await sender_of(message, self._senders)
        if sender is not None and sender.is_bot:
            # Bots are skipped unless a moderator opted in, so the decision is per match.
            matches = [m for m in matches if m.accepts_bots]
            if not matches:
                self.metrics.skipped_bot += 1
                self.cursors.note(chat_id, message.id, moment)
                self.cursors.release(chat_id, message.id)
                return []
        if sender is None:
            # No entity came with the update and we refuse to make an RPC here, so we cannot
            # tell a human from a bot. Notify anyway — a missed spammer is worse than a
            # notification that says "unknown sender" — but count it.
            self.metrics.unknown_sender += 1

        self.metrics.matched += 1
        since = await self._storage.subscribed_since(chat_id)
        claimed: list[OutboxItem] = []
        for match in matches:
            started = since.get(match.user_id)
            if started is not None and moment < started:
                # Backfill must not replay what happened before this user subscribed.
                continue
            item = await self._claim(
                message,
                chat_id,
                match,
                prepared,
                sender,
                catchup=catchup,
                catchup_capped=catchup_capped,
            )
            if item is not None:
                claimed.append(item)
        # The cursor may only advance once every match for this message is durable — and only
        # then is the hold released, so a message whose claim raised keeps the cursor behind
        # it rather than being lost under it.
        self.cursors.note(chat_id, message.id, moment)
        self.cursors.release(chat_id, message.id)
        for item in claimed:
            await self._deliver(item)
        return claimed

    async def _claim(
        self,
        message: tl.Message,
        chat_id: int,
        match: Match,
        prepared: PreparedText,
        sender: SenderInfo | None,
        *,
        catchup: bool,
        catchup_capped: bool,
    ) -> OutboxItem | None:
        chat = self._chat_cache.get(chat_id) or ChatRef(chat_id, str(chat_id), None, "supergroup")
        spans = [prepared.to_original(s) for s in match.spans]
        payload = Payload(
            chat_id=chat_id,
            chat_title=chat.title,
            chat_username=chat.username,
            chat_kind=chat.kind,
            msg_id=message.id,
            topic_id=topic_id_of(message),
            date=message.date or self._clock(),
            text=message.message or "",
            spans=[(s.start, s.end) for s in spans],
            rule_pattern=match.rule.pattern,
            rule_kind=match.rule.kind.value,
            rule_is_global=match.rule.is_global,
            sender_id=sender.id if sender else None,
            sender_name=sender.display_name if sender else "",
            sender_username=sender.username if sender else None,
            sender_is_channel=bool(sender and sender.is_channel),
            sender_is_deleted=bool(sender and sender.is_deleted),
            is_forwarded=message.fwd_from is not None,
            catchup=catchup,
            catchup_capped=catchup_capped,
        )
        try:
            item = await self._storage.claim_delivery(
                match.user_id, chat_id, message.id, match.rule.id, payload.to_json()
            )
        except sqlite3.OperationalError:
            # The database was busy. Do not advance the cursor for this message: catch-up will
            # find it again, which is the whole point of writing the row before the cursor.
            log.warning(
                "could not claim %s/%s for %s; leaving it to catch-up",
                chat_id,
                message.id,
                match.user_id,
            )
            raise
        if item is None:
            self.metrics.duplicates += 1
            return None
        self.metrics.claimed += 1
        self._matcher.note_hit(match.rule.id)
        return item

    async def _refresh_chat(self, chat_id: int, entity: Any) -> None:
        ref = chat_ref_of(entity)
        if ref is None:
            return
        cached = self._chat_cache.get(chat_id)
        if cached == ref:
            return
        self._chat_cache[chat_id] = ref
        access_hash = getattr(entity, "access_hash", None)
        await safe_write(self._storage.upsert_chat(ref, access_hash=access_hash), "chat upsert")

    async def prime_chat_cache(self) -> None:
        """Load known chat metadata so notifications have titles before the first update."""
        for row in await self._storage.list_chats():
            self._chat_cache[row.id] = row.ref


def _chat_id_of(message: tl.Message) -> int | None:
    peer = message.peer_id
    if peer is None:
        return None
    try:
        return int(utils.get_peer_id(peer))
    except (TypeError, ValueError):  # pragma: no cover - malformed update
        return None


def _sender_id_of(message: tl.Message) -> int | None:
    sender_id = getattr(message, "sender_id", None)
    return int(sender_id) if sender_id is not None else None

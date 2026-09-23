"""Bot-side dispatch, authorisation and pending-input state.

Handlers never touch Telethon: the router turns an update into an :class:`Incoming` and each
handler returns :class:`Action` objects that an adapter performs. That keeps the whole UI
testable offline and keeps the authorisation invariants in exactly one place:

* the acting user id comes from ``event.sender_id`` (server-set), never from the payload;
* owner-only actions additionally check ``storage.is_owner``;
* unauthorised users get at most one reply and the owner at most one card per day.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ddiver.i18n import DEFAULT_LANG, LANGS, t
from ddiver.storage import Role, Storage, User
from ddiver.timeutil import Clock, utcnow

log = logging.getLogger(__name__)

PENDING_TTL = dt.timedelta(minutes=15)
COMMANDS = ("start", "help", "test", "cancel", "status", "menu")
GATE_REMINDER = dt.timedelta(days=1)


@dataclass(frozen=True, slots=True)
class Incoming:
    """One update, reduced to what the UI needs."""

    user_id: int
    text: str | None = None
    data: str | None = None  # callback payload
    msg_id: int | None = None
    lang_code: str | None = None  # Telegram's client language, used to seed the UI language
    is_private: bool = True

    @property
    def command(self) -> str | None:
        """A known command, or ``None``. See :attr:`looks_like_command` for the rest."""
        name = self._command_name()
        return name if name in COMMANDS else None

    @property
    def looks_like_command(self) -> bool:
        """Starts with a slash but is not one of ours — answer, do not treat it as input."""
        name = self._command_name()
        return name is not None and name not in COMMANDS

    def _command_name(self) -> str | None:
        if not self.text:
            return None
        head = self.text.split(maxsplit=1)[0]
        if not head.startswith("/") or len(head) < 2:
            return None
        return head[1:].split("@", 1)[0].lower()

    @property
    def args(self) -> str:
        if not self.text:
            return ""
        parts = self.text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""


# -- actions a handler can request ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reply:
    key_or_text: str
    params: dict[str, Any] = field(default_factory=dict)
    buttons: Any = None
    literal: bool = False
    html: bool = False
    """Render simple HTML (<b>, <code>). Only ever set on static catalog strings: anything
    interpolated would have to be escaped first."""


@dataclass(frozen=True, slots=True)
class Edit:
    key_or_text: str
    params: dict[str, Any] = field(default_factory=dict)
    buttons: Any = None
    literal: bool = False
    html: bool = False
    """Render simple HTML (<b>, <code>). Only ever set on static catalog strings: anything
    interpolated would have to be escaped first."""


@dataclass(frozen=True, slots=True)
class Toast:
    key_or_text: str
    params: dict[str, Any] = field(default_factory=dict)
    alert: bool = False
    literal: bool = False


@dataclass(frozen=True, slots=True)
class Send:
    """A message to somebody other than the acting user (e.g. the owner's approval card)."""

    user_id: int
    key_or_text: str
    params: dict[str, Any] = field(default_factory=dict)
    buttons: Any = None
    literal: bool = False


Action = Reply | Edit | Toast | Send
Handler = Callable[["Context"], Awaitable[list[Action]]]


@dataclass(slots=True)
class PendingInput:
    kind: str
    payload: dict[str, Any]
    expires_at: dt.datetime


@dataclass(slots=True)
class Context:
    incoming: Incoming
    user: User
    storage: Storage
    router: Router

    @property
    def user_id(self) -> int:
        return self.user.id

    @property
    def lang(self) -> str:
        return self.user.lang or DEFAULT_LANG

    @property
    def is_owner(self) -> bool:
        return self.user.is_owner

    def t(self, key: str, **params: Any) -> str:
        return t(self.lang, key, **params)


class Router:
    def __init__(
        self,
        storage: Storage,
        *,
        clock: Clock = utcnow,
        on_user_state_changed: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self._storage = storage
        self._clock = clock
        self._on_user_state_changed = on_user_state_changed
        self._commands: dict[str, Handler] = {}
        self._callbacks: dict[str, tuple[Handler, bool]] = {}
        self._pending_handlers: dict[str, Handler] = {}
        self._pending: dict[int, PendingInput] = {}
        self._fallback: Handler | None = None

    # -- registration ----------------------------------------------------------------------

    def command(self, name: str) -> Callable[[Handler], Handler]:
        def register(handler: Handler) -> Handler:
            self._commands[name] = handler
            return handler

        return register

    def callback(self, prefix: str, *, owner_only: bool = False) -> Callable[[Handler], Handler]:
        def register(handler: Handler) -> Handler:
            self._callbacks[prefix] = (handler, owner_only)
            return handler

        return register

    def pending(self, kind: str) -> Callable[[Handler], Handler]:
        def register(handler: Handler) -> Handler:
            self._pending_handlers[kind] = handler
            return handler

        return register

    def fallback(self, handler: Handler) -> Handler:
        self._fallback = handler
        return handler

    # -- pending input ---------------------------------------------------------------------

    def set_pending(self, user_id: int, kind: str, payload: dict[str, Any] | None = None) -> None:
        self._pending[user_id] = PendingInput(
            kind, dict(payload or {}), self._clock() + PENDING_TTL
        )

    def get_pending(self, user_id: int) -> PendingInput | None:
        state = self._pending.get(user_id)
        if state is None:
            return None
        if self._clock() >= state.expires_at:
            del self._pending[user_id]
            return None
        return state

    def clear_pending(self, user_id: int) -> None:
        self._pending.pop(user_id, None)

    # -- dispatch --------------------------------------------------------------------------

    async def dispatch(self, incoming: Incoming) -> list[Action]:
        if not incoming.is_private:
            return []  # the bot only works in DMs; group commands are ignored
        user = await self._storage.ensure_user(
            incoming.user_id, lang=_seed_lang(incoming.lang_code)
        )
        if not user.is_owner and user.role is not Role.ALLOWED:
            return await self._gate(incoming, user)
        if user.paused and await self._storage.resume_delivery(user.id):
            log.info("delivery resumed for %s after an incoming update", user.id)
            # The matcher dropped this user's chats while they were paused; without refreshing
            # it they would silently receive nothing at all after unblocking the bot.
            if self._on_user_state_changed is not None:
                await self._on_user_state_changed()
            user = await self._storage.ensure_user(user.id)

        ctx = Context(incoming, user, self._storage, self)
        if incoming.data is not None:
            return await self._dispatch_callback(ctx)
        return await self._dispatch_message(ctx)

    async def _dispatch_callback(self, ctx: Context) -> list[Action]:
        data = ctx.incoming.data or ""
        prefix = data.split(":", 1)[0]
        entry = self._callbacks.get(prefix)
        if entry is None:
            return [Toast("bot.err.unknown_action")]
        handler, owner_only = entry
        if owner_only and not ctx.is_owner:
            log.warning("user %s tried an owner-only action %r", ctx.user_id, prefix)
            return [Toast("bot.err.owner_only", alert=True)]
        return await handler(ctx)

    async def _dispatch_message(self, ctx: Context) -> list[Action]:
        command = ctx.incoming.command
        if command is not None:
            self.clear_pending(ctx.user_id)
            handler = self._commands.get(command)
            if handler is not None:
                return await handler(ctx)
            return [Reply("bot.err.unknown_command")]

        # A rule line may legitimately start with a slash, so only complain about an unknown
        # command when we are not waiting for input.
        state = self.get_pending(ctx.user_id)
        if state is None and ctx.incoming.looks_like_command:
            return [Reply("bot.err.unknown_command")]
        if state is not None:
            handler = self._pending_handlers.get(state.kind)
            if handler is not None:
                return await handler(ctx)
            self.clear_pending(ctx.user_id)
        if self._fallback is not None:
            return await self._fallback(ctx)
        return [Reply("bot.err.not_waiting")]

    def pending_payload(self, user_id: int) -> dict[str, Any]:
        state = self.get_pending(user_id)
        return dict(state.payload) if state else {}

    # -- the allow-list gate ---------------------------------------------------------------

    async def _gate(self, incoming: Incoming, user: User) -> list[Action]:
        """Strangers get one reply and the owner one card per day; denied users get silence."""
        if user.role is Role.DENIED:
            return []
        lang = user.lang or DEFAULT_LANG
        now = self._clock()
        if user.notified_owner_at is not None and now - user.notified_owner_at < GATE_REMINDER:
            return [] if incoming.data is None else [Toast("bot.gate.pending")]
        await self._storage.update_user(user.id, notified_owner_at=now)
        actions: list[Action] = [Reply("bot.gate.pending")]
        owner = await self._owner_id()
        if owner is not None:
            actions.append(
                Send(
                    owner,
                    "bot.gate.card",
                    {"user_id": user.id, "lang": lang},
                    buttons=[
                        [
                            ("bot.btn.allow", f"allow:{user.id}"),
                            ("bot.btn.deny", f"deny:{user.id}"),
                        ]
                    ],
                )
            )
        return actions

    async def _owner_id(self) -> int | None:
        for candidate in await self._storage.list_users():
            if candidate.is_owner:
                return candidate.id
        return None


def _seed_lang(lang_code: str | None) -> str | None:
    """Map Telegram's client language to one of ours so even the gate message is localized."""
    if not lang_code:
        return None
    head = lang_code.split("-", 1)[0].lower()
    return head if head in LANGS else None

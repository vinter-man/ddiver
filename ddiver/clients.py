"""Telethon client construction, connection handling and fatal-error classification."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any

from telethon import TelegramClient, errors
from telethon.errors import common as common_errors

from ddiver.config import Settings

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 30.0
USER_FLOOD_THRESHOLD = 60  # catch-up wants Telethon's transparent sleep-and-retry
BOT_FLOOD_THRESHOLD = 0  # delivery owns the retry policy, so it must see the error
ENTITY_CACHE_LIMIT = 20_000


class ListenerClient(TelegramClient):  # type: ignore[misc]
    """Telethon client that tells us when it reconnected.

    Telethon requests ``getChannelDifference`` with a limit of 100 for user accounts and, on
    ``ChannelDifferenceTooLong``, jumps ``pts`` and returns no updates at all. In a busy chat
    that silently loses everything beyond 100 messages per gap, so after every reconnect we
    re-run the cursor-based catch-up.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.reconnected = asyncio.Event()
        self.reconnects = 0

    async def _handle_auto_reconnect(self) -> None:
        try:
            await super()._handle_auto_reconnect()
        except Exception:  # the callback runs detached; an exception here would vanish
            log.warning("auto-reconnect handler failed", exc_info=True)
        self.reconnects += 1
        self.reconnected.set()
        log.info("listener reconnected (%d since start); scheduling a catch-up", self.reconnects)


def is_fatal_auth_error(exc: BaseException) -> bool:
    """Errors that mean the session is dead and only ``ddiver login`` can fix it.

    Classified by base class rather than by a list of names: ``UnauthorizedError`` (401) covers
    session revoked/expired and auth key unregistered, ``AuthKeyError`` (406) covers the
    duplicated key you get when two processes share one session file, and ``AuthKeyNotFound``
    is a plain ``Exception`` raised by the transport, not an ``RPCError``.
    """
    if isinstance(exc, errors.SessionPasswordNeededError):
        return False  # 401, but it only means "2FA password required" during login
    return isinstance(
        exc,
        (
            errors.UnauthorizedError,
            errors.AuthKeyError,
            common_errors.AuthKeyNotFound,
            errors.PhoneNumberBannedError,
        ),
    )


def is_session_file_error(exc: BaseException) -> bool:
    """A locked or corrupt Telethon session file — usually a second instance."""
    return isinstance(exc, sqlite3.OperationalError | sqlite3.DatabaseError)


async def connect_with_timeout(client: TelegramClient, timeout: float = CONNECT_TIMEOUT) -> None:
    """``connect()`` can hang forever (telethon#1536); on timeout tear the half-open sender down."""
    try:
        async with asyncio.timeout(timeout):
            await client.connect()
    except TimeoutError:
        log.warning("connect() timed out after %.0fs; disconnecting before retrying", timeout)
        try:
            await client.disconnect()
        except Exception:
            log.debug("disconnect after a failed connect also failed", exc_info=True)
        raise


def make_user_client(settings: Settings, *, session: str | Path | None = None) -> ListenerClient:
    return ListenerClient(
        str(session or settings.user_session),
        settings.api_id,
        settings.api_hash.get_secret_value(),
        flood_sleep_threshold=USER_FLOOD_THRESHOLD,
        connection_retries=None,
        retry_delay=5,
        auto_reconnect=True,
        catch_up=False,
        sequential_updates=False,
        receive_updates=True,
        entity_cache_limit=ENTITY_CACHE_LIMIT,
        request_retries=5,
    )


def make_bot_client(settings: Settings, *, session: str | Path | None = None) -> TelegramClient:
    return TelegramClient(
        str(session or settings.bot_session),
        settings.api_id,
        settings.api_hash.get_secret_value(),
        flood_sleep_threshold=BOT_FLOOD_THRESHOLD,
        raise_last_call_error=True,
        request_retries=3,
        connection_retries=None,
        retry_delay=5,
        auto_reconnect=True,
        receive_updates=True,
    )

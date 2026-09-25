"""The listener's Telethon user client: construction and reconnect tracking.

The bot client, ``connect_with_timeout`` and the fatal-error classifiers live in ddiver-core
(``ddiver_core.botclient``, ``ddiver_core.tgerrors``).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from telethon import TelegramClient

from ddiver.config import Settings

# Kept as a name in this module rather than imported from ddiver-core where it is used: the CLI
# and the runtime read make_bot_client from here, and test_cli patches it here.
from ddiver_core.botclient import make_bot_client as make_bot_client  # noqa: PLC0414

log = logging.getLogger(__name__)

USER_FLOOD_THRESHOLD = 60  # catch-up wants Telethon's transparent sleep-and-retry
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

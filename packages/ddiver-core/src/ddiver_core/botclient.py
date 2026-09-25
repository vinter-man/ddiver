"""The bot's Telethon client, and a connect that cannot hang.

Every app built on ddiver-core talks to its users through a BotFather bot driven over MTProto,
so the bot client is shared; a listener's user client is not, and stays with the app that has
one. ``flood_sleep_threshold=0`` matters: delivery owns the retry policy and has to see every
flood wait instead of having Telethon sleep through it.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telethon import TelegramClient

from ddiver_core.config import Settings

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 30.0
BOT_FLOOD_THRESHOLD = 0  # delivery owns the retry policy, so it must see the error


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

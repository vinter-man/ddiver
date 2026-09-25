"""Offline fakes for the tests of apps built on ddiver-core.

A clock that moves only when a test says so, and a bot client that records what it was asked
to send and can be told to fail or to stall. This package is a dev dependency and is never
installed in an image. The Telethon *event* fakes stay with the app that listens to Telegram:
they describe its source, not the shared core.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from typing import Any

T0 = dt.datetime(2026, 9, 22, 12, 0, 0, tzinfo=dt.UTC)


class FakeClock:
    """Callable UTC clock that only moves when the test says so."""

    def __init__(self, start: dt.datetime = T0) -> None:
        self.now = start

    def __call__(self) -> dt.datetime:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += dt.timedelta(seconds=seconds)

    def set(self, moment: dt.datetime) -> None:
        """Jump to a wall-clock time, for the tests that care which hour it is somewhere."""
        self.now = moment


@dataclass
class SentMessage:
    user_id: int
    text: str
    entities: list[Any] = field(default_factory=list)
    buttons: Any = None
    link_preview: bool = True


class FakeBotClient:
    """Captures ``send_message`` calls.

    ``fail_next`` queues exceptions to raise; ``hold``/``release`` block sending so a test can
    build up a backlog deterministically.
    """

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self.errors: list[BaseException | None] = []
        self._gate = asyncio.Event()
        self._gate.set()

    def fail_next(self, *errors: BaseException | None) -> None:
        self.errors.extend(errors)

    def hold(self) -> None:
        self._gate.clear()

    def release(self) -> None:
        self._gate.set()

    async def send_message(
        self,
        user_id: int,
        text: str,
        *,
        formatting_entities: list[Any] | None = None,
        buttons: Any = None,
        link_preview: bool = True,
    ) -> SentMessage:
        await self._gate.wait()
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        msg = SentMessage(user_id, text, list(formatting_entities or []), buttons, link_preview)
        self.sent.append(msg)
        return msg

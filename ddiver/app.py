"""Wiring and the supervisor loop.

The supervisor is a plain object with injected clients, clock, sleep and rng, and it *returns*
an exit code instead of calling ``sys.exit`` — so the whole restart/degraded-mode policy is
unit-testable without a Telegram connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ddiver.clients import is_fatal_auth_error, is_session_file_error
from ddiver.timeutil import Clock, utcnow

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_AUTHORIZED = 3
EXIT_LOCKED = 4
EXIT_DB_CORRUPT = 5

BACKOFF_BASE = 2.0
BACKOFF_CAP = 300.0
OWNER_REMINDER = dt.timedelta(hours=6)
BOOT_STORM_BOOTS = 3
BOOT_STORM_DELAY = 300.0

Sleeper = Callable[[float], Awaitable[None]]


@dataclass(slots=True)
class SupervisorState:
    """What ``/status`` and the tests look at."""

    started_at: dt.datetime | None = None
    degraded: bool = False
    degraded_reason: str | None = None
    attempts: int = 0
    reconnects: int = 0
    owner_notified_at: dt.datetime | None = None
    backoffs: list[float] = field(default_factory=list)


class Supervisor:
    """Keeps the listener alive, or parks it in degraded mode when only a re-login can help.

    Degraded mode matters because the container policy is ``restart: unless-stopped``, which
    ignores exit codes: exiting on a dead session would produce an infinite restart loop that
    re-notifies the owner and re-runs catch-up every few seconds.
    """

    def __init__(
        self,
        *,
        run_listener: Callable[[], Awaitable[None]],
        notify_owner: Callable[[str], Awaitable[None]] | None = None,
        clock: Clock = utcnow,
        sleep: Sleeper = asyncio.sleep,
        rng: random.Random | None = None,
        max_attempts: int | None = None,
    ) -> None:
        self._run_listener = run_listener
        self._notify_owner = notify_owner
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random(0)  # jitter for reconnects, not cryptography
        self._max_attempts = max_attempts
        self.state = SupervisorState()
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    async def run(self) -> int:
        self.state.started_at = self._clock()
        while not self._stop.is_set():
            self.state.attempts += 1
            try:
                await self._run_listener()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if is_fatal_auth_error(exc):
                    await self._enter_degraded(type(exc).__name__)
                    return EXIT_OK
                if is_session_file_error(exc):
                    await self._enter_degraded(f"session file: {exc}")
                    return EXIT_OK
                log.warning("listener stopped with %s", type(exc).__name__, exc_info=True)
            else:
                if self._stop.is_set():
                    break
                log.info("listener disconnected; reconnecting")
            if self._max_attempts is not None and self.state.attempts >= self._max_attempts:
                return EXIT_ERROR
            if self._stop.is_set():
                break
            await self._backoff()
        return EXIT_OK

    async def _backoff(self) -> None:
        delay = min(BACKOFF_CAP, BACKOFF_BASE * 2 ** (self.state.attempts - 1))
        delay += self._rng.uniform(0, delay / 4)
        self.state.backoffs.append(delay)
        log.info("reconnecting in %.0fs", delay)
        await self._sleep(delay)

    async def _enter_degraded(self, reason: str) -> None:
        self.state.degraded = True
        self.state.degraded_reason = reason
        log.error("listener needs a new login (%s); entering degraded mode", reason)
        await self.notify_owner_if_due(force=True)

    async def notify_owner_if_due(self, *, force: bool = False) -> bool:
        """Tell the owner about degraded mode, at most once every six hours."""
        if not self.state.degraded or self._notify_owner is None:
            return False
        now = self._clock()
        last = self.state.owner_notified_at
        if not force and last is not None and now - last < OWNER_REMINDER:
            return False
        self.state.owner_notified_at = now
        with contextlib.suppress(Exception):
            await self._notify_owner(self.state.degraded_reason or "unknown")
        return True


async def boot_delay(boots_recently: int, sleep: Sleeper = asyncio.sleep) -> float:
    """Delay catch-up when the process is crash-looping, so it stops hammering GetHistory."""
    if boots_recently < BOOT_STORM_BOOTS:
        return 0.0
    log.warning("%d boots in the last 10 minutes; delaying catch-up", boots_recently)
    await sleep(BOOT_STORM_DELAY)
    return BOOT_STORM_DELAY


def format_uptime(started_at: dt.datetime | None, now: dt.datetime) -> str:
    if started_at is None:
        return "—"
    seconds = max(0, int((now - started_at).total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


class PeriodicTask:
    """A cancellable ``while True: await work(); await sleep(interval)`` loop that never dies."""

    def __init__(
        self,
        name: str,
        work: Callable[[], Awaitable[Any]],
        interval: float,
        *,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        self._name = name
        self._work = work
        self._interval = interval
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self.runs = 0
        self.failures = 0

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name=self._name)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self._work()
                self.runs += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self.failures += 1
                log.exception("periodic task %s failed", self._name)
            await self._sleep(self._interval)

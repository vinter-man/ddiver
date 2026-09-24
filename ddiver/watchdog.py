"""Dead-man's switch: a ping that stops when we do.

Grafana, the heartbeat file and the container health check all live on the same machine as
ddiver, so none of them can tell you the machine is gone — a dead box and a quiet chat look
identical from your phone. This pings a URL you control from somewhere else; when the pings
stop arriving, that service alerts you.

Deliberately not tied to one provider: healthchecks.io, Uptime Kuma's push monitors, Cronitor
and BetterStack all expose the same shape — a URL you GET on a schedule. Self-host Uptime Kuma
somewhere other than this box, or use a free hosted tier; either way the contract is one URL.

The request is written straight onto an asyncio connection rather than handed to ``urllib`` in
a worker thread. Only the status line matters, so the HTTP involved is four lines out and one
line back — and a thread cannot be cancelled. A peer that trickles bytes slower than the socket
timeout, or answers with an endless run of ``100 Continue``, would leave that thread running
for the life of the process while the heartbeat waited on it. Here the deadline ends the work.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import ssl
from urllib.parse import urlsplit

from ddiver.timeutil import Clock, utcnow

log = logging.getLogger(__name__)

USER_AGENT = "ddiver-watchdog"
STATUS_LINE_LIMIT = 512
"""A status line is about forty bytes; anything longer is not answering our question."""


class Watchdog:
    """Pings ``url`` when asked, and never raises.

    A ping is a statement about liveness, not about health: it is sent while the process is
    running its heartbeat loop, degraded or not. Degraded mode means a human has to run
    ``ddiver login`` — the owner is told about that over Telegram, and it is not what this
    switch is for.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 5.0,
        min_interval: float = 60.0,
        clock: Clock = utcnow,
    ) -> None:
        self._url = url
        self._timeout = timeout
        self._min_interval = min_interval
        self._clock = clock
        self._last_sent: dt.datetime | None = None
        self.sent = 0
        self.skipped = 0
        self.failed = 0
        self._warned = False

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    async def ping(self) -> bool:
        """Ping, unless one went out recently.

        The heartbeat runs every thirty seconds, which is far more often than any monitor
        needs and 86 000 requests a month at somebody else's expense. What decides how fast
        an outage is noticed is the *period* configured on the monitor, not how often we
        stack pings inside it — so one a minute is plenty, and it keeps the ping log on the
        other end readable instead of a wall of identical entries.
        """
        if not self._url:
            return False
        now = self._clock()
        recent = (
            self._last_sent is not None
            and (now - self._last_sent).total_seconds() < self._min_interval
        )
        if recent:
            self.skipped += 1
            return True  # one went out recently; as far as liveness goes, we are fine
        self._last_sent = now
        try:
            async with asyncio.timeout(self._timeout):
                ok = await self._get()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a watchdog that can crash the process is worse
            ok = False
        if ok:
            self.sent += 1
            self._warned = False
            return True
        self.failed += 1
        if not self._warned:
            # Once per outage, not once per minute: a flapping network would otherwise fill
            # the log with the failure of the thing that watches for failures.
            log.warning("watchdog ping failed; the dead-man's switch may alert")
            self._warned = True
        return False

    async def _get(self) -> bool:
        parts = urlsplit(self._url)
        secure = parts.scheme == "https"
        context = ssl.create_default_context() if secure else None
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"

        reader, writer = await asyncio.open_connection(
            parts.hostname,
            parts.port or (443 if secure else 80),
            ssl=context,
            server_hostname=parts.hostname if secure else None,
        )
        try:
            writer.write(
                (
                    f"GET {path} HTTP/1.1\r\n"
                    f"Host: {parts.netloc}\r\n"
                    f"User-Agent: {USER_AGENT}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("latin-1")
            )
            await writer.drain()
            # The body is never read: this is a ping, and whatever the service has to say
            # about it belongs on the service's own dashboard.
            return _is_success(await reader.readuntil(b"\n"))
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, TimeoutError, ssl.SSLError):
                await writer.wait_closed()


def _is_success(status_line: bytes) -> bool:
    """``HTTP/1.1 200 OK`` is a yes. Anything we cannot parse is a no."""
    fields = status_line[:STATUS_LINE_LIMIT].split(b" ")
    if len(fields) < 2 or not fields[0].upper().startswith(b"HTTP/"):
        return False
    try:
        code = int(fields[1])
    except ValueError:
        return False
    return 200 <= code < 300

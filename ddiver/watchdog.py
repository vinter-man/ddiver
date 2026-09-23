"""Dead-man's switch: a ping that stops when we do.

Grafana, the heartbeat file and the container health check all live on the same machine as
ddiver, so none of them can tell you the machine is gone — a dead box and a quiet chat look
identical from your phone. This pings a URL you control from somewhere else; when the pings
stop arriving, that service alerts you.

Deliberately not tied to one provider: healthchecks.io, Uptime Kuma's push monitors, Cronitor
and BetterStack all expose the same shape — a URL you GET on a schedule. Self-host Uptime Kuma
somewhere other than this box, or use a free hosted tier; either way the contract is one URL.

``urllib`` in a worker thread rather than an HTTP client dependency: this is one request a
minute to one URL, and a failed ping must never be able to disturb the listener.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

USER_AGENT = "ddiver-watchdog"


class Watchdog:
    """Pings ``url`` when asked, and never raises.

    A ping is a statement about liveness, not about health: it is sent while the process is
    running its heartbeat loop, degraded or not. Degraded mode means a human has to run
    ``ddiver login`` — the owner is told about that over Telegram, and it is not what this
    switch is for.
    """

    def __init__(self, url: str, *, timeout: float = 5.0) -> None:
        self._url = url
        self._timeout = timeout
        self.sent = 0
        self.failed = 0
        self._warned = False

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    async def ping(self) -> bool:
        if not self._url:
            return False
        try:
            ok = await asyncio.to_thread(self._get)
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

    def _get(self) -> bool:
        request = urllib.request.Request(  # noqa: S310 - the scheme is validated in Settings
            self._url, headers={"User-Agent": USER_AGENT}, method="GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                return bool(200 <= int(response.status) < 300)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return False

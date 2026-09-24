"""Prometheus exposition, hand-rolled.

The text format is four lines per metric and the whole surface here is a few dozen counters,
so pulling in ``prometheus_client`` would add a dependency and an abstraction for something
this file does in a hundred lines. It also keeps the counters where they already live — in the
listener, delivery and matcher objects — instead of forcing a second source of truth that can
drift from them.

Cardinality is deliberately flat: no per-chat or per-rule labels. A moderator with 200 rules
across 50 chats would otherwise produce 10 000 series for a box that has one process on it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

BIND_BACKLOG = 16
READ_LIMIT = 4096
"""Longest request line we will read. Anything longer is not a scraper."""

REQUEST_TIMEOUT = 10.0
"""How long one request may take. Bounds `stop()`, which waits for handlers to finish."""


@dataclass(frozen=True, slots=True)
class Metric:
    name: str
    kind: str  # "counter" or "gauge"
    help: str
    value: float
    labels: tuple[tuple[str, str], ...] = ()
    """Only ``build_info`` uses these. Everything else stays unlabelled on purpose."""

    def render(self) -> str:
        pairs = ",".join(f'{k}="{_escape(v)}"' for k, v in self.labels)
        series = f"{self.name}{{{pairs}}}" if pairs else self.name
        return (
            f"# HELP {self.name} {self.help}\n"
            f"# TYPE {self.name} {self.kind}\n"
            f"{series} {_number(self.value)}\n"
        )


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _number(value: float) -> str:
    """Integers without a decimal point; Prometheus accepts both, humans prefer the former."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


class MetricsSource:
    """Reads the live objects on every scrape, so nothing has to be pushed into it."""

    def __init__(
        self,
        *,
        listener: Any = None,
        delivery: Any = None,
        matcher: Any = None,
        supervisor: Any = None,
        transport: Callable[[], bool | None] = lambda: None,
        uptime: Callable[[], float] = lambda: 0.0,
    ) -> None:
        self._listener = listener
        self._delivery = delivery
        self._matcher = matcher
        self._supervisor = supervisor
        self._transport = transport
        self._uptime = uptime

    def collect(self) -> Iterator[Metric]:
        yield Metric("ddiver_up", "gauge", "1 while the process is serving metrics", 1)
        # The standard build_info shape: always 1, and the answer is in the label. One series,
        # and it changes only on a deploy — which is exactly what makes a deploy visible.
        yield Metric(
            "ddiver_build_info",
            "gauge",
            "Always 1; the commit the running image was built from is in the label",
            1,
            labels=(("commit", os.environ.get("DDIVER_COMMIT", "unknown")[:12]),),
        )
        yield Metric(
            "ddiver_uptime_seconds", "gauge", "Seconds since the supervisor started", self._uptime()
        )
        transport = self._transport()
        yield Metric(
            "ddiver_listener_connected",
            "gauge",
            "1 when the user session's transport is up, 0 when it is down or not yet known",
            1 if transport else 0,
        )
        if self._supervisor is not None:
            yield Metric(
                "ddiver_degraded",
                "gauge",
                "1 when the listener needs `ddiver login` and no restart will help",
                int(bool(self._supervisor.state.degraded)),
            )

        listener = self._listener
        if listener is not None:
            m = listener.metrics
            yield from _counters(
                "ddiver_messages",
                {
                    "seen_total": (m.seen, "Updates the listener looked at"),
                    "considered_total": (m.considered, "Updates that reached the matcher"),
                    "matched_total": (m.matched, "Messages that matched at least one rule"),
                    "claimed_total": (m.claimed, "Outbox rows written"),
                    "duplicates_total": (m.duplicates, "Rows already present (live vs catch-up)"),
                    "skipped_bot_total": (m.skipped_bot, "Matches dropped: sender is a bot"),
                    "skipped_private_total": (m.skipped_private, "Updates from a private chat"),
                    "skipped_unmonitored_total": (
                        m.skipped_unmonitored,
                        "Updates from a chat nobody is subscribed to",
                    ),
                    "skipped_own_total": (m.skipped_own, "The listener's own outgoing messages"),
                    "unknown_sender_total": (m.unknown_sender, "Matches with no sender entity"),
                },
            )

        delivery = self._delivery
        if delivery is not None:
            d = delivery.metrics
            yield from _counters(
                "ddiver_delivery",
                {
                    "delivered_total": (d.delivered, "Notifications sent"),
                    "digested_total": (d.digested, "Notifications folded into a digest"),
                    "failed_total": (d.failed, "Notifications given up on"),
                    "dropped_overflow_total": (
                        d.dropped_overflow,
                        "Notifications dropped because the queue was full",
                    ),
                    "dropped_inactive_total": (
                        d.dropped_inactive,
                        "Notifications dropped because the moderator is paused or revoked",
                    ),
                    "worker_restarts_total": (d.worker_restarts, "Delivery workers restarted"),
                    "degraded_sends_total": (d.degraded_sends, "Retries without formatting"),
                    "flood_waits_total": (d.flood_waits, "FloodWaitError responses"),
                    "flood_seconds_total": (d.flood_seconds, "Seconds spent waiting out floods"),
                },
            )
            yield Metric(
                "ddiver_delivery_queued",
                "gauge",
                "Notifications waiting in the per-user queues right now",
                delivery.total_queued(),
            )

        matcher = self._matcher
        if matcher is not None:
            yield from _counters(
                "ddiver_matcher",
                {
                    "evaluated_total": (matcher.stats_evaluated, "Messages evaluated"),
                    "matched_total": (matcher.stats_matched, "Evaluations with a hit"),
                    "timeouts_total": (matcher.stats_timeouts, "Rules that hit the regex timeout"),
                    "budget_exceeded_total": (
                        matcher.stats_budget_exceeded,
                        "Evaluations cut short by the per-subscriber budget",
                    ),
                },
            )
            yield Metric(
                "ddiver_monitored_chats",
                "gauge",
                "Chats at least one active moderator is subscribed to",
                len(matcher.monitored),
            )

    def render(self) -> str:
        return "".join(metric.render() for metric in self.collect())


def _counters(prefix: str, items: dict[str, tuple[float, str]]) -> Iterator[Metric]:
    for suffix, (value, help_text) in items.items():
        yield Metric(f"{prefix}_{suffix}", "counter", help_text, value)


class MetricsServer:
    """A one-endpoint HTTP server: ``GET /metrics``, anything else is 404.

    Bound to localhost by default. It answers with no authentication, so exposing it further
    means exposing chat and rule counts — flat ones, but still operational detail.
    """

    def __init__(self, source: MetricsSource, host: str, port: int) -> None:
        self._source = source
        self._host = host
        self._port = port
        # asyncio.Server rather than AbstractServer: only the concrete class exposes `sockets`,
        # which is how a test can ask for port 0 and then find out what it actually got.
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        """The bound port, which differs from the requested one when that was 0."""
        if self._server is None or not self._server.sockets:
            return self._port
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self._host, self._port, backlog=BIND_BACKLOG, limit=READ_LIMIT
        )
        log.info("metrics on http://%s:%d/metrics", self._host, self.port)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            # Bounded on both axes. Without the timeout a socket that connects and says
            # nothing keeps this coroutine alive, and `stop()` waits on it forever — which
            # made a clean shutdown impossible and every deploy a SIGKILL. Without the limit
            # a request line with no newline reads until memory runs out.
            async with asyncio.timeout(REQUEST_TIMEOUT):
                request = await reader.readuntil(b"\n")
            path = request.decode("latin-1", "replace").split(" ")[1:2]
            if path and path[0].split("?")[0] == "/metrics":
                body = self._source.render().encode()
                head = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/plain; version=0.0.4; charset=utf-8\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
            else:
                body = b"try /metrics\n"
                head = (
                    "HTTP/1.1 404 Not Found\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
            writer.write(head + body)
            await writer.drain()
        except (
            ConnectionError,
            TimeoutError,
            IndexError,
            UnicodeError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            pass  # a scraper that hung up, stalled or sent rubbish is not worth a log line
        except Exception:
            log.warning("metrics request failed", exc_info=True)
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, TimeoutError):
                await writer.wait_closed()

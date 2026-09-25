"""The /metrics endpoint and what it reports."""

from __future__ import annotations

import asyncio
import urllib.request

from ddiver.delivery import Metrics as DeliveryMetrics
from ddiver.listener import ListenerMetrics
from ddiver_core.metrics import MetricsServer, MetricsSource, _number


class FakeListener:
    """The real metrics dataclass, not a look-alike.

    A hand-written stand-in let `/metrics` name a delivery counter that does not exist: the
    suite was green and every scrape 500'd. Using the real class makes that a collection error.
    """

    def __init__(self) -> None:
        self.metrics = ListenerMetrics(
            seen=7,
            considered=6,
            matched=2,
            claimed=2,
            duplicates=1,
            skipped_bot=3,
            skipped_own=1,
        )


class FakeDelivery:
    def __init__(self) -> None:
        self.metrics = DeliveryMetrics(
            delivered=2, flood_waits=1, flood_seconds=30.5, dropped_overflow=4
        )

    def total_queued(self) -> int:
        return 4


class FakeMatcher:
    stats_evaluated = 6
    stats_matched = 2
    stats_timeouts = 0
    stats_budget_exceeded = 0
    stats_rules_skipped = 0
    monitored = frozenset({-1001, -1002})


def source(**kwargs) -> MetricsSource:
    defaults = {
        "listener": FakeListener(),
        "delivery": FakeDelivery(),
        "matcher": FakeMatcher(),
        "transport": lambda: True,
        "uptime": lambda: 90.0,
    }
    return MetricsSource(**{**defaults, **kwargs})


class TestRendering:
    def test_every_metric_carries_help_and_type(self):
        text = source().render()
        names = [
            line.split()[0].split("{")[0]
            for line in text.splitlines()
            if line and not line.startswith("#")
        ]
        assert names
        for name in names:
            assert f"# HELP {name} " in text, name
            assert f"# TYPE {name} " in text, name

    def test_the_counters_come_from_the_live_objects(self):
        text = source().render()
        assert "ddiver_messages_seen_total 7" in text
        assert "ddiver_messages_skipped_bot_total 3" in text
        assert "ddiver_delivery_delivered_total 2" in text
        assert "ddiver_delivery_queued 4" in text
        assert "ddiver_delivery_dropped_overflow_total 4" in text
        assert "ddiver_monitored_chats 2" in text

    def test_a_float_counter_keeps_its_fraction(self):
        assert "ddiver_delivery_flood_seconds_total 30.5" in source().render()
        assert _number(30.0) == "30"  # but a whole number reads as one

    def test_a_disconnected_transport_is_zero_not_missing(self):
        """A missing series and a down listener look the same on a graph."""
        text = source(transport=lambda: False).render()
        assert "ddiver_listener_connected 0" in text

    def test_components_that_do_not_exist_yet_are_simply_absent(self):
        """Metrics are collected during startup too, before the listener is built."""
        text = MetricsSource().render()
        assert "ddiver_up 1" in text
        assert "ddiver_messages_seen_total" not in text

    def test_build_info_is_the_only_labelled_metric(self):
        """Cardinality is the one thing that turns a tiny Prometheus into a big one.

        build_info is the exception every exporter makes: constant 1, one series, and the
        label changes only when a deploy changes it. Nothing else may grow a label — a
        per-chat or per-rule one would mint thousands of series on a box with one process.
        """
        labelled = [
            line.split("{")[0]
            for line in source().render().splitlines()
            if line and not line.startswith("#") and "{" in line
        ]
        assert labelled == ["ddiver_build_info"]

    def test_one_series_per_metric(self):
        lines = [x for x in source().render().splitlines() if x and not x.startswith("#")]
        names = [x.split()[0].split("{")[0] for x in lines]
        assert len(names) == len(set(names))


class TestServer:
    async def test_it_serves_metrics_and_404s_everything_else(self):
        server = MetricsServer(source(), "127.0.0.1", 0)
        await server.start()
        try:
            url = f"http://127.0.0.1:{server.port}"
            body, status, ctype = await asyncio.to_thread(_get, f"{url}/metrics")
            assert status == 200
            assert "text/plain" in ctype
            assert "ddiver_up 1" in body

            body, status, _ = await asyncio.to_thread(_get, f"{url}/")
            assert status == 404
        finally:
            await server.stop()

    async def test_a_query_string_still_reaches_metrics(self):
        """Some scrapers append one; answering 404 to those is a confusing afternoon."""
        server = MetricsServer(source(), "127.0.0.1", 0)
        await server.start()
        try:
            body, status, _ = await asyncio.to_thread(
                _get, f"http://127.0.0.1:{server.port}/metrics?x=1"
            )
            assert status == 200 and "ddiver_up 1" in body
        finally:
            await server.stop()

    async def test_stopping_twice_is_harmless(self):
        server = MetricsServer(source(), "127.0.0.1", 0)
        await server.start()
        await server.stop()
        await server.stop()

    async def test_a_client_that_hangs_up_does_not_raise(self):
        """A scrape that times out must not surface as an error in the log."""
        server = MetricsServer(source(), "127.0.0.1", 0)
        await server.start()
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", server.port)
            writer.close()
            await asyncio.sleep(0)
            # the server is still serving
            _, status, _ = await asyncio.to_thread(_get, f"http://127.0.0.1:{server.port}/metrics")
            assert status == 200
        finally:
            await server.stop()


def _get(url: str) -> tuple[str, int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - localhost
            return response.read().decode(), response.status, response.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        return exc.read().decode(), exc.code, ""


class TestBuildInfo:
    def test_the_commit_is_a_label_on_a_constant_gauge(self, monkeypatch):
        """The standard build_info shape: one series whose label answers "which version?"."""
        monkeypatch.setenv("DDIVER_COMMIT", "0dbdf026abcdef1234567890")
        text = source().render()
        assert 'ddiver_build_info{commit="0dbdf026abcd"} 1' in text  # truncated, not the full sha

    def test_an_unbuilt_image_says_so_rather_than_lying(self, monkeypatch):
        monkeypatch.delenv("DDIVER_COMMIT", raising=False)
        assert 'ddiver_build_info{commit="unknown"} 1' in source().render()

    def test_a_label_value_cannot_break_the_exposition(self, monkeypatch):
        """Prometheus parses this by line; an unescaped quote would corrupt the scrape."""
        monkeypatch.setenv("DDIVER_COMMIT", 'a"b\\c')
        rendered = source().render().splitlines()
        line = next(x for x in rendered if x.startswith("ddiver_build_info{"))
        assert line == 'ddiver_build_info{commit="a\\"b\\\\c"} 1'


class TestShutdownIsBounded:
    """stop() waits for handlers, so a handler that never finishes makes shutdown impossible."""

    async def test_an_idle_connection_does_not_park_stop_forever(self, monkeypatch):
        monkeypatch.setattr("ddiver_core.metrics.REQUEST_TIMEOUT", 0.3)
        server = MetricsServer(source(), "127.0.0.1", 0)
        await server.start()
        _, writer = await asyncio.open_connection("127.0.0.1", server.port)
        try:
            # connected, and deliberately says nothing at all
            await asyncio.wait_for(server.stop(), timeout=5)
        finally:
            writer.close()

    async def test_a_request_line_with_no_newline_is_dropped(self, monkeypatch):
        """Without a limit this reads until memory runs out."""
        monkeypatch.setattr("ddiver_core.metrics.REQUEST_TIMEOUT", 1.0)
        server = MetricsServer(source(), "127.0.0.1", 0)
        await server.start()
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", server.port)
            writer.write(b"GET /" + b"x" * (5000))
            await writer.drain()
            await asyncio.sleep(0.2)
            writer.close()
            # the server is still answering
            _, status, _ = await asyncio.to_thread(_get, f"http://127.0.0.1:{server.port}/metrics")
            assert status == 200
        finally:
            await asyncio.wait_for(server.stop(), timeout=5)

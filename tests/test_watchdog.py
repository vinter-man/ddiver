"""The dead-man's switch: it must ping, and it must never be able to break anything."""

from __future__ import annotations

import asyncio
import http.server
import threading
import urllib.error
from typing import ClassVar

import pytest

from ddiver.watchdog import Watchdog


class Recorder(http.server.BaseHTTPRequestHandler):
    paths: ClassVar[list[str]] = []
    agents: ClassVar[list[str]] = []
    status = 200

    def do_GET(self) -> None:
        Recorder.paths.append(self.path)
        Recorder.agents.append(self.headers.get("User-Agent", ""))
        self.send_response(Recorder.status)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args) -> None:
        pass  # the test output is not a web server access log


@pytest.fixture
def server():
    Recorder.paths, Recorder.agents, Recorder.status = [], [], 200
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


class TestPing:
    async def test_it_reaches_the_url(self, server):
        port = server.server_address[1]
        dog = Watchdog(f"http://127.0.0.1:{port}/ping/abc")
        assert await dog.ping() is True
        assert Recorder.paths == ["/ping/abc"]
        assert Recorder.agents == ["ddiver-watchdog"]
        assert (dog.sent, dog.failed) == (1, 0)

    async def test_a_server_error_counts_as_a_failure(self, server):
        Recorder.status = 500
        dog = Watchdog(f"http://127.0.0.1:{server.server_address[1]}/ping")
        assert await dog.ping() is False
        assert (dog.sent, dog.failed) == (0, 1)

    async def test_an_unreachable_host_never_raises(self):
        """The watchdog runs inside the heartbeat; an exception here stops the heartbeat."""
        dog = Watchdog("http://127.0.0.1:1/ping", timeout=0.2)
        assert await dog.ping() is False
        assert dog.failed == 1

    async def test_it_is_off_without_a_url(self):
        dog = Watchdog("")
        assert dog.enabled is False
        assert await dog.ping() is False
        assert (dog.sent, dog.failed) == (0, 0)

    async def test_an_outage_is_logged_once_not_every_minute(self, caplog):
        dog = Watchdog("http://127.0.0.1:1/ping", timeout=0.2)
        with caplog.at_level("WARNING"):
            for _ in range(3):
                await dog.ping()
        assert len([r for r in caplog.records if "watchdog" in r.message]) == 1

    async def test_a_recovery_re_arms_the_warning(self, server, caplog):
        port = server.server_address[1]
        dog = Watchdog(f"http://127.0.0.1:{port}/ping", timeout=1.0)
        with caplog.at_level("WARNING"):
            Recorder.status = 500
            await dog.ping()
            Recorder.status = 200
            await dog.ping()  # recovered
            Recorder.status = 500
            await dog.ping()  # a second, distinct outage must speak up again
        assert len([r for r in caplog.records if "watchdog" in r.message]) == 2

    async def test_an_exploding_transport_is_swallowed(self, monkeypatch):
        """Even a bug inside urllib must not take the process down."""

        def boom(*_args, **_kwargs):
            raise RuntimeError("urllib is having a day")

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        dog = Watchdog("http://example.invalid/ping")
        assert await dog.ping() is False

    async def test_the_request_does_not_block_the_event_loop(self, server):
        """It runs in a thread: a slow ping must not stall the listener."""
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0)

        task = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        before = ticks
        await Watchdog(f"http://127.0.0.1:{server.server_address[1]}/ping").ping()
        assert ticks > before
        task.cancel()

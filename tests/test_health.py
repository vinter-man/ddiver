import datetime as dt

from ddiver.health import (
    Heartbeat,
    check,
    heartbeat_age,
    read_heartbeat,
    write_heartbeat,
)
from tests.fakes import T0


def beat(**kw) -> Heartbeat:
    return Heartbeat(ts=T0.isoformat(), **kw)


class TestFile:
    def test_roundtrip(self, tmp_path):
        path = tmp_path / "sub" / "heartbeat"
        write_heartbeat(path, T0, beat(ping_ok_at=T0.isoformat(), transport=True, queue=3))
        stored = read_heartbeat(path)
        assert stored.written_at == T0
        assert stored.transport is True and stored.queue == 3
        assert heartbeat_age(path, T0 + dt.timedelta(seconds=30)) == 30

    def test_written_atomically(self, tmp_path):
        path = tmp_path / "heartbeat"
        write_heartbeat(path, T0)
        write_heartbeat(path, T0 + dt.timedelta(seconds=5))
        assert not (tmp_path / "heartbeat.tmp").exists()
        assert read_heartbeat(path).written_at == T0 + dt.timedelta(seconds=5)

    def test_missing_and_garbage(self, tmp_path):
        path = tmp_path / "heartbeat"
        assert read_heartbeat(path) is None
        assert heartbeat_age(path) is None
        path.write_text("not a heartbeat")
        assert read_heartbeat(path) is None
        path.write_text('{"nope": 1}')
        assert read_heartbeat(path) is None

    def test_a_bare_timestamp_still_parses(self, tmp_path):
        """Older versions wrote just the timestamp; do not go unhealthy on an upgrade."""
        path = tmp_path / "heartbeat"
        path.write_text(T0.isoformat())
        assert read_heartbeat(path).written_at == T0


class TestCheck:
    def test_fresh_file_with_a_live_listener(self, tmp_path):
        path = tmp_path / "heartbeat"
        write_heartbeat(path, T0, beat(ping_ok_at=T0.isoformat(), transport=True))
        report = check(path, now=T0 + dt.timedelta(seconds=30))
        assert report.healthy and "ok" in report.reason

    def test_stale_file(self, tmp_path):
        path = tmp_path / "heartbeat"
        write_heartbeat(path, T0)
        assert not check(path, now=T0 + dt.timedelta(minutes=5)).healthy

    def test_future_timestamp(self, tmp_path):
        path = tmp_path / "heartbeat"
        write_heartbeat(path, T0 + dt.timedelta(minutes=5))
        assert not check(path, now=T0).healthy

    def test_a_dead_transport_is_unhealthy_even_with_a_fresh_file(self, tmp_path):
        """The writer keeps running while Telethon reconnects forever — that is the failure."""
        path = tmp_path / "heartbeat"
        write_heartbeat(path, T0, beat(transport=False, reconnecting=True))
        report = check(path, now=T0 + dt.timedelta(seconds=10))
        assert not report.healthy and "transport" in report.reason

    def test_a_stale_ping_is_unhealthy(self, tmp_path):
        path = tmp_path / "heartbeat"
        stale = (T0 - dt.timedelta(minutes=10)).isoformat()
        write_heartbeat(path, T0, beat(ping_ok_at=stale, transport=True))
        report = check(path, now=T0 + dt.timedelta(seconds=10))
        assert not report.healthy and "answered" in report.reason

    def test_degraded_is_reported_healthy_so_docker_does_not_restart_loop(self, tmp_path):
        path = tmp_path / "heartbeat"
        write_heartbeat(path, T0, beat(degraded=True, transport=False))
        report = check(path, now=T0 + dt.timedelta(seconds=10))
        assert report.healthy and "login" in report.reason

    def test_no_file(self, tmp_path):
        assert not check(tmp_path / "missing").healthy

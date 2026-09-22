import datetime as dt

from ddiver.health import heartbeat_age, is_healthy, write_heartbeat


def test_heartbeat_roundtrip(tmp_path):
    path = tmp_path / "sub" / "heartbeat"
    now = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.UTC)
    write_heartbeat(path, now)
    assert heartbeat_age(path, now + dt.timedelta(seconds=30)) == 30
    assert is_healthy(path, now=now + dt.timedelta(seconds=119))
    assert not is_healthy(path, now=now + dt.timedelta(seconds=121))


def test_missing_or_garbage_file(tmp_path):
    path = tmp_path / "heartbeat"
    assert heartbeat_age(path) is None
    assert not is_healthy(path)
    path.write_text("not a date")
    assert not is_healthy(path)


def test_future_stamp_is_unhealthy(tmp_path):
    path = tmp_path / "heartbeat"
    now = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.UTC)
    write_heartbeat(path, now + dt.timedelta(minutes=5))
    assert not is_healthy(path, now=now)

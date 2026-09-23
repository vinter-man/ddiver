"""Heartbeat file used by ``ddiver health`` and the Docker HEALTHCHECK.

A timestamp alone is not enough: the writer is a background task that keeps running even when
the listener is disconnected, and Telethon's ``is_connected()`` stays true throughout its
(infinite) reconnect loop. So the file also carries when the listener last answered a ping and
whether the transport is actually up, and ``ddiver health`` checks those too.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from ddiver.timeutil import utcnow

MAX_AGE_SECONDS = 120
MAX_PING_AGE_SECONDS = 180


@dataclass(frozen=True, slots=True)
class Heartbeat:
    ts: str
    ping_ok_at: str | None = None
    last_update_at: str | None = None
    transport: bool | None = None
    reconnecting: bool | None = None
    degraded: bool = False
    queue: int = 0

    @property
    def written_at(self) -> dt.datetime | None:
        return _parse(self.ts)

    @property
    def last_ping(self) -> dt.datetime | None:
        return _parse(self.ping_ok_at)


def _parse(text: str | None) -> dt.datetime | None:
    if not text:
        return None
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


def write_heartbeat(
    path: Path, now: dt.datetime | None = None, state: Heartbeat | None = None
) -> None:
    """Write the file atomically, so a concurrent ``ddiver health`` never reads half of it."""
    moment = now or utcnow()
    payload = state or Heartbeat(ts=moment.isoformat())
    payload = Heartbeat(**{**asdict(payload), "ts": moment.isoformat()})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(payload)))
    tmp.replace(path)


def read_heartbeat(path: Path) -> Heartbeat | None:
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        # An older file held a bare timestamp; keep understanding it.
        return Heartbeat(ts=raw) if _parse(raw) else None
    if not isinstance(data, dict) or not isinstance(data.get("ts"), str):
        return None
    return Heartbeat(
        ts=data["ts"],
        ping_ok_at=_as_str(data.get("ping_ok_at")),
        last_update_at=_as_str(data.get("last_update_at")),
        transport=_as_bool(data.get("transport")),
        reconnecting=_as_bool(data.get("reconnecting")),
        degraded=bool(data.get("degraded", False)),
        queue=int(data.get("queue") or 0),
    )


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_bool(value: object) -> bool | None:
    return bool(value) if isinstance(value, bool) else None


def heartbeat_age(path: Path, now: dt.datetime | None = None) -> float | None:
    """Seconds since the last heartbeat, or ``None`` if the file is missing/unreadable."""
    beat = read_heartbeat(path)
    written = beat.written_at if beat else None
    if written is None:
        return None
    return ((now or utcnow()) - written).total_seconds()


@dataclass(frozen=True, slots=True)
class HealthReport:
    healthy: bool
    reason: str
    age: float | None = None


def check(
    path: Path,
    *,
    max_age: float = MAX_AGE_SECONDS,
    max_ping_age: float = MAX_PING_AGE_SECONDS,
    now: dt.datetime | None = None,
) -> HealthReport:
    moment = now or utcnow()
    beat = read_heartbeat(path)
    if beat is None or beat.written_at is None:
        return HealthReport(False, "no heartbeat file")
    age = (moment - beat.written_at).total_seconds()
    if age < 0 or age > max_age:
        return HealthReport(False, f"heartbeat is {age:.0f}s old", age)
    if beat.degraded:
        # The listener needs a new login. Restarting the container would not help, so report
        # healthy and let /status and the owner notification carry the message.
        return HealthReport(True, "degraded: the listener needs `ddiver login`", age)
    if beat.transport is False:
        return HealthReport(False, "listener transport is down", age)
    if beat.ping_ok_at is not None:
        ping_age = (moment - (beat.last_ping or beat.written_at)).total_seconds()
        if ping_age > max_ping_age:
            return HealthReport(False, f"listener last answered {ping_age:.0f}s ago", age)
    return HealthReport(True, f"ok ({age:.0f}s)", age)

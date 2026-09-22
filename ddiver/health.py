"""Heartbeat file used by ``ddiver health`` / Docker HEALTHCHECK."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from ddiver.timeutil import utcnow

MAX_AGE_SECONDS = 120


def write_heartbeat(path: Path, now: dt.datetime | None = None) -> None:
    moment = now or utcnow()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(moment.isoformat())
    tmp.replace(path)


def heartbeat_age(path: Path, now: dt.datetime | None = None) -> float | None:
    """Seconds since the last heartbeat, or ``None`` if the file is missing/unreadable."""
    try:
        stamp = dt.datetime.fromisoformat(path.read_text().strip())
    except (OSError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.UTC)
    return ((now or utcnow()) - stamp).total_seconds()


def is_healthy(
    path: Path, max_age: float = MAX_AGE_SECONDS, now: dt.datetime | None = None
) -> bool:
    age = heartbeat_age(path, now)
    return age is not None and 0 <= age <= max_age

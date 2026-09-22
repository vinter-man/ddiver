"""Timezone conversion, timestamp formatting and short relative-time strings."""

from __future__ import annotations

import datetime as dt
import zoneinfo
from collections.abc import Callable

Clock = Callable[[], dt.datetime]
"""Injectable UTC-now source so tests can freeze time."""


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def validate_tz(name: str) -> str | None:
    """Return the canonical IANA name if ``name`` is a valid zone, else ``None``.

    Accepts a few friendly spellings: ``UTC``, ``utc``, ``+3``, ``+03:00``, ``-5``.
    """
    candidate = name.strip()
    if not candidate:
        return None
    if candidate.upper() in {"UTC", "Z", "GMT"}:
        return "UTC"
    offset = _parse_fixed_offset(candidate)
    if offset is not None:
        return offset
    try:
        zoneinfo.ZoneInfo(candidate)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return None
    return candidate


def _parse_fixed_offset(text: str) -> str | None:
    """``+3`` / ``-05`` / ``+03:00`` → the matching ``Etc/GMT∓N`` zone (only whole hours)."""
    if text[0] not in "+-" or len(text) > 6:
        return None
    body = text[1:]
    hours_part, _, minutes_part = body.partition(":")
    if not hours_part.isdigit() or (minutes_part and minutes_part != "00"):
        return None
    hours = int(hours_part)
    if hours > 14:
        return None
    if hours == 0:
        return "UTC"
    # POSIX sign inversion: Etc/GMT-3 is UTC+3.
    sign = "-" if text[0] == "+" else "+"
    name = f"Etc/GMT{sign}{hours}"
    try:
        zoneinfo.ZoneInfo(name)
    except zoneinfo.ZoneInfoNotFoundError:
        return None
    return name


def to_zone(moment: dt.datetime, tz: str) -> dt.datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(zoneinfo.ZoneInfo(tz))


def format_local(moment: dt.datetime, tz: str) -> str:
    """``2026-09-22 14:03:05 (Europe/Kyiv)``. Fixed-offset zones show ``UTC+3``."""
    local = to_zone(moment, tz)
    label = tz
    if tz.startswith("Etc/GMT"):
        offset = local.utcoffset() or dt.timedelta()
        hours = int(offset.total_seconds() // 3600)
        label = f"UTC{hours:+d}"
    return f"{local:%Y-%m-%d %H:%M:%S} ({label})"


_UNITS: dict[str, tuple[str, str, str, str]] = {
    # seconds, minutes, hours, days
    "en": ("s", "min", "h", "d"),
    "ru": ("сек", "мин", "ч", "д"),
    "uk": ("с", "хв", "год", "д"),
}
_AGO: dict[str, str] = {"en": "{n} {u} ago", "ru": "{n} {u} назад", "uk": "{n} {u} тому"}


def relative(moment: dt.datetime, now: dt.datetime, lang: str) -> str:
    """Short 'N unit ago' string; negative deltas (clock skew) are shown as 0 s."""
    seconds = max(0, int((now - moment).total_seconds()))
    units = _UNITS.get(lang, _UNITS["en"])
    template = _AGO.get(lang, _AGO["en"])
    if seconds < 60:
        n, u = seconds, units[0]
    elif seconds < 3600:
        n, u = seconds // 60, units[1]
    elif seconds < 86400:
        n, u = seconds // 3600, units[2]
    else:
        n, u = seconds // 86400, units[3]
    return template.format(n=n, u=u)

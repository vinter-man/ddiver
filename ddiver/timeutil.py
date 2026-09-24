"""Timezone resolution, timestamp formatting and short relative-time strings."""

from __future__ import annotations

import datetime as dt
import functools
import zoneinfo
from collections.abc import Callable
from dataclasses import dataclass, field

Clock = Callable[[], dt.datetime]
"""Injectable UTC-now source so tests can freeze time."""

MAX_CANDIDATES = 5

# Legacy / colloquial spellings people type; values must exist in the tz database.
_ALIASES: dict[str, str] = {
    "kiev": "Europe/Kyiv",
    "kyiv": "Europe/Kyiv",
    "київ": "Europe/Kyiv",
    "киев": "Europe/Kyiv",
    "moscow": "Europe/Moscow",
    "москва": "Europe/Moscow",
    "warsaw": "Europe/Warsaw",
    "варшава": "Europe/Warsaw",
    "berlin": "Europe/Berlin",
    "берлин": "Europe/Berlin",
    "берлін": "Europe/Berlin",
    "london": "Europe/London",
    "лондон": "Europe/London",
    "utc": "UTC",
    "gmt": "UTC",
    "z": "UTC",
}


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@functools.cache
def _zones() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones())


@functools.cache
def _zones_lower() -> dict[str, str]:
    return {z.lower(): z for z in _zones()}


@dataclass(frozen=True, slots=True)
class TzResolution:
    """``exact`` when the input unambiguously names a zone; otherwise up to five candidates."""

    exact: str | None = None
    candidates: list[str] = field(default_factory=list)
    fixed_offset: bool = False  # Etc/GMT±N zones have no DST; the UI warns about it


def validate_tz(name: str) -> str | None:
    """Exact validation used for stored values: the name must exist in the tz database.

    Matching is case-sensitive on purpose (macOS resolves ``europe/kyiv`` from its
    case-insensitive filesystem, Linux does not), except for the ``UTC`` spellings.
    """
    candidate = name.strip()
    if not candidate:
        return None
    if candidate.upper() in {"UTC", "Z", "GMT"}:
        return "UTC"
    if candidate in _zones():
        return candidate
    return None


def resolve_tz(text: str) -> TzResolution:
    """Resolve user input into a zone name or a short list of suggestions."""
    raw = text.strip()
    if not raw:
        return TzResolution()
    exact = validate_tz(raw)
    if exact:
        return TzResolution(exact=exact)
    offset = _parse_fixed_offset(raw)
    if offset is not None:
        return TzResolution(exact=offset, fixed_offset=offset != "UTC")
    lowered = raw.lower()
    alias = _ALIASES.get(lowered)
    if alias:
        return TzResolution(exact=alias)
    case_fixed = _zones_lower().get(lowered)
    if case_fixed:
        return TzResolution(exact=case_fixed)
    needle = lowered.replace(" ", "_")
    suffix_hits = sorted(z for z in _zones() if z.lower().rsplit("/", 1)[-1] == needle)
    if len(suffix_hits) == 1:
        return TzResolution(exact=suffix_hits[0])
    if suffix_hits:
        return TzResolution(candidates=suffix_hits[:MAX_CANDIDATES])
    partial = sorted(z for z in _zones() if needle in z.lower() and "/" in z)
    return TzResolution(candidates=partial[:MAX_CANDIDATES])


def _parse_fixed_offset(text: str) -> str | None:
    """``+3`` / ``-05`` / ``+03:00`` / ``UTC+3`` → the matching ``Etc/GMT∓N`` zone (whole hours)."""
    body = text.upper().removeprefix("UTC").removeprefix("GMT")
    if not body or body[0] not in "+-" or len(body) > 6:
        return None
    hours_part, _, minutes_part = body[1:].partition(":")
    if minutes_part and minutes_part != "00":
        return None
    try:
        # `'²'.isdigit()` is True and `int('²')` raises, and this is reached from a callback
        # button as well as from typed text — where the exception is a spinner that never ends.
        hours = int(hours_part)
    except ValueError:
        return None
    if hours > 14:
        return None
    if hours == 0:
        return "UTC"
    sign = "-" if body[0] == "+" else "+"  # POSIX sign inversion: Etc/GMT-3 is UTC+3
    name = f"Etc/GMT{sign}{hours}"
    return name if name in _zones() else None


def to_zone(moment: dt.datetime, tz: str) -> dt.datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(zoneinfo.ZoneInfo(tz))


def utc_offset_label(local: dt.datetime) -> str:
    offset = local.utcoffset() or dt.timedelta()
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def format_local(moment: dt.datetime, tz: str) -> str:
    """``2026-09-22 14:03:05 +03:00 (Europe/Kyiv)``; the offset disambiguates DST folds."""
    local = to_zone(moment, tz)
    label = "UTC" if tz.startswith("Etc/GMT") else tz
    return f"{local:%Y-%m-%d %H:%M:%S} {utc_offset_label(local)} ({label})"


def format_clock(moment: dt.datetime, tz: str) -> str:
    """``14:03 (UTC+03:00)`` for the timezone confirmation prompt."""
    local = to_zone(moment, tz)
    return f"{local:%H:%M} (UTC{utc_offset_label(local)})"


_UNITS: dict[str, tuple[str, str, str, str]] = {
    # seconds, minutes, hours, days — abbreviations, so no plural forms are needed
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

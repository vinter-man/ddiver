import datetime as dt

import pytest

from ddiver.timeutil import (
    format_clock,
    format_local,
    relative,
    resolve_tz,
    to_zone,
    validate_tz,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Europe/Kyiv", "Europe/Kyiv"),
        ("  Europe/Kyiv ", "Europe/Kyiv"),
        ("UTC", "UTC"),
        ("utc", "UTC"),
        ("Etc/UTC", "Etc/UTC"),
        ("Mars/Olympus", None),
        ("", None),
        ("../../etc/passwd", None),
        # case-sensitive on purpose: macOS would accept this, Linux would not
        ("europe/kyiv", None),
        ("+3", None),  # offsets are only accepted by resolve_tz
    ],
)
def test_validate_tz(raw, expected):
    assert validate_tz(raw) == expected


@pytest.mark.parametrize(
    ("raw", "exact", "fixed"),
    [
        ("Europe/Kyiv", "Europe/Kyiv", False),
        ("europe/kyiv", "Europe/Kyiv", False),
        ("kyiv", "Europe/Kyiv", False),
        ("Kiev", "Europe/Kyiv", False),
        ("Київ", "Europe/Kyiv", False),
        ("Warsaw", "Europe/Warsaw", False),
        ("berlin", "Europe/Berlin", False),
        ("Prague", "Europe/Prague", False),
        ("+3", "Etc/GMT-3", True),
        ("UTC+3", "Etc/GMT-3", True),
        ("+03:00", "Etc/GMT-3", True),
        ("-5", "Etc/GMT+5", True),
        ("+0", "UTC", False),
        ("utc", "UTC", False),
    ],
)
def test_resolve_tz_exact(raw, exact, fixed):
    res = resolve_tz(raw)
    assert res.exact == exact
    assert res.fixed_offset is fixed
    assert res.candidates == []


def test_resolve_tz_candidates_and_misses():
    amb = resolve_tz("Europe")
    assert amb.exact is None
    assert 1 <= len(amb.candidates) <= 5
    assert all(c.startswith("Europe/") for c in amb.candidates)
    assert resolve_tz("Mars/Olympus") == resolve_tz("zzzz")
    assert resolve_tz("zzzz").candidates == []
    assert resolve_tz("").exact is None
    assert resolve_tz("+15").exact is None
    assert resolve_tz("+3:30").exact is None


def test_to_zone_and_format():
    moment = dt.datetime(2026, 9, 22, 11, 3, 5, tzinfo=dt.UTC)
    assert to_zone(moment, "Europe/Kyiv").hour == 14
    assert format_local(moment, "Europe/Kyiv") == "2026-09-22 14:03:05 +03:00 (Europe/Kyiv)"
    assert format_local(moment, "Etc/GMT-3") == "2026-09-22 14:03:05 +03:00 (UTC)"
    assert format_local(moment, "Etc/GMT+5") == "2026-09-22 06:03:05 -05:00 (UTC)"
    assert format_local(moment, "UTC") == "2026-09-22 11:03:05 +00:00 (UTC)"
    assert format_local(moment, "Asia/Kolkata") == "2026-09-22 16:33:05 +05:30 (Asia/Kolkata)"
    assert format_clock(moment, "Europe/Kyiv") == "14:03 (UTC+03:00)"


def test_naive_datetime_is_treated_as_utc():
    assert to_zone(dt.datetime(2026, 1, 1, 0, 0), "Europe/Kyiv").hour == 2


def test_dst_fall_back_hour_is_disambiguated_by_offset():
    before = dt.datetime(2026, 10, 25, 0, 30, tzinfo=dt.UTC)  # 03:30 EEST
    after = dt.datetime(2026, 10, 25, 1, 30, tzinfo=dt.UTC)  # 03:30 EET, after fall back
    assert format_local(before, "Europe/Kyiv") == "2026-10-25 03:30:00 +03:00 (Europe/Kyiv)"
    assert format_local(after, "Europe/Kyiv") == "2026-10-25 03:30:00 +02:00 (Europe/Kyiv)"


@pytest.mark.parametrize(
    ("seconds", "lang", "expected"),
    [
        (0, "en", "0 s ago"),
        (12, "en", "12 s ago"),
        (59, "ru", "59 сек назад"),
        (60, "ru", "1 мин назад"),
        (3599, "uk", "59 хв тому"),
        (3600, "uk", "1 год тому"),
        (86399, "en", "23 h ago"),
        (86400 * 3, "ru", "3 д назад"),
        (-30, "en", "0 s ago"),  # clock skew never goes negative
        (5, "xx", "5 s ago"),  # unknown language falls back to English
    ],
)
def test_relative(seconds, lang, expected):
    now = dt.datetime(2026, 9, 22, 12, 0, 0, tzinfo=dt.UTC)
    assert relative(now - dt.timedelta(seconds=seconds), now, lang) == expected


def test_relative_rejects_naive_now():
    aware = dt.datetime(2026, 9, 22, 12, 0, 0, tzinfo=dt.UTC)
    with pytest.raises(TypeError):
        relative(aware, dt.datetime(2026, 9, 22, 12, 0, 0), "en")


@pytest.mark.parametrize("raw", ["UTC+²", "+²", "-³:00", "+٣٢١٤٣"])
def test_a_digit_that_int_cannot_read_is_not_a_timezone(raw):
    """`'²'.isdigit()` is True and `int('²')` raises ValueError.

    This is reached from the timezone *button* as well as from typed text, where an exception
    is a spinner that never stops — the same failure `_parts` and `_int` were hardened against.
    Arabic-Indic digits are not collateral: int() reads them, and the rest of the guard decides.
    """
    assert resolve_tz(raw).exact is None

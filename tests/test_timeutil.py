import datetime as dt

import pytest

from ddiver.timeutil import format_local, relative, to_zone, validate_tz


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Europe/Kyiv", "Europe/Kyiv"),
        ("  Europe/Kyiv ", "Europe/Kyiv"),
        ("UTC", "UTC"),
        ("utc", "UTC"),
        ("Etc/UTC", "Etc/UTC"),
        ("+3", "Etc/GMT-3"),
        ("+03:00", "Etc/GMT-3"),
        ("-5", "Etc/GMT+5"),
        ("+0", "UTC"),
        ("Mars/Olympus", None),
        ("", None),
        ("+15", None),
        ("+3:30", None),
        ("../../etc/passwd", None),
    ],
)
def test_validate_tz(raw, expected):
    assert validate_tz(raw) == expected


def test_to_zone_and_format():
    moment = dt.datetime(2026, 9, 22, 11, 3, 5, tzinfo=dt.UTC)
    assert to_zone(moment, "Europe/Kyiv").hour == 14
    assert format_local(moment, "Europe/Kyiv") == "2026-09-22 14:03:05 (Europe/Kyiv)"
    assert format_local(moment, "Etc/GMT-3") == "2026-09-22 14:03:05 (UTC+3)"
    assert format_local(moment, "Etc/GMT+5") == "2026-09-22 06:03:05 (UTC-5)"
    assert format_local(moment, "UTC") == "2026-09-22 11:03:05 (UTC)"


def test_naive_datetime_is_treated_as_utc():
    assert to_zone(dt.datetime(2026, 1, 1, 0, 0), "Europe/Kyiv").hour == 2


def test_dst_fall_back_hour_is_unambiguous_in_output():
    # 2026-10-25 01:30 UTC is 03:30 EEST → after fall back it is 03:30 EET at 02:30 UTC too.
    before = dt.datetime(2026, 10, 25, 0, 30, tzinfo=dt.UTC)
    after = dt.datetime(2026, 10, 25, 1, 30, tzinfo=dt.UTC)
    assert format_local(before, "Europe/Kyiv").startswith("2026-10-25 03:30:00")
    assert format_local(after, "Europe/Kyiv").startswith("2026-10-25 03:30:00")


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

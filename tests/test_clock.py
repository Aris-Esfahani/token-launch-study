"""Times shown to people: 12-hour clock, local time zone."""

import time

from agent import clock


def local(*parts: int) -> float:
    """A Unix time from local date and time parts (year, month, day, hour, minute, second)."""
    return time.mktime((*parts, 0, 0, -1))


def test_a_twelve_hour_clock():
    assert clock.clock(local(2026, 11, 12, 0, 5, 9)) == "12:05 AM"
    assert clock.clock(local(2026, 11, 12, 9, 5, 9)) == "9:05 AM"
    assert clock.clock(local(2026, 11, 12, 12, 0, 0)) == "12:00 PM"
    assert clock.clock(local(2026, 11, 12, 21, 5, 27), seconds=True) == "9:05:27 PM"
    assert clock.stamp(local(2026, 11, 3, 23, 40, 1)) == "Nov 3 11:40:01 PM"
    assert clock.stamp(local(2026, 11, 3, 23, 40, 1), seconds=False) == "Nov 3 11:40 PM"


def test_today_yesterday_tomorrow():
    now = local(2026, 11, 12, 21, 0, 0)
    assert clock.when(local(2026, 11, 12, 6, 10, 0), now) == "6:10 AM today"
    assert clock.when(local(2026, 11, 11, 23, 10, 0), now) == "11:10 PM yesterday"
    assert clock.when(local(2026, 11, 13, 5, 54, 0), now) == "5:54 AM tomorrow"
    assert clock.when(local(2026, 11, 9, 4, 3, 0), now) == "Nov 9 4:03 AM"


def test_the_zone_is_a_short_name():
    name = clock.zone()
    assert name and " " not in name and name.isupper()

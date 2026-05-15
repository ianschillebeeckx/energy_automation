"""Tests for the daily Solcast fetch schedule."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from elec_auto.solcast import daily_schedule

_TZ = ZoneInfo("America/Los_Angeles")
_LAT, _LON = 37.736015, -122.452026


def test_returns_eight_slots_when_lat_lon_set() -> None:
    now = datetime(2026, 6, 21, 0, 0, tzinfo=_TZ)
    s = daily_schedule(now, _LAT, _LON)
    assert len(s) == 8


def test_first_slot_is_pre_dawn() -> None:
    now = datetime(2026, 6, 21, 0, 0, tzinfo=_TZ)
    s = daily_schedule(now, _LAT, _LON)
    assert s[0].hour == 4 and s[0].minute == 50


def test_slots_strictly_inside_sunrise_sunset() -> None:
    from astral import Observer
    from astral.sun import sun

    now = datetime(2026, 6, 21, 0, 0, tzinfo=_TZ)
    obs = Observer(latitude=_LAT, longitude=_LON)
    sd = sun(obs, date=now.date(), tzinfo=_TZ)
    sunrise, sunset = sd["sunrise"], sd["sunset"]

    s = daily_schedule(now, _LAT, _LON)
    daylight = [t for t in s if t != s[0]]  # drop the pre-dawn slot
    assert len(daylight) == 7
    assert all(sunrise < t < sunset for t in daylight)


def test_slots_evenly_spaced_in_daylight() -> None:
    now = datetime(2026, 6, 21, 0, 0, tzinfo=_TZ)
    s = daily_schedule(now, _LAT, _LON)
    daylight = sorted(t for t in s if t != s[0])  # drop the pre-dawn slot
    # 7 slots between sunrise and sunset, evenly spaced -> 8 equal gaps.
    diffs = [(daylight[i+1] - daylight[i]).total_seconds()
             for i in range(len(daylight) - 1)]
    # All gaps within 1 second of each other (floating-point divisions of dates).
    assert max(diffs) - min(diffs) < 1.0


def test_slots_sorted() -> None:
    now = datetime(2026, 6, 21, 0, 0, tzinfo=_TZ)
    s = daily_schedule(now, _LAT, _LON)
    assert s == sorted(s)


def test_falls_back_to_pre_dawn_only_without_coords() -> None:
    now = datetime(2026, 6, 21, 0, 0, tzinfo=_TZ)
    s = daily_schedule(now, None, None)
    assert len(s) == 1
    assert s[0].hour == 4 and s[0].minute == 50


def test_winter_has_shorter_daylight() -> None:
    summer = datetime(2026, 6, 21, 0, 0, tzinfo=_TZ)
    winter = datetime(2026, 12, 21, 0, 0, tzinfo=_TZ)
    s_summer = daily_schedule(summer, _LAT, _LON)
    s_winter = daily_schedule(winter, _LAT, _LON)
    # Same number of slots, but the winter ones are clustered closer in time.
    assert len(s_summer) == len(s_winter)
    summer_span = (s_summer[-1] - s_summer[1]).total_seconds()
    winter_span = (s_winter[-1] - s_winter[1]).total_seconds()
    assert winter_span < summer_span


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError):
        daily_schedule(datetime(2026, 6, 21, 0, 0), _LAT, _LON)

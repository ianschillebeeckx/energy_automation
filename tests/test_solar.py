"""Tests for the clear-sky theoretical PV output model."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from elec_auto.config import Settings
from elec_auto.solar import theoretical_day_kwh, theoretical_w

_TZ = ZoneInfo("America/Los_Angeles")
# Roughly San Francisco — coordinates are far enough from poles that sunrise
# and sunset behave normally year-round.
_LAT = 37.736015
_LON = -122.452026


def _settings(**overrides) -> Settings:
    defaults = dict(
        latitude=_LAT,
        longitude=_LON,
        solar_array_max_kw=6.6,
        solar_panel_azimuth_deg=155.0,  # SE
        solar_panel_tilt_deg=30.0,
        solar_system_loss_factor=0.09,
        timezone="America/Los_Angeles",
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type]


def test_zero_when_coordinates_unset() -> None:
    s = _settings(latitude=None, longitude=None)
    when = datetime(2026, 6, 21, 12, 0, tzinfo=_TZ)
    assert theoretical_w(when, s) == 0.0


def test_zero_at_night() -> None:
    # Midnight in summer — sun is well below the horizon.
    when = datetime(2026, 6, 21, 0, 0, tzinfo=_TZ)
    assert theoretical_w(when, _settings()) == 0.0


def test_solar_noon_summer_is_close_to_rated() -> None:
    # Summer solstice solar noon ~13:08 PDT in SF. With 30° tilt + 155° az,
    # we should be drawing nearly the full rated output minus the loss
    # factor. Expect something in the 80–95% rated range.
    when = datetime(2026, 6, 21, 13, 0, tzinfo=_TZ)
    w = theoretical_w(when, _settings())
    rated_w = 6600 * (1 - 0.09)  # 6006 W after losses
    assert 0.80 * rated_w < w < 1.0 * rated_w


def test_winter_noon_lower_than_summer_noon() -> None:
    summer = theoretical_w(datetime(2026, 6, 21, 13, 0, tzinfo=_TZ), _settings())
    winter = theoretical_w(datetime(2026, 12, 21, 12, 0, tzinfo=_TZ), _settings())
    assert winter < summer


def test_morning_lower_than_afternoon_for_se_facing() -> None:
    # 155° azimuth = facing SSE. Output peaks earlier than for due-south
    # panels. But morning still ramps up — comparing 8 AM to 1 PM, the
    # afternoon should typically dominate near solar noon.
    morning = theoretical_w(datetime(2026, 6, 21, 8, 0, tzinfo=_TZ), _settings())
    afternoon = theoretical_w(datetime(2026, 6, 21, 13, 0, tzinfo=_TZ), _settings())
    assert morning < afternoon
    assert morning > 0


def test_due_west_facing_panels_peak_in_afternoon() -> None:
    s = _settings(solar_panel_azimuth_deg=270.0)  # due west
    morning = theoretical_w(datetime(2026, 6, 21, 9, 0, tzinfo=_TZ), s)
    afternoon = theoretical_w(datetime(2026, 6, 21, 17, 0, tzinfo=_TZ), s)
    assert afternoon > morning


def test_higher_loss_factor_gives_lower_output() -> None:
    when = datetime(2026, 6, 21, 13, 0, tzinfo=_TZ)
    a = theoretical_w(when, _settings(solar_system_loss_factor=0.05))
    b = theoretical_w(when, _settings(solar_system_loss_factor=0.20))
    assert a > b


@pytest.mark.parametrize(
    "hour", [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
)
def test_returns_non_negative(hour: int) -> None:
    when = datetime(2026, 6, 21, hour, 0, tzinfo=_TZ)
    assert theoretical_w(when, _settings()) >= 0.0


# --- theoretical_day_kwh -----------------------------------------------------


def test_theoretical_day_kwh_winter_less_than_summer() -> None:
    summer = theoretical_day_kwh(
        datetime(2026, 6, 21, 0, 0, tzinfo=_TZ), _settings(),
    )
    winter = theoretical_day_kwh(
        datetime(2026, 12, 21, 0, 0, tzinfo=_TZ), _settings(),
    )
    assert winter < summer
    # SF + SSE 30° tilt: winter ≈ 75% of summer (panel tilt compensates).
    assert 0.5 < winter / summer < 0.9


def test_theoretical_day_kwh_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        theoretical_day_kwh(datetime(2026, 6, 21, 0, 0), _settings())


def test_theoretical_day_kwh_zero_without_coordinates() -> None:
    s = _settings(latitude=None, longitude=None)
    out = theoretical_day_kwh(datetime(2026, 6, 21, 0, 0, tzinfo=_TZ), s)
    assert out == 0.0

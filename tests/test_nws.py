"""Offline tests for NWS client parsers and helpers.

The HTTP-calling part is exercised end-to-end in production; here we
just lock in the gnarly bits: ISO-8601 duration parsing, the
hour-by-hour expansion of multi-hour grid values, and the wind-speed
string parser.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from elec_auto.nws import (
    _cloud_layers_to_pct, _deg_to_cardinal, _expand_grid, _extract_value,
    _observation_from, _parse_duration, _parse_wind_speed_mph,
)


def test_parse_duration_simple_hours() -> None:
    assert _parse_duration("PT1H") == timedelta(hours=1)
    assert _parse_duration("PT3H") == timedelta(hours=3)


def test_parse_duration_days_and_hours() -> None:
    assert _parse_duration("P1DT3H") == timedelta(days=1, hours=3)
    assert _parse_duration("P4D") == timedelta(days=4)


def test_parse_duration_minutes() -> None:
    assert _parse_duration("PT15M") == timedelta(minutes=15)
    assert _parse_duration("PT2H30M") == timedelta(hours=2, minutes=30)


def test_parse_duration_unrecognized_returns_zero() -> None:
    assert _parse_duration("garbage") == timedelta()
    assert _parse_duration("") == timedelta()


def test_parse_wind_speed_mph_simple() -> None:
    assert _parse_wind_speed_mph("3 mph") == 3.0
    assert _parse_wind_speed_mph("12 mph") == 12.0


def test_parse_wind_speed_mph_range_takes_low_end() -> None:
    assert _parse_wind_speed_mph("3 to 8 mph") == 3.0


def test_parse_wind_speed_mph_handles_none() -> None:
    assert _parse_wind_speed_mph(None) is None
    assert _parse_wind_speed_mph("") is None


def test_extract_value_pulls_value_field() -> None:
    assert _extract_value({"unitCode": "wmoUnit:percent", "value": 84}) == 84.0
    assert _extract_value({"value": None}) is None
    assert _extract_value(None) is None


def _gridval(start: datetime, dur: str, value: float) -> dict:
    return {"validTime": f"{start.isoformat()}/{dur}", "value": value}


def test_expand_grid_single_hour() -> None:
    t0 = datetime(2026, 5, 14, 3, 0, tzinfo=timezone.utc)
    out = _expand_grid([_gridval(t0, "PT1H", 12.0)])
    assert out == {int(t0.timestamp()): 12.0}


def test_expand_grid_multi_hour_expands_to_each_hour() -> None:
    t0 = datetime(2026, 5, 14, 3, 0, tzinfo=timezone.utc)
    out = _expand_grid([_gridval(t0, "PT3H", 25.0)])
    expected = {
        int(t0.timestamp()): 25.0,
        int((t0 + timedelta(hours=1)).timestamp()): 25.0,
        int((t0 + timedelta(hours=2)).timestamp()): 25.0,
    }
    assert out == expected


def test_expand_grid_overlapping_entries_latest_wins() -> None:
    # If two entries cover the same hour, the later one in the list overwrites
    # the earlier — mirrors how NWS lists are read in order.
    t0 = datetime(2026, 5, 14, 3, 0, tzinfo=timezone.utc)
    out = _expand_grid([
        _gridval(t0, "PT2H", 10.0),
        _gridval(t0 + timedelta(hours=1), "PT1H", 50.0),
    ])
    assert out[int(t0.timestamp())] == 10.0
    assert out[int((t0 + timedelta(hours=1)).timestamp())] == 50.0


def test_expand_grid_skips_malformed_entries() -> None:
    t0 = datetime(2026, 5, 14, 3, 0, tzinfo=timezone.utc)
    out = _expand_grid([
        {"validTime": "not a slash format"},
        {"value": 99.0},  # no validTime
        _gridval(t0, "PT1H", 5.0),
    ])
    assert out == {int(t0.timestamp()): 5.0}


# --- _deg_to_cardinal --------------------------------------------------------


def test_deg_to_cardinal_known_compass_points() -> None:
    # The 16-point compass: each sector spans 22.5°.
    assert _deg_to_cardinal(0) == "N"
    assert _deg_to_cardinal(90) == "E"
    assert _deg_to_cardinal(180) == "S"
    assert _deg_to_cardinal(270) == "W"
    assert _deg_to_cardinal(290) == "WNW"
    assert _deg_to_cardinal(247.5) == "WSW"  # exact boundary


def test_deg_to_cardinal_wraps_at_360() -> None:
    assert _deg_to_cardinal(360) == "N"


def test_deg_to_cardinal_none_passes_through() -> None:
    assert _deg_to_cardinal(None) is None


# --- _cloud_layers_to_pct ----------------------------------------------------


def test_cloud_layers_to_pct_picks_densest_layer() -> None:
    layers = [{"amount": "FEW", "base": {}}, {"amount": "BKN", "base": {}}]
    assert _cloud_layers_to_pct(layers) == 75.0


def test_cloud_layers_to_pct_known_codes() -> None:
    assert _cloud_layers_to_pct([{"amount": "CLR"}]) == 0.0
    assert _cloud_layers_to_pct([{"amount": "SCT"}]) == 37.0
    assert _cloud_layers_to_pct([{"amount": "OVC"}]) == 100.0


def test_cloud_layers_to_pct_handles_empty_and_unknown() -> None:
    assert _cloud_layers_to_pct(None) is None
    assert _cloud_layers_to_pct([]) is None
    # Unknown codes are skipped; if all are unknown, returns None.
    assert _cloud_layers_to_pct([{"amount": "WAT"}]) is None


# --- _observation_from -------------------------------------------------------


def test_observation_from_converts_units_and_codes() -> None:
    # windSpeed in m/s (5.0 m/s ≈ 11.18 mph), windDirection in degrees,
    # cloudLayers categorical → sky_cover_pct via _cloud_layers_to_pct.
    props = {
        "temperature": {"value": 14.0},
        "dewpoint": {"value": 10.0},
        "relativeHumidity": {"value": 76.0},
        "windSpeed": {"value": 5.0},
        "windDirection": {"value": 290},
        "textDescription": "Clear",
        "cloudLayers": [{"amount": "FEW", "base": {"value": 3000}}],
    }
    o = _observation_from(props, period_ts=1000, station_id="KSFO",
                          fetched_at=900)
    assert o is not None
    assert o.temperature_c == 14.0
    assert abs(o.wind_speed_mph - 11.1847) < 1e-3
    assert o.wind_dir == "WNW"
    assert o.text_description == "Clear"
    assert o.sky_cover_pct == 15.0


def test_observation_from_returns_none_on_empty_props() -> None:
    assert _observation_from({}, 1000, "KSFO", 900) is None

"""Unit tests for the charge-mode controller.

Covers each mode's decision against representative telemetry, plus the
unknown-mode guard. Uses `Settings(_env_file=None, ...)` to ignore any local
.env and force deterministic thresholds.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from elec_auto.config import Settings
from elec_auto.controller import compute_target
from elec_auto.emporia import ChargerState
from elec_auto.powerwall import PowerReading

_TZ = ZoneInfo("America/Los_Angeles")
# A wall-clock moment inside the default dump window (7:00–8:00) so tests
# that don't care about scheduling can pin it and stay deterministic.
_IN_WINDOW = datetime(2026, 4, 22, 7, 0, tzinfo=_TZ)


def _settings(**overrides) -> Settings:
    defaults = dict(
        battery_reserve_pct=80, ev_min_amps=6, ev_max_amps=40, ev_voltage=240,
        battery_capacity_kwh=13.5, morning_dump_floor_pct=15,
        morning_dump_hours=1.0, morning_dump_start_hour=7, trickle_kw=2.0,
        timezone="America/Los_Angeles",
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type]


def _pw(soc=80.0, solar=0.0, load=0.0, battery=0.0, grid=0.0) -> PowerReading:
    return PowerReading(solar_w=solar, load_w=load, battery_w=battery,
                        grid_w=grid, battery_soc_pct=soc)


def _ev(on=False, rate=0) -> ChargerState:
    return ChargerState(gid=1, name="t", on=on, charge_rate_a=rate,
                        max_charge_rate_a=40, status="Standby")


def test_trickle_converts_kw_to_amps() -> None:
    # 2 kW / 240 V = 8.33 A -> floor 8 A (above the 6 A minimum).
    d = compute_target("trickle", _pw(), _ev(), _settings())
    assert d.target_amps == 8


def test_trickle_clamps_to_max() -> None:
    d = compute_target("trickle", _pw(), _ev(), _settings(trickle_kw=20.0))
    assert d.target_amps == 40


def test_trickle_raises_to_min() -> None:
    # 0.5 kW -> 2 A but below min; clamp up to 6 A.
    d = compute_target("trickle", _pw(), _ev(), _settings(trickle_kw=0.5))
    assert d.target_amps == 6


def test_morning_dump_drains_headroom_over_window() -> None:
    # SoC 80%, floor 15%, 13.5 kWh, 1 h window at t=start: headroom 8.775
    # kWh / 1 h -> 36 A.
    d = compute_target("morning_dump", _pw(soc=80), _ev(), _settings(), now=_IN_WINDOW)
    assert d.target_amps == 36


def test_morning_dump_below_floor_returns_zero() -> None:
    d = compute_target("morning_dump", _pw(soc=10), _ev(), _settings(), now=_IN_WINDOW)
    assert d.target_amps == 0
    assert "floor" in d.reason


def test_morning_dump_respects_longer_window() -> None:
    # 65% headroom over 4 h -> ~9 A. A 7:00 click with a 4 h window keeps
    # us at the start of the window so remaining hours == configured hours.
    d = compute_target(
        "morning_dump", _pw(soc=80), _ev(),
        _settings(morning_dump_hours=4.0), now=_IN_WINDOW,
    )
    assert d.target_amps == 9


def test_morning_dump_before_window_previews_rate_but_off() -> None:
    # Evening click: charger off (on=False) but target amps still carry the
    # preview rate so the dashboard shows what the EVSE is configured for.
    evening = datetime(2026, 4, 21, 22, 0, tzinfo=_TZ)
    d = compute_target("morning_dump", _pw(soc=80), _ev(), _settings(), now=evening)
    assert d.on is False
    assert d.target_amps == 36  # same preview as in-window start-of-window
    assert "scheduled" in d.reason and "07:00" in d.reason


def test_morning_dump_after_today_rolls_to_tomorrow() -> None:
    # Afternoon the same day: today's window already closed, so we wait
    # until tomorrow's 07:00; charger off but rate still previewed.
    afternoon = datetime(2026, 4, 22, 14, 0, tzinfo=_TZ)
    d = compute_target("morning_dump", _pw(soc=80), _ev(), _settings(), now=afternoon)
    assert d.on is False
    assert d.target_amps == 36
    assert "scheduled" in d.reason


def test_decision_on_flag_matches_mode_semantics() -> None:
    # Active modes set on=True; below-min or scheduled should report False.
    s = _settings()
    assert compute_target("trickle", _pw(soc=80), _ev(), s).on is True
    assert compute_target("surplus", _pw(solar=5000, load=1000, soc=85),
                          _ev(on=False), s).on is True
    assert compute_target("surplus", _pw(solar=100, load=1000, soc=85),
                          _ev(on=False), s).on is False  # below min


def test_morning_dump_rate_rises_when_time_shrinks() -> None:
    # 30 min into a 1 h window, same 65% headroom must finish in 0.5 h,
    # so the rate roughly doubles vs start-of-window.
    half = datetime(2026, 4, 22, 7, 30, tzinfo=_TZ)
    d_start = compute_target("morning_dump", _pw(soc=80), _ev(), _settings(), now=_IN_WINDOW)
    d_half = compute_target("morning_dump", _pw(soc=80), _ev(), _settings(), now=half)
    # 36 A clamps at 40 A max, so compare against the clamp-aware target.
    assert d_start.target_amps == 36
    assert d_half.target_amps == 40  # clamped to ev_max_amps


def test_surplus_uses_policy() -> None:
    # 5 kW solar, 1 kW house, battery at reserve -> 16 A to EV.
    d = compute_target(
        "surplus",
        _pw(solar=5000, load=1000, soc=85),
        _ev(on=False),
        _settings(),
    )
    assert d.target_amps == 16


def test_surplus_waits_for_telemetry() -> None:
    d = compute_target("surplus", None, None, _settings())
    assert d.target_amps == 0
    assert "telemetry" in d.reason


def test_unknown_mode_is_zero() -> None:
    d = compute_target("bogus", _pw(), _ev(), _settings())
    assert d.target_amps == 0
    assert "unknown" in d.reason

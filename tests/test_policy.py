"""Unit tests for the surplus-allocation policy.

Each test covers a single step change — "battery below reserve locks EV out",
"exactly enough surplus for min amps", "clamp at max", etc. Tests use the real
`Settings` class with `_env_file=None` so a developer's local `.env` can't
leak in and change the thresholds under test.
"""

from __future__ import annotations

import pytest

from elec_auto.config import Settings
from elec_auto.emporia import ChargerState
from elec_auto.policy import Decision, decide_ev_amps
from elec_auto.powerwall import PowerReading


def _settings(**overrides) -> Settings:
    defaults = dict(
        battery_reserve_pct=80,
        ev_min_amps=6,
        ev_max_amps=40,
        ev_voltage=240,
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type]


def _pw(*, solar=0.0, load=0.0, soc=85.0, battery=0.0, grid=0.0) -> PowerReading:
    return PowerReading(
        solar_w=solar, load_w=load, battery_w=battery,
        grid_w=grid, battery_soc_pct=soc,
    )


def _ev(*, on=False, rate=0, maxrate=40, status="Standby") -> ChargerState:
    return ChargerState(
        gid=1, name="test", on=on,
        charge_rate_a=rate, max_charge_rate_a=maxrate,
        status=status,
    )


# --- battery reserve gate ------------------------------------------------

def test_below_reserve_returns_zero_even_with_surplus() -> None:
    d = decide_ev_amps(
        _pw(solar=10_000, load=500, soc=79.9),
        _ev(on=False),
        _settings(),
    )
    assert d.target_amps == 0
    assert "reserve" in d.reason


def test_at_reserve_exactly_proceeds() -> None:
    # 80% SoC, 80% reserve — the boundary should admit the EV.
    d = decide_ev_amps(
        _pw(solar=2000, load=0, soc=80.0),
        _ev(on=False),
        _settings(),
    )
    assert d.target_amps > 0


# --- surplus -> amps conversion -----------------------------------------

@pytest.mark.parametrize(
    "solar,load,expected,note",
    [
        (1440, 0, 6, "1440 W / 240 V == 6 A, exactly min"),
        (1439, 0, 0, "5 A truncated surplus is below min"),
        (2500, 0, 10, "2500 W / 240 V = 10.4 A -> floor 10 A"),
        (9600, 0, 40, "40 A surplus at configured max"),
        (12_000, 0, 40, "50 A surplus clamps down to max 40 A"),
    ],
    ids=["exact_min", "below_min", "floor_division", "at_max", "clamp_to_max"],
)
def test_surplus_is_floored_and_clamped(solar, load, expected, note) -> None:
    d = decide_ev_amps(
        _pw(solar=solar, load=load, soc=85),
        _ev(on=False),
        _settings(),
    )
    assert d.target_amps == expected, f"{note}: got {d}"


# --- EV's own draw is backed out of load ---------------------------------

def test_ev_on_subtracts_its_own_draw_from_load() -> None:
    # Charger currently pulling 16 A * 240 V = 3840 W, reported inside total
    # load of 4000 W. Non-EV load is 160 W. Solar 8000 W -> surplus 7840 W
    # -> 7840 // 240 = 32 A.
    d = decide_ev_amps(
        _pw(solar=8000, load=4000, soc=85),
        _ev(on=True, rate=16),
        _settings(),
    )
    assert d.target_amps == 32


def test_ev_off_does_not_subtract_stale_rate() -> None:
    # Charger OFF but still carries rate=16 from last session — must be ignored.
    d = decide_ev_amps(
        _pw(solar=5000, load=1000, soc=85),
        _ev(on=False, rate=16),
        _settings(),
    )
    # surplus = 5000 - 1000 = 4000 W -> 16 A
    assert d.target_amps == 16


# --- no / negative surplus -----------------------------------------------

def test_zero_surplus_returns_zero() -> None:
    d = decide_ev_amps(
        _pw(solar=1000, load=1000, soc=90),
        _ev(on=False),
        _settings(),
    )
    assert d.target_amps == 0


def test_house_pulling_from_grid_returns_zero() -> None:
    d = decide_ev_amps(
        _pw(solar=500, load=2000, soc=90),
        _ev(on=False),
        _settings(),
    )
    assert d.target_amps == 0


# --- contract: always returns a Decision with a non-empty reason ---------

def test_always_returns_decision_with_reason() -> None:
    d = decide_ev_amps(_pw(), _ev(), _settings())
    assert isinstance(d, Decision)
    assert d.reason

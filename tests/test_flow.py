"""Unit tests for the flow-decomposition visualization helper.

Covers the priority heuristic (solar-first-to-home, battery-discharge-first-
to-home, grid-fills-deficit) and checks conservation of power.
"""

from __future__ import annotations

import pytest

from elec_auto.flow import decompose
from elec_auto.powerwall import PowerReading


def _pw(*, solar=0.0, load=0.0, battery=0.0, grid=0.0, soc=85.0) -> PowerReading:
    return PowerReading(
        solar_w=solar, load_w=load, battery_w=battery,
        grid_w=grid, battery_soc_pct=soc,
    )


def test_pure_surplus_exports_to_grid() -> None:
    # 5 kW solar, 1 kW home, battery full (no charge), exporting 4 kW.
    f = decompose(_pw(solar=5000, load=1000, battery=0, grid=-4000))
    assert f.solar_to_home == 1000
    assert f.solar_to_grid == 4000
    assert f.solar_to_battery == 0
    assert f.battery_to_home == 0
    assert f.battery_to_grid == 0
    assert f.grid_to_home == 0


def test_solar_charges_battery_before_exporting() -> None:
    # 5 kW solar, 1 kW home, battery charging at 3 kW, exporting 1 kW.
    f = decompose(_pw(solar=5000, load=1000, battery=-3000, grid=-1000))
    assert f.solar_to_home == 1000
    assert f.solar_to_battery == 3000
    assert f.solar_to_grid == 1000


def test_battery_covers_home_at_night() -> None:
    # No solar, 2 kW home, battery discharging 2 kW, grid idle.
    f = decompose(_pw(solar=0, load=2000, battery=2000, grid=0))
    assert f.battery_to_home == 2000
    assert f.battery_to_grid == 0
    assert f.grid_to_home == 0
    assert f.solar_to_home == 0


def test_grid_fills_home_deficit() -> None:
    # 500 W solar, 2 kW home, battery at reserve (idle), grid imports 1.5 kW.
    f = decompose(_pw(solar=500, load=2000, battery=0, grid=1500))
    assert f.solar_to_home == 500
    assert f.grid_to_home == 1500
    assert f.battery_to_home == 0


def test_battery_exports_when_solar_off_and_home_covered() -> None:
    # Manual force-export: no solar, home 500 W, battery discharging 3 kW,
    # exporting 2.5 kW to grid.
    f = decompose(_pw(solar=0, load=500, battery=3000, grid=-2500))
    assert f.battery_to_home == 500
    assert f.battery_to_grid == 2500
    assert f.solar_to_home == 0


def test_mixed_solar_and_battery_discharge() -> None:
    # Cloudy afternoon: 1 kW solar, 4 kW home, battery pitching in 3 kW.
    f = decompose(_pw(solar=1000, load=4000, battery=3000, grid=0))
    assert f.solar_to_home == 1000
    assert f.battery_to_home == 3000
    assert f.grid_to_home == 0


@pytest.mark.parametrize(
    "reading",
    [
        _pw(solar=5000, load=1000, battery=-3000, grid=-1000),
        _pw(solar=0, load=2000, battery=2000, grid=0),
        _pw(solar=500, load=2000, battery=0, grid=1500),
        _pw(solar=1000, load=4000, battery=3000, grid=0),
        _pw(solar=0, load=500, battery=3000, grid=-2500),
        _pw(solar=0, load=500, battery=-5000, grid=5500),
    ],
)
def test_flows_conserve_power(reading) -> None:
    # Every watt in the meter reading should be accounted for by exactly one edge.
    f = decompose(reading)
    solar_out = f.solar_to_home + f.solar_to_battery + f.solar_to_grid
    grid_in_total = f.grid_to_home + f.grid_to_battery
    grid_out_total = f.solar_to_grid + f.battery_to_grid
    batt_in_total = f.solar_to_battery + f.grid_to_battery
    batt_out_total = f.battery_to_home + f.battery_to_grid
    home_in = f.solar_to_home + f.grid_to_home + f.battery_to_home

    assert solar_out == pytest.approx(max(reading.solar_w, 0))
    assert home_in == pytest.approx(max(reading.load_w, 0))
    assert batt_in_total == pytest.approx(max(-reading.battery_w, 0))
    assert batt_out_total == pytest.approx(max(reading.battery_w, 0))
    assert grid_in_total == pytest.approx(max(reading.grid_w, 0))
    assert grid_out_total == pytest.approx(max(-reading.grid_w, 0))


def test_grid_to_battery_populates_for_storm_watch() -> None:
    # No solar, home tiny, battery charging 5 kW, grid importing 5.5 kW.
    # The unrendered grid->battery edge absorbs the charge flow.
    f = decompose(_pw(solar=0, load=500, battery=-5000, grid=5500))
    assert f.grid_to_home == 500
    assert f.grid_to_battery == 5000
    assert f.solar_to_battery == 0


def test_all_flows_non_negative() -> None:
    f = decompose(_pw(solar=5000, load=1000, battery=-3000, grid=-1000))
    for v in (f.solar_to_home, f.solar_to_battery, f.solar_to_grid,
              f.grid_to_home, f.battery_to_home, f.battery_to_grid):
        assert v >= 0

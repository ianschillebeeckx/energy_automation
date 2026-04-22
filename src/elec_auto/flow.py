"""Decompose a PowerReading into directed flows for the dashboard.

The meter gives four scalars: solar, home load, battery (+discharge/-charge),
and grid (+import/-export). The dashboard wants up to six directed flows:

    solar   -> home, battery, grid
    battery -> home, grid
    grid    -> home

The system is under-determined, so we pick a priority heuristic that matches
the README policy: solar covers home first, then charges the battery, then
exports; battery discharge covers home first, then exports; grid import fills
any remaining home deficit.

A real PW3 can also charge from grid (e.g. Storm Watch). `grid_to_battery`
captures that flow so the totals balance, but the dashboard doesn't draw it
per the current edge spec.
"""

from __future__ import annotations

from dataclasses import dataclass

from .powerwall import PowerReading


@dataclass(slots=True)
class Flows:
    solar_to_home: float
    solar_to_battery: float
    solar_to_grid: float
    grid_to_home: float
    battery_to_home: float
    battery_to_grid: float
    grid_to_battery: float


def decompose(pw: PowerReading) -> Flows:
    solar = max(pw.solar_w, 0.0)
    home = max(pw.load_w, 0.0)
    batt_out = max(pw.battery_w, 0.0)
    batt_in = max(-pw.battery_w, 0.0)
    grid_in = max(pw.grid_w, 0.0)
    grid_out = max(-pw.grid_w, 0.0)

    s_to_h = min(solar, home)
    solar -= s_to_h
    home -= s_to_h

    b_to_h = min(batt_out, home)
    batt_out -= b_to_h
    home -= b_to_h

    g_to_h = min(grid_in, home)
    grid_in -= g_to_h
    home -= g_to_h

    s_to_b = min(solar, batt_in)
    solar -= s_to_b
    batt_in -= s_to_b

    s_to_g = min(solar, grid_out)
    solar -= s_to_g
    grid_out -= s_to_g

    b_to_g = min(batt_out, grid_out)
    batt_out -= b_to_g
    grid_out -= b_to_g

    g_to_b = min(grid_in, batt_in)
    grid_in -= g_to_b
    batt_in -= g_to_b

    return Flows(
        solar_to_home=s_to_h,
        solar_to_battery=s_to_b,
        solar_to_grid=s_to_g,
        grid_to_home=g_to_h,
        battery_to_home=b_to_h,
        battery_to_grid=b_to_g,
        grid_to_battery=g_to_b,
    )

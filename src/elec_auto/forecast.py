"""Heuristic forecasters for home load and battery SoC.

Today both are "yesterday repeats": `load_forecast` shifts yesterday's
sample stream by +24 h, and `soc_forecast` integrates the Solcast PV
forecast minus that load forecast forward from the most recent SoC
reading. Designed as pure functions returning lightweight dataclasses so
the control loop can call them too without dragging in chart code.

Edge cases (intentional):
  - No yesterday samples → load_forecast returns []; soc_forecast then
    treats load as 0 (charges from PV only).
  - No current_soc_pct → soc_forecast returns [] (caller draws nothing).
  - PV/load missing for a step → 0 W is used (honest "we don't know"
    rather than extrapolating yesterday's tail).
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass

from .config import Settings
from .samples import Forecast, LoadStore, SampleStore

_DAY_SEC = 24 * 3600


@dataclass(slots=True)
class LoadForecast:
    ts: int
    load_w: float


@dataclass(slots=True)
class SocForecast:
    ts: int
    soc_pct: float  # displayed-% (Tesla-app scale), [0, 100]


def load_forecast(
    samples: SampleStore, start_ts: int, end_ts: int,
) -> list[LoadForecast]:
    """Yesterday-shifted load samples landing in [start_ts, end_ts]."""
    src = samples.read_range(start_ts - _DAY_SEC, end_ts - _DAY_SEC)
    return [
        LoadForecast(ts=s.ts + _DAY_SEC, load_w=s.load_w)
        for s in src
        if s.load_w is not None and s.load_w >= 0
    ]


def _integrate_ts_w(
    points: list[tuple[int, float]], start_ts: int, end_ts: int,
) -> float:
    """Trapezoidal integral of (ts, watts) over [start_ts, end_ts] → kWh.

    5-min step with linear interp between bracketing samples; returns 0
    when the window is empty/inverted or the points list is empty.
    """
    if end_ts <= start_ts or not points:
        return 0.0
    step = 300
    total_kwh = 0.0
    t = start_ts
    while t < end_ts:
        t_next = min(t + step, end_ts)
        avg_w = (_interp(points, t) + _interp(points, t_next)) / 2.0
        total_kwh += avg_w * (t_next - t) / 3600.0 / 1000.0
        t = t_next
    return total_kwh


def non_ev_load_kwh_in_window(
    samples: SampleStore,
    loads: LoadStore,
    start_ts: int,
    end_ts: int,
    ev_circuit_name: str = "EV Charger",
) -> float:
    """Yesterday's non-EV load in `[start_ts, end_ts]` (shifted -24 h).

    Integrates the PW3-reported `load_w` over yesterday's same-window
    and subtracts the integrated Emporia EV-circuit draw for the same
    period. Both inputs are real measurements — no configured-rate or
    status assumptions. Returns kWh, clamped at 0 if the EV circuit
    happens to integrate higher than total load (sensor noise).
    """
    yest_lo, yest_hi = start_ts - _DAY_SEC, end_ts - _DAY_SEC
    house_pts = [
        (s.ts, s.load_w)
        for s in samples.read_range(yest_lo, yest_hi)
        if s.load_w is not None and s.load_w >= 0
    ]
    ev_pts = [
        (r.ts, r.watts)
        for r in loads.read_range(yest_lo, yest_hi, circuit=ev_circuit_name)
    ]
    house_kwh = _integrate_ts_w(house_pts, yest_lo, yest_hi)
    ev_kwh = _integrate_ts_w(ev_pts, yest_lo, yest_hi)
    return max(0.0, house_kwh - ev_kwh)


def _interp(points: list[tuple[int, float]], ts: int, default: float = 0.0) -> float:
    """Linear interpolation between bracketing (ts, value) points.

    Returns `default` when `points` is empty or `ts` lies outside the
    data span. Inside the span, lerps between the two neighbors found
    via bisect.
    """
    if not points:
        return default
    n = len(points)
    if ts <= points[0][0] or ts >= points[-1][0]:
        if ts == points[0][0]:
            return points[0][1]
        if ts == points[-1][0]:
            return points[-1][1]
        return default
    i = bisect_left(points, (ts,))
    # bisect_left on (ts,) returns the position where (ts,) would insert,
    # which lands on the right neighbor (or on an exact match).
    if i < n and points[i][0] == ts:
        return points[i][1]
    t0, v0 = points[i - 1]
    t1, v1 = points[i]
    frac = (ts - t0) / (t1 - t0)
    return v0 + (v1 - v0) * frac


def pv_kwh_in_range(
    pv_forecasts: list[Forecast], start_ts: int, end_ts: int,
) -> float:
    """Integrate p50 PV forecast watts over [start_ts, end_ts] → kWh.

    Trapezoidal sum at 5-minute steps using the same linear-interp helper
    as soc_forecast. Returns 0 when end<=start or no forecast data
    overlaps the window.
    """
    if end_ts <= start_ts or not pv_forecasts:
        return 0.0
    pts = sorted(
        (f.period_ts, f.pv_w_p50)
        for f in pv_forecasts if f.pv_w_p50 is not None
    )
    if not pts:
        return 0.0
    step = 300
    total_kwh = 0.0
    t = start_ts
    while t < end_ts:
        t_next = min(t + step, end_ts)
        avg_w = (_interp(pts, t) + _interp(pts, t_next)) / 2.0
        total_kwh += avg_w * (t_next - t) / 3600.0 / 1000.0
        t = t_next
    return total_kwh


def load_kwh_in_range(
    load_forecasts: list[LoadForecast], start_ts: int, end_ts: int,
) -> float:
    """Integrate forecast load watts over [start_ts, end_ts] → kWh.

    Same trapezoidal-at-5-min-steps shape as `pv_kwh_in_range`. Returns
    0 when end<=start or there's no forecast data overlapping the window.
    """
    if end_ts <= start_ts or not load_forecasts:
        return 0.0
    pts = [(lf.ts, lf.load_w) for lf in load_forecasts]
    step = 300
    total_kwh = 0.0
    t = start_ts
    while t < end_ts:
        t_next = min(t + step, end_ts)
        avg_w = (_interp(pts, t) + _interp(pts, t_next)) / 2.0
        total_kwh += avg_w * (t_next - t) / 3600.0 / 1000.0
        t = t_next
    return total_kwh


def soc_forecast(
    *,
    now_ts: int,
    end_ts: int,
    current_soc_pct: float | None,
    pv_forecasts: list[Forecast],
    load_forecasts: list[LoadForecast],
    settings: Settings,
    step_sec: int = 300,
) -> list[SocForecast]:
    """Integrate (PV − load) forward from now_ts, returning SoC at each step.

    Operates in displayed-% units (the same scale shown in the Tesla app
    and on the dashboard). Energy delta per step is mapped to displayed-%
    via `usable_kwh = battery_capacity_kwh × (1 − raw_floor_pct/100)`.
    Charge power into the battery is clamped at `battery_max_charge_kw`;
    excess PV is treated as spilling to the grid. Discharge is not rate-
    clamped — the 0% floor catches the only practical case.
    """
    if current_soc_pct is None or end_ts <= now_ts:
        return []

    usable_kwh = settings.battery_capacity_kwh * (
        1.0 - settings.battery_raw_floor_pct / 100.0
    )
    max_charge_w = settings.battery_max_charge_kw * 1000.0

    pv_pts = sorted(
        (f.period_ts, f.pv_w_p50)
        for f in pv_forecasts if f.pv_w_p50 is not None
    )
    load_pts = [(lf.ts, lf.load_w) for lf in load_forecasts]  # already sorted

    soc = float(current_soc_pct)
    out: list[SocForecast] = [SocForecast(now_ts, soc)]
    t = now_ts + step_sec
    while t <= end_ts:
        pv_w = _interp(pv_pts, t)
        load_w = _interp(load_pts, t)
        net_w = pv_w - load_w
        if net_w > max_charge_w:
            net_w = max_charge_w
        delta_kwh = net_w * step_sec / 3600.0 / 1000.0
        soc = max(0.0, min(100.0, soc + delta_kwh / usable_kwh * 100.0))
        out.append(SocForecast(t, soc))
        t += step_sec
    return out

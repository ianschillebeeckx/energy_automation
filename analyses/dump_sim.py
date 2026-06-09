"""Dump-window control simulator.

Reproduces the 2026-05-19 late-window oscillation by running the
production MorningDump heuristic in isolation against a synthetic but
plausible PV trajectory and a constant base load. Designed for swapping
strategies: today it's `HeuristicDump`; the next iteration will plug in
a PID and compare under identical inputs.

Each 15-second step:
  1. Strategy.decide(soc, t, cfg) → commanded EV amps
  2. Actual EV draw = commanded_amps × `ev_voltage_real`  (note: the
     controller divides kW by `ev_voltage_assumed` instead, modelling
     the real-world panel-vs-nameplate voltage mismatch)
  3. battery_w = pv − other_load − ev_draw     (+ charging / − discharging)
  4. soc += battery_w · dt / capacity_kwh / 100   (clamped to [0,100])

Knobs in `SimConfig`:
  - initial_soc_pct, floor_pct, battery_capacity_kwh
  - other_load_w (constant base, no spikes for now)
  - pv_w_fn(t_sec) — actual PV at t (W)
  - pv_forecast_fn(t_sec) — what the strategy sees as a forecast
  - ev_voltage_assumed / ev_voltage_real
  - remaining_hr_floor — same `max(remaining, 0.05)` clamp as production
  - dt_sec

Run: `uv run python -m analyses.dump_sim`
"""

from __future__ import annotations

import dataclasses
import datetime
import sqlite3
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from elec_auto.forecast import pv_w_at
from elec_auto.samples import Forecast

_TZ = ZoneInfo("America/Los_Angeles")
_DB_PATH = Path("state/samples.db")


# --- config -----------------------------------------------------------------


@dataclasses.dataclass
class SimConfig:
    # Battery.
    battery_capacity_kwh: float = 13.5
    initial_soc_pct: float = 51.0          # match 2026-05-19 ~05:00

    # Dump window.
    window_minutes: int = 180              # 3 h: 05:00 → 08:00
    floor_pct: float = 5.0                 # sunny-day floor (matches today)
    dump_start: datetime.datetime = dataclasses.field(
        default_factory=lambda: datetime.datetime(
            2026, 5, 19, 5, 0, tzinfo=_TZ,
        ),
    )

    # Loads (callables of t_sec from sim start).
    #   *_fn      → ground truth, drives the SoC integration
    #   *_forecast_fn → what the controller "sees" when deciding
    # Production splits these: actual PV from telemetry vs Solcast p50,
    # actual non-EV load vs yesterday's same-window measurement.
    other_load_fn: Callable[[int], float] = dataclasses.field(
        default_factory=lambda: (lambda _t: 400.0),
    )
    non_ev_load_forecast_fn: Callable[[int], float] = dataclasses.field(
        default_factory=lambda: (lambda _t: 400.0),
    )
    pv_w_fn: Callable[[int], float] = dataclasses.field(
        default_factory=lambda: (lambda _t: 0.0),
    )
    pv_forecast_fn: Callable[[int], float] = dataclasses.field(
        default_factory=lambda: (lambda _t: 0.0),
    )

    # EV charger.
    ev_voltage_assumed: float = 245.0      # what the controller divides by
    ev_voltage_real: float = 245.0         # what the EV actually pulls at
    ev_min_amps: int = 6
    ev_max_amps: int = 40
    ev_dump_max_amps: int = 29

    # Heuristic formula.
    pv_credit_pct: float = 90.0
    remaining_hr_floor: float = 0.05       # production cap

    # Tick.
    dt_sec: int = 15


@dataclasses.dataclass
class SimStep:
    t_sec: int
    soc_pct: float
    pv_w: float
    other_load_w: float
    ev_amps: int                            # commanded
    ev_draw_w: float                        # actual at voltage_real
    battery_w: float                        # net (+ charging / − discharging)


# --- strategies -------------------------------------------------------------


class HeuristicDump:
    """Port of `actions.py:MorningDump.decide` for offline simulation.

    Same math, no Settings/State plumbing — so we can tweak the formula
    here freely without touching production. The simulator's job is to
    reveal what the production formula does given a known input; the
    strategy must match production exactly to be useful.
    """

    name = "heuristic"

    def decide(self, soc_pct: float, t_sec: int, cfg: SimConfig) -> int:
        window_end_sec = cfg.window_minutes * 60
        remaining_sec = max(
            window_end_sec - t_sec,
            int(cfg.remaining_hr_floor * 3600),
        )
        remaining_hr = remaining_sec / 3600.0

        battery_kwh = (soc_pct - cfg.floor_pct) / 100.0 * cfg.battery_capacity_kwh
        if battery_kwh <= 0:
            return 0

        pv_ws = _integrate(cfg.pv_forecast_fn, t_sec, window_end_sec)
        pv_credit_kwh = (pv_ws / 3.6e6) * cfg.pv_credit_pct / 100.0
        non_ev_ws = _integrate(cfg.non_ev_load_forecast_fn, t_sec, window_end_sec)
        # Match production's `non_ev_load_kwh_in_window`: clamp at the
        # kWh level so brief moments where ev_circuit > load_w (sensor
        # noise) can be cancelled by surrounding samples before clipping.
        non_ev_load_kwh = max(0.0, non_ev_ws / 3.6e6)

        headroom_kwh = battery_kwh + pv_credit_kwh - non_ev_load_kwh
        kw = headroom_kwh / remaining_hr
        raw_amps = int(kw * 1000.0 / cfg.ev_voltage_assumed)

        if raw_amps < cfg.ev_min_amps:
            return 0
        return min(raw_amps, cfg.ev_max_amps, cfg.ev_dump_max_amps)


class LinearTrajectoryP:
    """PI controller tracking the SoC trajectory implied by the plan.

    Reference curve isn't a naive linear `initial → floor` straight line
    (early-window SoC dips below that geometrically — no PV yet — and a
    naive integral would mis-correct). Instead, the controller plans a
    constant `base_amps` draw and projects forward the SoC trajectory
    that would result if reality matched the forecast — concave when PV
    arrives late, etc. The PI then corrects deviations *from the plan*,
    so error stays near zero unless reality really diverges from forecast.

      base_amps     = (battery_kwh + pv_credit − non_ev_load) / window_h
                      × 1000 / voltage_assumed
      expected_soc(t)
          = forward-integrate (forecast_pv·credit − forecast_load − base_amps·V)
            over [0, t] starting at initial_soc
      error_pp(t)   = current_soc − expected_soc(t)
      ∫error(t)     = ∫₀ᵗ error_pp(s) ds            (units: pp·hour)
      target_amps   = base_amps + Kp·error_pp + Ki·∫error

    Units:
      Kp: amps per pp of instantaneous SoC error
      Ki: amps per (pp · hour) of accumulated error — pick by time
          constant Ti = Kp/Ki. E.g. Kp=1, Ki=1 → Ti = 1 h.

    Anti-windup: integral pauses when the commanded amps would clamp
    (saturate at ev_max / dump_max, or fall below ev_min).
    """

    def __init__(
        self, Kp: float, Ki: float = 0.0, name: str | None = None,
    ) -> None:
        self.Kp = Kp
        self.Ki = Ki
        if name is None:
            self.name = (
                f"P(Kp={Kp:g})" if Ki == 0
                else f"PI(Kp={Kp:g},Ki={Ki:g})"
            )
        else:
            self.name = name
        self._base_amps: float | None = None
        self._traj_ts: list[int] = []
        self._traj_soc: list[float] = []
        self._integral_pp_h: float = 0.0       # accumulated error (pp · hour)
        self._last_t_sec: int | None = None

    def _compute_base(self, cfg: SimConfig) -> float:
        # One-shot at t=0 using the forecasts the controller would see.
        window_sec = cfg.window_minutes * 60
        window_h = window_sec / 3600.0
        battery_kwh = (
            (cfg.initial_soc_pct - cfg.floor_pct) / 100.0
            * cfg.battery_capacity_kwh
        )
        pv_ws = _integrate(cfg.pv_forecast_fn, 0, window_sec)
        pv_credit_kwh = (pv_ws / 3.6e6) * cfg.pv_credit_pct / 100.0
        non_ev_ws = _integrate(cfg.non_ev_load_forecast_fn, 0, window_sec)
        non_ev_load_kwh = max(0.0, non_ev_ws / 3.6e6)
        base_kw = (battery_kwh + pv_credit_kwh - non_ev_load_kwh) / window_h
        return base_kw * 1000.0 / cfg.ev_voltage_assumed

    def _compute_trajectory(self, cfg: SimConfig) -> None:
        """Forward-integrate the SoC the plan would produce, using the
        controller's own forecasts. This is the curve the PI will track."""
        window_sec = cfg.window_minutes * 60
        step = 60
        ev_w = (self._base_amps or 0.0) * cfg.ev_voltage_assumed
        self._traj_ts = [0]
        self._traj_soc = [cfg.initial_soc_pct]
        soc = cfg.initial_soc_pct
        t = 0
        while t < window_sec:
            dt = min(step, window_sec - t)
            t_mid = t + dt // 2
            pv_credited = (
                cfg.pv_forecast_fn(t_mid) * cfg.pv_credit_pct / 100.0
            )
            load_w = max(0.0, cfg.non_ev_load_forecast_fn(t_mid))
            battery_w = pv_credited - load_w - ev_w
            delta = (
                battery_w * dt / 3.6e6
                / cfg.battery_capacity_kwh * 100.0
            )
            soc = max(0.0, min(100.0, soc + delta))
            t += dt
            self._traj_ts.append(t)
            self._traj_soc.append(soc)

    def _expected_soc(self, t_sec: int) -> float:
        ts, soc = self._traj_ts, self._traj_soc
        if not ts or t_sec <= ts[0]:
            return soc[0] if soc else 0.0
        if t_sec >= ts[-1]:
            return soc[-1]
        # ts is monotonic and dense; linear scan is fine for ~180 points.
        for i in range(len(ts) - 1):
            if ts[i] <= t_sec <= ts[i + 1]:
                t0, t1 = ts[i], ts[i + 1]
                s0, s1 = soc[i], soc[i + 1]
                return s0 + (s1 - s0) * (t_sec - t0) / (t1 - t0)
        return soc[-1]

    def decide(self, soc_pct: float, t_sec: int, cfg: SimConfig) -> int:
        if soc_pct <= cfg.floor_pct:
            return 0
        if self._base_amps is None:
            self._base_amps = self._compute_base(cfg)
            self._compute_trajectory(cfg)

        error_pp = soc_pct - self._expected_soc(t_sec)

        # Tentative output without integral update — used to decide whether
        # to accumulate (clamp-pause anti-windup).
        unclamped = (
            self._base_amps
            + self.Kp * error_pp
            + self.Ki * self._integral_pp_h
        )
        amps = int(round(unclamped))
        cmd = 0 if amps < cfg.ev_min_amps else min(
            amps, cfg.ev_max_amps, cfg.ev_dump_max_amps,
        )
        # Only accumulate when not saturated — prevents the integral from
        # winding up while the output can't physically respond.
        saturated = (
            (amps < cfg.ev_min_amps)
            or (amps >= cfg.ev_max_amps)
            or (amps >= cfg.ev_dump_max_amps)
        )
        if self._last_t_sec is not None and not saturated:
            dt_h = (t_sec - self._last_t_sec) / 3600.0
            self._integral_pp_h += error_pp * dt_h
        self._last_t_sec = t_sec
        return cmd


# --- helpers ----------------------------------------------------------------


def _integrate(
    fn: Callable[[int], float], t_lo: int, t_hi: int, step: int = 300,
) -> float:
    """Trapezoidal ∫ fn(t) dt over [t_lo, t_hi], result in W·s.

    Default step matches production's `_integrate_ts_w` (5 min) so the
    sim's kWh forecasts hit the same trapezoid samples as the deployed
    code.
    """
    if t_hi <= t_lo:
        return 0.0
    total = 0.0
    t = t_lo
    prev = fn(t)
    while t < t_hi:
        t_next = min(t + step, t_hi)
        v_next = fn(t_next)
        total += (prev + v_next) / 2.0 * (t_next - t)
        prev = v_next
        t = t_next
    return total


def piecewise_linear(anchors: list[tuple[float, float]]) -> Callable[[int], float]:
    """Build a piecewise-linear PV-like function from (minute, watts) anchors."""
    pts = sorted(anchors)

    def f(t_sec: int) -> float:
        t_min = t_sec / 60.0
        if t_min <= pts[0][0]:
            return pts[0][1]
        if t_min >= pts[-1][0]:
            return pts[-1][1]
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            if t0 <= t_min <= t1:
                return v0 + (v1 - v0) * (t_min - t0) / (t1 - t0)
        return 0.0  # unreachable

    return f


# Piecewise approximation of 2026-05-19 dump-window PV
# (minutes from 05:00 → watts), eyeballed from samples.db.
MORNING_2026_05_19_PV = piecewise_linear([
    (0, 0), (70, 0), (90, 200), (120, 900), (150, 1600), (180, 2400),
])


# --- real-data loaders (read-only against state/samples.db) -----------------


def _interp_fn(points: list[tuple[int, float]], t0: int) -> Callable[[int], float]:
    """Build a linear-interp callable f(t_sec_from_t0) from (ts, value) points.

    Outside the range we hold the nearest-edge value (clamp), which matches
    the controller's behavior at window edges.
    """
    pts = sorted((ts - t0, v) for ts, v in points)
    if not pts:
        return lambda _t: 0.0

    def f(t_sec: int) -> float:
        if t_sec <= pts[0][0]:
            return pts[0][1]
        if t_sec >= pts[-1][0]:
            return pts[-1][1]
        for (a_t, a_v), (b_t, b_v) in zip(pts, pts[1:]):
            if a_t <= t_sec <= b_t:
                if b_t == a_t:
                    return a_v
                return a_v + (b_v - a_v) * (t_sec - a_t) / (b_t - a_t)
        return pts[-1][1]  # unreachable

    return f


def load_real_day(
    target_day: datetime.date,
    db_path: Path = _DB_PATH,
) -> tuple[Callable[[int], float], Callable[[int], float],
           Callable[[int], float], list[tuple[int, int]]]:
    """Pull `target_day`'s 05:00–08:00 dump-window inputs from samples.db.

    Returns (pv_w_fn, other_load_fn, pv_forecast_fn, real_amp_trace) where
    each function takes t_sec from dump start (05:00 local) and returns
    the real recorded value (or interpolated for the forecast).
    `real_amp_trace` is the production controller's commanded amperage
    per tick during that window.

    `other_load_fn` is `samples.load_w − loads."EV Charger".watts` at each
    sample — the home load with the production EV draw subtracted out, so
    we can re-simulate against a clean baseline.
    """
    t0 = int(datetime.datetime.combine(
        target_day, datetime.time(5, 0), tzinfo=_TZ,
    ).timestamp())
    t_end = int(datetime.datetime.combine(
        target_day, datetime.time(8, 0), tzinfo=_TZ,
    ).timestamp())
    pad_lo, pad_hi = t0 - 600, t_end + 600

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    samples = con.execute(
        "SELECT ts, solar_w, load_w, charger_amps FROM samples "
        "WHERE ts BETWEEN ? AND ? ORDER BY ts",
        (pad_lo, pad_hi),
    ).fetchall()
    ev_circuit = dict(con.execute(
        "SELECT ts, watts FROM loads WHERE ts BETWEEN ? AND ? "
        "AND circuit='EV Charger' ORDER BY ts",
        (pad_lo, pad_hi),
    ).fetchall())

    pv_pts: list[tuple[int, float]] = []
    load_pts: list[tuple[int, float]] = []
    ev_pts: list[tuple[int, float]] = [(ts, float(w)) for ts, w in ev_circuit.items()]
    real_amps: list[tuple[int, int]] = []
    for ts, solar_w, load_w, ev_amps in samples:
        if solar_w is not None:
            pv_pts.append((ts, float(solar_w)))
        if load_w is not None:
            load_pts.append((ts, float(load_w)))
        if ev_amps is not None:
            real_amps.append((ts, int(ev_amps)))

    # Forecast: latest p50 per period_ts in the window (snapshot — does not
    # model intra-dump fetch updates).
    forecast_rows = con.execute(
        """
        SELECT period_ts, MAX(fetched_at), pv_w_p50
        FROM forecasts
        WHERE source='solcast' AND pv_w_p50 IS NOT NULL
          AND period_ts BETWEEN ? AND ?
        GROUP BY period_ts ORDER BY period_ts
        """,
        (pad_lo, pad_hi),
    ).fetchall()
    forecasts = [
        Forecast(period_ts=p, fetched_at=0, source="solcast", pv_w_p50=v)
        for p, _, v in forecast_rows
    ]

    pv_fn = _interp_fn(pv_pts, t0)
    # Interp the two series independently then subtract on demand — same as
    # production's `_integrate_ts_w`, so ts alignment between samples and
    # loads tables doesn't matter. (Pointwise clamp at 0 only for the
    # *actual* SoC-integration path; the forecast path leaves it signed.)
    load_w_fn = _interp_fn(load_pts, t0)
    ev_w_fn = _interp_fn(ev_pts, t0) if ev_pts else (lambda _t: 0.0)
    def other_fn(t_sec: int) -> float:
        return max(0.0, load_w_fn(t_sec) - ev_w_fn(t_sec))

    def forecast_fn(t_sec: int) -> float:
        return pv_w_at(forecasts, t0 + t_sec) or 0.0

    real_amp_trace = [(ts - t0, a) for ts, a in real_amps if t0 - 60 <= ts <= t_end + 60]
    return pv_fn, other_fn, forecast_fn, real_amp_trace


def load_yesterday_non_ev_load(
    today: datetime.date = datetime.date(2026, 5, 19),
    db_path: Path = _DB_PATH,
) -> Callable[[int], float]:
    """Mirror of production's `non_ev_load_kwh_in_window`: returns
    `samples.load_w − loads."EV Charger".watts` from *yesterday's* same
    wall-clock window, as a callable of t_sec from today's dump start.

    Yesterday's measurement is what the controller treats as the load
    forecast for today — static during the dump, so the formula's
    `non_ev_load_kwh` term doesn't wiggle with today's fridge spikes.
    """
    yesterday = today - datetime.timedelta(days=1)
    today_t0 = int(datetime.datetime.combine(
        today, datetime.time(5, 0), tzinfo=_TZ,
    ).timestamp())
    yest_t0 = int(datetime.datetime.combine(
        yesterday, datetime.time(5, 0), tzinfo=_TZ,
    ).timestamp())
    yest_t_end = int(datetime.datetime.combine(
        yesterday, datetime.time(8, 0), tzinfo=_TZ,
    ).timestamp())

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    load_pts = [
        (ts, float(load_w)) for ts, load_w in con.execute(
            "SELECT ts, load_w FROM samples WHERE ts BETWEEN ? AND ? "
            "AND load_w IS NOT NULL ORDER BY ts",
            (yest_t0 - 600, yest_t_end + 600),
        ).fetchall()
    ]
    ev_pts = [
        (ts, float(w)) for ts, w in con.execute(
            "SELECT ts, watts FROM loads WHERE ts BETWEEN ? AND ? "
            "AND circuit='EV Charger' ORDER BY ts",
            (yest_t0 - 600, yest_t_end + 600),
        ).fetchall()
    ]

    # Interp each series independently (same as production's
    # `_integrate_ts_w`); subtract on demand. Negative pointwise values
    # are fine — the strategy clamps the final integrated kWh at 0.
    load_w_fn = _interp_fn(load_pts, yest_t0)
    ev_w_fn = _interp_fn(ev_pts, yest_t0) if ev_pts else (lambda _t: 0.0)
    del today_t0  # not needed beyond the docstring

    def yesterday_non_ev_w(t_sec: int) -> float:
        return load_w_fn(t_sec) - ev_w_fn(t_sec)

    return yesterday_non_ev_w


# --- simulator --------------------------------------------------------------


def run(strategy, cfg: SimConfig) -> list[SimStep]:
    history: list[SimStep] = []
    soc = cfg.initial_soc_pct
    t = 0
    duration = cfg.window_minutes * 60
    while t < duration:
        amps = strategy.decide(soc, t, cfg)
        ev_draw_w = amps * cfg.ev_voltage_real if amps >= cfg.ev_min_amps else 0.0
        pv = cfg.pv_w_fn(t)
        other_load = cfg.other_load_fn(t)
        battery_w = pv - other_load - ev_draw_w
        delta_kwh = battery_w * cfg.dt_sec / 3.6e6
        delta_pct = delta_kwh / cfg.battery_capacity_kwh * 100.0
        soc = max(0.0, min(100.0, soc + delta_pct))
        history.append(SimStep(
            t_sec=t,
            soc_pct=soc,
            pv_w=pv,
            other_load_w=other_load,
            ev_amps=amps,
            ev_draw_w=ev_draw_w,
            battery_w=battery_w,
        ))
        t += cfg.dt_sec
    return history


# --- plot -------------------------------------------------------------------


def plot(
    history: list[SimStep], cfg: SimConfig, name: str, out: Path,
    real_amp_trace: list[tuple[int, int]] | None = None,
) -> None:
    def t_to_dt(t_sec: int) -> datetime.datetime:
        return cfg.dump_start + datetime.timedelta(seconds=t_sec)

    times = [t_to_dt(h.t_sec) for h in history]
    socs = [h.soc_pct for h in history]
    amps = [h.ev_amps for h in history]
    pvs = [h.pv_w for h in history]
    ev_draws = [h.ev_draw_w for h in history]
    battery_ws = [h.battery_w for h in history]

    fig, (ax_soc, ax_pv, ax_amp) = plt.subplots(
        3, 1, sharex=True, figsize=(11, 8.5),
        gridspec_kw={"height_ratios": [2, 2, 3]},
    )

    ax_soc.plot(times, socs, color="#2ea56a", linewidth=1.6)
    ax_soc.axhline(cfg.floor_pct, color="#888", linestyle=":", linewidth=1,
                   label=f"floor {cfg.floor_pct:.0f}%")
    ax_soc.set_ylabel("SoC (%)")
    ax_soc.grid(True, alpha=0.3)
    ax_soc.legend(loc="upper right", fontsize=9)
    ax_soc.set_title(
        f"Dump simulator — strategy={name}  ·  "
        f"V_assumed={cfg.ev_voltage_assumed:.0f}  V_real={cfg.ev_voltage_real:.0f}  ·  "
        f"final SoC {socs[-1]:.1f}%"
    )

    ax_pv.plot(times, pvs, color="#e8a33d", linewidth=1.6, label="PV")
    ax_pv.plot(times, ev_draws, color="#9b6dc7", linewidth=1.4,
               label=f"EV draw ({cfg.ev_voltage_real:.0f} V)")
    ax_pv.plot(times, battery_ws, color="#3aa5c7", linewidth=1.2, alpha=0.8,
               label="battery (+charge)")
    others = [h.other_load_w for h in history]
    ax_pv.plot(times, others, color="#888", linewidth=0.8, linestyle=":",
               label="other load (non-EV)")
    ax_pv.axhline(0, color="#888", linewidth=0.6)
    ax_pv.set_ylabel("Power (W)")
    ax_pv.grid(True, alpha=0.3)
    ax_pv.legend(loc="upper left", fontsize=9)

    ax_amp.step(times, amps, color="#d04545", linewidth=1.6, where="post",
                label="sim (strategy)")
    ax_amp.fill_between(times, 0, amps, step="post", alpha=0.25, color="#d04545")
    if real_amp_trace:
        real_times = [t_to_dt(t) for t, _ in real_amp_trace]
        real_a = [a for _, a in real_amp_trace]
        ax_amp.step(real_times, real_a, color="#1f77b4", linewidth=1.2,
                    where="post", alpha=0.8, label="production (recorded)")
    ax_amp.set_ylabel("EV amps commanded")
    ax_amp.set_xlabel(f"time of day ({cfg.dump_start.tzinfo})")
    ax_amp.grid(True, alpha=0.3)
    ax_amp.set_ylim(bottom=0)
    ax_amp.legend(loc="upper left", fontsize=9)

    tz = cfg.dump_start.tzinfo
    ax_amp.xaxis.set_major_locator(mdates.MinuteLocator(byminute=[0, 30], tz=tz))
    ax_amp.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=tz))

    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=110)
    print(f"wrote {out}")


def plot_compare(
    runs: list[tuple[str, list[SimStep], str]],   # (label, history, color)
    cfg: SimConfig,
    out: Path,
    real_amp_trace: list[tuple[int, int]] | None = None,
) -> None:
    """Overlay SoC + amp traces from multiple strategies on the same inputs.

    PV / load / forecast are shared, so we draw them once from the first
    run's history (any of them would do — they're identical).
    """
    def t_to_dt(t_sec: int) -> datetime.datetime:
        return cfg.dump_start + datetime.timedelta(seconds=t_sec)

    fig, (ax_soc, ax_pv, ax_amp) = plt.subplots(
        3, 1, sharex=True, figsize=(12, 9),
        gridspec_kw={"height_ratios": [2, 2, 3]},
    )

    # SoC: reference trajectories + each strategy's actual.
    times = [t_to_dt(h.t_sec) for h in runs[0][1]]
    # Naive linear schedule (for visual comparison).
    target_socs = [
        cfg.initial_soc_pct - (cfg.initial_soc_pct - cfg.floor_pct)
        * min(h.t_sec / (cfg.window_minutes * 60.0), 1.0)
        for h in runs[0][1]
    ]
    ax_soc.plot(times, target_socs, color="#bbb", linestyle=":", linewidth=0.8,
                label="linear")
    # Forecast-derived expected trajectory (what the PI strategies track).
    probe = LinearTrajectoryP(Kp=0.0)
    probe._base_amps = probe._compute_base(cfg)
    probe._compute_trajectory(cfg)
    expected_socs = [probe._expected_soc(h.t_sec) for h in runs[0][1]]
    ax_soc.plot(times, expected_socs, color="#666", linestyle="--", linewidth=1.0,
                label="expected (forecast)")
    for label, history, color in runs:
        ax_soc.plot(times, [h.soc_pct for h in history],
                    color=color, linewidth=1.4, label=label)
    ax_soc.axhline(cfg.floor_pct, color="#888", linestyle="--", linewidth=0.8)
    ax_soc.set_ylabel("SoC (%)")
    ax_soc.grid(True, alpha=0.3)
    ax_soc.legend(loc="upper right", fontsize=9, ncol=2)
    ax_soc.set_title(
        f"Dump strategies on {cfg.dump_start.date().isoformat()} real inputs  ·  "
        f"V_assumed={cfg.ev_voltage_assumed:.0f}  V_real={cfg.ev_voltage_real:.0f}"
    )

    # PV / load (shared inputs — pull from any run).
    h0 = runs[0][1]
    ax_pv.plot(times, [h.pv_w for h in h0], color="#e8a33d", linewidth=1.4,
               label="PV (actual)")
    ax_pv.plot(times, [h.other_load_w for h in h0], color="#888",
               linewidth=0.9, linestyle=":", label="other load (non-EV)")
    ax_pv.axhline(0, color="#888", linewidth=0.6)
    ax_pv.set_ylabel("Power (W)")
    ax_pv.grid(True, alpha=0.3)
    ax_pv.legend(loc="upper left", fontsize=9)

    # Amps: one step trace per strategy, plus optional production overlay.
    if real_amp_trace:
        real_times = [t_to_dt(t) for t, _ in real_amp_trace]
        ax_amp.step([t for t, _ in zip(real_times, real_amp_trace)],
                    [a for _, a in real_amp_trace],
                    color="#1f77b4", linewidth=1.0, where="post", alpha=0.6,
                    label="production (recorded)")
    for label, history, color in runs:
        ax_amp.step(times, [h.ev_amps for h in history],
                    color=color, linewidth=1.4, where="post", label=label)
    ax_amp.set_ylabel("EV amps commanded")
    ax_amp.set_xlabel(f"time of day ({cfg.dump_start.tzinfo})")
    ax_amp.grid(True, alpha=0.3)
    ax_amp.set_ylim(bottom=0)
    ax_amp.legend(loc="upper left", fontsize=9, ncol=3)

    tz = cfg.dump_start.tzinfo
    ax_amp.xaxis.set_major_locator(mdates.MinuteLocator(byminute=[0, 30], tz=tz))
    ax_amp.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=tz))

    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=110)
    print(f"wrote {out}")


# --- main -------------------------------------------------------------------


def _summarize(history: list[SimStep], label: str, cfg: SimConfig) -> None:
    changes = sum(
        1 for i in range(1, len(history))
        if history[i].ev_amps != history[i - 1].ev_amps
    )
    print(f"[{label}] ticks={len(history)}  amp-changes={changes}  "
          f"final SoC={history[-1].soc_pct:.2f}%  (floor {cfg.floor_pct:.0f}%)")


def _run_for_day(target_day: datetime.date) -> None:
    """Run heuristic + Kp sweep against `target_day`'s recorded inputs.

    Writes `analyses/output/dump-sim-compare-{YYYY-MM-DD}.png` and
    `dump-sim-zn-sweep-{YYYY-MM-DD}.png`. Skips silently if the DB is
    locked or missing for that day.
    """
    print(f"\n=== {target_day.isoformat()} ===")
    try:
        pv_fn, other_fn, forecast_fn, real_amps = load_real_day(target_day)
        yest_load_fn = load_yesterday_non_ev_load(today=target_day)
    except sqlite3.OperationalError as e:
        print(f"skipping {target_day}: {e}")
        return

    # Initial SoC from the first recorded sample at/before 05:00.
    t0 = int(datetime.datetime.combine(
        target_day, datetime.time(5, 0), tzinfo=_TZ,
    ).timestamp())
    con = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
    row = con.execute(
        "SELECT soc_pct FROM samples WHERE ts <= ? AND soc_pct IS NOT NULL "
        "ORDER BY ts DESC LIMIT 1",
        (t0,),
    ).fetchone()
    initial_soc = float(row[0]) if row and row[0] is not None else 50.0

    cfg = SimConfig(
        initial_soc_pct=initial_soc,
        pv_w_fn=pv_fn,
        other_load_fn=other_fn,
        non_ev_load_forecast_fn=yest_load_fn,
        pv_forecast_fn=forecast_fn,
        dump_start=datetime.datetime.combine(
            target_day, datetime.time(5, 0), tzinfo=_TZ,
        ),
    )

    # Heuristic + four P controllers.
    hist_heur = run(HeuristicDump(), cfg)
    _summarize(hist_heur, "heuristic", cfg)
    strategies_p = [
        (LinearTrajectoryP(Kp=0.5), "#c75d9b"),
        (LinearTrajectoryP(Kp=1.0), "#f2a93b"),
        (LinearTrajectoryP(Kp=3.0), "#2ea56a"),
        (LinearTrajectoryP(Kp=8.0), "#6d5dc7"),
    ]
    runs: list[tuple[str, list[SimStep], str]] = [
        ("heuristic", hist_heur, "#d04545"),
    ]
    for strat, color in strategies_p:
        hist = run(strat, cfg)
        _summarize(hist, strat.name, cfg)
        runs.append((strat.name, hist, color))

    plot_compare(
        runs, cfg,
        Path(f"analyses/output/dump-sim-compare-{target_day.isoformat()}.png"),
        real_amp_trace=real_amps,
    )

    # Ziegler-Nichols-style sweep.
    zn_strategies = [
        (LinearTrajectoryP(Kp=10),  "#f2a93b"),
        (LinearTrajectoryP(Kp=25),  "#2ea56a"),
        (LinearTrajectoryP(Kp=60),  "#6d5dc7"),
        (LinearTrajectoryP(Kp=150), "#c75d9b"),
    ]
    zn_runs: list[tuple[str, list[SimStep], str]] = [
        ("heuristic", hist_heur, "#d04545"),
    ]
    for strat, color in zn_strategies:
        hist = run(strat, cfg)
        _summarize(hist, f"ZN {strat.name}", cfg)
        zn_runs.append((strat.name, hist, color))

    plot_compare(
        zn_runs, cfg,
        Path(f"analyses/output/dump-sim-zn-sweep-{target_day.isoformat()}.png"),
        real_amp_trace=real_amps,
    )

    # 5. PI sweep — Kp fixed at 1 (smooth, the value that under-corrects on
    #    its own), Ki swept across {0.5, 1, 2, 4} to see how the integral
    #    term absorbs the forecast bias and brings end-SoC down to floor.
    pi_strategies = [
        (LinearTrajectoryP(Kp=1.0, Ki=0.0), "#f2a93b"),   # P-only baseline
        (LinearTrajectoryP(Kp=1.0, Ki=0.5), "#2ea56a"),
        (LinearTrajectoryP(Kp=1.0, Ki=1.0), "#6d5dc7"),
        (LinearTrajectoryP(Kp=1.0, Ki=2.0), "#c75d9b"),
        (LinearTrajectoryP(Kp=1.0, Ki=4.0), "#1f77b4"),
    ]
    pi_runs: list[tuple[str, list[SimStep], str]] = [
        ("heuristic", hist_heur, "#d04545"),
    ]
    for strat, color in pi_strategies:
        hist = run(strat, cfg)
        _summarize(hist, strat.name, cfg)
        pi_runs.append((strat.name, hist, color))

    plot_compare(
        pi_runs, cfg,
        Path(f"analyses/output/dump-sim-pi-sweep-{target_day.isoformat()}.png"),
        real_amp_trace=real_amps,
    )


def main() -> None:
    # 1. Synthetic baseline (constant load, smooth piecewise PV).
    cfg_syn = SimConfig(
        pv_w_fn=MORNING_2026_05_19_PV,
        pv_forecast_fn=MORNING_2026_05_19_PV,
    )
    hist_syn = run(HeuristicDump(), cfg_syn)
    _summarize(hist_syn, "synthetic", cfg_syn)
    plot(hist_syn, cfg_syn, HeuristicDump().name,
         Path("analyses/output/dump-sim-heuristic.png"))

    # 2-3. Real-data runs across multiple days.
    for day in (datetime.date(2026, 5, 15), datetime.date(2026, 5, 19)):
        _run_for_day(day)


if __name__ == "__main__":
    main()

"""Survey recent days of recorded PV vs Solcast forecast.

For each of the last N days with full data, plot forecast (latest p50
per period) vs recorded PV alongside, and report:

  - peak time and magnitude (forecast vs recorded)
  - daily total kWh ratio (recorded / forecast)
  - segment ratios for morning (06-09), midday (10-14), evening (15-18)

Purpose: decide whether the Solcast resource config needs tweaking
(azimuth → peak time, capacity/tilt → magnitude/shape). One sample
day can be noisy; aggregating across 5–7 clear-ish days should reveal
systematic bias.

Run: `uv run python -m analyses.pv_forecast_vs_recorded`
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from elec_auto.config import settings
from elec_auto.forecast import _interp

_TZ = ZoneInfo(settings.timezone)
DB_PATH = Path("state/samples.db")
OUT_PATH = Path("analyses/output/pv-forecast-vs-recorded.png")

# Days to survey. 2026-05-12 has partial coverage; 2026-05-11 and earlier
# are empty in this DB. 5/13 had an anomalously high morning ratio (1.85)
# and 5/14 had clouds clip the afternoon producing a misleading −99 min
# peak-time shift — both excluded so the systematic comparison isn't
# skewed by weather artifacts.
DAYS = [datetime.date(2026, 5, d) for d in range(15, 18)]

# Segment buckets (local hours) used for the bias breakdown.
SEGMENTS = [
    ("morning", 6, 10),
    ("midday",  10, 14),
    ("evening", 14, 19),
]


# Daylight bracket used for the "daily" total. 06-19 local hours cover
# every generating moment on these dates but exclude near-zero shoulder
# minutes that don't move the integrated kWh while dominating the ratio.
DAILY_START_HOUR = 6
DAILY_END_HOUR = 19


def _day_window(day: datetime.date) -> tuple[int, int]:
    s = int(datetime.datetime(day.year, day.month, day.day, 0, tzinfo=_TZ).timestamp())
    return s, s + 86400


def _daylight_window(day: datetime.date) -> tuple[int, int]:
    s = int(datetime.datetime(day.year, day.month, day.day, DAILY_START_HOUR, tzinfo=_TZ).timestamp())
    e = int(datetime.datetime(day.year, day.month, day.day, DAILY_END_HOUR, tzinfo=_TZ).timestamp())
    return s, e


def load_recorded(day: datetime.date) -> list[tuple[int, float]]:
    s, e = _day_window(day)
    con = sqlite3.connect(DB_PATH)
    return con.execute(
        "SELECT ts, solar_w FROM samples WHERE ts BETWEEN ? AND ? "
        "AND solar_w IS NOT NULL ORDER BY ts",
        (s, e),
    ).fetchall()


def load_forecast(day: datetime.date) -> list[tuple[int, float]]:
    """Latest p50 forecast per period for the day, sorted by period_ts."""
    s, e = _day_window(day)
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        """
        SELECT period_ts, MAX(fetched_at), pv_w_p50
        FROM forecasts
        WHERE period_ts BETWEEN ? AND ? AND pv_w_p50 IS NOT NULL
        GROUP BY period_ts ORDER BY period_ts
        """,
        (s, e),
    ).fetchall()
    return [(p, pv) for p, _, pv in rows]


def integrate_kwh(points: list[tuple[int, float]],
                  start_ts: int, end_ts: int) -> float:
    """Trapezoidal integration in kWh over [start_ts, end_ts] exactly.

    Uses linear interp at the boundaries so coarse-sampled inputs
    (Solcast's 30-min periods) don't silently lose up to half a period
    of energy at each edge — which would otherwise make segment ratios
    biased compared to a wider daylight ratio.
    """
    if end_ts <= start_ts or not points:
        return 0.0
    inside_ts = sorted({t for t, _ in points if start_ts <= t <= end_ts})
    knot_ts = [start_ts] + inside_ts + [end_ts]
    knot_ts = sorted(set(knot_ts))
    total = 0.0
    for i in range(1, len(knot_ts)):
        t0, t1 = knot_ts[i-1], knot_ts[i]
        w0 = _interp(points, t0)
        w1 = _interp(points, t1)
        total += (w0 + w1) / 2 * (t1 - t0) / 3600 / 1000
    return total


def peak(points: list[tuple[int, float]]) -> tuple[int, float]:
    return max(points, key=lambda p: p[1]) if points else (0, 0.0)


def main() -> None:
    n = len(DAYS)
    fig, axes = plt.subplots(n, 1, figsize=(11, 2.2 * n), sharex=False)
    if n == 1:
        axes = [axes]

    print(f"{'date':<12} {'rec_peak':>14} {'fcst_peak':>14} {'Δt(min)':>9} "
          f"{'rec_kWh':>8} {'fcst_kWh':>9} {'ratio':>6}  segments(rec/fcst)")
    for ax, day in zip(axes, DAYS):
        rec = load_recorded(day)
        fcst = load_forecast(day)
        if not rec or not fcst:
            continue

        rt, rw = peak(rec)
        ft, fw = peak(fcst)
        s, e = _daylight_window(day)
        rec_kwh = integrate_kwh(rec, s, e)
        fcst_kwh = integrate_kwh(fcst, s, e)
        ratio = rec_kwh / fcst_kwh if fcst_kwh else float("nan")
        dt_min = (rt - ft) / 60

        # Segment ratios
        seg_strs = []
        for name, h0, h1 in SEGMENTS:
            seg_s = int(datetime.datetime(day.year, day.month, day.day, h0, tzinfo=_TZ).timestamp())
            seg_e = int(datetime.datetime(day.year, day.month, day.day, h1, tzinfo=_TZ).timestamp())
            rkwh = integrate_kwh(rec, seg_s, seg_e)
            fkwh = integrate_kwh(fcst, seg_s, seg_e)
            r = rkwh / fkwh if fkwh else float("nan")
            seg_strs.append(f"{name[:3]}={r:.2f}")

        print(
            f"{day}   "
            f"{rw:>6.0f}W @ {datetime.datetime.fromtimestamp(rt,_TZ).strftime('%H:%M')}  "
            f"{fw:>6.0f}W @ {datetime.datetime.fromtimestamp(ft,_TZ).strftime('%H:%M')}  "
            f"{dt_min:>+7.0f}  "
            f"{rec_kwh:>7.1f}  {fcst_kwh:>8.1f}  {ratio:>5.2f}   "
            + " ".join(seg_strs)
        )

        rec_times = [datetime.datetime.fromtimestamp(t, _TZ) for t, _ in rec]
        rec_ws = [w for _, w in rec]
        fcst_times = [datetime.datetime.fromtimestamp(t, _TZ) for t, _ in fcst]
        fcst_ws = [w for _, w in fcst]

        ax.plot(rec_times, rec_ws, color="#2ea56a", linewidth=1.4,
                label=f"recorded ({rec_kwh:.1f} kWh)")
        ax.plot(fcst_times, fcst_ws, color="#d04545", linewidth=1.2,
                linestyle="--", alpha=0.9,
                label=f"Solcast p50 ({fcst_kwh:.1f} kWh)")
        ax.axvline(datetime.datetime.fromtimestamp(rt, _TZ),
                   color="#2ea56a", linewidth=0.7, alpha=0.4)
        ax.axvline(datetime.datetime.fromtimestamp(ft, _TZ),
                   color="#d04545", linewidth=0.7, alpha=0.4, linestyle="--")
        ax.set_title(f"{day}  ·  peak Δt = {dt_min:+.0f} min  ·  "
                     f"daily ratio = {ratio:.2f}",
                     fontsize=10, loc="left")
        ax.set_ylabel("PV (W)")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper right", fontsize=8)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=_TZ))
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
        ax.set_xlim(
            datetime.datetime(day.year, day.month, day.day, 5, tzinfo=_TZ),
            datetime.datetime(day.year, day.month, day.day, 21, tzinfo=_TZ),
        )

    axes[-1].set_xlabel("local time")
    fig.suptitle(f"Recorded vs Solcast p50 PV  ·  {DAYS[0]} → {DAYS[-1]} {_TZ.key}",
                 fontsize=11)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(OUT_PATH, dpi=110)
    print(f"\nwrote {OUT_PATH}")


if __name__ == "__main__":
    main()

"""Replay production `state.step()` with simulated PW3 outages.

Backbone is the same as the deployed control loop: feed real recorded
telemetry through `step()` exactly as the production code would. But
we *force `pw=None`* inside chosen windows to simulate PW3 outages,
so state.soc_pct dead-reckons across those gaps. The plot reveals
how much the state model would drift if PW3 went dark for hours.

Two simulated outages on 2026-05-18:

  - 05:00 - 07:00  Morning-dump heavy discharge.
  - 09:00 - 12:00  Solar charging.

These match the windows in `tests/test_replay.py`, so the plot and
the test thresholds describe the same behavior.

Three lines:
  1. Recorded PW3 SoC (truth)
  2. state.soc_pct after the replay (drifts inside outage windows,
     snaps back at the boundary)
  3. Difference (recorded − state)

Outage windows shaded so the drift is visually unambiguous.

Run: `uv run python -m analyses.replay_step`
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from elec_auto.config import settings
from elec_auto.forecast import pv_w_at
from elec_auto.powerwall import PowerReading
from elec_auto.samples import Forecast
from elec_auto.state import State, em_panel_sum, step

_TZ = ZoneInfo(settings.timezone)

WINDOW_START = datetime.datetime(2026, 5, 18, 4, 0, tzinfo=_TZ)
WINDOW_END = datetime.datetime(2026, 5, 18, 13, 0, tzinfo=_TZ)

# (start, end, label) tuples. Inside each, step() sees pw=None.
SIM_OUTAGES = [
    (
        datetime.datetime(2026, 5, 18, 5, 0, tzinfo=_TZ),
        datetime.datetime(2026, 5, 18, 7, 0, tzinfo=_TZ),
        "dump",
    ),
    (
        datetime.datetime(2026, 5, 18, 9, 0, tzinfo=_TZ),
        datetime.datetime(2026, 5, 18, 12, 0, tzinfo=_TZ),
        "charge",
    ),
]

DB_PATH = Path("state/samples.db")
OUT_PATH = Path("analyses/output") / f"{WINDOW_START.date()}-step-outage.png"

# Circuit roll-ups we never plot — they double-count.
_AGGREGATE_CIRCUITS = {"Main", "Garage Subpanel", "Balance"}
# Matplotlib's tab20-ish palette, matching the dashboard's circuit chart.
_CIRCUIT_PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#9467bd", "#8c564b",
    "#e377c2", "#7f7f7f", "#bcbd22", "#17becf", "#aec7e8",
    "#ffbb78", "#98df8a", "#ff9896", "#c5b0d5", "#c49c94", "#f7b6d2",
]
_EV_COLOR = "#d62728"        # explicit, stands apart from the rest


def load_samples(start: datetime.datetime, end: datetime.datetime):
    con = sqlite3.connect(DB_PATH)
    return con.execute(
        """
        SELECT ts, soc_pct, solar_w, load_w, battery_w, grid_w
        FROM samples
        WHERE ts BETWEEN ? AND ?
        ORDER BY ts
        """,
        (int(start.timestamp()), int(end.timestamp())),
    ).fetchall()


def load_circuits(start: datetime.datetime, end: datetime.datetime):
    """Per-circuit readings grouped into {name: [(ts, watts), ...]}."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        """
        SELECT ts, circuit, watts FROM loads
        WHERE ts BETWEEN ? AND ?
        ORDER BY ts
        """,
        (int(start.timestamp()), int(end.timestamp())),
    ).fetchall()
    by_circ: dict[str, list[tuple[int, float]]] = {}
    for ts, circuit, watts in rows:
        if circuit in _AGGREGATE_CIRCUITS:
            continue
        by_circ.setdefault(circuit, []).append((ts, watts))
    return by_circ


def load_circuits_by_ts(start: datetime.datetime, end: datetime.datetime):
    """Per-tick Emporia circuit dict (raw, including aggregate roll-ups
    so em_panel_sum can do its own exclusion)."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT ts, circuit, watts FROM loads WHERE ts BETWEEN ? AND ?",
        (int(start.timestamp()), int(end.timestamp())),
    ).fetchall()
    by_ts: dict[int, dict[str, float]] = {}
    for ts, circuit, watts in rows:
        by_ts.setdefault(ts, {})[circuit] = watts
    return by_ts


def load_forecasts(
    start: datetime.datetime, end: datetime.datetime,
) -> list[Forecast]:
    """Latest Solcast p50 PV forecast per period_ts in the window.

    Multiple `fetched_at` rows exist for each `period_ts`; we keep the
    most recently fetched value so the replay sees the forecast as it
    would have looked closest to truth at run time.
    """
    con = sqlite3.connect(DB_PATH)
    pad = 1800  # widen by one half-step so the interpolator brackets the window
    rows = con.execute(
        """
        SELECT period_ts, fetched_at, pv_w_p50
        FROM forecasts
        WHERE period_ts BETWEEN ? AND ?
          AND pv_w_p50 IS NOT NULL
        ORDER BY period_ts, fetched_at DESC
        """,
        (int(start.timestamp()) - pad, int(end.timestamp()) + pad),
    ).fetchall()
    latest: dict[int, float] = {}
    for period_ts, _fetched, pv in rows:
        if period_ts not in latest:
            latest[period_ts] = pv
    return [
        Forecast(period_ts=p, fetched_at=0, source="solcast", pv_w_p50=v)
        for p, v in sorted(latest.items())
    ]


def replay_with_outages(samples, circuits_by_ts, pv_forecasts, outages):
    """Step state forward across samples; force pw=None inside any
    outage window. Emporia readings from `circuits_by_ts` are run
    through the same `em_panel_sum` aggregator production uses, and
    the per-tick PV forecast is looked up via `pv_w_at` — so the
    inputs to step() match a real production tick (PW3 dark, Emporia
    and forecast alive).

    Returns (times, state_socs).
    """
    starts = [int(s.timestamp()) for s, _e, _l in outages]
    ends = [int(e.timestamp()) for _s, e, _l in outages]

    def in_outage(ts: int) -> bool:
        return any(s <= ts < e for s, e in zip(starts, ends))

    state = State()
    times: list[datetime.datetime] = []
    state_socs: list[float | None] = []
    for ts, soc, sw, lw, bw, gw in samples:
        if in_outage(ts) or None in (soc, sw, lw, bw, gw):
            pw = None
        else:
            pw = PowerReading(
                solar_w=sw, load_w=lw, battery_w=bw, grid_w=gw,
                battery_soc_pct=soc,
            )
        em_load_w = em_panel_sum(circuits_by_ts.get(ts))
        solar_forecast_w = pv_w_at(pv_forecasts, ts)
        state = step(state, float(ts), pw=pw, em_load_w=em_load_w,
                     solar_forecast_w=solar_forecast_w,
                     ev=None, settings=settings)
        times.append(datetime.datetime.fromtimestamp(ts, _TZ))
        state_socs.append(state.soc_pct)
    return times, state_socs


def main() -> None:
    samples = load_samples(WINDOW_START, WINDOW_END)
    if not samples:
        raise SystemExit(f"no samples in {WINDOW_START}..{WINDOW_END}")
    print(f"loaded {len(samples)} samples")

    circuits_by_ts = load_circuits_by_ts(WINDOW_START, WINDOW_END)
    pv_forecasts = load_forecasts(WINDOW_START, WINDOW_END)
    print(f"loaded {len(pv_forecasts)} forecast periods")
    times, state_socs = replay_with_outages(
        samples, circuits_by_ts, pv_forecasts, SIM_OUTAGES,
    )
    recorded_socs = [s[1] for s in samples]

    diffs: list[float | None] = []
    for rec, dr in zip(recorded_socs, state_socs):
        if rec is None or dr is None:
            diffs.append(None)
        else:
            diffs.append(rec - dr)
    valid = [d for d in diffs if d is not None]
    max_abs = max(abs(d) for d in valid) if valid else 0.0
    print(f"max |diff| = {max_abs:.3f} pp")

    circuits = load_circuits(WINDOW_START, WINDOW_END)
    # Order by peak draw, biggest first — bigger lines plotted underneath
    # so noisy small circuits don't paint over them, and the legend
    # reads top-down by significance.
    ordered = sorted(
        circuits.items(),
        key=lambda kv: max(w for _, w in kv[1]),
        reverse=True,
    )

    fig, (ax_soc, ax_diff, ax_circ) = plt.subplots(
        3, 1, sharex=True, figsize=(11, 9),
        gridspec_kw={"height_ratios": [3, 1, 3]},
    )

    for start, end, label in SIM_OUTAGES:
        for ax in (ax_soc, ax_diff, ax_circ):
            ax.axvspan(start, end, alpha=0.10, color="#c0504d", zorder=0)
        # Label inside the shaded region of the SoC panel, near the top,
        # so it doesn't clobber the figure title above the axis.
        ax_soc.text(
            start + (end - start) / 2, 0.95,
            f"simulated outage ({label})", ha="center", va="top",
            fontsize=9, color="#7a3030",
            transform=ax_soc.get_xaxis_transform(),
        )

    ax_soc.plot(times, recorded_socs, label="recorded (PW3)",
                color="#2ea56a", linewidth=1.6)
    ax_soc.plot(times, state_socs, label="state.soc_pct (replay)",
                color="#d04545", linewidth=1.2, linestyle="--", alpha=0.9)
    ax_soc.set_ylabel("SoC (displayed %)")
    ax_soc.grid(True, alpha=0.3)
    ax_soc.legend(loc="lower right")
    ax_soc.set_title(
        "SoC: recorded vs state.step() replay with simulated outages  ·  "
        f"{WINDOW_START:%Y-%m-%d %H:%M} → {WINDOW_END:%H:%M} {_TZ.key}"
    )

    ax_diff.plot(times, diffs, color="#888888", linewidth=1.0)
    ax_diff.axhline(0, color="#bbb", linewidth=0.6)
    ax_diff.set_ylabel("Δ (recorded − state)\npercentage points")
    ax_diff.grid(True, alpha=0.3)

    palette_idx = 0
    for name, pts in ordered:
        xs = [datetime.datetime.fromtimestamp(t, _TZ) for t, _ in pts]
        ys = [w for _, w in pts]
        if name == "EV Charger":
            ax_circ.plot(xs, ys, color=_EV_COLOR, linewidth=2.0,
                         label=name, zorder=10)
        else:
            ax_circ.plot(xs, ys,
                         color=_CIRCUIT_PALETTE[palette_idx % len(_CIRCUIT_PALETTE)],
                         linewidth=1.0, alpha=0.85, label=name)
            palette_idx += 1
    ax_circ.set_ylabel("circuit draw (W)")
    ax_circ.set_xlabel("time")
    ax_circ.grid(True, alpha=0.3)
    ax_circ.legend(
        loc="upper left", fontsize=7, ncol=2,
        framealpha=0.85, columnspacing=1.0, handlelength=1.5,
    )
    ax_circ.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=_TZ))
    ax_circ.xaxis.set_major_locator(mdates.HourLocator(interval=1))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(OUT_PATH, dpi=110)
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()

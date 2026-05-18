"""Replay SoC through a chosen window: measured vs. dead-reckoned.

Pulls samples from state/samples.db and answers the question: if our
state model only had the *first* recorded SoC as an anchor — and from
then on only had `battery_w` readings, never SoC — how far would the
forward-integrated estimate drift from what PW3 was actually reporting?

The integration math mirrors `state.step()` exactly (left-rectangle:
each interval uses the `battery_w` observed at its start, scaled to
displayed-% via the same `usable_kwh = capacity * (1 − raw_floor/100)`
formula). So this script is also a sanity check on `step()`'s math.

Plots three lines:
  1. Recorded PW3 SoC (the truth)
  2. Dead-reckoned SoC from the first sample onward
  3. Difference (1 − 2): drift in percentage points

Default window: midnight → noon today (local time). Override via
WINDOW_START / WINDOW_END constants below.

Run: `uv run python -m analyses.replay_soc`
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from elec_auto.config import settings

_TZ = ZoneInfo(settings.timezone)

# Window of interest. Replace these to analyze a different range.
WINDOW_START = datetime.datetime(2026, 5, 18, 0, 0, tzinfo=_TZ)
WINDOW_END = datetime.datetime(2026, 5, 18, 12, 0, tzinfo=_TZ)

DB_PATH = Path("state/samples.db")
OUT_PATH = Path("analyses/output") / f"{WINDOW_START.date()}-soc-replay.png"


def load_samples(start: datetime.datetime, end: datetime.datetime):
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        """
        SELECT ts, soc_pct, battery_w
        FROM samples
        WHERE ts BETWEEN ? AND ?
          AND soc_pct IS NOT NULL
          AND battery_w IS NOT NULL
        ORDER BY ts
        """,
        (int(start.timestamp()), int(end.timestamp())),
    ).fetchall()
    return rows  # list[(ts, soc_pct, battery_w)]


def dead_reckon(samples):
    """Left-rectangle integration of battery_w → SoC trace.

    Anchored at the first sample's recorded soc_pct. Each subsequent
    sample's dead-reckoned SoC = prev_dead + (−prev_battery_w · dt) /
    usable_kwh · 100, clamped to [0, 100].

    Returns (timestamps, dead_reckoned_socs).
    """
    j_per_kwh = 3_600_000.0
    usable_kwh = settings.battery_capacity_kwh * (
        1.0 - settings.battery_raw_floor_pct / 100.0
    )
    times: list[datetime.datetime] = []
    socs: list[float] = []

    ts0, soc0, _ = samples[0]
    times.append(datetime.datetime.fromtimestamp(ts0, _TZ))
    socs.append(soc0)
    soc = soc0
    prev_ts, _, prev_battery_w = samples[0]

    for ts, _, battery_w in samples[1:]:
        dt = ts - prev_ts
        delta_pct = -prev_battery_w * dt / j_per_kwh / usable_kwh * 100.0
        soc = max(0.0, min(100.0, soc + delta_pct))
        times.append(datetime.datetime.fromtimestamp(ts, _TZ))
        socs.append(soc)
        prev_ts, prev_battery_w = ts, battery_w
    return times, socs


def main() -> None:
    samples = load_samples(WINDOW_START, WINDOW_END)
    if not samples:
        raise SystemExit(f"no samples in {WINDOW_START}..{WINDOW_END}")
    print(f"loaded {len(samples)} samples")

    recorded_times = [datetime.datetime.fromtimestamp(s[0], _TZ) for s in samples]
    recorded_socs = [s[1] for s in samples]

    dr_times, dr_socs = dead_reckon(samples)
    diffs = [r - d for r, d in zip(recorded_socs, dr_socs)]
    max_abs_diff = max(abs(d) for d in diffs)
    final_diff = diffs[-1]
    print(
        f"max |diff| = {max_abs_diff:.2f} pp, "
        f"final diff = {final_diff:+.2f} pp",
    )

    fig, (ax_soc, ax_diff) = plt.subplots(
        2, 1, sharex=True, figsize=(11, 6),
        gridspec_kw={"height_ratios": [3, 1]},
    )

    ax_soc.plot(recorded_times, recorded_socs, label="recorded (PW3)",
                color="#2ea56a", linewidth=1.6)
    ax_soc.plot(dr_times, dr_socs, label="state (dead-reckoned)",
                color="#d04545", linewidth=1.4, linestyle="--")
    ax_soc.set_ylabel("SoC (displayed %)")
    ax_soc.set_ylim(0, 105)
    ax_soc.grid(True, alpha=0.3)
    ax_soc.legend(loc="upper right")
    ax_soc.set_title(
        f"SoC: recorded vs dead-reckoned  ·  "
        f"{WINDOW_START:%Y-%m-%d %H:%M} → {WINDOW_END:%H:%M} {_TZ.key}"
    )

    ax_diff.plot(recorded_times, diffs, color="#888888", linewidth=1.2)
    ax_diff.axhline(0, color="#bbb", linewidth=0.6)
    ax_diff.set_ylabel("Δ (recorded − DR)\npercentage points")
    ax_diff.set_xlabel("time")
    ax_diff.grid(True, alpha=0.3)
    ax_diff.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=_TZ))
    ax_diff.xaxis.set_major_locator(mdates.HourLocator(interval=1))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(OUT_PATH, dpi=110)
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()

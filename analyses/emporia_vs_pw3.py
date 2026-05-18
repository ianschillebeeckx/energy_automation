"""Compare Emporia panel sum to PW3-reported load_w.

The energy-balance derivation in `state.step()` substitutes
`em_panel_sum(emporia_circuits)` for PW3's `load_w` when the gateway
is dark. That substitution is only as good as Emporia's panel
coverage — circuits that aren't monitored (e.g., HVAC condenser, washer)
show up as a systematic undercount.

This plot makes the gap visible on real recorded data:

  Top axis:   PW3 load_w and Emporia panel sum overlaid.
  Bottom:    Δ = PW3 − Emporia (positive = Emporia underreports).

Run: `uv run python -m analyses.emporia_vs_pw3`
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from elec_auto.config import settings
from elec_auto.state import em_panel_sum

_TZ = ZoneInfo(settings.timezone)

WINDOW_START = datetime.datetime(2026, 5, 18, 4, 0, tzinfo=_TZ)
WINDOW_END = datetime.datetime(2026, 5, 18, 13, 0, tzinfo=_TZ)

DB_PATH = Path("state/samples.db")
OUT_PATH = Path("analyses/output") / f"{WINDOW_START.date()}-emporia-vs-pw3.png"


def load_pw_load(start: datetime.datetime, end: datetime.datetime):
    con = sqlite3.connect(DB_PATH)
    return con.execute(
        "SELECT ts, load_w FROM samples WHERE ts BETWEEN ? AND ? "
        "AND load_w IS NOT NULL ORDER BY ts",
        (int(start.timestamp()), int(end.timestamp())),
    ).fetchall()


def load_emporia_by_ts(start: datetime.datetime, end: datetime.datetime):
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT ts, circuit, watts FROM loads WHERE ts BETWEEN ? AND ?",
        (int(start.timestamp()), int(end.timestamp())),
    ).fetchall()
    by_ts: dict[int, dict[str, float]] = {}
    for ts, circuit, watts in rows:
        by_ts.setdefault(ts, {})[circuit] = watts
    return by_ts


def main() -> None:
    pw_rows = load_pw_load(WINDOW_START, WINDOW_END)
    em_by_ts = load_emporia_by_ts(WINDOW_START, WINDOW_END)
    if not pw_rows:
        raise SystemExit(f"no PW3 load samples in {WINDOW_START}..{WINDOW_END}")

    # Align on PW3 ticks; em_panel_sum returns None when nothing is reported
    # for that ts (Emporia ticks are independent of PW3 ticks in production).
    pw_times: list[datetime.datetime] = []
    pw_load: list[float] = []
    em_load: list[float | None] = []
    diff: list[float | None] = []
    for ts, lw in pw_rows:
        pw_times.append(datetime.datetime.fromtimestamp(ts, _TZ))
        pw_load.append(float(lw))
        e = em_panel_sum(em_by_ts.get(ts))
        em_load.append(e)
        diff.append(float(lw) - e if e is not None else None)

    aligned = [(p, e, d) for p, e, d in zip(pw_load, em_load, diff)
               if e is not None]
    if aligned:
        diffs_valid = [d for _, _, d in aligned]
        mean_diff = sum(diffs_valid) / len(diffs_valid)
        max_diff = max(diffs_valid)
        min_diff = min(diffs_valid)
        print(f"aligned ticks: {len(aligned)}")
        print(f"PW3 − Emporia: mean={mean_diff:+.0f} W  "
              f"min={min_diff:+.0f}  max={max_diff:+.0f}")

    fig, (ax_load, ax_diff) = plt.subplots(
        2, 1, sharex=True, figsize=(11, 6),
        gridspec_kw={"height_ratios": [3, 1]},
    )

    ax_load.plot(pw_times, pw_load, label="PW3 load_w",
                 color="#2ea56a", linewidth=1.4)
    ax_load.plot(pw_times, em_load, label="Emporia panel sum",
                 color="#d04545", linewidth=1.2, linestyle="--", alpha=0.9)
    ax_load.set_ylabel("load (W)")
    ax_load.grid(True, alpha=0.3)
    ax_load.legend(loc="upper left")
    ax_load.set_title(
        "PW3 load_w vs Emporia panel sum  ·  "
        f"{WINDOW_START:%Y-%m-%d %H:%M} → {WINDOW_END:%H:%M} {_TZ.key}"
    )

    ax_diff.plot(pw_times, diff, color="#888888", linewidth=1.0)
    ax_diff.axhline(0, color="#bbb", linewidth=0.6)
    ax_diff.set_ylabel("Δ = PW3 − Emporia\n(W; +ve = Emporia undercount)")
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

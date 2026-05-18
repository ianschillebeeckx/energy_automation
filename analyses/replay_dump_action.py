"""Replay Controller.tick() across a recorded morning + dump window.

Same idea as `replay_step.py` (feed recorded telemetry through
production code), but pulls in the *full* control pipeline — state +
action selection — so we can see what `Decision` MorningDump would
have produced at each tick. The recorded EV-charger circuit gives a
ground-truth comparison: how do simulated amps compare to the amps
the EV actually drew that morning?

Three subplots:
  1. SoC: recorded PW3 vs state.soc_pct from the replay.
  2. PV: recorded solar_w vs Solcast p50 (interpolated to each tick).
  3. EV amps: simulated Decision.target_amps (filled, per-action color)
     vs the EV charger's recorded draw, converted to amps via
     `ev_voltage` from Settings.

Window: 2026-05-18 04:00 - 09:00 local — covers pre-dump idle, the
full 05:00-08:00 dump window (which fired on production), and the
hand-off to surplus afterwards.

Run: `uv run python -m analyses.replay_dump_action`
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt

from elec_auto.config import settings
from elec_auto.control import Controller
from elec_auto.emporia import ChargerState
from elec_auto.powerwall import PowerReading
from elec_auto.samples import Forecast, LoadStore, SampleStore
from elec_auto.state import em_panel_sum

_TZ = ZoneInfo(settings.timezone)

WINDOW_START = datetime.datetime(2026, 5, 15, 4, 0, tzinfo=_TZ)
WINDOW_END = datetime.datetime(2026, 5, 15, 9, 0, tzinfo=_TZ)

DB_PATH = Path("state/samples.db")
OUT_PATH = Path("analyses/output") / f"{WINDOW_START.date()}-dump-action.png"


def load_samples(start: datetime.datetime, end: datetime.datetime):
    con = sqlite3.connect(DB_PATH)
    return con.execute(
        "SELECT ts, soc_pct, solar_w, load_w, battery_w, grid_w "
        "FROM samples WHERE ts BETWEEN ? AND ? ORDER BY ts",
        (int(start.timestamp()), int(end.timestamp())),
    ).fetchall()


def load_circuits_by_ts(start: datetime.datetime, end: datetime.datetime):
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT ts, circuit, watts FROM loads WHERE ts BETWEEN ? AND ?",
        (int(start.timestamp()), int(end.timestamp())),
    ).fetchall()
    by_ts: dict[int, dict[str, float]] = {}
    for ts, circuit, watts in rows:
        by_ts.setdefault(ts, {})[circuit] = watts
    return by_ts


def load_ev_recorded(start: datetime.datetime, end: datetime.datetime):
    """EV Charger circuit watts per tick (recorded reality)."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT ts, watts FROM loads WHERE ts BETWEEN ? AND ? "
        "AND circuit = 'EV Charger' ORDER BY ts",
        (int(start.timestamp()), int(end.timestamp())),
    ).fetchall()
    return rows


def load_forecasts(
    start: datetime.datetime, end: datetime.datetime,
) -> list[Forecast]:
    """Latest p50 forecast per period — what production would have
    seen for each period after the day finished."""
    con = sqlite3.connect(DB_PATH)
    pad = 86400  # widen so morning-dump's sunny-day full-day kWh check works
    rows = con.execute(
        """
        SELECT period_ts, MAX(fetched_at), pv_w_p50
        FROM forecasts
        WHERE period_ts BETWEEN ? AND ? AND pv_w_p50 IS NOT NULL
        GROUP BY period_ts ORDER BY period_ts
        """,
        (int(start.timestamp()) - pad, int(end.timestamp()) + pad),
    ).fetchall()
    return [
        Forecast(period_ts=p, fetched_at=0, source="solcast", pv_w_p50=v)
        for p, _, v in rows
    ]


_ACTION_COLORS = {
    "morning_dump": "#d04545",
    "surplus":      "#2ea56a",
    "none":         "#888888",
    "kill_switch":  "#222222",
}


def main() -> None:
    samples = load_samples(WINDOW_START, WINDOW_END)
    if not samples:
        raise SystemExit(f"no samples in {WINDOW_START}..{WINDOW_END}")
    print(f"loaded {len(samples)} samples")

    circuits_by_ts = load_circuits_by_ts(WINDOW_START, WINDOW_END)
    ev_recorded = load_ev_recorded(WINDOW_START, WINDOW_END)
    pv_forecasts = load_forecasts(WINDOW_START, WINDOW_END)
    print(f"loaded {len(pv_forecasts)} forecast periods, "
          f"{len(ev_recorded)} EV-circuit readings")

    sample_store = SampleStore(DB_PATH)
    load_store = LoadStore(DB_PATH)

    ctl = Controller(settings)

    times: list[datetime.datetime] = []
    state_socs: list[float | None] = []
    fcst_pv_w: list[float | None] = []
    target_amps: list[int] = []
    action_names: list[str] = []
    on_flags: list[bool] = []
    reasons: list[str] = []

    from elec_auto.forecast import pv_w_at
    for ts, soc, sw, lw, bw, gw in samples:
        if None in (soc, sw, lw, bw, gw):
            pw = None
        else:
            pw = PowerReading(
                solar_w=sw, load_w=lw, battery_w=bw, grid_w=gw,
                battery_soc_pct=soc,
            )
        em_load_w = em_panel_sum(circuits_by_ts.get(ts))
        now = datetime.datetime.fromtimestamp(ts, _TZ)
        decision = ctl.tick(
            now,
            pw=pw,
            em_load_w=em_load_w,
            ev=None,  # we don't reconstruct EV ChargerState per tick — dump action doesn't read it
            pv_forecasts=pv_forecasts,
            sample_store=sample_store,
            load_store=load_store,
        )
        times.append(now)
        state_socs.append(ctl.state.soc_pct)
        fcst_pv_w.append(pv_w_at(pv_forecasts, ts))
        target_amps.append(decision.target_amps)
        action_names.append(decision.action_name or "none")
        on_flags.append(decision.on)
        reasons.append(decision.reason)

    # Pull a few interesting decisions for the console summary.
    dump_decisions = [
        (t, a, on, r) for t, a, on, r, name in
        zip(times, target_amps, on_flags, reasons, action_names)
        if name == "morning_dump"
    ]
    if dump_decisions:
        first_on = next(((t, a, r) for t, a, on, r in dump_decisions if on), None)
        last_on = next(((t, a, r) for t, a, on, r in reversed(dump_decisions) if on), None)
        amps_when_on = [a for _, a, on, _ in dump_decisions if on]
        print(f"\nmorning_dump fired in {len(dump_decisions)} ticks, "
              f"{sum(1 for _,_,on,_ in dump_decisions if on)} with on=True")
        if first_on:
            print(f"  first firing: {first_on[0].strftime('%H:%M:%S')}  "
                  f"{first_on[1]} A  · {first_on[2]}")
        if last_on:
            print(f"  last  firing: {last_on[0].strftime('%H:%M:%S')}  "
                  f"{last_on[1]} A  · {last_on[2]}")
        if amps_when_on:
            print(f"  amps min/median/max: "
                  f"{min(amps_when_on)} / "
                  f"{sorted(amps_when_on)[len(amps_when_on)//2]} / "
                  f"{max(amps_when_on)}")

    fig, (ax_soc, ax_pv, ax_amp) = plt.subplots(
        3, 1, sharex=True, figsize=(11, 8.5),
        gridspec_kw={"height_ratios": [2, 2, 3]},
    )

    # Shade the dump window across all axes.
    dump_start = WINDOW_START.replace(
        hour=settings.morning_dump_start_hour,
        minute=settings.morning_dump_start_minute,
    )
    dump_end = WINDOW_START.replace(
        hour=settings.morning_dump_end_hour,
        minute=settings.morning_dump_end_minute,
    )
    for ax in (ax_soc, ax_pv, ax_amp):
        ax.axvspan(dump_start, dump_end, alpha=0.06, color="#c0504d", zorder=0)
    ax_soc.text(
        dump_start + (dump_end - dump_start) / 2, 0.95,
        f"dump window {dump_start:%H:%M}-{dump_end:%H:%M}",
        ha="center", va="top", fontsize=9, color="#7a3030",
        transform=ax_soc.get_xaxis_transform(),
    )

    # 1. SoC
    rec_socs = [s[1] for s in samples]
    ax_soc.plot(times, rec_socs, color="#2ea56a", linewidth=1.6,
                label="recorded SoC")
    ax_soc.plot(times, state_socs, color="#d04545", linewidth=1.2,
                linestyle="--", alpha=0.9, label="state.soc_pct")
    ax_soc.set_ylabel("SoC (%)")
    ax_soc.grid(True, alpha=0.3)
    ax_soc.legend(loc="lower right")
    ax_soc.set_title(
        "Controller.tick() replay — morning + dump window  ·  "
        f"{WINDOW_START:%Y-%m-%d %H:%M} → {WINDOW_END:%H:%M} {_TZ.key}"
    )

    # 2. PV
    rec_solar = [s[2] for s in samples]
    ax_pv.plot(times, rec_solar, color="#2ea56a", linewidth=1.6,
               label="recorded solar_w")
    ax_pv.plot(times, fcst_pv_w, color="#d04545", linewidth=1.2,
               linestyle="--", alpha=0.9, label="Solcast p50 forecast")
    ax_pv.set_ylabel("PV (W)")
    ax_pv.grid(True, alpha=0.3)
    ax_pv.legend(loc="upper left")

    # 3. Amperage decision + recorded EV draw
    ev_times = [datetime.datetime.fromtimestamp(t, _TZ) for t, _ in ev_recorded]
    ev_amps_recorded = [w / settings.ev_voltage for _, w in ev_recorded]

    # Color the decision trace by action_name so dump vs surplus vs none
    # are visually distinct. Draw step-style (decisions hold until next tick).
    for i in range(len(times) - 1):
        color = _ACTION_COLORS.get(action_names[i], "#888888")
        amps = target_amps[i] if on_flags[i] else 0
        ax_amp.fill_between(
            [times[i], times[i + 1]], 0, amps,
            color=color, alpha=0.55, linewidth=0,
        )
    ax_amp.plot(ev_times, ev_amps_recorded, color="#1f77b4", linewidth=1.5,
                label=f"EV recorded ({settings.ev_voltage} V)")

    # Build legend with action-color patches.
    handles = [
        mpatches.Patch(color=_ACTION_COLORS["morning_dump"], alpha=0.55,
                       label="morning_dump"),
        mpatches.Patch(color=_ACTION_COLORS["surplus"], alpha=0.55,
                       label="surplus"),
        mpatches.Patch(color=_ACTION_COLORS["none"], alpha=0.55,
                       label="none"),
        plt.Line2D([0], [0], color="#1f77b4", lw=1.5,
                   label=f"EV recorded ({settings.ev_voltage} V)"),
    ]
    ax_amp.legend(handles=handles, loc="upper left", fontsize=9, ncol=2)
    ax_amp.set_ylabel("amps")
    ax_amp.set_xlabel("time")
    ax_amp.grid(True, alpha=0.3)
    ax_amp.set_ylim(bottom=0)
    ax_amp.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=_TZ))
    ax_amp.xaxis.set_major_locator(mdates.HourLocator(interval=1))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(OUT_PATH, dpi=110)
    print(f"\nwrote {OUT_PATH}")


if __name__ == "__main__":
    main()

"""Production replay with simulated PW3 outages.

Backbone is the same as `analyses/replay_step.py` — feed real recorded
telemetry through `state.step()` exactly as the deployed code would.
But here we *force `pw=None`* inside a chosen window to simulate an
outage. State.soc_pct dead-reckons across the simulated gap, and we
compare it to the recorded SoC (which we hid from step()) to measure
how far the model would have drifted.

Two test windows from 2026-05-18 (frozen in
`tests/fixtures/replay_2026_05_18.py`, NORMAL_2026_05_18):

  1. 05:00 - 07:00  Morning-dump period. ~kW of sustained discharge —
                    high battery_w, drift accumulates fast.
  2. 09:00 - 12:00  Solar-charging period. Strong charge rates pulling
                    SoC up; drift in the other direction.

Each test asserts:
  A. Max per-tick |drift| stays under MAX_PER_TICK_PP.
  B. Mean per-tick |drift| stays under MEAN_PP.

If either fires, either step()'s integration regressed or the
underlying battery efficiency has shifted enough that we should
start modeling it.
"""

from __future__ import annotations

from elec_auto.config import Settings
from elec_auto.powerwall import PowerReading
from elec_auto.state import State, em_panel_sum, step

from .fixtures.replay_2026_05_18 import (
    NORMAL_2026_05_18, NORMAL_CIRCUITS_2026_05_18,
    NORMAL_SOLAR_FORECAST_W_2026_05_18,
)


def _settings() -> Settings:
    return Settings(  # type: ignore[arg-type]
        _env_file=None,
        battery_capacity_kwh=13.5,
        battery_raw_floor_pct=5.0,
        telemetry_fresh_sec=60,
    )


def replay_with_simulated_outage(
    samples,
    circuits_by_ts: dict[int, dict[str, float]],
    solar_forecast_by_ts: dict[int, float | None],
    outage_start_ts: int,
    outage_end_ts: int,
    settings: Settings,
) -> list[tuple[int, float, float]]:
    """Run production `step()` across `samples` with pw=None forced
    inside `[outage_start_ts, outage_end_ts)`.

    Emporia readings come from `circuits_by_ts` and get aggregated by
    the same `em_panel_sum` helper production calls. Per-tick PV
    forecast (latest Solcast p50 interpolated to the tick's ts, the
    same value `Controller.tick` would supply via `pv_w_at`) comes
    from `solar_forecast_by_ts`. So the inputs to step() match a real
    production tick where PW3 is dark but Emporia and the forecast
    are alive — the realistic outage shape.

    Returns one (ts, state_soc, recorded_soc) tuple for each tick
    inside the simulated outage where the recorded SoC exists; the
    recorded value is the truth that step() didn't get to see.
    """
    state = State()
    out: list[tuple[int, float, float]] = []
    for ts, soc, sw, lw, bw, gw in samples:
        in_outage = outage_start_ts <= ts < outage_end_ts
        pw: PowerReading | None
        if in_outage or None in (soc, sw, lw, bw, gw):
            pw = None
        else:
            pw = PowerReading(
                solar_w=sw, load_w=lw, battery_w=bw, grid_w=gw,
                battery_soc_pct=soc,
            )
        em_load_w = em_panel_sum(circuits_by_ts.get(ts))
        solar_forecast_w = solar_forecast_by_ts.get(ts)
        state = step(state, float(ts), pw=pw, em_load_w=em_load_w,
                     solar_forecast_w=solar_forecast_w,
                     ev=None, settings=settings)
        if in_outage and soc is not None and state.soc_pct is not None:
            out.append((ts, state.soc_pct, float(soc)))
    return out


def _errors(replayed: list[tuple[int, float, float]]) -> list[float]:
    return [abs(state_soc - recorded) for _, state_soc, recorded in replayed]


def _ts(h: int, m: int = 0) -> int:
    # 2026-05-18 at the given hour:minute PDT (UTC-7).
    import datetime
    from zoneinfo import ZoneInfo
    return int(
        datetime.datetime(
            2026, 5, 18, h, m, tzinfo=ZoneInfo("America/Los_Angeles"),
        ).timestamp()
    )


# ---- thresholds, calibrated to observed behavior --------------------------

# TODO: the 05:00-07:00 window mixes two distinct stressors — sustained
# discharge before sunrise, and the steep PV-forecast ramp between ~06:00
# and 07:00. Split into two separate test cases on windows that isolate
# each: one with sustained heavy draw and no PV in the outage, one
# centered on the sunrise ramp without big load events. Replay-day TBD.

# Window 1: 05:00 - 07:00 (~2 h dark across morning-dump discharge).
# Observed on 2026-05-18 real data: max 2.22 pp, mean 0.82 pp. Drift is
# dominated by the sunrise PV-forecast over-shoot during the 06:00-07:00
# ramp (Solcast interp between 30-min periods can over-predict mid-ramp).
DUMP_OUTAGE_MAX_PER_TICK_PP = 2.5
DUMP_OUTAGE_MEAN_PP = 1.0

# Window 2: 09:00 - 12:00 (~3 h dark across solar charging).
# Observed on 2026-05-18 real data: max 2.08 pp, mean 0.28 pp.
CHARGE_OUTAGE_MAX_PER_TICK_PP = 2.2
CHARGE_OUTAGE_MEAN_PP = 0.5


def test_simulated_outage_during_morning_dump() -> None:
    replayed = replay_with_simulated_outage(
        NORMAL_2026_05_18, NORMAL_CIRCUITS_2026_05_18,
        NORMAL_SOLAR_FORECAST_W_2026_05_18,
        _ts(5), _ts(7), _settings(),
    )
    assert replayed, "expected at least one comparison tick inside the outage"
    errs = _errors(replayed)
    max_err = max(errs)
    mean_err = sum(errs) / len(errs)
    assert max_err <= DUMP_OUTAGE_MAX_PER_TICK_PP, (
        f"max per-tick drift {max_err:.3f} pp exceeded "
        f"{DUMP_OUTAGE_MAX_PER_TICK_PP} pp over {len(errs)} ticks"
    )
    assert mean_err <= DUMP_OUTAGE_MEAN_PP, (
        f"mean drift {mean_err:.3f} pp exceeded {DUMP_OUTAGE_MEAN_PP} pp"
    )


def test_simulated_outage_during_solar_charging() -> None:
    replayed = replay_with_simulated_outage(
        NORMAL_2026_05_18, NORMAL_CIRCUITS_2026_05_18,
        NORMAL_SOLAR_FORECAST_W_2026_05_18,
        _ts(9), _ts(12), _settings(),
    )
    assert replayed, "expected at least one comparison tick inside the outage"
    errs = _errors(replayed)
    max_err = max(errs)
    mean_err = sum(errs) / len(errs)
    assert max_err <= CHARGE_OUTAGE_MAX_PER_TICK_PP, (
        f"max per-tick drift {max_err:.3f} pp exceeded "
        f"{CHARGE_OUTAGE_MAX_PER_TICK_PP} pp over {len(errs)} ticks"
    )
    assert mean_err <= CHARGE_OUTAGE_MEAN_PP, (
        f"mean drift {mean_err:.3f} pp exceeded {CHARGE_OUTAGE_MEAN_PP} pp"
    )

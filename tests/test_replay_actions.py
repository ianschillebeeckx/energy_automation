"""Per-tick replay/synthetic Controller tests.

Walks production `Controller.tick()` over recorded fixtures and synthetic
inputs, asserting per-tick charging behavior. Style mirrors
`tests/test_scenarios.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from elec_auto.config import Settings
from elec_auto.control import Controller
from elec_auto.emporia import ChargerState
from elec_auto.powerwall import PowerReading
from elec_auto.samples import Forecast
from elec_auto.state import em_panel_sum

from .fixtures.replay_2026_05_17_dump import (
    DUMP_2026_05_17,
    DUMP_2026_05_17_CIRCUITS,
    DUMP_2026_05_17_FORECASTS,
)
from .fixtures.replay_2026_05_17_surplus import (
    SURPLUS_2026_05_17,
    SURPLUS_2026_05_17_CIRCUITS,
    SURPLUS_2026_05_17_FORECASTS,
)

_TZ = ZoneInfo("America/Los_Angeles")


# --- helpers (verbatim from tests/test_scenarios.py) ------------------------


def _settings(**overrides) -> Settings:
    defaults = dict(
        battery_reserve_pct=80,
        ev_min_amps=6,
        ev_max_amps=40,
        ev_voltage=240,
        battery_capacity_kwh=13.5,
        morning_dump_floor_pct=10,
        morning_dump_sunny_floor_pct=5,
        morning_dump_sunny_threshold_kwh=30.0,
        morning_dump_pv_credit_pct=90.0,
        morning_dump_max_amps=29,
        morning_dump_start_hour=5,
        morning_dump_start_minute=0,
        morning_dump_end_hour=8,
        morning_dump_end_minute=0,
        surplus_enabled=True,
        morning_dump_enabled=True,
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type]


def _pw(
    *,
    soc: float = 80.0,
    solar: float = 0.0,
    load: float = 400.0,
    battery: float = 0.0,
    grid: float = 0.0,
) -> PowerReading:
    return PowerReading(
        solar_w=solar,
        load_w=load,
        battery_w=battery,
        grid_w=grid,
        battery_soc_pct=soc,
    )


def _ev(
    *,
    on: bool = False,
    amps: int = 0,
    status: str = "Standby",
    gid: int = 1,
    name: str = "EV Charger",
    max_amps: int = 40,
) -> ChargerState:
    return ChargerState(
        gid=gid,
        name=name,
        on=on,
        charge_rate_a=amps,
        max_charge_rate_a=max_amps,
        status=status,
    )


def _const_pv(
    start: datetime, end: datetime, watts: float,
) -> list[Forecast]:
    out: list[Forecast] = []
    t = int(start.timestamp())
    end_ts = int(end.timestamp())
    fetched_at = int(start.timestamp())
    while t <= end_ts:
        out.append(Forecast(
            period_ts=t, fetched_at=fetched_at, source="test",
            pv_w_p50=watts,
        ))
        t += 1800
    return out


# --- Test 1: replay dump 2026-05-17 -----------------------------------------


def test_morning_dump_charges_continuously_on_2026_05_17() -> None:
    s = _settings()
    ctl = Controller(s)

    window_start = datetime(2026, 5, 17, 5, 0, tzinfo=_TZ)
    window_end = datetime(2026, 5, 17, 8, 0, tzinfo=_TZ)

    for ts, soc, solar_w, load_w, battery_w, grid_w in DUMP_2026_05_17:
        now = datetime.fromtimestamp(ts, _TZ)
        if all(v is not None for v in (soc, solar_w, load_w, battery_w, grid_w)):
            pw = _pw(
                soc=soc, solar=solar_w, load=load_w,
                battery=battery_w, grid=grid_w,
            )
        else:
            pw = None
        em_load_w = em_panel_sum(DUMP_2026_05_17_CIRCUITS.get(ts))
        d = ctl.tick(
            now,
            pw=pw,
            em_load_w=em_load_w,
            ev=_ev(on=False, amps=0),
            pv_forecasts=DUMP_2026_05_17_FORECASTS,
        )
        if window_start <= now < window_end:
            assert (
                d.action_name == "morning_dump"
                and d.on is True
                and d.target_amps >= 6
            ), (
                f"dump should fire at {now.strftime('%H:%M:%S')}: "
                f"soc={ctl.state.soc_pct} action={d.action_name} "
                f"on={d.on} target_amps={d.target_amps} reason={d.reason!r}"
            )


# --- Test 2: synthetic dump drains to floor ---------------------------------


def test_morning_dump_stops_when_soc_hits_floor() -> None:
    s = _settings()
    ctl = Controller(s)

    day = datetime(2026, 5, 14, 0, 0, tzinfo=_TZ)
    forecasts = _const_pv(
        day.replace(hour=4), day.replace(hour=9), 1500.0,
    )

    start = day.replace(hour=5, minute=0)
    drain_end = day.replace(hour=6, minute=30)
    window_end = day.replace(hour=8, minute=0)

    # 5-minute cadence, 05:00 through 08:00 inclusive = 37 ticks.
    for i in range(37):
        now = start + timedelta(minutes=5 * i)
        # Linear interp 40% -> 10% across 05:00 -> 06:30, then 10%.
        if now <= drain_end:
            frac = (now - start).total_seconds() / (
                (drain_end - start).total_seconds()
            )
            soc = 40.0 - 30.0 * frac
        else:
            soc = 10.0
        d = ctl.tick(
            now,
            pw=_pw(soc=soc, solar=0, load=400, battery=-1500, grid=1100),
            em_load_w=400,
            ev=_ev(on=False, amps=0),
            pv_forecasts=forecasts,
        )
        if now < drain_end:
            assert (
                d.action_name == "morning_dump"
                and d.on is True
                and d.target_amps >= 6
            ), (
                f"dump should fire at {now.strftime('%H:%M:%S')}: "
                f"soc_in={soc:.2f} soc_state={ctl.state.soc_pct} "
                f"action={d.action_name} on={d.on} "
                f"target_amps={d.target_amps} reason={d.reason!r}"
            )
        else:
            assert (
                d.action_name == "none"
                and d.on is False
                and d.target_amps == 0
            ), (
                f"dump should be stopped at {now.strftime('%H:%M:%S')}: "
                f"soc_in={soc:.2f} soc_state={ctl.state.soc_pct} "
                f"action={d.action_name} on={d.on} "
                f"target_amps={d.target_amps} reason={d.reason!r}"
            )
        assert now <= window_end


# --- Test 3: synthetic SoC below floor never fires --------------------------


def test_morning_dump_never_fires_when_below_floor() -> None:
    s = _settings()
    ctl = Controller(s)

    day = datetime(2026, 5, 14, 0, 0, tzinfo=_TZ)
    forecasts = _const_pv(
        day.replace(hour=4), day.replace(hour=9), 200.0,
    )

    start = day.replace(hour=5, minute=0)
    for i in range(37):
        now = start + timedelta(minutes=5 * i)
        d = ctl.tick(
            now,
            pw=_pw(soc=9, solar=0, load=400, battery=-100, grid=-300),
            em_load_w=400,
            ev=_ev(on=False, amps=0),
            pv_forecasts=forecasts,
        )
        assert (
            d.action_name == "none"
            and d.on is False
            and d.target_amps == 0
        ), (
            f"no action should fire at {now.strftime('%H:%M:%S')}: "
            f"soc_state={ctl.state.soc_pct} "
            f"action={d.action_name} on={d.on} "
            f"target_amps={d.target_amps} reason={d.reason!r}"
        )


# --- Test 4: replay surplus 2026-05-17 --------------------------------------


def test_surplus_charges_on_2026_05_17() -> None:
    s = _settings()
    ctl = Controller(s)

    fired = 0
    for ts, soc, solar_w, load_w, battery_w, grid_w in SURPLUS_2026_05_17:
        now = datetime.fromtimestamp(ts, _TZ)
        if all(v is not None for v in (soc, solar_w, load_w, battery_w, grid_w)):
            pw = _pw(
                soc=soc, solar=solar_w, load=load_w,
                battery=battery_w, grid=grid_w,
            )
        else:
            pw = None
        em_load_w = em_panel_sum(SURPLUS_2026_05_17_CIRCUITS.get(ts))
        d = ctl.tick(
            now,
            pw=pw,
            em_load_w=em_load_w,
            ev=_ev(on=False, amps=0),
            pv_forecasts=SURPLUS_2026_05_17_FORECASTS,
        )
        st = ctl.state
        # Merged Surplus applies whenever solar > 0 and we're out of the
        # dump window. Whether it *charges* is determined inside
        # decide(): SoC below threshold → battery-first hold; SoC at/
        # above threshold with enough net surplus → EV on.
        surplus_applies = (
            st.solar_w is not None
            and st.solar_w > 0
            and st.load_w is not None
        )
        soc_above_threshold = (
            st.soc_pct is not None and st.soc_pct >= 80
        )
        # Net surplus must clear the min-amps bar to actually charge.
        min_w = s.ev_min_amps * s.ev_voltage
        net_surplus_w = (
            (st.solar_w or 0) - (st.load_w or 0)
            if surplus_applies else 0.0
        )
        if surplus_applies and soc_above_threshold and net_surplus_w >= min_w:
            assert (
                d.action_name == "surplus"
                and d.on is True
                and d.target_amps >= 6
            ), (
                f"surplus should fire at {now.strftime('%H:%M:%S')}: "
                f"soc={st.soc_pct} solar={st.solar_w} load={st.load_w} "
                f"action={d.action_name} on={d.on} "
                f"target_amps={d.target_amps} reason={d.reason!r}"
            )
            fired += 1
        elif surplus_applies:
            # Action applies but decide holds — either battery-first
            # (below threshold) or below-min surplus. Both look like
            # Decision(0, off, name=surplus).
            assert d.on is False and d.target_amps == 0, (
                f"surplus should hold at {now.strftime('%H:%M:%S')}: "
                f"soc={st.soc_pct} solar={st.solar_w} load={st.load_w} "
                f"action={d.action_name} on={d.on} "
                f"target_amps={d.target_amps} reason={d.reason!r}"
            )
        else:
            assert d.on is False and d.action_name in ("none", ""), (
                f"nothing should fire at {now.strftime('%H:%M:%S')}: "
                f"soc={st.soc_pct} solar={st.solar_w} load={st.load_w} "
                f"action={d.action_name} on={d.on} "
                f"target_amps={d.target_amps} reason={d.reason!r}"
            )

    assert fired >= 30, f"expected >= 30 surplus firings, got {fired}"

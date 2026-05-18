"""End-to-end Controller scenarios.

Each test constructs a fresh `Controller(settings)` and walks it through a
sequence of ticks with explicit `now` and telemetry, asserting the
returned `Decision` at each step. Style mirrors `tests/test_actions.py`:
helpers at the top, timezone-aware datetimes pinned to
America/Los_Angeles, no fixtures module.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from elec_auto.config import Settings
from elec_auto.control import Controller
from elec_auto.emporia import ChargerState
from elec_auto.policy import Decision
from elec_auto.powerwall import PowerReading
from elec_auto.samples import Forecast

_TZ = ZoneInfo("America/Los_Angeles")


# --- helpers (verbatim from tests/test_actions.py where applicable) ---------


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
        trickle_kw=2.0,
        surplus_enabled=True,
        morning_dump_enabled=True,
        trickle_enabled=False,
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
    """Synthetic flat-PV forecast at 30-min cadence covering [start, end]."""
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


# --- Scenario 1: A day in the life ------------------------------------------


def test_scenario_1_day_in_the_life() -> None:
    """Stitch together a typical sunny / overnight / morning sequence."""
    s = _settings()
    ctl = Controller(s)

    day1 = datetime(2026, 5, 13, 12, 0, tzinfo=_TZ)

    # Tick 1: 14:00 — sunny afternoon, full battery, surplus should fire.
    # solar = 6 kW, load = 1 kW, ev off -> surplus = 5000 W -> 20 A.
    t1 = day1.replace(hour=14, minute=0)
    d1 = ctl.tick(
        t1,
        pw=_pw(soc=99, solar=6000, load=1000),
        em_load_w=1000,
        ev=_ev(on=False, amps=0),
        pv_forecasts=[],
    )
    assert d1.on is True
    assert d1.target_amps == 20
    assert "surplus" in d1.reason

    # Tick 2: 21:00 — sun's down, fresh telemetry says solar=0.
    # Surplus.applies fails (solar<=0), nothing else qualifies outside
    # the dump window -> "no action applies".
    t2 = day1.replace(hour=21, minute=0)
    d2 = ctl.tick(
        t2,
        pw=_pw(soc=90, solar=0, load=400),
        em_load_w=400,
        ev=_ev(on=False, amps=0),
        pv_forecasts=[],
    )
    assert d2.on is False
    assert d2.target_amps == 0
    assert d2.reason == "no action applies"

    # Tick 3: next-day 05:00 — dump window opens (5:00-8:00).
    day2 = day1 + timedelta(days=1)
    t3 = day2.replace(hour=5, minute=0)
    d3 = ctl.tick(
        t3,
        pw=_pw(soc=80, solar=0, load=400),
        em_load_w=400,
        ev=_ev(on=False, amps=0),
        pv_forecasts=[],
    )
    assert "dump" in d3.reason
    # Depending on natural amperage we either hold (on=False) or fire
    # (on=True). With SoC=80% across 3 h that's 9.45 kWh / 3 = 3.15 kW
    # = 13 A, so the dump fires.
    assert d3.on is True
    assert d3.target_amps == 13

    # Tick 4: next-day 08:00:30 — just past window. SoC=10% (== floor),
    # but the window-closed check should fire first anyway.
    t4 = day2.replace(hour=8, minute=0, second=30)
    d4 = ctl.tick(
        t4,
        pw=_pw(soc=10, solar=0, load=400),
        em_load_w=400,
        ev=_ev(on=False, amps=0),
        pv_forecasts=[],
    )
    assert d4.on is False
    assert d4.target_amps == 0
    assert d4.reason == "no action applies"

    # Tick 5: next-day 10:00 — sunny, but SoC well below 80% reserve.
    # Surplus.applies fails on the reserve check.
    t5 = day2.replace(hour=10, minute=0)
    d5 = ctl.tick(
        t5,
        pw=_pw(soc=10, solar=5000, load=1000),
        em_load_w=1000,
        ev=_ev(on=False, amps=0),
        pv_forecasts=[],
    )
    assert d5.on is False
    assert d5.target_amps == 0
    assert d5.reason == "no action applies"

    # Tick 6: next-day 14:00 — SoC has recovered to 85%, sun still up,
    # surplus fires again. solar=4 kW, load=1 kW -> 3000 W / 240 V = 12 A.
    t6 = day2.replace(hour=14, minute=0)
    d6 = ctl.tick(
        t6,
        pw=_pw(soc=85, solar=4000, load=1000),
        em_load_w=1000,
        ev=_ev(on=False, amps=0),
        pv_forecasts=[],
    )
    assert d6.on is True
    assert d6.target_amps == 12
    assert "surplus" in d6.reason


# --- Scenario 2: Telemetry blip overnight (regression test) -----------------


def test_scenario_2_overnight_telemetry_blip_does_not_turn_evse_on() -> None:
    """Regression: pw=None overnight must not turn the EVSE on.

    The old code returned Decision(0, "waiting on telemetry", on=True)
    when PW3 was momentarily dark, which (combined with a stale 13 A
    target from an earlier surplus preview) caused the EVSE to enable
    at 13 A in the middle of the night. The fix is that when no action
    applies, Decision.on is False — and the dead-reckoning in state.py
    keeps state.solar_w pinned at 0 from the last fresh reading.
    """
    s = _settings()
    ctl = Controller(s)

    # Tick at 22:00: fresh telemetry, EVSE off but configured at 13 A
    # (carried over from an earlier preview). No surplus, outside dump.
    base = datetime(2026, 5, 13, 22, 0, tzinfo=_TZ)
    d0 = ctl.tick(
        base,
        pw=_pw(soc=80, solar=0, load=400),
        em_load_w=400,
        ev=_ev(on=False, amps=13, status="Standby"),
        pv_forecasts=[],
    )
    assert d0.on is False
    assert d0.target_amps == 0
    assert d0.reason == "no action applies"

    # 00:30 next day: PW3 goes dark mid-night (pw=None). Emporia still
    # reporting. The critical assertion: Decision.on stays False.
    t1 = datetime(2026, 5, 14, 0, 30, tzinfo=_TZ)
    d1 = ctl.tick(
        t1,
        pw=None,
        em_load_w=400,
        ev=_ev(on=False, amps=13, status="Standby"),
        pv_forecasts=[],
    )
    assert d1.on is False, (
        "PW3 outage must not enable the EVSE (regression test for the "
        "'waiting on telemetry' on=True bug)"
    )
    assert d1.target_amps == 0
    assert d1.reason == "no action applies"

    # Several more pw=None ticks: still no EVSE activation.
    for minute in (30, 31, 32, 33):
        t = datetime(2026, 5, 14, 0, minute, tzinfo=_TZ)
        d = ctl.tick(
            t,
            pw=None,
            em_load_w=400,
            ev=_ev(on=False, amps=13, status="Standby"),
            pv_forecasts=[],
        )
        assert d.on is False
        assert d.target_amps == 0
        assert d.reason == "no action applies"


# --- Scenario 3: PW3 dark mid-dump ------------------------------------------


def test_scenario_3_pw3_dark_mid_dump_continues() -> None:
    """A brief PW3 outage during the dump window doesn't stall the dump.

    state.soc_pct dead-reckons from the held battery_w, so the dump
    action sees a plausible SoC and the window predicate still holds.
    """
    s = _settings()
    ctl = Controller(s)

    day = datetime(2026, 5, 14, 0, 0, tzinfo=_TZ)

    # 04:55 — pre-window, fresh telemetry, SoC fine but window closed.
    t0 = day.replace(hour=4, minute=55)
    d0 = ctl.tick(
        t0,
        pw=_pw(soc=70, solar=0, load=500, battery=0),
        em_load_w=500,
        ev=_ev(on=False, amps=0),
        pv_forecasts=[],
    )
    assert d0.reason == "no action applies"
    assert d0.on is False

    # 05:00 — window opens. Same telemetry. Dump fires.
    t1 = day.replace(hour=5, minute=0)
    d1 = ctl.tick(
        t1,
        pw=_pw(soc=70, solar=0, load=500, battery=0),
        em_load_w=500,
        ev=_ev(on=False, amps=0),
        pv_forecasts=[],
    )
    assert d1.on is True
    assert d1.reason.startswith("dump")

    # 05:30 — mid-window, fresh telemetry. SoC=65, battery discharging
    # at 1500 W. Dump still fires.
    t2 = day.replace(hour=5, minute=30)
    d2 = ctl.tick(
        t2,
        pw=_pw(soc=65, solar=0, load=500, battery=1500),
        em_load_w=500,
        ev=_ev(on=True, amps=d1.target_amps, status="Charging"),
        pv_forecasts=[],
    )
    assert d2.on is True
    assert d2.reason.startswith("dump")

    # 06:00-06:04: 5 consecutive ticks with pw=None.
    # state.soc_pct dead-reckons from the held battery_w; we're still
    # in the window; SoC drifts down but stays above floor. Each tick
    # the dump still fires.
    prev_amps = d2.target_amps
    for minute in (0, 1, 2, 3, 4):
        t = day.replace(hour=6, minute=minute)
        d = ctl.tick(
            t,
            pw=None,
            em_load_w=400,
            ev=_ev(on=True, amps=prev_amps, status="Charging"),
            pv_forecasts=[],
        )
        assert d.on is True, (
            f"Dump should keep firing during PW3 outage at {t}, "
            f"got reason={d.reason!r}"
        )
        assert d.reason.startswith("dump")
        prev_amps = d.target_amps


# --- Scenario 4: Kill switch ------------------------------------------------


def test_scenario_4_kill_switch() -> None:
    """Engaged kill switch short-circuits action dispatch; release restores."""
    s = _settings()
    ctl = Controller(s)

    t = datetime(2026, 5, 13, 14, 0, tzinfo=_TZ)

    # Establish surplus running: soc=85, solar=5 kW, load=1 kW -> 16 A.
    d0 = ctl.tick(
        t,
        pw=_pw(soc=85, solar=5000, load=1000),
        em_load_w=1000,
        ev=_ev(on=False, amps=0),
        pv_forecasts=[],
    )
    assert d0.on is True
    assert d0.target_amps > 0

    # Engage kill switch; same telemetry.
    ctl.engage_kill_switch()
    # Pass through an EVSE state so state.ev_amps / ev_on are set, and
    # the kill-switch Decision reflects them.
    t_kill = t + timedelta(seconds=30)
    d1 = ctl.tick(
        t_kill,
        pw=_pw(soc=85, solar=5000, load=1000),
        em_load_w=1000,
        ev=_ev(on=True, amps=d0.target_amps, status="Charging"),
        pv_forecasts=[],
    )
    assert d1.reason == "kill switch engaged"
    # Reflects the EVSE state observed this tick, not a freshly-computed plan.
    assert d1.target_amps == d0.target_amps
    assert d1.on is True

    # Release; surplus fires again.
    ctl.release_kill_switch()
    t_release = t_kill + timedelta(seconds=30)
    d2 = ctl.tick(
        t_release,
        pw=_pw(soc=85, solar=5000, load=1000),
        em_load_w=1000,
        ev=_ev(on=True, amps=d0.target_amps, status="Charging"),
        pv_forecasts=[],
    )
    assert d2.on is True
    assert "surplus" in d2.reason


def test_scenario_4_kill_switch_when_ev_off() -> None:
    """Kill-switch Decision reflects ev_on=False when EVSE is off."""
    s = _settings()
    ctl = Controller(s)
    ctl.engage_kill_switch()

    t = datetime(2026, 5, 13, 14, 0, tzinfo=_TZ)
    d = ctl.tick(
        t,
        pw=_pw(soc=85, solar=5000, load=1000),
        em_load_w=1000,
        ev=_ev(on=False, amps=0),
        pv_forecasts=[],
    )
    assert d.reason == "kill switch engaged"
    assert d.target_amps == 0
    assert d.on is False


# --- Scenario 5: morning_dump_enabled = False bypasses dump -----------------


def test_scenario_5_morning_dump_disabled_does_not_let_surplus_run_in_window() -> None:
    """When morning_dump is disabled, the dump-window slot remains empty.

    Surplus.applies has its own `if ctx.in_dump_window: return False`
    guard — independent of whether MorningDump is enabled — so during
    the dump window with morning_dump_enabled=False the result is
    still "no action applies", even with abundant solar.
    """
    s = _settings(
        morning_dump_enabled=False,
        surplus_enabled=True,
        trickle_enabled=False,
    )
    ctl = Controller(s)

    # 06:00 — inside the 05:00-08:00 dump window. Solar 0: no surplus,
    # dump disabled, no action applies.
    t1 = datetime(2026, 5, 13, 6, 0, tzinfo=_TZ)
    d1 = ctl.tick(
        t1,
        pw=_pw(soc=80, solar=0, load=400),
        em_load_w=400,
        ev=_ev(on=False, amps=0),
        pv_forecasts=[],
    )
    assert d1.reason == "no action applies"
    assert d1.on is False
    assert d1.target_amps == 0

    # Same window, now with 4 kW solar. Surplus.applies still returns
    # False because we're in the dump window, so no action applies —
    # this documents the existing semantics: the dump window is a
    # surplus-exclusion zone regardless of dump-action enabledness.
    t2 = t1 + timedelta(seconds=30)
    d2 = ctl.tick(
        t2,
        pw=_pw(soc=80, solar=4000, load=1000),
        em_load_w=1000,
        ev=_ev(on=False, amps=0),
        pv_forecasts=[],
    )
    assert d2.reason == "no action applies"
    assert d2.on is False
    assert d2.target_amps == 0

    # Sanity: outside the dump window, surplus does fire with the same
    # solar/load — confirms it's the window-guard, not some other check.
    t3 = t1.replace(hour=14)
    d3 = ctl.tick(
        t3,
        pw=_pw(soc=80, solar=4000, load=1000),
        em_load_w=1000,
        ev=_ev(on=False, amps=0),
        pv_forecasts=[],
    )
    assert d3.on is True
    assert "surplus" in d3.reason


# --- Scenario 6: trickle_enabled wins when others off -----------------------


def test_scenario_6_trickle_wins_when_others_disabled() -> None:
    """With surplus and morning_dump off, Trickle is unconditional."""
    s = _settings(
        surplus_enabled=False,
        morning_dump_enabled=False,
        trickle_enabled=True,
        trickle_kw=2.0,
    )
    ctl = Controller(s)

    # Mid-afternoon, anything goes — trickle fires.
    t1 = datetime(2026, 5, 13, 14, 0, tzinfo=_TZ)
    d1 = ctl.tick(
        t1,
        pw=_pw(soc=50, solar=5000, load=1000),
        em_load_w=1000,
        ev=_ev(on=False, amps=0),
        pv_forecasts=[],
    )
    # 2000 W / 240 V = 8 A, within [6, 40].
    assert d1.target_amps == 8
    assert d1.on is True
    assert "trickle" in d1.reason

    # Inside the dump window (06:00) trickle still fires — its applies()
    # is unconditional and the dump action is disabled.
    t2 = datetime(2026, 5, 13, 6, 0, tzinfo=_TZ)
    d2 = ctl.tick(
        t2,
        pw=_pw(soc=80, solar=0, load=400),
        em_load_w=400,
        ev=_ev(on=False, amps=0),
        pv_forecasts=[],
    )
    assert d2.target_amps == 8
    assert d2.on is True
    assert "trickle" in d2.reason


def test_scenario_6_trickle_applies_even_without_telemetry() -> None:
    """Trickle.applies() is unconditional — fires before any telemetry."""
    s = _settings(
        surplus_enabled=False,
        morning_dump_enabled=False,
        trickle_enabled=True,
        trickle_kw=2.0,
    )
    ctl = Controller(s)

    # Very first tick, no PW3, no Emporia load, no EV state -> state has
    # all-None fields. Trickle.applies returns True regardless.
    t = datetime(2026, 5, 13, 3, 0, tzinfo=_TZ)
    d = ctl.tick(
        t,
        pw=None,
        em_load_w=None,
        ev=None,
        pv_forecasts=[],
    )
    assert d.on is True
    assert d.target_amps == 8
    assert "trickle" in d.reason

"""Unit tests for the three actions in elec_auto.actions.

Style mirrors tests/test_state.py: pytest, no fixtures module, small
helper functions at the top, explicit timestamps. ActionContext fields
`now`, `dump_start`, `dump_end` are timezone-aware datetimes (not unix
floats), so the baseline here is a fixed tz-aware datetime instead of
the unix-seconds baseline used by state tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from elec_auto.actions import (
    DEFAULT_ACTIONS, ActionContext, MorningDump, PeakExport,
    SolarPassthrough, Surplus,
)
from elec_auto.config import Settings
from elec_auto.samples import Forecast, LoadStore, Sample, SampleStore
from elec_auto.state import State

_TZ = ZoneInfo("America/Los_Angeles")
_T0 = datetime(2026, 5, 18, 6, 30, tzinfo=_TZ)


# --- helpers -----------------------------------------------------------------


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
        trickle_kw=2.0,
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type]


def _state(
    *,
    soc: float | None = 85.0,
    solar: float | None = 0.0,
    load: float | None = 0.0,
    battery: float | None = 0.0,
    ev_amps: int | None = 0,
    ev_on: bool | None = False,
    ev_status: str | None = None,
    ev_circuit_w: float | None = None,
) -> State:
    return State(
        ts=_T0.timestamp(),
        soc_pct=soc,
        solar_w=solar,
        battery_w=battery,
        load_w=load,
        ev_amps=ev_amps,
        ev_on=ev_on,
        ev_status=ev_status,
        ev_circuit_w=ev_circuit_w,
    )


def _ctx(
    *,
    now: datetime = _T0,
    settings: Settings | None = None,
    dump_start: datetime | None = None,
    dump_end: datetime | None = None,
    pv_forecasts: list[Forecast] | None = None,
    sample_store: SampleStore | None = None,
    load_store: LoadStore | None = None,
    ev_circuit_name: str = "EV Charger",
) -> ActionContext:
    s = settings if settings is not None else _settings()
    # Default dump window: a benign past slice that doesn't contain `now`,
    # used for Surplus tests that need `in_dump_window=False`.
    ds = dump_start if dump_start is not None else now - timedelta(hours=4)
    de = dump_end if dump_end is not None else now - timedelta(hours=2)
    return ActionContext(
        now=now,
        settings=s,
        dump_start=ds,
        dump_end=de,
        pv_forecasts=pv_forecasts or [],
        sample_store=sample_store,
        load_store=load_store,
        ev_circuit_name=ev_circuit_name,
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


# --- Surplus.applies() -------------------------------------------------------


def test_surplus_applies_true_above_reserve_with_surplus() -> None:
    a = Surplus()
    st = _state(soc=85, solar=5000, load=1000)
    ctx = _ctx()  # default dump window is in the past, so out-of-window
    assert a.applies(st, ctx) is True


def test_surplus_applies_false_when_soc_none() -> None:
    a = Surplus()
    st = _state(soc=None, solar=5000, load=1000)
    assert a.applies(st, _ctx()) is False


def test_surplus_applies_false_when_solar_none() -> None:
    a = Surplus()
    st = _state(soc=85, solar=None, load=1000)
    assert a.applies(st, _ctx()) is False


def test_surplus_applies_false_when_load_none() -> None:
    a = Surplus()
    st = _state(soc=85, solar=5000, load=None)
    assert a.applies(st, _ctx()) is False


def test_surplus_applies_false_below_reserve() -> None:
    a = Surplus()
    st = _state(soc=79, solar=5000, load=1000)
    assert a.applies(st, _ctx()) is False


def test_surplus_applies_false_when_solar_nonpositive() -> None:
    a = Surplus()
    st = _state(soc=85, solar=0, load=1000)
    assert a.applies(st, _ctx()) is False


def test_surplus_applies_false_in_dump_window() -> None:
    a = Surplus()
    st = _state(soc=85, solar=5000, load=1000)
    # Bracket `now` (_T0 = 06:30) with a dump window that contains it.
    ctx = _ctx(
        dump_start=_T0 - timedelta(minutes=30),  # 06:00
        dump_end=_T0 + timedelta(hours=2),       # 08:30
    )
    assert ctx.in_dump_window is True
    assert a.applies(st, ctx) is False


# --- Surplus.decide() --------------------------------------------------------


def test_surplus_decide_ev_off_simple_surplus() -> None:
    a = Surplus()
    st = _state(soc=85, solar=5000, load=1000, ev_on=False, ev_amps=0)
    d = a.decide(st, _ctx())
    # surplus = 5000 - 1000 = 4000 W -> 4000 // 240 = 16 A
    assert d.target_amps == 16
    assert d.on is True
    assert "4000" in d.reason


def test_surplus_decide_backs_out_ev_own_draw() -> None:
    a = Surplus()
    # EV running at 12 A * 240 V = 2880 W is included in the 4000 W total
    # load reading. Non-EV load = 1120 W; surplus = 5000 - 1120 = 3880 W.
    # 3880 // 240 = 16 A.
    st = _state(soc=85, solar=5000, load=4000, ev_on=True, ev_amps=12)
    d = a.decide(st, _ctx())
    assert d.target_amps == 16
    assert d.on is True


def test_surplus_decide_uses_measured_ev_circuit_w_over_proxy() -> None:
    """Regression for the 2026-05-18 phantom-draw bug: EV configured ON
    at 40 A but Standby (car unplugged) → real draw ~0 W. The configured
    proxy would say `ev_w_now = 40*240 = 9600 W`, making surplus look
    9.6 kW larger than reality. With state.ev_circuit_w available the
    action must use it instead, giving the actual surplus.
    """
    a = Surplus()
    # Solar 3000 W, total house load 400 W, EVSE configured ON at 40 A
    # but Emporia reports 14 W on the EV circuit (Standby idle draw).
    # Real non-EV load = 400 - 14 = 386 W; real surplus = 2614 W → 10 A.
    # Without the fix: ev_w_now=9600 → non_ev=-9200 → surplus=12200 → 40 A.
    st = _state(
        soc=85, solar=3000, load=400,
        ev_on=True, ev_amps=40, ev_circuit_w=14.0,
    )
    d = a.decide(st, _ctx())
    assert d.target_amps == 10
    assert d.on is True


def test_surplus_decide_prefers_configured_rate_when_charging() -> None:
    """Regression for the 2026-05-30 oscillation bug: pw.load_w is
    instantaneous but Emporia's ev_circuit_w is a 1-minute rolling
    average. When the EV is actively Charging, the action must use the
    configured-rate proxy (same time-base as load_w) rather than the
    lagged Emporia value — otherwise each new session phantom-detects
    a huge non-EV load and the EV is switched off the tick after it
    starts.

    Scenario: solar 5300 W, PW3 instant load 4880 W (includes 12 A EV
    that just turned on), Emporia ev_circuit_w still showing 1200 W
    (1-min average across mostly-off seconds).
    """
    a = Surplus()
    st = _state(
        soc=85, solar=5300, load=4880,
        ev_on=True, ev_amps=12, ev_status="Charging",
        ev_circuit_w=1200.0,
    )
    d = a.decide(st, _ctx())
    # With fix: ev_w_now = 12*240 = 2880, non_ev = 4880-2880 = 2000,
    # surplus = 5300-2000 = 3300 → 13 A. Stable across the lag window.
    # Without fix: ev_w_now = 1200 (lagged), non_ev = 3680,
    # surplus = 1620 → 6 A or below-min, cycling off.
    assert d.target_amps == 13
    assert d.on is True


def test_surplus_decide_uses_circuit_w_when_standby_even_if_configured_on() -> None:
    """Phantom-draw protection: EVSE configured ON at 40 A but car is
    Standby (contactor open, car not pulling). Trust the measured
    Emporia value over the configured-rate proxy.
    """
    a = Surplus()
    st = _state(
        soc=85, solar=3000, load=400,
        ev_on=True, ev_amps=40, ev_status="Standby",
        ev_circuit_w=14.0,
    )
    d = a.decide(st, _ctx())
    # Real non-EV load = 400 - 14 = 386 W; surplus = 2614 W → 10 A.
    assert d.target_amps == 10
    assert d.on is True


def test_surplus_decide_below_min_returns_zero_off() -> None:
    a = Surplus()
    st = _state(soc=85, solar=500, load=1000, ev_on=False, ev_amps=0)
    d = a.decide(st, _ctx())
    assert d.target_amps == 0
    assert d.on is False
    assert "< min" in d.reason


# --- MorningDump.applies() ---------------------------------------------------


def test_morning_dump_applies_true_in_window_above_floor() -> None:
    a = MorningDump()
    st = _state(soc=50)
    ctx = _ctx(
        dump_start=_T0 - timedelta(minutes=30),
        dump_end=_T0 + timedelta(hours=2),
    )
    assert a.applies(st, ctx) is True


def test_morning_dump_applies_false_when_soc_none() -> None:
    a = MorningDump()
    st = _state(soc=None)
    ctx = _ctx(
        dump_start=_T0 - timedelta(minutes=30),
        dump_end=_T0 + timedelta(hours=2),
    )
    assert a.applies(st, ctx) is False


def test_morning_dump_applies_false_outside_window() -> None:
    a = MorningDump()
    st = _state(soc=50)
    # Default ctx window is in the past, so `now` is outside.
    assert a.applies(st, _ctx()) is False


def test_morning_dump_applies_false_at_floor() -> None:
    # Docstring says "<= floor → return False"; SoC equal to floor must
    # not trigger a dump.
    a = MorningDump()
    st = _state(soc=10)  # equal to morning_dump_floor_pct default of 10
    ctx = _ctx(
        dump_start=_T0 - timedelta(minutes=30),
        dump_end=_T0 + timedelta(hours=2),
    )
    assert a.applies(st, ctx) is False


# --- MorningDump.decide() ----------------------------------------------------


def test_morning_dump_decide_basic_no_forecast_no_stores() -> None:
    """Full 3 h window, SoC 80%, floor 10%, no PV, no stores.

    battery_kwh = (80 - 10)/100 * 13.5 = 9.45 kWh; /3 h = 3.15 kW =
    13 A (floored). Note that 9.45 as a float lands just under 9.45
    and formats as "9.4" with `:.1f`.
    """
    a = MorningDump()
    st = _state(soc=80)
    ctx = _ctx(
        dump_start=_T0,
        dump_end=_T0 + timedelta(hours=3),
    )
    d = a.decide(st, ctx)
    assert d.target_amps == 13
    assert d.on is True
    assert d.reason.startswith("dump 9.4+0.0-0.0 kWh in 3.00 h")


def test_morning_dump_decide_credits_pv_forecast() -> None:
    """Flat 2 kW PV across the 3 h window with 90% credit.

    pv_credit = 2 kW * 3 h * 0.9 = 5.4 kWh.
    headroom = 9.45 + 5.4 = 14.85 / 3 h = 4.95 kW = 20 A (floored).
    """
    a = MorningDump()
    st = _state(soc=80)
    dump_start = _T0
    dump_end = _T0 + timedelta(hours=3)
    # Cover the window with 4 h of synthetic PV (extra half-hour on each
    # side so the linear interp on the boundaries is unambiguous).
    pv = _const_pv(
        dump_start - timedelta(minutes=30),
        dump_end + timedelta(minutes=30),
        2000.0,
    )
    # Day-integration for the sunny floor check: 4 h * 2 kW = 8 kWh,
    # well under the 30 kWh threshold -> normal floor of 10%.
    ctx = _ctx(
        dump_start=dump_start, dump_end=dump_end, pv_forecasts=pv,
    )
    d = a.decide(st, ctx)
    assert d.target_amps == 20
    assert d.on is True
    assert "+5.4-0.0" in d.reason
    assert not d.reason.startswith("sunny:")


def test_morning_dump_decide_subtracts_non_ev_load(tmp_path: Path) -> None:
    """Yesterday's same 3 h window: PW3 4 kWh, EV circuit 1 kWh -> 3 kWh non-EV.

    battery_kwh = 9.45, pv_credit = 0, non_ev_load = 3.0.
    headroom = 9.45 - 3.0 = 6.45 / 3 h = 2.15 kW = 8 A.
    """
    a = MorningDump()
    st = _state(soc=80)
    dump_start = _T0
    dump_end = _T0 + timedelta(hours=3)

    # Populate yesterday's same-window data. Constant power over the
    # whole window so the trapezoidal integral collapses to W * h.
    samples = SampleStore(tmp_path / "samples.db")
    loads = LoadStore(tmp_path / "samples.db")
    yest_lo = int(dump_start.timestamp()) - 86_400
    yest_hi = int(dump_end.timestamp()) - 86_400
    house_w = 4000.0 / 3.0      # 4 kWh over 3 h
    ev_w = 1000.0 / 3.0          # 1 kWh over 3 h
    for t in range(yest_lo, yest_hi + 1, 60):
        samples.insert(Sample(
            ts=t, solar_w=None, load_w=house_w, battery_w=None, grid_w=None,
            soc_pct=None, theoretical_w=None,
        ))
        loads.insert_tick(t, {"EV Charger": ev_w})

    ctx = _ctx(
        dump_start=dump_start, dump_end=dump_end,
        sample_store=samples, load_store=loads,
    )
    d = a.decide(st, ctx)
    assert d.target_amps == 8
    assert d.on is True
    assert "-3.0" in d.reason


def test_morning_dump_decide_sunny_floor(tmp_path: Path) -> None:
    """Clear-day forecast clears the 30 kWh threshold -> sunny floor of 5%.

    Use 4 kW PV from 10:00 to 22:00 on the dump's day -> 48 kWh, > 30 kWh.
    Dump window 06:30-09:30 sits entirely before the PV points, so the
    in-window PV integration falls outside the data span and returns 0.
    headroom = (80 - 5)/100 * 13.5 = 10.125 / 3 h = 3.375 kW = 14 A.
    """
    a = MorningDump()
    st = _state(soc=80)
    dump_start = _T0  # 06:30
    dump_end = _T0 + timedelta(hours=3)  # 09:30
    pv_start = _T0.replace(hour=10, minute=0)
    pv_end = _T0.replace(hour=22, minute=0)
    pv = _const_pv(pv_start, pv_end, 4000.0)
    ctx = _ctx(
        dump_start=dump_start, dump_end=dump_end, pv_forecasts=pv,
    )
    d = a.decide(st, ctx)
    assert d.target_amps == 14
    assert d.on is True
    assert d.reason.startswith("sunny: dump 10.1+0.0-0.0 kWh in 3.00 h")


def test_morning_dump_decide_hold_below_min() -> None:
    """SoC barely above floor: natural rate < ev_min_amps -> hold at 0.

    battery_kwh = (12 - 10)/100 * 13.5 = 0.27 kWh; /3 h = 0.09 kW = 0 A.
    """
    a = MorningDump()
    st = _state(soc=12)
    ctx = _ctx(
        dump_start=_T0,
        dump_end=_T0 + timedelta(hours=3),
    )
    d = a.decide(st, ctx)
    assert d.target_amps == 0
    assert d.on is False
    assert d.reason.startswith("hold:")
    assert "natural 0 A < min 6 A" in d.reason


def test_morning_dump_decide_clamps_to_max_amps() -> None:
    """SoC 99%, only 0.5 h remaining: unclamped amps huge, clamped to 29.

    battery_kwh = (99 - 10)/100 * 13.5 = 12.015; /0.5 h = 24.03 kW =
    100 A unclamped -> min(40 ev_max, 29 morning_dump_max) = 29.
    """
    a = MorningDump()
    st = _state(soc=99)
    ctx = _ctx(
        dump_start=_T0 - timedelta(hours=2),
        dump_end=_T0 + timedelta(minutes=30),
    )
    d = a.decide(st, ctx)
    assert d.target_amps == 29
    assert d.on is True


# --- SolarPassthrough --------------------------------------------------------


def test_solar_passthrough_applies_below_reserve_with_solar() -> None:
    """SolarPassthrough fires where Surplus does NOT — below the reserve."""
    a = SolarPassthrough()
    st = _state(soc=40, solar=4000, load=800)
    assert a.applies(st, _ctx()) is True


def test_solar_passthrough_partitions_with_surplus_at_reserve() -> None:
    """At/above the reserve, SolarPassthrough yields to Surplus."""
    a = SolarPassthrough()
    s = _settings(battery_reserve_pct=80)
    st = _state(soc=80, solar=4000, load=800)
    assert a.applies(st, _ctx(settings=s)) is False
    st_above = _state(soc=95, solar=4000, load=800)
    assert a.applies(st_above, _ctx(settings=s)) is False


def test_solar_passthrough_applies_false_without_solar() -> None:
    a = SolarPassthrough()
    st = _state(soc=40, solar=0, load=800)
    assert a.applies(st, _ctx()) is False


def test_solar_passthrough_applies_inside_dump_window() -> None:
    """Unlike Surplus, SolarPassthrough fires in the dump window —
    MorningDump's priority (40) handles the tiebreak when both apply,
    and disabling MorningDump leaves SolarPassthrough free to run."""
    a = SolarPassthrough()
    st = _state(soc=40, solar=4000, load=800)
    ctx = _ctx(
        dump_start=_T0 - timedelta(hours=1),
        dump_end=_T0 + timedelta(hours=1),
    )
    assert ctx.in_dump_window is True
    assert a.applies(st, ctx) is True


def test_solar_passthrough_decide_matches_surplus_math() -> None:
    """The point of the shared `_surplus_w` helper: same in, same out."""
    state = _state(soc=40, solar=5000, load=1200)
    ctx = _ctx()
    sp_decision = SolarPassthrough().decide(state, ctx)
    su_decision = Surplus().decide(state, ctx)
    assert sp_decision.target_amps == su_decision.target_amps
    assert sp_decision.on == su_decision.on


def test_solar_passthrough_decide_below_min_returns_zero_off() -> None:
    a = SolarPassthrough()
    # Solar barely covers home load — surplus < ev_min_amps × ev_voltage.
    st = _state(soc=40, solar=1200, load=1100)
    d = a.decide(st, _ctx())
    assert d.target_amps == 0
    assert d.on is False


# --- DEFAULT_ACTIONS roster --------------------------------------------------


def test_default_actions_contains_known_actions() -> None:
    names = {type(a).__name__ for a in DEFAULT_ACTIONS}
    assert names == {"MorningDump", "PeakExport", "SolarPassthrough", "Surplus"}


# --- PeakExport --------------------------------------------------------------


def _settings_pe(**kw):
    """Settings preset for PeakExport tests."""
    defaults = dict(
        peak_export_enabled=True,
        peak_export_floor_pct=40,
        peak_export_start_hour=19,
        peak_export_end_hour=20,
        battery_capacity_kwh=13.5,
    )
    defaults.update(kw)
    return _settings(**defaults)


def _t(month: int, day: int, hour: int, minute: int = 0):
    """tz-aware datetime in the test timezone."""
    return datetime(2026, month, day, hour, minute, tzinfo=_TZ)


def test_peak_export_applies_july_weekday_inside_window() -> None:
    """July weekday 7:30 PM with SoC=100% should fire (window 19-20)."""
    a = PeakExport()
    # 2026-07-08 is a Wednesday.
    ctx = _ctx(now=_t(7, 8, 19, 30), settings=_settings_pe())
    st = _state(soc=100.0)
    assert a.applies(st, ctx) is True


def test_peak_export_does_not_apply_outside_summer_months() -> None:
    """September is not in PEAK_DAYS_BY_MONTH."""
    a = PeakExport()
    # 2026-09-02 is a Wednesday at 7:30 PM.
    ctx = _ctx(now=_t(9, 2, 19, 30), settings=_settings_pe())
    st = _state(soc=100.0)
    assert a.applies(st, ctx) is False


def test_peak_export_no_weekend_in_june() -> None:
    """June weekend excluded (June: weekday only)."""
    a = PeakExport()
    # 2026-06-13 is a Saturday at 7:30 PM.
    ctx = _ctx(now=_t(6, 13, 19, 30), settings=_settings_pe())
    st = _state(soc=100.0)
    assert a.applies(st, ctx) is False


def test_peak_export_august_weekend_applies() -> None:
    """August weekend included (August: weekday + weekend)."""
    a = PeakExport()
    # 2026-08-08 is a Saturday at 7:30 PM.
    ctx = _ctx(now=_t(8, 8, 19, 30), settings=_settings_pe())
    st = _state(soc=100.0)
    assert a.applies(st, ctx) is True


def test_peak_export_does_not_apply_below_floor() -> None:
    """SoC at/below floor — no headroom to discharge."""
    a = PeakExport()
    ctx = _ctx(now=_t(7, 8, 19, 30), settings=_settings_pe(peak_export_floor_pct=40))
    st = _state(soc=40.0)
    assert a.applies(st, ctx) is False
    st2 = _state(soc=39.0)
    assert a.applies(st2, ctx) is False


def test_peak_export_does_not_apply_after_end_hour() -> None:
    """Past peak_export_end_hour (20:00) — window closed."""
    a = PeakExport()
    ctx = _ctx(now=_t(7, 8, 20, 5), settings=_settings_pe())
    st = _state(soc=100.0)
    assert a.applies(st, ctx) is False


def test_peak_export_does_not_apply_before_start_hour() -> None:
    """Before peak_export_start_hour (19:00) — window not yet open."""
    a = PeakExport()
    ctx = _ctx(now=_t(7, 8, 18, 59), settings=_settings_pe())
    st = _state(soc=100.0)
    assert a.applies(st, ctx) is False


def test_peak_export_decide_returns_pw3_target() -> None:
    """decide() emits a PW3-targeted Decision with mode + reserve set."""
    a = PeakExport()
    ctx = _ctx(now=_t(7, 8, 19, 30), settings=_settings_pe(peak_export_floor_pct=40))
    st = _state(soc=95.0)
    d = a.decide(st, ctx)
    assert d.target_system == "pw3"
    assert d.pw3_mode == "autonomous"
    assert d.pw3_reserve_pct == 40
    # EV-shaped fields meaningless but should be present + safe.
    assert d.target_amps == 0
    assert d.on is False

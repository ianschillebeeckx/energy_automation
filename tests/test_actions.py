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
    DEFAULT_ACTIONS, ActionContext, MorningDump, Surplus, Trickle,
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
) -> State:
    return State(
        ts=_T0.timestamp(),
        soc_pct=soc,
        solar_w=solar,
        battery_w=battery,
        load_w=load,
        ev_amps=ev_amps,
        ev_on=ev_on,
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


# --- Trickle -----------------------------------------------------------------


def test_trickle_applies_always_true() -> None:
    a = Trickle()
    assert a.applies(_state(), _ctx()) is True
    # No SoC, no solar, no load — still applies.
    assert a.applies(
        _state(soc=None, solar=None, load=None), _ctx(),
    ) is True


def test_trickle_decide_default_2_kw_to_8_amps() -> None:
    a = Trickle()
    d = a.decide(_state(), _ctx(settings=_settings(trickle_kw=2.0)))
    # 2000 W / 240 V = 8.33 -> 8 A, within [6, 40] -> 8.
    assert d.target_amps == 8
    assert d.on is True


def test_trickle_decide_clamps_below_min() -> None:
    a = Trickle()
    d = a.decide(_state(), _ctx(settings=_settings(trickle_kw=0.5)))
    # 500 W / 240 V = 2 -> clamp up to ev_min_amps = 6.
    assert d.target_amps == 6
    assert d.on is True


def test_trickle_decide_clamps_above_max() -> None:
    a = Trickle()
    d = a.decide(_state(), _ctx(settings=_settings(trickle_kw=20.0)))
    # 20000 / 240 = 83 -> clamp down to ev_max_amps = 40.
    assert d.target_amps == 40
    assert d.on is True


# --- DEFAULT_ACTIONS roster --------------------------------------------------


def test_default_actions_contains_three_known_actions() -> None:
    names = {type(a).__name__ for a in DEFAULT_ACTIONS}
    assert names == {"MorningDump", "Surplus", "Trickle"}

"""Tests for State + step()."""

from __future__ import annotations

from elec_auto.config import Settings
from elec_auto.emporia import ChargerState
from elec_auto.powerwall import PowerReading
from elec_auto.state import State, step


def _settings(**overrides) -> Settings:
    defaults = dict(
        battery_capacity_kwh=13.5,
        battery_raw_floor_pct=5.0,
        telemetry_fresh_sec=60,
        # Disabled here so these unit tests exercise the energy-balance
        # math in isolation. The empirically-fit production default is
        # validated by tests/test_replay.py against recorded telemetry.
        battery_vampire_w=0.0,
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type]


def _pw(soc=80.0, solar=0.0, load=500.0, battery=0.0, grid=0.0) -> PowerReading:
    return PowerReading(
        solar_w=solar, load_w=load, battery_w=battery,
        grid_w=grid, battery_soc_pct=soc,
    )


def _ev(on=False, amps=16, status="Standby") -> ChargerState:
    return ChargerState(
        gid=1, name="EV Charger", on=on, charge_rate_a=amps,
        max_charge_rate_a=40, status=status,
    )


_TZ_NOON = 1_700_000_000.0  # arbitrary fixed unix-second baseline


# --- first tick / immutability ---------------------------------------------


def test_state_is_immutable() -> None:
    s = State()
    try:
        s.soc_pct = 50  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("expected frozen dataclass to refuse assignment")


def test_first_tick_with_fresh_pw_snaps_values() -> None:
    s = step(State(), _TZ_NOON, pw=_pw(soc=72, solar=3000, load=900, battery=-100),
             em_load_w=None, ev=None, settings=_settings())
    assert s.ts == _TZ_NOON
    assert s.soc_pct == 72
    assert s.solar_w == 3000
    assert s.battery_w == -100
    assert s.pw_load_w == 900
    assert s.load_w == 900           # PW3 fresh wins


def test_first_tick_with_only_emporia_falls_back() -> None:
    s = step(State(), _TZ_NOON, pw=None, em_load_w=750.0, ev=None,
             settings=_settings())
    assert s.em_load_w == 750.0
    assert s.load_w == 750.0
    assert s.soc_pct is None         # no PW3 → no SoC info


def test_first_tick_empty_stays_unknown() -> None:
    s = step(State(), _TZ_NOON, pw=None, em_load_w=None, ev=None,
             settings=_settings())
    assert s.load_w is None
    assert s.soc_pct is None


# --- load_w source preference ------------------------------------------------


def test_load_prefers_fresh_pw_over_fresh_emporia() -> None:
    s = step(State(), _TZ_NOON, pw=_pw(load=1200), em_load_w=900,
             ev=None, settings=_settings())
    assert s.load_w == 1200


def test_load_falls_back_to_emporia_when_pw_stale() -> None:
    s1 = step(State(), _TZ_NOON, pw=_pw(load=1200), em_load_w=None,
              ev=None, settings=_settings())
    s2 = step(s1, _TZ_NOON + 120, pw=None, em_load_w=1100,
              ev=None, settings=_settings())
    # PW3 last 60 s+ ago (default fresh threshold = 60), Emporia just-fresh.
    assert s2.load_w == 1100


def test_load_uses_stale_pw_when_nothing_fresh() -> None:
    s1 = step(State(), _TZ_NOON, pw=_pw(load=1200), em_load_w=None,
              ev=None, settings=_settings())
    s2 = step(s1, _TZ_NOON + 600, pw=None, em_load_w=None,
              ev=None, settings=_settings())
    assert s2.load_w == 1200         # stale, but better than nothing


# --- SoC: snap vs dead-reckon ------------------------------------------------


def test_soc_snaps_to_fresh_pw_even_with_held_battery_w() -> None:
    s1 = step(State(), _TZ_NOON, pw=_pw(soc=80, battery=2000),
              em_load_w=None, ev=None, settings=_settings())
    # 30 s later, fresh PW3 with a different SoC — should snap, not integrate.
    s2 = step(s1, _TZ_NOON + 30, pw=_pw(soc=75, battery=2000),
              em_load_w=None, ev=None, settings=_settings())
    assert s2.soc_pct == 75


def test_soc_dead_reckons_when_pw_dark() -> None:
    # 1 h with battery sustaining a 1 kW load (no solar, no grid),
    # so the Tesla balance gives battery_w = 1000 W discharging.
    # delta = -1 kWh / 12.825 kWh × 100 ≈ -7.8 percentage points.
    s1 = step(State(), _TZ_NOON,
              pw=_pw(soc=80, battery=1000, load=1000, solar=0, grid=0),
              em_load_w=None, ev=None, settings=_settings())
    s2 = step(s1, _TZ_NOON + 3600, pw=None, em_load_w=None, ev=None,
              settings=_settings())
    assert abs(s2.soc_pct - (80 - 7.797)) < 0.1


def test_soc_dead_reckons_up_when_charging() -> None:
    # battery_w < 0 = charging, SoC rises. 2 kW of surplus PV charges
    # the battery: solar = 2 kW, load = 0, grid = 0 → battery = -2 kW.
    s1 = step(State(), _TZ_NOON,
              pw=_pw(soc=40, battery=-2000, solar=2000, load=0, grid=0),
              em_load_w=None, ev=None, settings=_settings())
    s2 = step(s1, _TZ_NOON + 3600, pw=None, em_load_w=None, ev=None,
              settings=_settings())
    assert abs(s2.soc_pct - (40 + 15.594)) < 0.1


def test_soc_clamps_at_100() -> None:
    # Heavy charging, starting SoC 98% — should saturate at 100.
    s1 = step(State(), _TZ_NOON,
              pw=_pw(soc=98, battery=-5000, solar=5000, load=0, grid=0),
              em_load_w=None, ev=None, settings=_settings())
    s2 = step(s1, _TZ_NOON + 3600, pw=None, em_load_w=None, ev=None,
              settings=_settings())
    assert s2.soc_pct == 100.0


def test_soc_clamps_at_0() -> None:
    s1 = step(State(), _TZ_NOON,
              pw=_pw(soc=2, battery=5000, load=5000, solar=0, grid=0),
              em_load_w=None, ev=None, settings=_settings())
    s2 = step(s1, _TZ_NOON + 3600, pw=None, em_load_w=None, ev=None,
              settings=_settings())
    assert s2.soc_pct == 0.0


def test_consecutive_dark_ticks_each_integrate_their_own_dt() -> None:
    """Two ticks of dt=600 s should produce the same drift as one tick
    of dt=1200 s — no double-counting from advancing anchors."""
    s_initial = step(State(), _TZ_NOON, pw=_pw(soc=80, battery=1000),
                     em_load_w=None, ev=None, settings=_settings())
    # Two ticks
    s_a1 = step(s_initial, _TZ_NOON + 600, pw=None, em_load_w=None,
                ev=None, settings=_settings())
    s_a2 = step(s_a1, _TZ_NOON + 1200, pw=None, em_load_w=None,
                ev=None, settings=_settings())
    # One tick of double duration
    s_b = step(s_initial, _TZ_NOON + 1200, pw=None, em_load_w=None,
               ev=None, settings=_settings())
    assert abs(s_a2.soc_pct - s_b.soc_pct) < 1e-6


def test_dead_reckoning_skipped_without_prior_battery_w() -> None:
    """Service just booted, no prior battery_w → can't extrapolate."""
    s1 = step(State(), _TZ_NOON, pw=None, em_load_w=500, ev=None,
              settings=_settings())
    s2 = step(s1, _TZ_NOON + 30, pw=None, em_load_w=500, ev=None,
              settings=_settings())
    assert s2.soc_pct is None


# --- provenance --------------------------------------------------------------


def test_soc_source_pw3_when_snapped() -> None:
    s = step(State(), _TZ_NOON, pw=_pw(soc=80), em_load_w=None, ev=None,
             settings=_settings())
    assert s.soc_source == "pw3"


def test_soc_source_estimated_when_dead_reckoned() -> None:
    s1 = step(State(), _TZ_NOON, pw=_pw(soc=80, battery=500), em_load_w=None,
              ev=None, settings=_settings())
    s2 = step(s1, _TZ_NOON + 30, pw=None, em_load_w=None, ev=None,
              settings=_settings())
    assert s2.soc_source == "estimated"


def test_soc_source_none_when_no_data() -> None:
    s = step(State(), _TZ_NOON, pw=None, em_load_w=None, ev=None,
             settings=_settings())
    assert s.soc_source is None


def test_soc_source_carries_estimated_across_consecutive_dark_ticks() -> None:
    s1 = step(State(), _TZ_NOON, pw=_pw(soc=80, battery=500), em_load_w=None,
              ev=None, settings=_settings())
    s2 = step(s1, _TZ_NOON + 30, pw=None, em_load_w=None, ev=None,
              settings=_settings())
    s3 = step(s2, _TZ_NOON + 60, pw=None, em_load_w=None, ev=None,
              settings=_settings())
    assert s3.soc_source == "estimated"


def test_load_source_pw3_when_pw_fresh() -> None:
    s = step(State(), _TZ_NOON, pw=_pw(load=1200), em_load_w=800,
             ev=None, settings=_settings())
    assert s.load_source == "pw3"


def test_load_source_emporia_when_pw_dark_but_emporia_fresh() -> None:
    s1 = step(State(), _TZ_NOON, pw=_pw(load=1200), em_load_w=None,
              ev=None, settings=_settings())
    s2 = step(s1, _TZ_NOON + 120, pw=None, em_load_w=800,
              ev=None, settings=_settings())
    # PW3 stale (> 60s default fresh threshold), Emporia just-snapped.
    assert s2.load_source == "emporia"


def test_load_source_none_when_no_data_anywhere() -> None:
    s = step(State(), _TZ_NOON, pw=None, em_load_w=None, ev=None,
             settings=_settings())
    assert s.load_source is None


# --- EV state ---------------------------------------------------------------


def test_ev_state_snaps_and_holds() -> None:
    s1 = step(State(), _TZ_NOON, pw=None, em_load_w=None,
              ev=_ev(on=True, amps=20, status="Charging"), settings=_settings())
    assert s1.ev_amps == 20 and s1.ev_on is True and s1.ev_status == "Charging"
    # Dark tick: hold last-known.
    s2 = step(s1, _TZ_NOON + 30, pw=None, em_load_w=None, ev=None,
              settings=_settings())
    assert s2.ev_amps == 20 and s2.ev_on is True and s2.ev_status == "Charging"
    assert s2.ev_last_ts == _TZ_NOON              # unchanged

"""Unit tests for the heuristic forecasters in elec_auto.forecast."""

from __future__ import annotations

from pathlib import Path

from elec_auto.config import Settings
from elec_auto.forecast import (
    LoadForecast, SocForecast, load_forecast, pv_kwh_in_range, soc_forecast,
)
from elec_auto.samples import Forecast, Sample, SampleStore

_DAY = 24 * 3600


def _settings() -> Settings:
    # _env_file=None keeps the test isolated from the dev .env on disk.
    return Settings(_env_file=None)  # type: ignore[arg-type]


def _store(tmp_path: Path) -> SampleStore:
    return SampleStore(tmp_path / "samples.db")


def _sample(ts: int, load_w: float | None) -> Sample:
    return Sample(
        ts=ts, solar_w=None, load_w=load_w, battery_w=None, grid_w=None,
        soc_pct=None, theoretical_w=None,
    )


# --- load_forecast -----------------------------------------------------------


def test_load_forecast_shifts_yesterday_by_24h(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.insert(_sample(1000, 800.0))
    out = load_forecast(s, start_ts=1000 + _DAY - 60, end_ts=1000 + _DAY + 60)
    assert out == [LoadForecast(ts=1000 + _DAY, load_w=800.0)]


def test_load_forecast_skips_none_load(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.insert(_sample(1000, None))
    s.insert(_sample(1030, 500.0))
    out = load_forecast(s, start_ts=_DAY, end_ts=_DAY + 2000)
    assert [f.load_w for f in out] == [500.0]


def test_load_forecast_skips_negative_load(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.insert(_sample(1000, -10.0))  # sensor noise
    s.insert(_sample(1030, 500.0))
    out = load_forecast(s, start_ts=_DAY, end_ts=_DAY + 2000)
    assert [f.load_w for f in out] == [500.0]


# --- shared helpers for pv_kwh_in_range / soc_forecast -----------------------


def _const_pv(now_ts: int, end_ts: int, watts: float) -> list[Forecast]:
    """Synthetic Solcast 30-min forecast holding `watts` flat."""
    pts: list[Forecast] = []
    t = now_ts - 1800
    while t <= end_ts + 1800:
        pts.append(Forecast(period_ts=t, fetched_at=now_ts, source="test",
                            pv_w_p50=watts))
        t += 1800
    return pts


def _const_load(now_ts: int, end_ts: int, watts: float) -> list[LoadForecast]:
    """Dense (~30s) constant-load forecast spanning the window."""
    out: list[LoadForecast] = []
    t = now_ts
    while t <= end_ts:
        out.append(LoadForecast(ts=t, load_w=watts))
        t += 30
    return out


# --- pv_kwh_in_range ---------------------------------------------------------


def test_pv_kwh_in_range_constant_two_kw_for_one_hour() -> None:
    # 2 kW flat over 1 h → exactly 2 kWh.
    forecasts = _const_pv(0, 3600, 2000)
    assert abs(pv_kwh_in_range(forecasts, 0, 3600) - 2.0) < 1e-6


def test_pv_kwh_in_range_zero_when_window_empty() -> None:
    forecasts = _const_pv(0, 3600, 2000)
    assert pv_kwh_in_range(forecasts, 1000, 1000) == 0.0
    assert pv_kwh_in_range(forecasts, 1000, 500) == 0.0  # inverted


def test_pv_kwh_in_range_zero_without_forecast_data() -> None:
    assert pv_kwh_in_range([], 0, 3600) == 0.0


def test_pv_kwh_in_range_skips_periods_outside_data_span() -> None:
    # Forecast only covers 1000..2000; querying 3000..4000 → 0.
    forecasts = _const_pv(1000, 2000, 5000)
    assert pv_kwh_in_range(forecasts, 3000, 4000) == 0.0


# --- soc_forecast ------------------------------------------------------------


def test_soc_forecast_empty_when_no_current_soc() -> None:
    s = _settings()
    out = soc_forecast(
        now_ts=0, end_ts=3600, current_soc_pct=None,
        pv_forecasts=[], load_forecasts=[], settings=s,
    )
    assert out == []


def test_soc_forecast_empty_when_end_before_now() -> None:
    s = _settings()
    out = soc_forecast(
        now_ts=1000, end_ts=500, current_soc_pct=50.0,
        pv_forecasts=[], load_forecasts=[], settings=s,
    )
    assert out == []


def test_soc_forecast_charges_with_surplus() -> None:
    s = _settings()
    # 1 h, pv=2000 W, load=500 W → net 1500 W = 1.5 kWh
    # usable = 13.5 * 0.95 = 12.825 kWh → Δ% ≈ 11.696
    out = soc_forecast(
        now_ts=0, end_ts=3600, current_soc_pct=50.0,
        pv_forecasts=_const_pv(0, 3600, 2000),
        load_forecasts=_const_load(0, 3600, 500),
        settings=s,
    )
    assert out[0] == SocForecast(0, 50.0)
    assert abs(out[-1].soc_pct - 61.696) < 0.1


def test_soc_forecast_discharges_with_deficit() -> None:
    s = _settings()
    # 1 h, pv=0, load=1000 W → net -1000 W = -1 kWh → Δ% ≈ -7.8
    out = soc_forecast(
        now_ts=0, end_ts=3600, current_soc_pct=50.0,
        pv_forecasts=[],
        load_forecasts=_const_load(0, 3600, 1000),
        settings=s,
    )
    assert abs(out[-1].soc_pct - 42.203) < 0.1


def test_soc_forecast_clamps_at_100() -> None:
    s = _settings()
    out = soc_forecast(
        now_ts=0, end_ts=3600, current_soc_pct=99.0,
        pv_forecasts=_const_pv(0, 3600, 5000),  # huge surplus
        load_forecasts=[],
        settings=s,
    )
    assert out[-1].soc_pct == 100.0


def test_soc_forecast_clamps_at_0() -> None:
    s = _settings()
    out = soc_forecast(
        now_ts=0, end_ts=3600, current_soc_pct=1.0,
        pv_forecasts=[],
        load_forecasts=_const_load(0, 3600, 10_000),  # huge deficit
        settings=s,
    )
    assert out[-1].soc_pct == 0.0


def test_soc_forecast_respects_battery_max_charge_kw() -> None:
    # With pv=20 kW load=0 the *unclamped* slope would charge ~12.825 kWh
    # in ~38 min. Clamped to 5 kW, the slope matches the deficit-mirror
    # case at +5 kW exactly.
    s = _settings()
    out_clamped = soc_forecast(
        now_ts=0, end_ts=3600, current_soc_pct=20.0,
        pv_forecasts=_const_pv(0, 3600, 20_000),
        load_forecasts=[],
        settings=s,
    )
    # Reference: same net power obtained without clamp via pv=5 kW.
    out_ref = soc_forecast(
        now_ts=0, end_ts=3600, current_soc_pct=20.0,
        pv_forecasts=_const_pv(0, 3600, 5000),
        load_forecasts=[],
        settings=s,
    )
    assert abs(out_clamped[-1].soc_pct - out_ref[-1].soc_pct) < 1e-6
    # Slope confirms 5 kW: 5 kWh / 12.825 kWh × 100 ≈ 38.986% → final ≈ 58.99
    assert abs(out_clamped[-1].soc_pct - 58.986) < 0.1

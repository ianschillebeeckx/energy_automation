"""SampleStore round-trip tests against a temp SQLite file."""

from __future__ import annotations

from pathlib import Path

from elec_auto.samples import Forecast, ForecastStore, Sample, SampleStore


def _store(tmp_path: Path) -> SampleStore:
    return SampleStore(tmp_path / "samples.db")


def test_round_trip(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.insert(Sample(
        ts=1000, solar_w=4200.0, load_w=900.0, battery_w=-500.0, grid_w=-2800.0,
        soc_pct=75.5, theoretical_w=5100.0,
        charger_amps=20, charger_on=True,
    ))
    rows = s.read_range(0, 2000)
    assert len(rows) == 1
    assert rows[0].solar_w == 4200.0
    assert rows[0].charger_on is True


def test_missing_fields_stored_as_none(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.insert(Sample(
        ts=1000, solar_w=None, load_w=None, battery_w=None, grid_w=None,
        soc_pct=None, theoretical_w=None,
    ))
    [row] = s.read_range(0, 2000)
    assert row.solar_w is None
    assert row.charger_on is None


def test_range_filter(tmp_path: Path) -> None:
    s = _store(tmp_path)
    for t in [10, 20, 30, 40, 50]:
        s.insert(Sample(
            ts=t, solar_w=t * 1.0, load_w=None, battery_w=None, grid_w=None,
            soc_pct=None, theoretical_w=None,
        ))
    rows = s.read_range(20, 40)
    assert [r.ts for r in rows] == [20, 30, 40]


def test_backfill_theoretical_preserves_existing_columns(tmp_path: Path) -> None:
    # An existing real sample shouldn't lose its solar/load/etc when we
    # later backfill theoretical at the same timestamp.
    s = _store(tmp_path)
    s.insert(Sample(
        ts=1000, solar_w=4200.0, load_w=900.0, battery_w=-500.0, grid_w=-2800.0,
        soc_pct=75.5, theoretical_w=None,
        charger_amps=20, charger_on=True,
    ))
    s.backfill_theoretical(1000, 5100.0)
    [row] = s.read_range(0, 2000)
    assert row.solar_w == 4200.0       # preserved
    assert row.charger_on is True      # preserved
    assert row.theoretical_w == 5100.0 # updated


def test_backfill_theoretical_creates_row_when_missing(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.backfill_theoretical(1000, 5100.0)
    [row] = s.read_range(0, 2000)
    assert row.theoretical_w == 5100.0
    assert row.solar_w is None  # no real data


def test_insert_or_replace(tmp_path: Path) -> None:
    # Re-inserting at the same ts should overwrite, not add a duplicate.
    s = _store(tmp_path)
    s.insert(Sample(ts=1000, solar_w=1.0, load_w=None, battery_w=None,
                    grid_w=None, soc_pct=None, theoretical_w=None))
    s.insert(Sample(ts=1000, solar_w=2.0, load_w=None, battery_w=None,
                    grid_w=None, soc_pct=None, theoretical_w=None))
    [row] = s.read_range(0, 2000)
    assert row.solar_w == 2.0


# --- ForecastStore -----------------------------------------------------------

def test_forecast_round_trip(tmp_path: Path) -> None:
    fs = ForecastStore(tmp_path / "samples.db")
    fs.insert_many([Forecast(
        period_ts=1000, fetched_at=900, source="solcast",
        pv_w_p10=2000.0, pv_w_p50=2500.0, pv_w_p90=3000.0,
        air_temp_c=18.5, cloud_opacity_pct=22.0, weather="Sunny",
    )])
    rows = fs.latest_in_range(0, 2000)
    assert len(rows) == 1
    r = rows[0]
    assert r.pv_w_p50 == 2500.0
    assert r.air_temp_c == 18.5
    assert r.weather == "Sunny"


def test_forecast_latest_in_range_picks_most_recent_fetched(tmp_path: Path) -> None:
    fs = ForecastStore(tmp_path / "samples.db")
    # Two forecasts for the same period_ts=1000, fetched at different times.
    fs.insert_many([
        Forecast(period_ts=1000, fetched_at=800, source="solcast", pv_w_p50=1.0),
        Forecast(period_ts=1000, fetched_at=900, source="solcast", pv_w_p50=2.0),
        Forecast(period_ts=2000, fetched_at=900, source="solcast", pv_w_p50=3.0),
    ])
    rows = fs.latest_in_range(0, 3000)
    by_period = {r.period_ts: r.pv_w_p50 for r in rows}
    assert by_period == {1000: 2.0, 2000: 3.0}  # newest fetch for each period


def test_operational_picks_most_recent_fetch_no_later_than_period(tmp_path: Path) -> None:
    fs = ForecastStore(tmp_path / "samples.db")
    # Three fetches at 5:00, 7:42, 9:29 (Unix seconds rough proxy).
    fs.insert_many([
        # 05:00 fetch — predicted values for periods at 09:00, 10:00, 11:00.
        Forecast(period_ts=9_00, fetched_at=5_00, source="solcast", pv_w_p50=11.0),
        Forecast(period_ts=10_00, fetched_at=5_00, source="solcast", pv_w_p50=12.0),
        Forecast(period_ts=11_00, fetched_at=5_00, source="solcast", pv_w_p50=13.0),
        # 07:42 fetch — refined.
        Forecast(period_ts=9_00, fetched_at=7_42, source="solcast", pv_w_p50=21.0),
        Forecast(period_ts=10_00, fetched_at=7_42, source="solcast", pv_w_p50=22.0),
        Forecast(period_ts=11_00, fetched_at=7_42, source="solcast", pv_w_p50=23.0),
        # 09:29 fetch — refined again.
        Forecast(period_ts=9_00, fetched_at=9_29, source="solcast", pv_w_p50=31.0),
        Forecast(period_ts=10_00, fetched_at=9_29, source="solcast", pv_w_p50=32.0),
        Forecast(period_ts=11_00, fetched_at=9_29, source="solcast", pv_w_p50=33.0),
    ])
    rows = fs.operational_in_range(0, 20_00)
    by_period = {r.period_ts: r.pv_w_p50 for r in rows}
    # Period 09:00 — fetches 05:00 and 07:42 were eligible (both ≤ 09:00),
    # 09:29 was not yet issued. Latest eligible = 07:42 → 21.0.
    assert by_period[9_00] == 21.0
    # Period 10:00 — all three fetches eligible. Latest = 09:29 → 32.0.
    assert by_period[10_00] == 32.0
    # Period 11:00 — all three eligible, no later fetch. Latest = 09:29 → 33.0.
    assert by_period[11_00] == 33.0


def test_forecast_fetch_events_returns_one_row_per_fetch(tmp_path: Path) -> None:
    fs = ForecastStore(tmp_path / "samples.db")
    # Two distinct fetches, each writing two period rows.
    fs.insert_many([
        # Fetch at t=1000. Periods at t=900 (closer) and t=2700 (far).
        Forecast(period_ts=900, fetched_at=1000, source="solcast", pv_w_p50=11.0),
        Forecast(period_ts=2700, fetched_at=1000, source="solcast", pv_w_p50=12.0),
        # Fetch at t=2000. Periods at t=2700 (closer) and t=4500.
        Forecast(period_ts=2700, fetched_at=2000, source="solcast", pv_w_p50=21.0),
        Forecast(period_ts=4500, fetched_at=2000, source="solcast", pv_w_p50=22.0),
    ])
    events = fs.fetch_events(0, 10_000)
    # Two events, each picking the period closest to its fetched_at.
    assert events == [(1000, 11.0), (2000, 21.0)]


def test_forecast_source_filter(tmp_path: Path) -> None:
    fs = ForecastStore(tmp_path / "samples.db")
    fs.insert_many([
        Forecast(period_ts=1000, fetched_at=900, source="solcast", pv_w_p50=1.0),
        Forecast(period_ts=1000, fetched_at=900, source="other", pv_w_p50=2.0),
    ])
    [row] = fs.latest_in_range(0, 2000, source="solcast")
    assert row.pv_w_p50 == 1.0
    [row] = fs.latest_in_range(0, 2000, source="other")
    assert row.pv_w_p50 == 2.0

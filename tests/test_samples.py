"""SampleStore round-trip tests against a temp SQLite file."""

from __future__ import annotations

from pathlib import Path

from elec_auto.samples import Sample, SampleStore


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
    assert row.forecast_w is None


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

"""Time-series persistence for chart rendering.

A thin SQLite wrapper around a single `samples` table. The control loop
calls `insert()` every tick; the chart renderer calls `read_range()` for
the last N hours. Missing readings (Powerwall or Emporia briefly down)
are stored as NULL — gaps in the data are honest gaps, not fabrications.

The db lives at `state/samples.db` so it survives restarts but stays out
of git (`state/` is .gitignored).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts              INTEGER PRIMARY KEY,
    solar_w         REAL,
    load_w          REAL,
    battery_w       REAL,
    grid_w          REAL,
    soc_pct         REAL,
    theoretical_w   REAL,
    forecast_w      REAL,
    charger_amps    INTEGER,
    charger_on      INTEGER,
    pw_ok           INTEGER,
    em_ok           INTEGER,
    mode            TEXT,
    decision_amps   INTEGER,
    decision_on     INTEGER,
    decision_reason TEXT
);
"""

# Columns added after the initial schema. Applied via ALTER TABLE for
# existing DBs; no-op for fresh installs (the column already exists from
# the CREATE TABLE above).
_MIGRATIONS: list[tuple[str, str]] = [
    ("pw_ok", "INTEGER"),
    ("em_ok", "INTEGER"),
    ("mode", "TEXT"),
    ("decision_amps", "INTEGER"),
    ("decision_on", "INTEGER"),
    ("decision_reason", "TEXT"),
]


@dataclass(slots=True)
class Sample:
    ts: int  # unix seconds
    solar_w: float | None
    load_w: float | None
    battery_w: float | None
    grid_w: float | None
    soc_pct: float | None
    theoretical_w: float | None
    forecast_w: float | None = None
    charger_amps: int | None = None
    charger_on: bool | None = None
    pw_ok: bool | None = None
    em_ok: bool | None = None
    mode: str | None = None
    decision_amps: int | None = None
    decision_on: bool | None = None
    decision_reason: str | None = None


class SampleStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            existing = {row[1] for row in conn.execute("PRAGMA table_info(samples)")}
            for col, sql_type in _MIGRATIONS:
                if col not in existing:
                    conn.execute(f"ALTER TABLE samples ADD COLUMN {col} {sql_type}")

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: autocommit, simpler for our write-only-from-
        # one-thread workload. timeout=5s tolerates the very rare lock from
        # an overlapping read.
        return sqlite3.connect(self._db_path, isolation_level=None, timeout=5.0)

    def insert(self, sample: Sample) -> None:
        def _bool(v: bool | None) -> int | None:
            return None if v is None else int(v)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO samples
                (ts, solar_w, load_w, battery_w, grid_w, soc_pct,
                 theoretical_w, forecast_w, charger_amps, charger_on,
                 pw_ok, em_ok, mode, decision_amps, decision_on, decision_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sample.ts, sample.solar_w, sample.load_w, sample.battery_w,
                    sample.grid_w, sample.soc_pct, sample.theoretical_w,
                    sample.forecast_w, sample.charger_amps,
                    _bool(sample.charger_on),
                    _bool(sample.pw_ok), _bool(sample.em_ok), sample.mode,
                    sample.decision_amps, _bool(sample.decision_on),
                    sample.decision_reason,
                ),
            )

    def backfill_theoretical(self, ts: int, theoretical_w: float | None) -> None:
        """Set just theoretical_w at `ts`, preserving any other columns.

        Used to seed historical theoretical values without clobbering
        actual readings that happen to share a timestamp.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO samples (ts, theoretical_w) VALUES (?, ?)
                ON CONFLICT(ts) DO UPDATE SET theoretical_w = excluded.theoretical_w
                """,
                (ts, theoretical_w),
            )

    def read_range(self, start_ts: int, end_ts: int) -> list[Sample]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ts, solar_w, load_w, battery_w, grid_w, soc_pct,
                       theoretical_w, forecast_w, charger_amps, charger_on,
                       pw_ok, em_ok, mode, decision_amps, decision_on,
                       decision_reason
                FROM samples WHERE ts BETWEEN ? AND ? ORDER BY ts
                """,
                (start_ts, end_ts),
            ).fetchall()

        def _opt_bool(v: int | None) -> bool | None:
            return None if v is None else bool(v)

        return [
            Sample(
                ts=r[0], solar_w=r[1], load_w=r[2], battery_w=r[3], grid_w=r[4],
                soc_pct=r[5], theoretical_w=r[6], forecast_w=r[7],
                charger_amps=r[8], charger_on=_opt_bool(r[9]),
                pw_ok=_opt_bool(r[10]), em_ok=_opt_bool(r[11]), mode=r[12],
                decision_amps=r[13], decision_on=_opt_bool(r[14]),
                decision_reason=r[15],
            )
            for r in rows
        ]

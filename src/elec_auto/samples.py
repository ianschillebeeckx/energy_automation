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
    charger_amps    INTEGER,
    charger_on      INTEGER,
    charger_status  TEXT,
    action_name     TEXT,
    pw_ok           INTEGER,
    em_ok           INTEGER,
    mode            TEXT,
    decision_amps   INTEGER,
    decision_on     INTEGER,
    decision_reason TEXT
);
CREATE TABLE IF NOT EXISTS forecasts (
    period_ts                    INTEGER NOT NULL,
    fetched_at                   INTEGER NOT NULL,
    source                       TEXT    NOT NULL,
    pv_w_p10                     REAL,
    pv_w_p50                     REAL,
    pv_w_p90                     REAL,
    ghi_w_per_m2                 REAL,
    ghi_w_per_m2_p10             REAL,
    ghi_w_per_m2_p90             REAL,
    dni_w_per_m2                 REAL,
    dni_w_per_m2_p10             REAL,
    dni_w_per_m2_p90             REAL,
    dhi_w_per_m2                 REAL,
    air_temp_c                   REAL,
    cloud_opacity_pct            REAL,
    relative_humidity_pct        REAL,
    surface_pressure_hpa         REAL,
    precipitable_water_kg_per_m2 REAL,
    wind_speed_m_per_s           REAL,
    wind_direction_deg           REAL,
    weather                      TEXT,
    PRIMARY KEY (period_ts, fetched_at, source)
);
CREATE INDEX IF NOT EXISTS idx_forecasts_period ON forecasts(period_ts);
CREATE TABLE IF NOT EXISTS weather (
    period_ts        INTEGER NOT NULL,
    fetched_at       INTEGER NOT NULL,
    source           TEXT    NOT NULL,
    temperature_c    REAL,
    dewpoint_c       REAL,
    rel_humidity_pct REAL,
    prob_precip_pct  REAL,
    wind_speed_mph   REAL,
    wind_dir         TEXT,
    short_forecast   TEXT,
    sky_cover_pct    REAL,
    PRIMARY KEY (period_ts, fetched_at, source)
);
CREATE INDEX IF NOT EXISTS idx_weather_period ON weather(period_ts);
CREATE TABLE IF NOT EXISTS observations (
    period_ts        INTEGER NOT NULL,
    station_id       TEXT    NOT NULL,
    fetched_at       INTEGER,
    temperature_c    REAL,
    dewpoint_c       REAL,
    rel_humidity_pct REAL,
    wind_speed_mph   REAL,
    wind_dir         TEXT,
    text_description TEXT,
    sky_cover_pct    REAL,
    PRIMARY KEY (period_ts, station_id)
);
CREATE INDEX IF NOT EXISTS idx_observations_period ON observations(period_ts);
CREATE TABLE IF NOT EXISTS loads (
    ts      INTEGER NOT NULL,
    circuit TEXT    NOT NULL,
    watts   REAL    NOT NULL,
    PRIMARY KEY (ts, circuit)
);
CREATE INDEX IF NOT EXISTS idx_loads_ts ON loads(ts);
CREATE INDEX IF NOT EXISTS idx_loads_circuit ON loads(circuit);
"""

# Columns added after the initial samples schema. Applied via ALTER TABLE
# for existing DBs; no-op for fresh installs (the columns are already in
# CREATE TABLE above).
_SAMPLES_MIGRATIONS: list[tuple[str, str]] = [
    ("pw_ok", "INTEGER"),
    ("em_ok", "INTEGER"),
    ("mode", "TEXT"),
    ("decision_amps", "INTEGER"),
    ("decision_on", "INTEGER"),
    ("decision_reason", "TEXT"),
    ("charger_status", "TEXT"),
    ("action_name", "TEXT"),
]


def _init_db(db_path: Path) -> None:
    """Create the schema if missing and apply forward migrations. Idempotent."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, isolation_level=None, timeout=5.0) as conn:
        conn.executescript(_SCHEMA)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(samples)")}
        for col, sql_type in _SAMPLES_MIGRATIONS:
            if col not in existing:
                conn.execute(f"ALTER TABLE samples ADD COLUMN {col} {sql_type}")
        # Legacy: forecasts used to live as samples.forecast_w; they're now in
        # their own table. Drop the column on existing DBs (SQLite 3.35+).
        if "forecast_w" in existing:
            try:
                conn.execute("ALTER TABLE samples DROP COLUMN forecast_w")
            except sqlite3.OperationalError:
                # Older SQLite: leave the column alone; it just stays NULL.
                pass


@dataclass(slots=True)
class Sample:
    ts: int  # unix seconds
    solar_w: float | None
    load_w: float | None
    battery_w: float | None
    grid_w: float | None
    soc_pct: float | None
    theoretical_w: float | None
    charger_amps: int | None = None
    charger_on: bool | None = None
    charger_status: str | None = None  # Emporia EVSE status (e.g. "Charging")
    action_name: str | None = None     # Which Action fired this tick (e.g. "surplus")
    pw_ok: bool | None = None
    em_ok: bool | None = None
    mode: str | None = None
    decision_amps: int | None = None
    decision_on: bool | None = None
    decision_reason: str | None = None


@dataclass(slots=True)
class Forecast:
    """One 30-minute forecast period from a weather/PV provider."""
    period_ts: int                       # period midpoint, unix seconds
    fetched_at: int                      # when we fetched, unix seconds
    source: str                          # 'solcast', 'forecast.solar', etc.
    pv_w_p10: float | None = None
    pv_w_p50: float | None = None
    pv_w_p90: float | None = None
    ghi_w_per_m2: float | None = None
    ghi_w_per_m2_p10: float | None = None
    ghi_w_per_m2_p90: float | None = None
    dni_w_per_m2: float | None = None
    dni_w_per_m2_p10: float | None = None
    dni_w_per_m2_p90: float | None = None
    dhi_w_per_m2: float | None = None
    air_temp_c: float | None = None
    cloud_opacity_pct: float | None = None
    relative_humidity_pct: float | None = None
    surface_pressure_hpa: float | None = None
    precipitable_water_kg_per_m2: float | None = None
    wind_speed_m_per_s: float | None = None
    wind_direction_deg: float | None = None
    weather: str | None = None


class SampleStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        _init_db(db_path)

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
                 theoretical_w, charger_amps, charger_on, charger_status,
                 action_name,
                 pw_ok, em_ok, mode, decision_amps, decision_on, decision_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sample.ts, sample.solar_w, sample.load_w, sample.battery_w,
                    sample.grid_w, sample.soc_pct, sample.theoretical_w,
                    sample.charger_amps,
                    _bool(sample.charger_on),
                    sample.charger_status,
                    sample.action_name,
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
                       theoretical_w, charger_amps, charger_on, charger_status,
                       action_name,
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
                soc_pct=r[5], theoretical_w=r[6],
                charger_amps=r[7], charger_on=_opt_bool(r[8]),
                charger_status=r[9], action_name=r[10],
                pw_ok=_opt_bool(r[11]), em_ok=_opt_bool(r[12]), mode=r[13],
                decision_amps=r[14], decision_on=_opt_bool(r[15]),
                decision_reason=r[16],
            )
            for r in rows
        ]


def _cloud_to_label(pct: float) -> str:
    """Coarse qualitative label from cloud opacity %."""
    if pct < 12.5:
        return "Clear"
    if pct < 37.5:
        return "Mostly clear"
    if pct < 62.5:
        return "Partly cloudy"
    if pct < 87.5:
        return "Mostly cloudy"
    return "Overcast"


_FORECAST_COLS = (
    "period_ts", "fetched_at", "source",
    "pv_w_p10", "pv_w_p50", "pv_w_p90",
    "ghi_w_per_m2", "ghi_w_per_m2_p10", "ghi_w_per_m2_p90",
    "dni_w_per_m2", "dni_w_per_m2_p10", "dni_w_per_m2_p90",
    "dhi_w_per_m2",
    "air_temp_c", "cloud_opacity_pct", "relative_humidity_pct",
    "surface_pressure_hpa", "precipitable_water_kg_per_m2",
    "wind_speed_m_per_s", "wind_direction_deg",
    "weather",
)


class ForecastStore:
    """SQLite store for versioned weather/PV forecasts.

    Each Solcast (or other source) fetch inserts one row per 30-min period
    keyed by (period_ts, fetched_at, source), so we keep the full history
    of how predictions evolved. The chart reads the *latest* per period
    via `latest_in_range`.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        _init_db(db_path)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, isolation_level=None, timeout=5.0)

    def fetch_events(
        self, start_ts: int, end_ts: int, source: str = "solcast",
    ) -> list[tuple[int, float | None]]:
        """Refresh events in [start_ts, end_ts]: (fetched_at, pv_w_p50_at_fetch).

        The y-value is the median forecast for the 30-min period that
        contains `fetched_at` — i.e. "what did Solcast predict for the
        moment we made the call?". Useful for placing markers on the
        chart's forecast line.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT f.fetched_at, f.pv_w_p50
                FROM forecasts f
                WHERE f.source = ?
                  AND f.fetched_at BETWEEN ? AND ?
                  AND f.period_ts = (
                      SELECT period_ts FROM forecasts f2
                      WHERE f2.source = f.source AND f2.fetched_at = f.fetched_at
                      ORDER BY ABS(f2.period_ts - f2.fetched_at)
                      LIMIT 1
                  )
                ORDER BY f.fetched_at
                """,
                (source, start_ts, end_ts),
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def current_qualitative(self, source: str = "solcast") -> str | None:
        """Qualitative weather label for the period containing "now".

        Prefers Solcast's categorical `weather` field; falls back to a
        cloud-opacity-derived label so we still get a useful answer when
        the API tier doesn't surface `weather` directly.
        """
        import time as _t
        now_ts = int(_t.time())
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT weather, cloud_opacity_pct FROM forecasts
                WHERE source = ?
                  AND ABS(period_ts - ?) <= 900
                ORDER BY fetched_at DESC, ABS(period_ts - ?) ASC
                LIMIT 1
                """,
                (source, now_ts, now_ts),
            ).fetchone()
        if not row:
            return None
        weather, cloud_pct = row
        if weather:
            return weather
        if cloud_pct is None:
            return None
        return _cloud_to_label(float(cloud_pct))

    def last_fetched_at(self, source: str = "solcast") -> int | None:
        """Most recent fetched_at for `source`, or None if no rows."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(fetched_at) FROM forecasts WHERE source = ?",
                (source,),
            ).fetchone()
        return row[0] if row and row[0] is not None else None

    def insert_many(self, forecasts: list[Forecast]) -> None:
        placeholders = ", ".join("?" * len(_FORECAST_COLS))
        rows = [
            tuple(getattr(f, col) for col in _FORECAST_COLS) for f in forecasts
        ]
        with self._connect() as conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO forecasts ({', '.join(_FORECAST_COLS)}) "
                f"VALUES ({placeholders})",
                rows,
            )

    def operational_in_range(
        self, start_ts: int, end_ts: int, source: str = "solcast",
    ) -> list[Forecast]:
        """For each period in [start, end], the forecast that was the most
        recent one *as of that period's own timestamp*.

        For future periods this is identical to `latest_in_range` (every
        fetch we have is older than the period). For past periods it's the
        forecast that was operational at that time — ignoring later
        refinements that we have the benefit of hindsight on.

        So if Solcast refreshed at 05:00, 07:42, 09:29 today, the
        operational forecast for the 09:00 period comes from the 07:42
        fetch (not 09:29, which arrived later).
        """
        select_cols = ", ".join(f"f1.{c}" for c in _FORECAST_COLS)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {select_cols}
                FROM forecasts f1
                WHERE f1.source = ?
                  AND f1.period_ts BETWEEN ? AND ?
                  AND f1.fetched_at = (
                      SELECT MAX(f2.fetched_at) FROM forecasts f2
                      WHERE f2.source = f1.source
                        AND f2.period_ts = f1.period_ts
                        AND f2.fetched_at <= f1.period_ts
                  )
                ORDER BY f1.period_ts
                """,
                (source, start_ts, end_ts),
            ).fetchall()
        return [Forecast(**dict(zip(_FORECAST_COLS, r))) for r in rows]

    def latest_in_range(
        self, start_ts: int, end_ts: int, source: str = "solcast",
    ) -> list[Forecast]:
        """Return the most recent forecast per period in [start_ts, end_ts]."""
        select_cols = ", ".join(f"f1.{c}" for c in _FORECAST_COLS)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {select_cols}
                FROM forecasts f1
                WHERE f1.source = ?
                  AND f1.period_ts BETWEEN ? AND ?
                  AND f1.fetched_at = (
                      SELECT MAX(f2.fetched_at) FROM forecasts f2
                      WHERE f2.period_ts = f1.period_ts AND f2.source = f1.source
                  )
                ORDER BY f1.period_ts
                """,
                (source, start_ts, end_ts),
            ).fetchall()
        return [Forecast(**dict(zip(_FORECAST_COLS, r))) for r in rows]


@dataclass(slots=True)
class Weather:
    """One hourly weather period from a forecast provider (NWS today)."""
    period_ts: int                      # period midpoint, unix seconds
    fetched_at: int                     # when we fetched, unix seconds
    source: str                         # 'nws'
    temperature_c: float | None = None
    dewpoint_c: float | None = None
    rel_humidity_pct: float | None = None
    prob_precip_pct: float | None = None
    wind_speed_mph: float | None = None
    wind_dir: str | None = None         # cardinal, e.g. "WSW"
    short_forecast: str | None = None   # e.g. "Sunny", "Mostly Cloudy"
    sky_cover_pct: float | None = None  # 0-100, merged from /gridpoints


_WEATHER_COLS = (
    "period_ts", "fetched_at", "source",
    "temperature_c", "dewpoint_c", "rel_humidity_pct", "prob_precip_pct",
    "wind_speed_mph", "wind_dir", "short_forecast", "sky_cover_pct",
)


class WeatherStore:
    """SQLite store for versioned hourly weather rows.

    Mirrors ForecastStore: (period_ts, fetched_at, source) is the PK so
    we keep the full history of how the forecast evolved. Readers use
    `latest_in_range` for "what's the best estimate per hour right now".
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        _init_db(db_path)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, isolation_level=None, timeout=5.0)

    def insert_many(self, rows: list[Weather]) -> None:
        if not rows:
            return
        placeholders = ", ".join("?" * len(_WEATHER_COLS))
        payload = [
            tuple(getattr(w, col) for col in _WEATHER_COLS) for w in rows
        ]
        with self._connect() as conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO weather ({', '.join(_WEATHER_COLS)}) "
                f"VALUES ({placeholders})",
                payload,
            )

    def latest_in_range(
        self, start_ts: int, end_ts: int, source: str = "nws",
    ) -> list[Weather]:
        """Most recent weather row per period in [start_ts, end_ts]."""
        select_cols = ", ".join(f"w1.{c}" for c in _WEATHER_COLS)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {select_cols}
                FROM weather w1
                WHERE w1.source = ?
                  AND w1.period_ts BETWEEN ? AND ?
                  AND w1.fetched_at = (
                      SELECT MAX(w2.fetched_at) FROM weather w2
                      WHERE w2.period_ts = w1.period_ts AND w2.source = w1.source
                  )
                ORDER BY w1.period_ts
                """,
                (source, start_ts, end_ts),
            ).fetchall()
        return [Weather(**dict(zip(_WEATHER_COLS, r))) for r in rows]

    def last_fetched_at(self, source: str = "nws") -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(fetched_at) FROM weather WHERE source = ?",
                (source,),
            ).fetchone()
        return row[0] if row and row[0] is not None else None


@dataclass(slots=True)
class Observation:
    """One past hourly weather observation from an NWS station."""
    period_ts: int                      # top-of-hour, UTC unix seconds
    station_id: str                     # e.g. 'KSFO'
    fetched_at: int | None = None
    temperature_c: float | None = None
    dewpoint_c: float | None = None
    rel_humidity_pct: float | None = None
    wind_speed_mph: float | None = None
    wind_dir: str | None = None         # cardinal, e.g. 'WSW' (from degrees)
    text_description: str | None = None # e.g. 'Clear', 'Light Rain'
    sky_cover_pct: float | None = None  # derived from cloudLayers amount


_OBSERVATION_COLS = (
    "period_ts", "station_id", "fetched_at",
    "temperature_c", "dewpoint_c", "rel_humidity_pct",
    "wind_speed_mph", "wind_dir", "text_description", "sky_cover_pct",
)


class ObservationStore:
    """SQLite store for past hourly weather observations.

    The PK is (period_ts, station_id) — past observations don't get
    revised between fetches (NWS QC tweaks are negligible for our use),
    so re-fetching the same hour overwrites in place. NWS only retains
    ~7 days of observations publicly, so this table is also our local
    long-term archive: every 5 AM fetch tops it up.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        _init_db(db_path)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, isolation_level=None, timeout=5.0)

    def insert_many(self, rows: list[Observation]) -> None:
        if not rows:
            return
        placeholders = ", ".join("?" * len(_OBSERVATION_COLS))
        payload = [
            tuple(getattr(o, col) for col in _OBSERVATION_COLS) for o in rows
        ]
        with self._connect() as conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO observations "
                f"({', '.join(_OBSERVATION_COLS)}) VALUES ({placeholders})",
                payload,
            )

    def read_range(
        self, start_ts: int, end_ts: int, station_id: str | None = None,
    ) -> list[Observation]:
        """All observations in [start_ts, end_ts], optionally filtered by station."""
        select_cols = ", ".join(_OBSERVATION_COLS)
        query = (
            f"SELECT {select_cols} FROM observations "
            f"WHERE period_ts BETWEEN ? AND ?"
        )
        args: tuple = (start_ts, end_ts)
        if station_id is not None:
            query += " AND station_id = ?"
            args = (start_ts, end_ts, station_id)
        query += " ORDER BY period_ts"
        with self._connect() as conn:
            rows = conn.execute(query, args).fetchall()
        return [Observation(**dict(zip(_OBSERVATION_COLS, r))) for r in rows]

    def last_fetched_at(self, station_id: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(fetched_at) FROM observations WHERE station_id = ?",
                (station_id,),
            ).fetchone()
        return row[0] if row and row[0] is not None else None


@dataclass(slots=True)
class CircuitReading:
    ts: int
    circuit: str
    watts: float


class LoadStore:
    """Per-circuit load samples. Sparse — only rows with non-trivial draw."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        _init_db(db_path)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, isolation_level=None, timeout=5.0)

    def insert_tick(self, ts: int, circuits: dict[str, float]) -> None:
        """Insert one tick of circuit readings. `circuits` should already be
        filtered to non-zero entries by the caller."""
        rows = [(ts, name, float(watts)) for name, watts in circuits.items()]
        if not rows:
            return
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO loads (ts, circuit, watts) VALUES (?, ?, ?)",
                rows,
            )

    def read_range(
        self,
        start_ts: int,
        end_ts: int,
        circuit: str | None = None,
    ) -> list[CircuitReading]:
        """All non-zero readings in [start_ts, end_ts], optionally filtered."""
        query = "SELECT ts, circuit, watts FROM loads WHERE ts BETWEEN ? AND ?"
        args: tuple = (start_ts, end_ts)
        if circuit is not None:
            query += " AND circuit = ?"
            args = (start_ts, end_ts, circuit)
        query += " ORDER BY ts, circuit"
        with self._connect() as conn:
            rows = conn.execute(query, args).fetchall()
        return [CircuitReading(ts=r[0], circuit=r[1], watts=r[2]) for r in rows]

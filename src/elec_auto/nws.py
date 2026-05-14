"""US National Weather Service (api.weather.gov) hourly forecast client.

Builds a row per future hour by merging two endpoints:

  - `/gridpoints/{office}/{x},{y}/forecast/hourly` — pre-merged, clean
    per-hour periods (temperature, dewpoint, humidity, wind, short-forecast).
  - `/gridpoints/{office}/{x},{y}` — raw grid timeseries; we pull
    `skyCover` from here since it's not in the friendlier endpoint.

The `/points/{lat},{lon}` → grid lookup is cached in memory after the
first call. Lat/lon must be configured (NWS is US-only); the caller is
expected to short-circuit when they're not set.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

_OBS_LIMIT = 500  # NWS max per /observations call

from .config import Settings
from .samples import Observation, Weather

_API_BASE = "https://api.weather.gov"
_DURATION_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")

# Standard METAR cloud-amount codes mapped to nominal sky-cover %.
# Based on the meteorological definition (CLR=0/8 sky, FEW=1-2/8, SCT=3-4/8,
# BKN=5-7/8, OVC=8/8). Used to normalize observation cloudLayers into the
# same `sky_cover_pct` column the forecast uses.
_CLOUD_PCT = {
    "SKC": 0.0, "CLR": 0.0,
    "FEW": 15.0, "SCT": 37.0, "BKN": 75.0, "OVC": 100.0,
    "VV": 100.0,  # vertical visibility (obscured)
}
_CARDINAL_16 = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)
_MS_TO_MPH = 2.23694


def _parse_duration(s: str) -> timedelta:
    """Parse an ISO 8601 duration like 'PT2H', 'P1DT3H', 'PT15M'.

    Returns timedelta(0) on anything we don't recognize — NWS only emits
    the H/D/M subset for forecast grids, so a stricter parser is fine.
    """
    m = _DURATION_RE.match(s)
    if not m:
        return timedelta()
    days, hours, mins = (int(x) if x else 0 for x in m.groups())
    return timedelta(days=days, hours=hours, minutes=mins)


def _parse_wind_speed_mph(s: str | None) -> float | None:
    """'3 mph' or '3 to 8 mph' → 3.0 (low end of any range)."""
    if not s:
        return None
    m = re.match(r"\s*(\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else None


def _extract_value(v: dict | None) -> float | None:
    """Pull `.value` out of an NWS quantitative dict, casting to float."""
    if not v:
        return None
    val = v.get("value")
    return float(val) if val is not None else None


def _deg_to_cardinal(deg: float | None) -> str | None:
    """Compass degree → 16-point cardinal label (e.g., 290° → 'WNW')."""
    if deg is None:
        return None
    return _CARDINAL_16[round(deg / 22.5) % 16]


def _cloud_layers_to_pct(layers: list | None) -> float | None:
    """Pick the densest layer's nominal sky-cover %."""
    if not layers:
        return None
    pcts = [
        _CLOUD_PCT[l["amount"]]
        for l in layers
        if isinstance(l, dict) and l.get("amount") in _CLOUD_PCT
    ]
    return max(pcts) if pcts else None


def _expand_grid(values: list[dict]) -> dict[int, Any]:
    """Expand multi-hour grid entries into a per-hour map (hour_start_ts → value).

    Each NWS grid value has a validTime like
    '2026-05-14T03:00:00+00:00/PT2H', meaning the value applies for 2 hours
    starting at that instant. We materialize one entry per covered hour,
    keyed by the unix timestamp at the top of the hour (UTC).
    """
    out: dict[int, Any] = {}
    for entry in values:
        vt = entry.get("validTime")
        if not vt or "/" not in vt:
            continue
        start_str, dur_str = vt.split("/", 1)
        try:
            start = datetime.fromisoformat(start_str)
        except ValueError:
            continue
        duration = _parse_duration(dur_str)
        hours = max(1, int(duration.total_seconds() // 3600))
        v = entry.get("value")
        for h in range(hours):
            hour_ts = int((start + timedelta(hours=h)).timestamp())
            out[hour_ts] = v
    return out


def _observation_from(
    props: dict, period_ts: int, station_id: str, fetched_at: int,
) -> Observation | None:
    """Map an /observations feature's properties → Observation row.

    Returns None on an empty payload; otherwise produces a row with any
    missing fields stored as NULL.
    """
    if not props:
        return None
    wind_ms = _extract_value(props.get("windSpeed"))
    wind_mph = wind_ms * _MS_TO_MPH if wind_ms is not None else None
    return Observation(
        period_ts=period_ts,
        station_id=station_id,
        fetched_at=fetched_at,
        temperature_c=_extract_value(props.get("temperature")),
        dewpoint_c=_extract_value(props.get("dewpoint")),
        rel_humidity_pct=_extract_value(props.get("relativeHumidity")),
        wind_speed_mph=wind_mph,
        wind_dir=_deg_to_cardinal(_extract_value(props.get("windDirection"))),
        text_description=props.get("textDescription") or None,
        sky_cover_pct=_cloud_layers_to_pct(props.get("cloudLayers")),
    )


@dataclass
class _Gridpoint:
    office: str
    x: int
    y: int


class NWS:
    SOURCE = "nws"

    def __init__(self, settings: Settings, timeout: float = 15.0) -> None:
        self._s = settings
        self._timeout = timeout
        self._gridpoint: _Gridpoint | None = None
        self._headers = {
            "User-Agent": settings.nws_user_agent,
            "Accept": "application/geo+json",
        }

    @property
    def configured(self) -> bool:
        return self._s.latitude is not None and self._s.longitude is not None

    def _get_gridpoint(self) -> _Gridpoint:
        if self._gridpoint is not None:
            return self._gridpoint
        r = requests.get(
            f"{_API_BASE}/points/{self._s.latitude},{self._s.longitude}",
            headers=self._headers, timeout=self._timeout,
        )
        r.raise_for_status()
        props = r.json()["properties"]
        self._gridpoint = _Gridpoint(
            office=props["gridId"],
            x=int(props["gridX"]),
            y=int(props["gridY"]),
        )
        return self._gridpoint

    def fetch(self, horizon_hours: int | None = None) -> list[Weather]:
        if not self.configured:
            raise RuntimeError("LATITUDE/LONGITUDE not set; NWS fetch needs them")
        gp = self._get_gridpoint()
        fetched_at = int(time.time())

        # /forecast/hourly — pre-merged hourly rows.
        r_h = requests.get(
            f"{_API_BASE}/gridpoints/{gp.office}/{gp.x},{gp.y}/forecast/hourly",
            headers=self._headers, timeout=self._timeout,
        )
        r_h.raise_for_status()
        periods = r_h.json()["properties"]["periods"]
        if horizon_hours is not None:
            periods = periods[:horizon_hours]

        # /gridpoints — raw; we want skyCover specifically.
        r_g = requests.get(
            f"{_API_BASE}/gridpoints/{gp.office}/{gp.x},{gp.y}",
            headers=self._headers, timeout=self._timeout,
        )
        r_g.raise_for_status()
        gp_props = r_g.json()["properties"]
        sky_by_hour = _expand_grid(gp_props.get("skyCover", {}).get("values", []))

        return [
            row for row in (
                self._row(p, fetched_at, sky_by_hour) for p in periods
            )
            if row is not None
        ]

    def fetch_observations(
        self, hours: int = 24, station_id: str = "KSFO",
    ) -> list[Observation]:
        """Past station observations downsampled to one row per top-of-hour.

        For each hour boundary in [now - hours, now], picks the observation
        whose timestamp is closest to the boundary. Buckets with no obs
        within their hour are silently skipped — gaps remain honest gaps.
        """
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=hours)
        r = requests.get(
            f"{_API_BASE}/stations/{station_id}/observations",
            params={
                "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit": _OBS_LIMIT,
            },
            headers=self._headers, timeout=self._timeout,
        )
        r.raise_for_status()
        raw = r.json()["features"]

        # Bucket by nearest top-of-hour (UTC). Keep the observation whose
        # timestamp is closest to that hour mark.
        buckets: dict[int, tuple[int, dict]] = {}
        for obs in raw:
            try:
                ts_dt = datetime.fromisoformat(obs["properties"]["timestamp"])
            except (KeyError, ValueError):
                continue
            ts = int(ts_dt.timestamp())
            hour_ts = ((ts + 1800) // 3600) * 3600
            cur = buckets.get(hour_ts)
            if cur is None or abs(ts - hour_ts) < abs(cur[0] - hour_ts):
                buckets[hour_ts] = (ts, obs)

        fetched_at = int(time.time())
        out: list[Observation] = []
        for hour_ts, (_, obs) in sorted(buckets.items()):
            o = _observation_from(obs.get("properties") or {}, hour_ts,
                                  station_id, fetched_at)
            if o is not None:
                out.append(o)
        return out

    def _row(
        self, p: dict, fetched_at: int, sky_by_hour: dict[int, Any],
    ) -> Weather | None:
        try:
            start = datetime.fromisoformat(p["startTime"])
            end = datetime.fromisoformat(p["endTime"])
        except (KeyError, ValueError):
            return None
        mid_ts = int((start + (end - start) / 2).timestamp())
        # skyCover is keyed by UTC top-of-hour; the period startTime sits
        # exactly on a clock-hour boundary so converting is exact.
        hour_ts = int(start.astimezone(timezone.utc).timestamp())

        temp = p.get("temperature")
        unit = (p.get("temperatureUnit") or "F").upper()
        if temp is None:
            temp_c = None
        elif unit == "F":
            temp_c = (float(temp) - 32.0) * 5.0 / 9.0
        else:
            temp_c = float(temp)

        sky = sky_by_hour.get(hour_ts)
        return Weather(
            period_ts=mid_ts,
            fetched_at=fetched_at,
            source=self.SOURCE,
            temperature_c=temp_c,
            dewpoint_c=_extract_value(p.get("dewpoint")),
            rel_humidity_pct=_extract_value(p.get("relativeHumidity")),
            prob_precip_pct=_extract_value(p.get("probabilityOfPrecipitation")),
            wind_speed_mph=_parse_wind_speed_mph(p.get("windSpeed")),
            wind_dir=p.get("windDirection"),
            short_forecast=p.get("shortForecast"),
            sky_cover_pct=float(sky) if sky is not None else None,
        )

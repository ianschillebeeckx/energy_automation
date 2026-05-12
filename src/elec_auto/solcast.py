"""Solcast PV + weather forecast client.

Hobbyist API: 10 GET requests/day per resource. We refresh every
`settings.solcast_refresh_hours` (default 4 h = 6 calls/day, well under
the limit). Each call returns ~14 days of 30-minute periods.

We request every output parameter the API offers so the forecasts table
is loaded for future automations (e.g. precool/preheat based on air
temperature or cloud cover).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import requests

from .config import Settings
from .samples import Forecast

_API_BASE = "https://api.solcast.com.au"

# All the fields we ask Solcast to return. Order doesn't matter; we map by
# name when parsing the JSON.
_OUTPUT_PARAMS = ",".join((
    "pv_estimate", "pv_estimate10", "pv_estimate90",
    "ghi", "ghi10", "ghi90",
    "dni", "dni10", "dni90",
    "dhi",
    "air_temp", "cloud_opacity", "relative_humidity",
    "surface_pressure", "precipitable_water",
    "wind_speed_10m", "wind_direction_10m",
    "weather",
))


_FIVE_AM_HOUR = 5
_DAYLIGHT_SLOTS = 7  # number of fetches spread between sunrise and sunset


def daily_schedule(
    now_local: datetime,
    latitude: float | None,
    longitude: float | None,
) -> list[datetime]:
    """Today's scheduled solcast fetch times in the same timezone as `now_local`.

    Returns:
      1 slot at 05:00 local + 7 slots evenly placed between sunrise and
      sunset (8 total, leaving 2 calls of the 10/day budget in reserve).

    Falls back to just the 05:00 slot when latitude/longitude aren't set.
    """
    if now_local.tzinfo is None:
        raise ValueError("now_local must be timezone-aware")

    five_am = now_local.replace(
        hour=_FIVE_AM_HOUR, minute=0, second=0, microsecond=0,
    )
    schedule: list[datetime] = [five_am]

    if latitude is None or longitude is None:
        return schedule

    try:
        from astral import Observer
        from astral.sun import sun

        obs = Observer(latitude=latitude, longitude=longitude)
        sundata = sun(obs, date=now_local.date(), tzinfo=now_local.tzinfo)
    except Exception:
        return schedule

    sunrise, sunset = sundata["sunrise"], sundata["sunset"]
    if sunset <= sunrise:
        return schedule  # polar edge cases — fall back to just 5 AM

    # Place 7 slots strictly between sunrise and sunset (not on them):
    # at sunrise + step*1, +step*2, ..., +step*7, where step = (sunset-sunrise)/8.
    step = (sunset - sunrise) / (_DAYLIGHT_SLOTS + 1)
    for i in range(1, _DAYLIGHT_SLOTS + 1):
        schedule.append(sunrise + step * i)
    return sorted(schedule)


def _parse_period_end(s: str) -> datetime:
    # Solcast returns 7-digit fractional seconds + "Z"; strip to the second.
    return datetime.fromisoformat(s[:19]).replace(tzinfo=timezone.utc)


def _kw_to_w(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v) * 1000.0
    except (TypeError, ValueError):
        return None


def _float(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Solcast:
    SOURCE = "solcast"

    def __init__(self, settings: Settings, timeout: float = 15.0) -> None:
        self._s = settings
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._s.solcast_api_key and self._s.solcast_resource_id)

    def fetch(self, hours: int = 48) -> list[Forecast]:
        """Return forecast rows for roughly the next `hours` hours.

        Solcast actually returns ~14 days; `hours` is advisory. We don't
        truncate so the DB gets the full picture.
        """
        if not self.configured:
            raise RuntimeError("Solcast API key or resource ID not configured")
        r = requests.get(
            f"{_API_BASE}/rooftop_sites/{self._s.solcast_resource_id}/forecasts",
            params={
                "api_key": self._s.solcast_api_key,
                "format": "json",
                "hours": hours,
                "output_parameters": _OUTPUT_PARAMS,
            },
            timeout=self._timeout,
        )
        r.raise_for_status()
        fetched_at = int(time.time())
        out: list[Forecast] = []
        for f in r.json().get("forecasts", []):
            try:
                end = _parse_period_end(f["period_end"])
            except (KeyError, ValueError):
                continue
            # Plot at the period midpoint so the line aligns with where
            # production was averaged.
            mid = end - timedelta(minutes=15)
            out.append(Forecast(
                period_ts=int(mid.timestamp()),
                fetched_at=fetched_at,
                source=self.SOURCE,
                pv_w_p10=_kw_to_w(f.get("pv_estimate10")),
                pv_w_p50=_kw_to_w(f.get("pv_estimate")),
                pv_w_p90=_kw_to_w(f.get("pv_estimate90")),
                ghi_w_per_m2=_float(f.get("ghi")),
                ghi_w_per_m2_p10=_float(f.get("ghi10")),
                ghi_w_per_m2_p90=_float(f.get("ghi90")),
                dni_w_per_m2=_float(f.get("dni")),
                dni_w_per_m2_p10=_float(f.get("dni10")),
                dni_w_per_m2_p90=_float(f.get("dni90")),
                dhi_w_per_m2=_float(f.get("dhi")),
                air_temp_c=_float(f.get("air_temp")),
                cloud_opacity_pct=_float(f.get("cloud_opacity")),
                relative_humidity_pct=_float(f.get("relative_humidity")),
                surface_pressure_hpa=_float(f.get("surface_pressure")),
                precipitable_water_kg_per_m2=_float(f.get("precipitable_water")),
                wind_speed_m_per_s=_float(f.get("wind_speed_10m")),
                wind_direction_deg=_float(f.get("wind_direction_10m")),
                weather=f.get("weather"),
            ))
        return out

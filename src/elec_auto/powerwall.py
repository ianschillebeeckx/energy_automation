"""Tesla Powerwall 3 client.

Two backends, selected by `POWERWALL_MODE`:

- **cloud**: Tesla Owner API via `pypowerwall` cloud mode. Works without any
  physical access to the Gateway. One-time OAuth setup via
  `uv run python -m pypowerwall -authpath state setup`.
- **local**: Local TEDAPI on the Gateway. Needs the Gateway password (printed
  on the sticker inside the front cover of the PW3). Lower latency, no cloud
  dependency.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pypowerwall

from .config import Settings


@dataclass(slots=True)
class PowerReading:
    """Instantaneous power balance. Units: watts; percent for SoC.

    Sign conventions (Tesla / pypowerwall):
      solar    >= 0, production
      load     >= 0, total home consumption
      battery  > 0 discharging to the house, < 0 charging from PV/grid
      grid     > 0 importing from utility, < 0 exporting to utility
    """

    solar_w: float
    load_w: float
    battery_w: float
    grid_w: float
    battery_soc_pct: float


class Powerwall:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        auth_dir = Path(settings.powerwall_auth_path).resolve()
        auth_dir.mkdir(parents=True, exist_ok=True)

        if settings.powerwall_mode == "cloud":
            if not settings.tesla_email:
                raise RuntimeError(
                    "cloud mode requires TESLA_EMAIL in .env and a one-time "
                    "`uv run python -m pypowerwall -authpath state setup`."
                )
            self._pw = pypowerwall.Powerwall(
                host="",
                password="",
                email=settings.tesla_email,
                timezone=settings.timezone,
                cloudmode=True,
                siteid=settings.tesla_site_id,
                authpath=str(auth_dir),
            )
        else:  # local TEDAPI
            if not (settings.powerwall_host and settings.powerwall_gw_password):
                raise RuntimeError(
                    "local mode requires POWERWALL_HOST and POWERWALL_GW_PASSWORD."
                )
            self._pw = pypowerwall.Powerwall(
                host=settings.powerwall_host,
                password="",
                email=settings.tesla_email or "nobody@nowhere.com",
                timezone=settings.timezone,
                gw_pwd=settings.powerwall_gw_password,
                authpath=str(auth_dir),
            )

    def read(self) -> PowerReading:
        p = self._pw.power() or {}
        soc = self._pw.level()
        return PowerReading(
            solar_w=float(p.get("solar", 0.0)),
            load_w=float(p.get("load", 0.0)),
            battery_w=float(p.get("battery", 0.0)),
            grid_w=float(p.get("site", 0.0)),
            battery_soc_pct=float(soc) if soc is not None else float("nan"),
        )

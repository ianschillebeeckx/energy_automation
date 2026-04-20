"""Tesla Powerwall 3 local client.

Powerwall 3 does not expose the legacy local web API that PW2 did. Access goes
through TEDAPI (Tesla Energy Device API) on the Gateway. `pypowerwall` handles
the TEDAPI handshake when given the Gateway password.
"""

from __future__ import annotations

from dataclasses import dataclass

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
        if not settings.powerwall_host:
            raise RuntimeError("POWERWALL_HOST is not set in .env")
        self._pw = pypowerwall.Powerwall(
            host=settings.powerwall_host,
            password=settings.powerwall_password or "",
            gw_pwd=settings.powerwall_password or "",
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

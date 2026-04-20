"""Emporia EV Charger Classic client (cloud API via pyemvue)."""

from __future__ import annotations

from dataclasses import dataclass

from pyemvue import PyEmVue
from pyemvue.device import ChargerDevice

from .config import Settings


@dataclass(slots=True)
class ChargerState:
    gid: int
    name: str
    on: bool
    charge_rate_a: int
    max_charge_rate_a: int


class Emporia:
    def __init__(self, settings: Settings) -> None:
        if not (settings.emporia_username and settings.emporia_password):
            raise RuntimeError("EMPORIA_USERNAME/PASSWORD not set in .env")
        self._vue = PyEmVue()
        self._vue.login(
            username=settings.emporia_username,
            password=settings.emporia_password,
        )
        self._evse_gid = settings.emporia_evse_gid

    def list_chargers(self) -> list[ChargerDevice]:
        return [
            d.ev_charger
            for d in self._vue.get_devices()
            if d.ev_charger is not None
        ]

    def _charger(self) -> ChargerDevice:
        chargers = self.list_chargers()
        if not chargers:
            raise RuntimeError("No EV chargers found on Emporia account")
        if self._evse_gid is None:
            if len(chargers) > 1:
                raise RuntimeError(
                    "Multiple chargers found; set EMPORIA_EVSE_GID in .env"
                )
            return chargers[0]
        for c in chargers:
            if c.device_gid == self._evse_gid:
                return c
        raise RuntimeError(f"Charger with gid={self._evse_gid} not found")

    def read(self) -> ChargerState:
        c = self._charger()
        return ChargerState(
            gid=c.device_gid,
            name=c.name or "EV Charger",
            on=bool(c.charger_on),
            charge_rate_a=int(c.charging_rate),
            max_charge_rate_a=int(c.max_charging_rate),
        )

    def set_amps(self, amps: int, *, on: bool | None = None) -> ChargerState:
        """Set charge current (A). If `on` is None, leave on/off state unchanged."""
        c = self._charger()
        c.charging_rate = amps
        if on is not None:
            c.charger_on = on
        updated = self._vue.update_charger(c)
        return ChargerState(
            gid=updated.device_gid,
            name=updated.name or "EV Charger",
            on=bool(updated.charger_on),
            charge_rate_a=int(updated.charging_rate),
            max_charge_rate_a=int(updated.max_charging_rate),
        )

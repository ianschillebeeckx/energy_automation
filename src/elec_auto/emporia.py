"""Emporia EV Charger Classic client (cloud API via pyemvue)."""

from __future__ import annotations

from dataclasses import dataclass

from pyemvue import PyEmVue
from pyemvue.device import ChargerDevice, VueDevice

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

    def _charger_devices(self) -> list[VueDevice]:
        return [d for d in self._vue.get_devices() if d.ev_charger is not None]

    def list_chargers(self) -> list[VueDevice]:
        return self._charger_devices()

    def _select(self) -> VueDevice:
        devices = self._charger_devices()
        if not devices:
            raise RuntimeError("No EV chargers found on Emporia account")
        if self._evse_gid is None:
            if len(devices) > 1:
                raise RuntimeError(
                    "Multiple chargers found; set EMPORIA_EVSE_GID in .env"
                )
            return devices[0]
        for d in devices:
            if d.device_gid == self._evse_gid:
                return d
        raise RuntimeError(f"Charger with gid={self._evse_gid} not found")

    @staticmethod
    def _state(parent: VueDevice, charger: ChargerDevice) -> ChargerState:
        return ChargerState(
            gid=parent.device_gid,
            name=parent.device_name or parent.display_name or "EV Charger",
            on=bool(charger.charger_on),
            charge_rate_a=int(charger.charging_rate),
            max_charge_rate_a=int(charger.max_charging_rate),
        )

    def read(self) -> ChargerState:
        parent = self._select()
        return self._state(parent, parent.ev_charger)

    def set_amps(self, amps: int, *, on: bool | None = None) -> ChargerState:
        """Set charge current (A). If `on` is None, leave on/off state unchanged."""
        parent = self._select()
        updated = self._vue.update_charger(parent.ev_charger, on=on, charge_rate=amps)
        return self._state(parent, updated)

"""Emporia EV Charger Classic client (cloud API via pyemvue).

Also exposes `top_consumers` when the account has a Vue2 panel monitor with
CTs on named circuits — used by the dashboard to list the biggest draws
alongside the Home node in the flow diagram.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from pyemvue import PyEmVue
from pyemvue.device import ChargerDevice, VueDevice
from pyemvue.enums import Scale

from .config import Settings

# Scale.MINUTE returns kWh consumed over the last minute; multiply to get avg W.
_KWH_PER_MIN_TO_W = 60_000.0

# Synthetic + EV channels we never want to show as a "consumer". The EV has
# its own node on the dashboard, so even if a CT is added to that breaker we
# don't want it double-counted in "Top loads".
_CONSUMER_EXCLUDE = {"Main", "Balance", "", "EV Charger", "EV", "Car", "Tesla"}


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

    def top_consumers(self, n: int = 3) -> list[tuple[str, float]]:
        """Return top-N named circuits by instantaneous power draw, in watts.

        Requires a Vue2 panel monitor on the account; with only the EVSE this
        returns []. Excludes the synthetic "Main"/"Balance" aggregate rows.
        """
        devices = self._vue.get_devices()
        gids = [d.device_gid for d in devices]
        if not gids:
            return []
        usage = self._vue.get_device_list_usage(
            gids, instant=datetime.now(timezone.utc), scale=Scale.MINUTE.value,
        )
        rows: list[tuple[str, float]] = []
        for dev in usage.values():
            for ch in dev.channels.values():
                name = (ch.name or "").strip()
                if name in _CONSUMER_EXCLUDE:
                    continue
                watts = float(ch.usage or 0.0) * _KWH_PER_MIN_TO_W
                if watts <= 0:
                    continue
                rows.append((name, watts))
        rows.sort(key=lambda r: r[1], reverse=True)
        return rows[:n]

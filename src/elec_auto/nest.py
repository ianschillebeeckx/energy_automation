"""Google Nest (SDM) thermostat client.

Uses the cached refresh_token to mint short-lived access tokens via
https://oauth2.googleapis.com/token (refresh-token grants don't validate
redirect_uri, so we sidestep the deprecated-redirect issue that broke
Tesla's Owner API). Then issues SDM REST calls to read state and push
heat setpoints.

The controller calls this only when ``settings.nest_enabled`` is True and
all the OAuth fields are populated (see `Nest.enabled`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from .config import Settings

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_API_BASE = "https://smartdevicemanagement.googleapis.com/v1"


def _f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


@dataclass(slots=True)
class ThermoState:
    mode: str  # "HEAT" | "COOL" | "HEATCOOL" | "OFF"
    heat_setpoint_f: float | None
    cool_setpoint_f: float | None
    ambient_f: float
    hvac_status: str  # "OFF" | "HEATING" | "COOLING"


class Nest:
    _TOKEN_LEEWAY_SEC = 60.0  # refresh access_token this many seconds before expiry

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._access_token: str | None = None
        self._access_expires = 0.0
        self._session = requests.Session()

    @property
    def enabled(self) -> bool:
        s = self._s
        return bool(
            s.nest_enabled and s.nest_project_id and s.nest_client_id
            and s.nest_client_secret and s.nest_refresh_token and s.nest_device_id
        )

    def _ensure_token(self) -> str:
        if (self._access_token
                and time.monotonic() < self._access_expires - self._TOKEN_LEEWAY_SEC):
            return self._access_token
        r = self._session.post(
            _TOKEN_URL,
            data={
                "client_id": self._s.nest_client_id,
                "client_secret": self._s.nest_client_secret,
                "refresh_token": self._s.nest_refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=10,
        )
        r.raise_for_status()
        body = r.json()
        self._access_token = body["access_token"]
        self._access_expires = time.monotonic() + float(body.get("expires_in", 3600))
        return self._access_token

    def _device_url(self, suffix: str = "") -> str:
        return (f"{_API_BASE}/enterprises/{self._s.nest_project_id}"
                f"/devices/{self._s.nest_device_id}{suffix}")

    def read(self) -> ThermoState:
        token = self._ensure_token()
        r = self._session.get(
            self._device_url(),
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        r.raise_for_status()
        traits = r.json()["traits"]
        sp = traits.get("sdm.devices.traits.ThermostatTemperatureSetpoint", {})
        heat_c = sp.get("heatCelsius")
        cool_c = sp.get("coolCelsius")
        amb_c = traits["sdm.devices.traits.Temperature"]["ambientTemperatureCelsius"]
        mode = traits["sdm.devices.traits.ThermostatMode"]["mode"]
        hvac = traits.get("sdm.devices.traits.ThermostatHvac", {}).get("status", "OFF")
        return ThermoState(
            mode=mode,
            heat_setpoint_f=_c_to_f(heat_c) if heat_c is not None else None,
            cool_setpoint_f=_c_to_f(cool_c) if cool_c is not None else None,
            ambient_f=_c_to_f(amb_c),
            hvac_status=hvac,
        )

    def _execute(self, command: str, params: dict) -> None:
        token = self._ensure_token()
        r = self._session.post(
            self._device_url(":executeCommand"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"command": command, "params": params},
            timeout=10,
        )
        r.raise_for_status()

    def set_heat_target_f(self, target_f: float) -> None:
        """Push a heat-only setpoint to the thermostat (Fahrenheit)."""
        self._execute(
            "sdm.devices.commands.ThermostatTemperatureSetpoint.SetHeat",
            {"heatCelsius": _f_to_c(target_f)},
        )

    def set_cool_target_f(self, target_f: float) -> None:
        """Push a cool-only setpoint to the thermostat (Fahrenheit)."""
        self._execute(
            "sdm.devices.commands.ThermostatTemperatureSetpoint.SetCool",
            {"coolCelsius": _f_to_c(target_f)},
        )

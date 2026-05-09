"""Runtime configuration loaded from .env (and process env)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Tesla Powerwall 3
    # "cloud" = Tesla Owner API via pypowerwall cloudmode (OAuth, needs one-time setup)
    # "local" = Local TEDAPI on the Gateway (needs the Gateway password from sticker)
    powerwall_mode: Literal["cloud", "local"] = "cloud"

    # Cloud mode
    tesla_email: str | None = None
    tesla_site_id: int | None = None  # required only if account has multiple sites
    # Directory holding pypowerwall's OAuth token cache (relative to project root).
    powerwall_auth_path: str = "state"

    # Local mode (TEDAPI)
    powerwall_host: str | None = None
    powerwall_gw_password: str | None = None
    powerwall_gw_serial: str | None = None

    # Emporia (cloud)
    emporia_username: str | None = None
    emporia_password: str | None = None
    emporia_evse_gid: int | None = None

    # Control loop
    poll_interval_sec: int = 30
    # After this many consecutive failed Powerwall reads, surplus/morning_dump
    # fail safe and turn the charger off. Default 5 ticks * 30 s = 2.5 min.
    pw_fail_safe_ticks: int = Field(default=5, ge=1)
    # Surplus mode: when SoC is below this, we reserve `battery_max_charge_kw`
    # for the Powerwall and let the car have whatever solar is left over.
    # Above this threshold the battery is treated as full and the EV gets
    # everything beyond the home load.
    battery_reserve_pct: int = Field(default=99, ge=0, le=100)
    battery_max_charge_kw: float = 5.0
    ev_min_amps: int = Field(default=6, ge=6)
    ev_max_amps: int = Field(default=40, ge=6)
    ev_voltage: int = 240
    # Below this draw (watts) while the EVSE is enabled, treat the car as
    # "not accepting" (full / paused / fault) and route surplus to Nest.
    # Default ~80% of 6 A × 240 V = 1440 W, with a margin so brief lulls
    # don't trigger the flag.
    ev_accepting_threshold_w: int = Field(default=1100, ge=100)
    # After this many seconds in the "not accepting" state, the controller
    # clears the latch and tries again. If the car still won't accept it'll
    # re-latch within a tick. Without this, a stuck latch persists until
    # the user unplugs / replugs or switches modes.
    ev_not_accepting_retest_sec: int = Field(default=600, ge=60)

    # Powerwall usable capacity (kWh). One PW3 unit is 13.5 kWh; override
    # in .env if the site has more. Used by the morning-dump calculator.
    battery_capacity_kwh: float = 13.5
    # Morning-dump window: starts at `start_hour` and runs for `hours`.
    # Default 06:00 + 2 h spreads the dump across two hours so the per-tick
    # amperage is roughly halved vs a 1 h window — gentler on the EVSE,
    # car charger, and battery.
    morning_dump_floor_pct: int = Field(default=15, ge=5, le=99)
    morning_dump_hours: float = 2.0
    morning_dump_start_hour: int = Field(default=6, ge=0, le=23)
    morning_dump_start_minute: int = Field(default=0, ge=0, le=59)
    # Trickle mode fixed rate.
    trickle_kw: float = 2.0

    # Google Nest (Smart Device Management). When enabled and `surplus` mode
    # is selected, the controller manages the thermostat while the EV isn't
    # accepting charge: setpoint goes to the *surplus* target while there's
    # spare PV, and back to the *default* target otherwise. The direction
    # (heat-up vs cool-down) is picked from the thermostat's current
    # operating mode, so we never push HEAT in the middle of a heat wave.
    nest_enabled: bool = False
    nest_default_heat_f: int = Field(default=63, ge=40, le=85)
    nest_surplus_heat_f: int = Field(default=65, ge=40, le=90)
    nest_default_cool_f: int = Field(default=72, ge=55, le=95)
    nest_surplus_cool_f: int = Field(default=70, ge=55, le=90)
    nest_project_id: str | None = None
    nest_client_id: str | None = None
    nest_client_secret: str | None = None
    nest_refresh_token: str | None = None
    nest_device_id: str | None = None

    # Time zone passed to pypowerwall for timestamp handling.
    timezone: str = "America/Los_Angeles"

    @field_validator("tesla_site_id", "emporia_evse_gid", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v: object) -> object:
        # Blank values in .env (`FOO=`) arrive as "" — treat as unset.
        if isinstance(v, str) and v.strip() == "":
            return None
        return v


settings = Settings()

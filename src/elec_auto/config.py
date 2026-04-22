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
    battery_reserve_pct: int = Field(default=80, ge=0, le=100)
    ev_min_amps: int = Field(default=6, ge=6)
    ev_max_amps: int = Field(default=40, ge=6)
    ev_voltage: int = 240

    # Powerwall usable capacity (kWh). One PW3 unit is 13.5 kWh; override
    # in .env if the site has more. Used by the morning-dump calculator.
    battery_capacity_kwh: float = 13.5
    # Morning-dump floor SoC and window (hours) the dump should finish in.
    morning_dump_floor_pct: int = Field(default=15, ge=5, le=95)
    morning_dump_hours: float = 1.0
    morning_dump_start_hour: int = Field(default=7, ge=0, le=23)
    # Trickle mode fixed rate.
    trickle_kw: float = 2.0

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

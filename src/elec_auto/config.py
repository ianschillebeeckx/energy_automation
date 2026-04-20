"""Runtime configuration loaded from .env (and process env)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Tesla Powerwall 3 (local Gateway / TEDAPI)
    powerwall_host: str | None = None
    powerwall_password: str | None = None
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


settings = Settings()

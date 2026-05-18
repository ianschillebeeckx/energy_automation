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
    # How long a telemetry reading stays "fresh" for ControlState's
    # load_w preference logic. A PW3 reading older than this falls back
    # to an Emporia reading if Emporia is fresher (and vice versa).
    telemetry_fresh_sec: int = Field(default=60, ge=10, le=600)
    battery_reserve_pct: int = Field(default=80, ge=0, le=100)
    ev_min_amps: int = Field(default=6, ge=6)
    ev_max_amps: int = Field(default=40, ge=6)
    ev_voltage: int = 240

    # Powerwall usable capacity (kWh). One PW3 unit is 13.5 kWh; override
    # in .env if the site has more. Used by the morning-dump calculator.
    battery_capacity_kwh: float = 13.5
    # Inverter max AC charge rate. One PW3 unit is ~5 kW; override if the
    # site has multiple units. Used by forecast.soc_forecast() to spill
    # PV surplus exceeding this rate to the grid in the integration model.
    battery_max_charge_kw: float = Field(default=5.0, ge=0.5, le=20.0)
    # Percent of raw battery capacity Tesla hides as the bottom-of-pack
    # reserve. The local /api/system_status/soe endpoint returns raw SoC;
    # we scale it to the Tesla-app "displayed" SoC via
    #     displayed = max(0, (raw - floor) / (100 - floor) * 100)
    # If Tesla ever changes this on PW3 firmware, adjust here.
    battery_raw_floor_pct: float = Field(default=5.0, ge=5.0, le=20.0)
    # Morning-dump window: starts at `start_hour` and runs for `hours`.
    # Default 06:00 + 2 h spreads the dump across two hours so the per-tick
    # amperage is roughly halved vs a 1 h window — gentler on the EVSE,
    # car charger, and battery.
    morning_dump_floor_pct: int = Field(default=10, ge=5, le=99)
    morning_dump_start_hour: int = Field(default=5, ge=0, le=23)
    morning_dump_start_minute: int = Field(default=0, ge=0, le=59)
    morning_dump_end_hour: int = Field(default=8, ge=0, le=23)
    morning_dump_end_minute: int = Field(default=0, ge=0, le=59)
    # Ceiling on dump amperage. The PW3 inverter sustains ~11.5 kW AC of
    # combined solar+battery → home; HVAC pulses can hit 4.5 kW. Capping
    # the EV at 29 A (~7 kW) leaves room for the HVAC + a bit of base
    # load without forcing grid import.
    morning_dump_max_amps: int = Field(default=29, ge=6, le=40)
    # Fraction of the Solcast PV forecast credited toward the dump
    # headroom. At 90%, 90% of the forecast PV between now and the window
    # end is added to the battery SoC headroom; the remaining 10% is held
    # back as a conservatism buffer. Bump toward 100% as forecast
    # confidence grows.
    morning_dump_pv_credit_pct: float = Field(default=90.0, ge=0.0, le=100.0)
    # Sunny-day deep dump. When today's forecast PV reaches this kWh
    # threshold, allow the dump to drain further than the normal floor —
    # making more battery room for the day's generation. Disabled when
    # no forecast data is available. 30 kWh ≈ a solidly sunny day at
    # this site; cloudy days fall well below.
    morning_dump_sunny_threshold_kwh: float = Field(
        default=30.0, ge=0.0, le=200.0,
    )
    morning_dump_sunny_floor_pct: int = Field(default=5, ge=1, le=99)
    # Trickle mode fixed rate.
    trickle_kw: float = 2.0

    # Sunset auto-transition: while in surplus mode, once local wall-clock
    # time passes today's astronomical sunset (computed from latitude /
    # longitude via the astral library), the control loop flips to
    # morning_dump (queuing the next morning's scheduled charge). The
    # transition is skipped if either coordinate is unset.
    latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)

    # Solar array geometry & rating for the theoretical-output model in
    # solar.theoretical_w(). Azimuth: 0=N, 90=E, 180=S, 270=W. Tilt: degrees
    # from horizontal. Loss factor: combined inverter + wiring + soiling +
    # mismatch + temperature derate. Typical residential is ~0.09 (PVWatts
    # default ~0.14, but newer micro-inverter installs tend lower).
    solar_array_max_kw: float = Field(default=6.6, ge=0.5, le=50.0)
    solar_panel_azimuth_deg: float = Field(default=180.0, ge=0.0, le=360.0)
    solar_panel_tilt_deg: float = Field(default=30.0, ge=0.0, le=90.0)
    solar_system_loss_factor: float = Field(default=0.09, ge=0.0, le=0.5)

    # Solcast solar forecast — hobbyist tier is 10 calls/day per resource.
    # We schedule 8 fetches (1 at 5 AM + 7 evenly between sunrise and sunset)
    # and keep 2 calls in reserve for retries / debugging.
    solcast_api_key: str | None = None
    solcast_resource_id: str | None = None
    # On service startup, skip the immediate fetch if the previous fetch is
    # younger than this many minutes — protects the daily call budget
    # against rapid restart cycles during development.
    solcast_skip_recent_minutes: int = Field(default=60, ge=0, le=240)
    # How far into the future to request forecasts. 72 h (3 days) covers
    # the PW3's ~2-day battery autonomy plus one extra day for "will day
    # 3 be a dud?" planning. Solcast hobbyist allows up to 168 h.
    solcast_forecast_horizon_hours: int = Field(default=72, ge=24, le=168)

    # NWS (US National Weather Service). Free, no API key, but they
    # require a descriptive User-Agent for politeness. Override in .env if
    # you'd like to identify yourself to NWS support.
    nws_user_agent: str = (
        "elec_auto (https://github.com/ianschillebeeckx/elec_auto)"
    )
    # How many hours of hourly weather to keep per fetch. NWS returns ~156
    # (6.5 days) but the further-out periods aren't actionable for next-day
    # planning, so we truncate to keep the table compact.
    nws_forecast_horizon_hours: int = Field(default=72, ge=24, le=156)
    # Past-observations companion to the forecast. We pull station readings
    # for the last `nws_obs_hours` and downsample to one row per top-of-
    # hour, stored as source='nws_obs'. NWS only retains ~7 days of
    # observations publicly; longer history accrues in our local DB.
    nws_obs_station_id: str = "KSFO"   # SFO airport ASOS, ~8 mi from site
    nws_obs_hours: int = Field(default=24, ge=1, le=168)

    # Per-circuit load logging: a row goes into the `loads` table only when
    # the channel's draw exceeds this threshold. Filters out noise on
    # always-off circuits and keeps the DB compact.
    load_log_threshold_w: float = Field(default=5.0, ge=0.0, le=100.0)

    # Time zone passed to pypowerwall for timestamp handling.
    timezone: str = "America/Los_Angeles"

    @field_validator(
        "tesla_site_id", "emporia_evse_gid", "latitude", "longitude",
        mode="before",
    )
    @classmethod
    def _empty_str_to_none(cls, v: object) -> object:
        # Blank values in .env (`FOO=`) arrive as "" — treat as unset.
        if isinstance(v, str) and v.strip() == "":
            return None
        return v


settings = Settings()

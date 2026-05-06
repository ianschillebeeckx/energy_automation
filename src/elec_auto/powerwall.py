"""Tesla Powerwall 3 client.

Two backends, selected by `POWERWALL_MODE`:

- **local**: direct HTTPS to the PW3 Gateway's customer API. Logs in with
  the customer credentials (username `customer`, password = last 5 chars of
  the Gateway password on the sticker), then reads power / SoC via Bearer
  token. No Tesla cloud dependency. pypowerwall's own v1r mode needs an
  RSA key registered via the Tesla Owner API, which is deprecated, so we
  skip it and talk to the Gateway ourselves.
- **cloud**: Tesla Owner API via `pypowerwall` cloud mode. Broken as of
  2026 for new registrations (redirect_uri rejected); kept as a fallback
  for pre-existing token caches.
"""

from __future__ import annotations

import time
import warnings
from dataclasses import dataclass
from pathlib import Path

import requests
import urllib3

from .config import Settings

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


@dataclass(slots=True)
class PowerReading:
    """Instantaneous power balance. Units: watts; percent for SoC.

    Sign conventions (Tesla customer API):
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


class _LocalGateway:
    """Direct PW3 Gateway customer-API client."""

    _TOKEN_REFRESH_SEC = 1800.0
    _RATELIMIT_COOLDOWN_SEC = 300.0  # back off 5 min on 429 to avoid amplifying it
    # Cache one read() result for this long. Set just under the default
    # poll_interval_sec (30s) so each control-loop tick still fetches fresh
    # data, but the dashboard's 15s auto-refreshes piggyback on that.
    _READ_CACHE_TTL_SEC = 25.0

    def __init__(self, host: str, customer_password: str, timeout: float = 15.0) -> None:
        self._host = host
        self._password = customer_password
        self._timeout = timeout
        self._session = requests.Session()
        self._session.verify = False
        self._token: str | None = None
        self._token_t = 0.0
        self._cooldown_until = 0.0
        self._cached_reading: PowerReading | None = None
        self._cached_at = 0.0

    def _login(self) -> None:
        if time.monotonic() < self._cooldown_until:
            raise RuntimeError("gateway login in cooldown after rate-limit")
        r = self._session.post(
            f"https://{self._host}/api/login/Basic",
            json={
                "username": "customer",
                "password": self._password,
                "email": "customer@customer.domain",
                "clientInfo": {"timezone": "America/Chicago"},
            },
            timeout=self._timeout,
        )
        if r.status_code == 429:
            self._cooldown_until = time.monotonic() + self._RATELIMIT_COOLDOWN_SEC
            raise RuntimeError("gateway rate-limited login (429), cooling down 5 min")
        r.raise_for_status()
        self._token = r.json()["token"]
        self._token_t = time.monotonic()

    def _get(self, path: str) -> dict:
        if not self._token or (time.monotonic() - self._token_t) > self._TOKEN_REFRESH_SEC:
            self._login()
        headers = {"Authorization": f"Bearer {self._token}"}
        r = self._session.get(f"https://{self._host}{path}", headers=headers, timeout=self._timeout)
        if r.status_code == 401:  # token expired early, retry once
            self._login()
            headers["Authorization"] = f"Bearer {self._token}"
            r = self._session.get(f"https://{self._host}{path}", headers=headers, timeout=self._timeout)
        r.raise_for_status()
        return r.json()

    def read(self) -> PowerReading:
        if (self._cached_reading is not None
                and time.monotonic() - self._cached_at < self._READ_CACHE_TTL_SEC):
            return self._cached_reading
        aggs = self._get("/api/meters/aggregates")
        soe = self._get("/api/system_status/soe")
        reading = PowerReading(
            solar_w=float(aggs.get("solar", {}).get("instant_power", 0.0)),
            load_w=float(aggs.get("load", {}).get("instant_power", 0.0)),
            battery_w=float(aggs.get("battery", {}).get("instant_power", 0.0)),
            grid_w=float(aggs.get("site", {}).get("instant_power", 0.0)),
            battery_soc_pct=float(soe.get("percentage", float("nan"))),
        )
        self._cached_reading = reading
        self._cached_at = time.monotonic()
        return reading


class _CloudGateway:
    """Legacy cloud path via pypowerwall. Kept for compatibility."""

    def __init__(self, settings: Settings, auth_dir: Path) -> None:
        import pypowerwall  # deferred: cloud mode isn't always needed

        if not settings.tesla_email:
            raise RuntimeError(
                "cloud mode requires TESLA_EMAIL and a one-time "
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


class Powerwall:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        auth_dir = Path(settings.powerwall_auth_path).resolve()
        auth_dir.mkdir(parents=True, exist_ok=True)

        if settings.powerwall_mode == "local":
            if not (settings.powerwall_host and settings.powerwall_gw_password):
                raise RuntimeError(
                    "local mode requires POWERWALL_HOST and POWERWALL_GW_PASSWORD."
                )
            # Customer-portal password is the last 5 chars of the gateway password.
            customer_pw = settings.powerwall_gw_password[-5:]
            self._impl: _LocalGateway | _CloudGateway = _LocalGateway(
                host=settings.powerwall_host, customer_password=customer_pw,
            )
        else:
            self._impl = _CloudGateway(settings, auth_dir)

    def read(self) -> PowerReading:
        return self._impl.read()

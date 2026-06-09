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

import math
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

import requests
import urllib3

from .config import Settings

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _to_displayed_soc(raw_pct: float, raw_floor_pct: float) -> float:
    """Map raw PW3 SoC to the Tesla app's "displayed" %.

    The bottom `raw_floor_pct` of physical capacity is invisible to the
    Tesla app (it's the cell-health reserve). At raw == floor we show 0;
    at raw == 100 we show 100. Everywhere in between we linearly map.
    """
    if math.isnan(raw_pct):
        return raw_pct
    span = max(1e-9, 100.0 - raw_floor_pct)
    scaled = (raw_pct - raw_floor_pct) / span * 100.0
    return max(0.0, min(100.0, scaled))


class PowerwallUnavailable(RuntimeError):
    """The gateway is in back-off after recent consecutive failures.

    Raised by `_LocalGateway.read()` when we're inside the cooldown window
    that follows a streak of failed reads. Carries no new diagnostic info
    — the originating failure was already logged when it happened — so
    callers should suppress this silently rather than re-logging every
    tick during the outage.
    """


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
    """Direct PW3 Gateway customer-API client.

    Tracks consecutive failures and enters an exponential back-off so a
    flaky or rebooting gateway isn't hammered every poll tick. After a
    short streak we also drop the cached bearer token and recycle the
    `requests.Session` — covers the common case where the gateway
    rebooted and our pooled TCP socket / cached token are stale.

    The back-off here subsumes the older 429-only login cooldown:
    whether the failure is a 429, a 5xx, or a timeout, the next attempt
    waits the same way. Per the discussion notes, honoring `Retry-After`
    would add little — PW3 rarely sends a meaningful one.
    """

    _TOKEN_REFRESH_SEC = 1800.0

    # Back-off schedule applied on consecutive read failures. Index i is
    # used after (i+1) failures; tail value caps the wait. Keeps polling
    # responsive after a single blip while making sustained outages cheap.
    _BACKOFF_SCHEDULE_SEC: tuple[float, ...] = (30.0, 60.0, 120.0, 300.0)
    # After this many failures in a row, drop the cached token + Session
    # — the gateway likely rebooted (stale token) or our pooled socket is
    # half-closed.
    _RESET_STREAK = 3

    def __init__(self, host: str, customer_password: str, raw_floor_pct: float,
                 timeout: float = 8.0) -> None:
        self._host = host
        self._password = customer_password
        self._raw_floor_pct = raw_floor_pct
        self._timeout = timeout
        self._session = requests.Session()
        self._session.verify = False
        self._token: str | None = None
        self._token_t = 0.0
        # Failure tracking. `_failure_streak` is the count of consecutive
        # failed read() calls; reset to 0 on a successful read.
        # `_backoff_until` is a monotonic-clock deadline before which
        # read() short-circuits with PowerwallUnavailable.
        self._failure_streak = 0
        self._backoff_until = 0.0

    def _login(self) -> None:
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
        now_mono = time.monotonic()
        if now_mono < self._backoff_until:
            # Inside the back-off window — don't touch the network.
            raise PowerwallUnavailable(
                f"in back-off after {self._failure_streak} failure(s), "
                f"{self._backoff_until - now_mono:.0f}s remaining",
            )
        try:
            aggs = self._get("/api/meters/aggregates")
            soe = self._get("/api/system_status/soe")
            raw_pct = float(soe.get("percentage", float("nan")))
            reading = PowerReading(
                solar_w=float(aggs.get("solar", {}).get("instant_power", 0.0)),
                load_w=float(aggs.get("load", {}).get("instant_power", 0.0)),
                battery_w=float(aggs.get("battery", {}).get("instant_power", 0.0)),
                grid_w=float(aggs.get("site", {}).get("instant_power", 0.0)),
                battery_soc_pct=_to_displayed_soc(raw_pct, self._raw_floor_pct),
            )
        except Exception:
            self._note_failure()
            raise
        # Success — clear back-off state.
        self._failure_streak = 0
        self._backoff_until = 0.0
        return reading

    def _note_failure(self) -> None:
        """Bump the streak, extend back-off, and recycle session at threshold."""
        self._failure_streak += 1
        idx = min(self._failure_streak - 1, len(self._BACKOFF_SCHEDULE_SEC) - 1)
        self._backoff_until = time.monotonic() + self._BACKOFF_SCHEDULE_SEC[idx]
        if self._failure_streak == self._RESET_STREAK:
            # Token may be stale (gateway reboot) and the pooled TCP
            # socket may be half-closed; drop both so the next attempt
            # starts clean. Idempotent w.r.t. higher streaks — only need
            # to do this once, on the threshold crossing.
            self._token = None
            try:
                self._session.close()
            except Exception:
                pass
            self._session = requests.Session()
            self._session.verify = False


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
                host=settings.powerwall_host,
                customer_password=customer_pw,
                raw_floor_pct=settings.battery_raw_floor_pct,
            )
        else:
            self._impl = _CloudGateway(settings, auth_dir)

    def read(self) -> PowerReading:
        return self._impl.read()

"""Powerwall 3 cloud control via Tesla Fleet API.

Thin wrapper around `pypowerwall.PyPowerwallFleetAPI` exposing only the
three operations we actually need from the automation:

  read_state()        — current (mode, reserve_pct)
  enable_tbc(reserve) — set mode=autonomous + reserve in the order the
                        firmware accepts cleanly (reserve first; setting
                        them in the other order races a default-reserve
                        re-apply that the mode transition triggers, and
                        the reserve write gets silently clobbered)
  restore(mode, rsv)  — same order, returning to a saved baseline

Lazy connect: the underlying client and its OAuth state come up on first
call, not at construction. That way the control loop can start even if
Fleet API auth hasn't been set up yet (peak_export_enabled=False is the
gate; this module never touches the network unless something asks).

Errors are logged and surfaced as exceptions to the caller — the
controller decides whether to retry or no-op. We don't swallow here
because a silently-failed engage means the PW3 is in an unknown state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from loguru import logger

VALID_MODES = frozenset({"self_consumption", "autonomous", "backup"})


@dataclass(slots=True)
class PW3State:
    mode: str           # 'self_consumption' | 'autonomous' | 'backup'
    reserve_pct: int


class PW3CloudClient:
    """Authenticates lazily, serializes writes, caches the live client."""

    def __init__(self, auth_path: str | Path = "state") -> None:
        self._auth_path = str(auth_path)
        self._client = None  # PyPowerwallFleetAPI, built lazily
        self._lock = Lock()

    def _ensure_client(self):
        """Build + connect the FleetAPI client on first use.

        Re-raises so the caller knows the network/auth path failed; we
        don't want a silent skip.
        """
        if self._client is not None:
            return self._client
        # Deferred import — pypowerwall pulls in heavy deps (urllib3,
        # requests stack) we don't want loaded at module-import time.
        from pypowerwall import PyPowerwallFleetAPI

        c = PyPowerwallFleetAPI(None, authpath=self._auth_path)
        if not c.connect():
            raise RuntimeError(
                "PW3CloudClient: Fleet API connect failed — check "
                f"{self._auth_path}/.pypowerwall.fleetapi",
            )
        self._client = c
        return c

    # --- reads --------------------------------------------------------

    def read_state(self) -> PW3State:
        """Cache-bypass read of mode + reserve.

        Validates + retries once on garbage. `pypowerwall`'s first
        force-read after connect has been observed to return
        `mode=None reserve=0` — an unauthenticated / partially-populated
        response. Trusting that garbage as a baseline poisons the
        subsequent restore (writing mode=None is a silent no-op that
        leaves the PW3 stuck in autonomous overnight; writing reserve=0
        drains the pack to firmware's absolute floor).
        """
        with self._lock:
            f = self._ensure_client().fleet
            for attempt in range(2):
                mode = f.get_operating_mode(force=True)
                reserve_raw = f.get_battery_reserve(force=True)
                if (
                    mode in VALID_MODES
                    and reserve_raw is not None
                    and 0 <= int(reserve_raw) <= 100
                ):
                    return PW3State(mode=mode, reserve_pct=int(reserve_raw))
                logger.warning(
                    "pw3_cloud: read_state got mode={!r} reserve={!r} "
                    "(attempt {}/2)",
                    mode, reserve_raw, attempt + 1,
                )
                if attempt == 0:
                    time.sleep(1.0)
            raise RuntimeError(
                f"pw3_cloud: read_state returned invalid mode={mode!r} "
                f"reserve={reserve_raw!r} after retry",
            )

    # --- writes -------------------------------------------------------

    def enable_tbc(self, reserve_pct: int) -> PW3State:
        """Switch to autonomous (TBC) with the given reserve %.

        Write order matters: reserve first, mode second. Discovered live
        — the mode transition re-applies whatever reserve was current at
        the moment of the transition, so writing reserve afterward gets
        racey. Reserve first → mode → both stick.
        """
        with self._lock:
            f = self._ensure_client().fleet
            logger.info("pw3_cloud: enable_tbc(reserve={})", reserve_pct)
            r1 = f.set_battery_reserve(reserve_pct)
            r2 = f.set_operating_mode("autonomous")
            logger.debug("pw3_cloud:   reserve write -> {}", r1)
            logger.debug("pw3_cloud:   mode write    -> {}", r2)
            return PW3State(mode="autonomous", reserve_pct=int(reserve_pct))

    def restore(self, mode: str, reserve_pct: int) -> PW3State:
        """Restore a previously-saved baseline."""
        with self._lock:
            f = self._ensure_client().fleet
            logger.info(
                "pw3_cloud: restore(mode={!r}, reserve={})",
                mode, reserve_pct,
            )
            r1 = f.set_battery_reserve(reserve_pct)
            r2 = f.set_operating_mode(mode)
            logger.debug("pw3_cloud:   reserve write -> {}", r1)
            logger.debug("pw3_cloud:   mode write    -> {}", r2)
            return PW3State(mode=mode, reserve_pct=int(reserve_pct))

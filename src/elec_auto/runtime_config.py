"""Ephemeral runtime overrides for action tunables.

Actions historically read all their tunables straight from `Settings`
(loaded from `.env` at boot). That's fine for defaults, but the user
wanted a way to nudge thresholds live from the dashboard, with each
override expiring on its own schedule so a late-night nudge to a
dump-window time isn't silently reset at midnight before the window
even fires.

Model: one JSON file `state/config_overrides.json` maps each override
name to `{value, expires_at}` where `expires_at` is a unix timestamp
(seconds). On read, entries past their expiry are dropped and the
`.env`/`Settings` default takes over again. Expiry policy is picked by
the caller (the web layer) at write time — different tunables have
different natural "windows":

  Dump-related knobs → expire when the dump window closes.
  Surplus threshold  → expires at local sunset (after which there's no
                       PV anyway, so the knob has no effect).

Because the expiry is stored with each entry, there's no need for a
scheduled reset — the next `read()` after `expires_at` naturally
returns fewer keys, and `effective()` seamlessly falls back to the
Settings default.

Only the current process needs this: the control loop reads it each
tick to build an effective-settings view (see `effective` below), and
the web layer writes to it when the user submits a form.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Lock
from typing import Any

from loguru import logger

from .config import Settings

_PATH = Path("state") / "config_overrides.json"
_LOCK = Lock()

# Whitelist: only these Settings fields may be overridden from the UI.
# Any name not in this set is silently rejected — protects against a
# malformed POST accidentally poking a critical field (auth paths,
# battery capacity, etc.). `morning_dump_sunny_floor_pct` is included
# so the UI's "Dump floor" knob can override both floors together;
# it isn't surfaced as its own control.
TUNABLE_FIELDS: frozenset[str] = frozenset({
    "battery_reserve_pct",              # Surplus threshold
    "morning_dump_floor_pct",
    "morning_dump_sunny_floor_pct",     # mirrored from morning_dump_floor_pct
    "morning_dump_start_hour",
    "morning_dump_start_minute",
    "morning_dump_end_hour",
    "morning_dump_end_minute",
    "peak_export_floor_pct",
    "peak_export_start_hour",
    "peak_export_start_minute",
    "peak_export_end_hour",
    "peak_export_end_minute",
})


def effective(settings: Settings) -> Settings:
    """Return a Settings view with the currently-live overrides applied."""
    overrides = {k: v for k, v in read().items() if k in TUNABLE_FIELDS}
    if not overrides:
        return settings
    try:
        return settings.model_copy(update=overrides)
    except Exception:
        logger.exception(
            "runtime_config: invalid overrides {!r} — falling back to defaults",
            overrides,
        )
        return settings


def _load_raw() -> dict[str, dict[str, Any]]:
    """Return the raw {name: {value, expires_at, source, note}} dict from disk.

    Malformed entries are dropped silently. `source` defaults to "user"
    for entries missing it (backward compat with earlier persisted
    files); `note` defaults to "".
    """
    if not _PATH.exists():
        return {}
    try:
        parsed = json.loads(_PATH.read_text())
    except Exception:
        logger.exception("runtime_config: parse failed; treating as empty")
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for k, v in parsed.items():
        if isinstance(v, dict) and "value" in v and "expires_at" in v:
            out[k] = {
                "value": v["value"],
                "expires_at": v["expires_at"],
                "source": v.get("source", "user"),
                "note": v.get("note", ""),
            }
    return out


def _prune(raw: dict[str, dict[str, Any]], now: float) -> dict[str, dict[str, Any]]:
    """Drop entries whose expiry has passed."""
    return {k: v for k, v in raw.items() if v["expires_at"] > now}


def read() -> dict[str, Any]:
    """Return {name: value} for currently-live overrides.

    Expired entries are dropped from disk on read so the file doesn't
    grow unbounded across many "test-then-let-it-expire" cycles.
    """
    with _LOCK:
        raw = _load_raw()
        live = _prune(raw, time.time())
        if len(live) != len(raw):
            _write_raw(live)
        return {k: v["value"] for k, v in live.items()}


def read_with_expiry() -> dict[str, dict[str, Any]]:
    """Return {name: {value, expires_at}} — for UI hints that show
    when an override will lapse."""
    with _LOCK:
        raw = _load_raw()
        return _prune(raw, time.time())


def _write_raw(data: dict[str, dict[str, Any]]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data))


def set_value(
    name: str,
    value: Any,
    expires_at: float,
    *,
    source: str = "user",
    note: str = "",
) -> None:
    """Set an override with an explicit expiry (unix seconds).

    `source` is "user" (dashboard button) or "auto" (background job).
    The distinction lets the auto job avoid stomping on a value the
    user explicitly set — see `set_if_absent_or_auto`.
    """
    with _LOCK:
        raw = _load_raw()
        raw[name] = {
            "value": value,
            "expires_at": float(expires_at),
            "source": source,
            "note": note,
        }
        _write_raw(raw)
        logger.info(
            "runtime_config: set {}={!r} ({}; expires at {:.0f})",
            name, value, source, expires_at,
        )


def set_if_absent_or_auto(
    name: str,
    value: Any,
    expires_at: float,
    note: str = "",
) -> bool:
    """Write an auto-sourced override, unless a user override is already
    in place. Returns True if the write happened, False if skipped.
    """
    with _LOCK:
        raw = _load_raw()
        existing = raw.get(name)
        if existing is not None and existing.get("source") == "user":
            logger.info(
                "runtime_config: auto write for {} skipped — user override present",
                name,
            )
            return False
        raw[name] = {
            "value": value,
            "expires_at": float(expires_at),
            "source": "auto",
            "note": note,
        }
        _write_raw(raw)
        logger.info(
            "runtime_config: set {}={!r} (auto; expires at {:.0f})",
            name, value, expires_at,
        )
        return True


def clear(name: str) -> None:
    """Remove one override (revert to Settings default now)."""
    with _LOCK:
        raw = _load_raw()
        if name not in raw:
            return
        del raw[name]
        _write_raw(raw)
        logger.info("runtime_config: cleared {}", name)


def clear_all() -> None:
    """Wipe every override (drops the file entirely)."""
    with _LOCK:
        try:
            _PATH.unlink(missing_ok=True)
            logger.info("runtime_config: cleared all")
        except Exception:
            logger.exception("runtime_config: clear_all failed")

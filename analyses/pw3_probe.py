"""Read-only probe of the PW3 local gateway for endpoints that might
expose an export-power-limit knob.

We reuse the existing `_LocalGateway` so credentials are read from
`.env` via Settings and never appear on a shell argv. All requests are
GETs; no state is written to the gateway. Output is grouped by endpoint
and filtered to keys whose names look power-cap-related.
"""

from __future__ import annotations

import json
from typing import Any

from elec_auto.config import settings
from elec_auto.powerwall import _LocalGateway

# Endpoints to probe. Mix of pypowerwall-known paths and informed guesses
# at where a "max export kW" knob might live (config / site_info /
# operation / customer/registration).
ENDPOINTS = [
    # Known-good on PW3 customer tier (sanity check our auth works).
    "/api/meters/aggregates",
    "/api/system_status/soe",
    "/api/system_status/grid_status",
    # PW2-era endpoints that may or may not still exist on PW3 customer.
    "/api/operation",
    "/api/config/completed",
    "/api/customer",
    "/api/customer/registration",
    "/api/site_info",
    "/api/site_info/site_name",
    "/api/site_info/grid_codes",
    "/api/sitemaster",
    "/api/system_status",
    "/api/system_status/grid_faults",
    "/api/powerwalls",
    "/api/solars",
    "/api/devices/vitals",
    "/api/networks",
    "/api/troubleshooting/problems",
    "/api/meters/site",
    "/api/meters/solar",
    "/api/meters/battery",
    "/api/meters/load",
    "/api/meters/readings",
    "/api/system/update/status",
    # Capability discovery — sometimes lists what *is* available.
    "/api/auth/toggle/supported",
    "/api/auth/supported",
    "/api/status",
    # Speculative — looking for explicit export-limit endpoints. 404
    # means "not here"; 401/403 means "there but locked" (interesting).
    "/api/config/export_limit",
    "/api/site_info/export_limit",
    "/api/grid_export_limit",
    "/api/customer/registration/configuration",
    "/api/config",
]

# Substrings we want to flag when walking response JSON. Anything matching
# probably touches export rate / discharge cap.
INTERESTING = (
    "export", "limit", "cap", "max_power", "max_kw", "max_w",
    "discharge", "setpoint", "ramp", "throttle",
)


def walk(prefix: str, obj: Any) -> list[tuple[str, Any]]:
    """Flatten nested dicts/lists into (dotted_path, value) pairs."""
    out: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(walk(f"{prefix}.{k}" if prefix else k, v))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(walk(f"{prefix}[{i}]", v))
    else:
        out.append((prefix, obj))
    return out


def is_interesting(path: str) -> bool:
    p = path.lower()
    return any(needle in p for needle in INTERESTING)


def main() -> None:
    gw = _LocalGateway(
        host=settings.powerwall_host,
        customer_password=settings.powerwall_gw_password[-5:],
        raw_floor_pct=settings.battery_raw_floor_pct,
    )

    print(f"== probing {settings.powerwall_host} ==\n")
    for path in ENDPOINTS:
        print(f"--- {path}")
        try:
            data = gw._get(path)
        except Exception as e:
            # Print just the HTTP status (or short error) — full stack
            # traces aren't useful in a survey.
            msg = str(e).splitlines()[0][:120]
            print(f"   ERR: {msg}\n")
            continue

        # Pretty-print, but truncate huge responses.
        text = json.dumps(data, indent=2, default=str)
        if len(text) > 4000:
            print(text[:4000] + f"\n   ... [truncated, {len(text)} chars total]")
        else:
            print(text)

        # Flag interesting-looking keys/values.
        flagged = [(k, v) for k, v in walk("", data) if is_interesting(k)]
        if flagged:
            print("   >> INTERESTING KEYS:")
            for k, v in flagged:
                print(f"      {k} = {v!r}")
        print()


if __name__ == "__main__":
    main()

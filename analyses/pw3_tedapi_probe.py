"""Read-only probe of the PW3 TEDAPI for export-limit / discharge-cap knobs.

TEDAPI is the installer-tier interface on the Powerwall 3 Gateway. Auth
uses the full gateway password (not the 5-char customer slice) and gives
access to a much wider surface — site config, mode, reserve, ramp rates,
and (per community reports) explicit grid-export limits.

Strictly GET-only here. Anything that mutates state is OUT OF SCOPE for
this probe.
"""

from __future__ import annotations

import json
from typing import Any

from pypowerwall.tedapi import TEDAPI

from elec_auto.config import settings

INTERESTING = (
    "export", "limit", "cap", "max_power", "max_kw", "max_w", "max_site",
    "discharge", "setpoint", "ramp", "throttle", "constrain", "site_meter",
    "site_export", "max_ac", "max_dc", "real_power",
)


def walk(prefix: str, obj: Any) -> list[tuple[str, Any]]:
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


def dump(label: str, data: Any) -> None:
    print(f"--- {label}")
    if data is None:
        print("   None\n")
        return
    text = json.dumps(data, indent=2, default=str)
    if len(text) > 3500:
        print(text[:3500] + f"\n   ... [truncated, {len(text)} chars total]")
    else:
        print(text)

    flagged = [(k, v) for k, v in walk("", data) if is_interesting(k)]
    if flagged:
        print("   >> INTERESTING KEYS:")
        for k, v in flagged:
            print(f"      {k} = {v!r}")
    print()


def main() -> None:
    api = TEDAPI(
        gw_pwd=settings.powerwall_gw_password,
        host=settings.powerwall_host,
    )

    print(f"== TEDAPI probe of {settings.powerwall_host} ==\n")

    # DIN + firmware: cheap, confirms auth works.
    try:
        din = api.get_din()
        print(f"DIN: {din}")
        fw = api.get_firmware_version()
        print(f"firmware: {fw}\n")
    except Exception as e:
        print(f"AUTH OR REACHABILITY FAILED: {e}")
        return

    for label, fn in [
        ("get_config", api.get_config),
        ("get_status", api.get_status),
        ("get_device_controller", api.get_device_controller),
        ("get_components", api.get_components),
        ("get_blocks", api.get_blocks),
    ]:
        try:
            dump(label, fn())
        except Exception as e:
            print(f"--- {label}\n   ERR: {str(e).splitlines()[0][:160]}\n")


if __name__ == "__main__":
    main()

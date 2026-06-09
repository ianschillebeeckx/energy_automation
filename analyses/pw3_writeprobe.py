"""Verification probe: confirm Fleet API mode/reserve/export writes land.

Read-modify-restore. Always reverts to baseline in a `finally`. Safe to
run on a live PW3 — at worst the system spends ~60 s in autonomous mode
with reserve=50% before being put back. If autonomous mode's internal
buy/sell arbitrage doesn't favor discharge (typical outside July/Aug
weekday evenings under NBT), we won't see negative battery_w in the
window; the writes still confirm we can flip mode at will.
"""

from __future__ import annotations

import time

from pypowerwall import PyPowerwallFleetAPI


def fmt_power(p: dict) -> str:
    """One-line summary of the live power dict.

    pypowerwall's top-level `power()` was returning zeros — likely a
    stale read-cache. We use `fleet.live_status()` instead, which hits
    the live endpoint directly. Field names differ from the wrapper
    (solar_power vs solar, etc.) — handle both.
    """
    aliases = {
        "solar":   ("solar_power", "solar"),
        "battery": ("battery_power", "battery"),
        "load":    ("load_power", "load"),
        "site":    ("grid_power", "site"),
    }
    out = []
    for label, keys in aliases.items():
        v = next((p.get(k) for k in keys if p.get(k) is not None), 0)
        out.append(f"{label}={int(v or 0):+6d} W")
    return " ".join(out)


def read_state(f) -> dict:
    """Bypass the read-cache for the three settings we care about."""
    return {
        "mode": f.get_operating_mode(force=True),
        "reserve": f.get_battery_reserve(force=True),
    }


def main() -> None:
    print("=== PW3 Fleet API write-probe ===\n")
    c = PyPowerwallFleetAPI(None, authpath="state")
    if not c.connect():
        print("FAILED: could not connect via Fleet API")
        return

    # The mode/reserve setters live on c.fleet (FleetAPI raw client);
    # the top-level PyPowerwallFleetAPI exposes only the export/charging
    # toggles + a power() helper. So we mix levels here intentionally.
    f = c.fleet

    # --- baseline ----------------------------------------------------
    s0 = read_state(f)
    baseline_export = c.get_grid_export()
    print("baseline:")
    print(f"  operating_mode = {s0['mode']!r}")
    print(f"  reserve_pct    = {s0['reserve']}")
    print(f"  grid_export    = {baseline_export!r}")
    print(f"  power          = {fmt_power(f.get_live_status(force=True) or {})}")
    print()

    try:
        # --- write: switch to TBC + lower reserve ---------------------
        print("writing: operating_mode='autonomous', reserve=50 ...")
        r1 = f.set_operating_mode("autonomous")
        r2 = f.set_battery_reserve(50)
        print(f"  set_operating_mode result = {r1!r}")
        print(f"  set_battery_reserve result = {r2!r}")
        print()

        # --- poll until propagation visible OR timeout ---------------
        print("waiting for change to propagate (force=True reads) ...")
        for i in range(13):
            time.sleep(5)
            s = read_state(f)
            power = fmt_power(f.get_live_status(force=True) or {})
            flag = " <-- CHANGED" if s["mode"] == "autonomous" else ""
            print(
                f"  t={(i+1)*5:3d}s  mode={s['mode']!r}  "
                f"reserve={s['reserve']}{flag}  {power}",
            )
            if s["mode"] == "autonomous":
                break
        print()

    finally:
        # --- always restore -------------------------------------------
        print("restoring baseline ...")
        try:
            f.set_operating_mode(s0["mode"])
            f.set_battery_reserve(s0["reserve"])
            # Poll until restore is visible, max ~60s.
            for i in range(12):
                time.sleep(5)
                s = read_state(f)
                flag = " <-- RESTORED" if s == s0 else ""
                print(
                    f"  t={(i+1)*5:3d}s  mode={s['mode']!r}  "
                    f"reserve={s['reserve']}{flag}",
                )
                if s == s0:
                    break
        except Exception as e:
            print(f"WARNING: restore failed: {e}")
            print(
                f"  manual revert needed: mode={s0['mode']}  "
                f"reserve={s0['reserve']}",
            )
        print()
        print(f"final power = {fmt_power(f.get_live_status(force=True) or {})}")


if __name__ == "__main__":
    main()

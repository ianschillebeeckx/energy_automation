"""Priority / allocation policy.

Surplus rule (default priority for PV generation):
    1. Real-time home consumption
    2. Powerwall charging at up to `battery_max_charge_kw`
       (skipped once SoC >= `battery_reserve_pct` — battery is full)
    3. EV charging (whatever's left after 1 and 2)
    4. Export to grid (only if EV is below min amps or unplugged)

`decide_ev_amps` returns the EV charge current we want to set *now*, given the
latest reading from Powerwall and the charger's current draw. The caller is
responsible for rounding, applying hysteresis, and actually pushing the value.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .emporia import ChargerState
from .powerwall import PowerReading


@dataclass(slots=True)
class Decision:
    target_amps: int
    reason: str
    # Whether the EVSE should be charging *right now*. When False, target_amps
    # still carries the rate the controller thinks the charger should be
    # configured at (e.g. the preview for a scheduled mode), so the dashboard
    # shows something meaningful while the breaker is paused.
    on: bool = True


def decide_ev_amps(
    pw: PowerReading,
    ev: ChargerState,
    settings: Settings,
) -> Decision:
    # Without a car plugged in, surplus has nowhere to go via the EVSE.
    # The Nest path (when enabled) handles that case at the controller level.
    if not ev.plugged_in:
        return Decision(0, "car unplugged", on=False)

    # Current EV draw in watts. If off, treat as 0.
    ev_w_now = ev.charge_rate_a * settings.ev_voltage if ev.on else 0
    # "True" non-EV load: subtract the EV's own consumption from the meter.
    non_ev_load_w = pw.load_w - ev_w_now

    # Reserve battery's max charge power until it's nearly full.
    if pw.battery_soc_pct < settings.battery_reserve_pct:
        battery_reserve_w = settings.battery_max_charge_kw * 1000
    else:
        battery_reserve_w = 0

    surplus_w = pw.solar_w - non_ev_load_w - battery_reserve_w

    target_amps = int(surplus_w // settings.ev_voltage)
    if target_amps < settings.ev_min_amps:
        return Decision(
            0,
            f"surplus {surplus_w:.0f}W < min {settings.ev_min_amps}A "
            f"(soc {pw.battery_soc_pct:.0f}%)",
            on=False,
        )
    target_amps = min(target_amps, settings.ev_max_amps)
    return Decision(
        target_amps,
        f"surplus {surplus_w:.0f}W -> {target_amps}A (soc {pw.battery_soc_pct:.0f}%)",
        on=True,
    )

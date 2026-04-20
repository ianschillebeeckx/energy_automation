"""Priority / allocation policy.

Default priority of PV generation:
    1. Real-time home consumption
    2. Powerwall charging (until reserve hit)
    3. EV charging (surplus only)
    4. Export to grid

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


def decide_ev_amps(
    pw: PowerReading,
    ev: ChargerState,
    settings: Settings,
) -> Decision:
    # Battery reserve gate: don't pull for EV until Powerwall is well-charged.
    if pw.battery_soc_pct < settings.battery_reserve_pct:
        return Decision(0, f"battery {pw.battery_soc_pct:.0f}% < reserve {settings.battery_reserve_pct}%")

    # Current EV draw in watts. If off, treat as 0.
    ev_w_now = ev.charge_rate_a * settings.ev_voltage if ev.on else 0

    # "True" non-EV load and grid flow: subtract the EV's own consumption.
    non_ev_load_w = pw.load_w - ev_w_now
    # Surplus = what would be exported if EV were off and battery were full.
    # Approximation: solar minus non-EV load (assumes battery can't accept more).
    surplus_w = pw.solar_w - non_ev_load_w

    target_amps = int(surplus_w // settings.ev_voltage)
    if target_amps < settings.ev_min_amps:
        return Decision(0, f"surplus {surplus_w:.0f}W < min {settings.ev_min_amps}A")
    target_amps = min(target_amps, settings.ev_max_amps)
    return Decision(target_amps, f"surplus {surplus_w:.0f}W -> {target_amps}A")

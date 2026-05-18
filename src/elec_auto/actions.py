"""Charge-mode actions for the EV: priority-ranked, prerequisites-driven.

An `Action` is a self-contained "here's when I want to fire and what I
want the EVSE to do" object. The Controller runs each tick:

  1. Filter to actions enabled in `Settings`.
  2. Of those, filter to ones whose `applies(state, ctx)` is True.
  3. Pick the highest-priority survivor.
  4. Call `decide(state, ctx)` to get a Decision for the EVSE.

No FSM, no transitions — every tick re-evaluates from scratch.
Prerequisites (the `applies()` logic) carry all the time- and
state-dependent gating: `MorningDump.applies` checks "in the dump
window AND SoC > floor"; `Surplus.applies` checks "out of the dump
window AND SoC at/above reserve AND surplus > 0". They partition by
construction so multiple actions don't fight over the same situation.

Future additions (per-action UI toggles, HVAC pre-heat, etc.) drop
into the same shape: implement the Protocol, append to DEFAULT_ACTIONS,
give it a priority and a `Settings` enable flag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from .config import Settings
from .forecast import non_ev_load_kwh_in_window, pv_kwh_in_range
from .policy import Decision
from .samples import Forecast, LoadStore, SampleStore
from .state import State


@dataclass(frozen=True)
class ActionContext:
    """Tick-scoped inputs the Controller passes to every Action.

    Pre-computed once per tick so actions don't repeat the same lookups
    against the DB. Marked `frozen` so individual actions can't mutate
    each other's view of the world.
    """

    now: datetime
    settings: Settings

    # Pre-computed dump window for this tick — actions can check
    # `in_dump_window` instead of recomputing from settings.
    dump_start: datetime
    dump_end: datetime

    # Optional data sources. Actions that need forecasts request them
    # via these references; tests can construct contexts without them
    # when the action under test doesn't need that data.
    pv_forecasts: list[Forecast] = field(default_factory=list)
    sample_store: SampleStore | None = None
    load_store: LoadStore | None = None
    ev_circuit_name: str = "EV Charger"

    @property
    def in_dump_window(self) -> bool:
        return self.dump_start <= self.now < self.dump_end


class Action(Protocol):
    """The interface every action must satisfy."""

    name: str
    priority: int
    enabled_setting: str  # attr name on Settings; True ⇒ action runnable

    def applies(self, state: State, ctx: ActionContext) -> bool: ...
    def decide(self, state: State, ctx: ActionContext) -> Decision: ...


# --- concrete actions --------------------------------------------------------


class Surplus:
    """Charge from solar surplus once the battery is at reserve.

    Excluded from the dump window so it doesn't fight MorningDump.
    Below-min rates result in a hold (target=0, on=False), not the
    legacy "round up to 6 A" behavior that wasted battery overnight.
    """

    name = "surplus"
    priority = 20
    enabled_setting = "surplus_enabled"

    def applies(self, state: State, ctx: ActionContext) -> bool:
        if state.soc_pct is None or state.solar_w is None or state.load_w is None:
            return False
        if state.soc_pct < ctx.settings.battery_reserve_pct:
            return False
        if state.solar_w <= 0:
            return False
        if ctx.in_dump_window:
            return False
        return True

    def decide(self, state: State, ctx: ActionContext) -> Decision:
        s = ctx.settings
        # Subtract the EV's own draw so we don't think we have more
        # surplus than we do. Prefer the measured Emporia EV-circuit
        # reading — the proxy `ev_amps × voltage` is a phantom when the
        # EVSE is configured ON but the car is Standby/Disconnected,
        # which made the controller ratchet to the max amperage even
        # with the car unplugged.
        if state.ev_circuit_w is not None:
            ev_w_now = state.ev_circuit_w
        else:
            ev_w_now = (
                (state.ev_amps or 0) * s.ev_voltage
                if state.ev_on else 0
            )
        non_ev_load_w = (state.load_w or 0) - ev_w_now
        surplus_w = (state.solar_w or 0) - non_ev_load_w

        target_amps = int(surplus_w // s.ev_voltage)
        if target_amps < s.ev_min_amps:
            return Decision(
                0, f"surplus {surplus_w:.0f}W < min {s.ev_min_amps}A", on=False,
            )
        target_amps = min(target_amps, s.ev_max_amps)
        return Decision(
            target_amps, f"surplus {surplus_w:.0f}W -> {target_amps}A", on=True,
        )


class MorningDump:
    """Drain battery to a configurable floor across a wall-clock window.

    Sizing: `(battery_headroom_kwh + pv_credit_kwh − non_ev_load_kwh) /
    remaining_window_h → kW → amps`. Holds at 0 when the natural rate
    falls below ev_min_amps (preserves battery for HVAC); fires when it
    rises above. Sunny-day mode lowers the floor when today's forecast
    PV clears `morning_dump_sunny_threshold_kwh`.
    """

    name = "morning_dump"
    priority = 40
    enabled_setting = "morning_dump_enabled"

    def applies(self, state: State, ctx: ActionContext) -> bool:
        if state.soc_pct is None:
            return False
        if not ctx.in_dump_window:
            return False
        if state.soc_pct <= self._floor_pct(ctx):
            return False
        return True

    def decide(self, state: State, ctx: ActionContext) -> Decision:
        s = ctx.settings
        soc = state.soc_pct or 0.0
        floor = self._floor_pct(ctx)
        battery_kwh = (soc - floor) / 100.0 * s.battery_capacity_kwh

        # Forecast credit (PV expected during remaining window).
        credit_frac = s.morning_dump_pv_credit_pct / 100.0
        window_lo = int(max(ctx.now, ctx.dump_start).timestamp())
        window_hi = int(ctx.dump_end.timestamp())
        raw_pv_kwh = pv_kwh_in_range(ctx.pv_forecasts, window_lo, window_hi)
        pv_credit_kwh = raw_pv_kwh * credit_frac

        # Non-EV load forecast (yesterday's same window, measured).
        if ctx.sample_store is not None and ctx.load_store is not None:
            non_ev_load_kwh = non_ev_load_kwh_in_window(
                ctx.sample_store, ctx.load_store,
                window_lo, window_hi,
                ev_circuit_name=ctx.ev_circuit_name,
            )
        else:
            non_ev_load_kwh = 0.0

        remaining_hr = max((ctx.dump_end - ctx.now).total_seconds() / 3600.0, 0.05)
        headroom = battery_kwh + pv_credit_kwh - non_ev_load_kwh
        kw = headroom / remaining_hr
        raw_amps = int(kw * 1000.0 / s.ev_voltage)

        sunny = floor != s.morning_dump_floor_pct
        sunny_prefix = "sunny: " if sunny else ""

        if raw_amps < s.ev_min_amps:
            reason = (
                f"{sunny_prefix}hold: natural {raw_amps} A < min "
                f"{s.ev_min_amps} A (SoC {soc:.0f}% > floor {floor}%)"
            )
            return Decision(0, reason, on=False)

        amps = min(raw_amps, s.ev_max_amps, s.morning_dump_max_amps)
        reason = (
            f"{sunny_prefix}dump {battery_kwh:.1f}+{pv_credit_kwh:.1f}"
            f"-{non_ev_load_kwh:.1f} kWh in {remaining_hr:.2f} h -> {amps} A"
        )
        return Decision(amps, reason, on=True)

    def _floor_pct(self, ctx: ActionContext) -> int:
        """Pick normal vs sunny floor based on today's forecast PV."""
        s = ctx.settings
        if not ctx.pv_forecasts:
            return s.morning_dump_floor_pct
        day_start = ctx.dump_start.replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        from datetime import timedelta
        day_end = day_start + timedelta(days=1)
        forecast_kwh = pv_kwh_in_range(
            ctx.pv_forecasts,
            int(day_start.timestamp()),
            int(day_end.timestamp()),
        )
        if forecast_kwh >= s.morning_dump_sunny_threshold_kwh:
            return s.morning_dump_sunny_floor_pct
        return s.morning_dump_floor_pct


# Default action roster. Order doesn't matter (priority decides winners),
# but listing high-priority first reads naturally.
#
# The old "off" / "manual" / "trickle" modes are deliberately not actions
# in the new world — the user disables automation entirely via the
# Controller's kill_switch and sets EVSE amperage from the Emporia app.
DEFAULT_ACTIONS: list[Action] = [
    MorningDump(),
    Surplus(),
]

"""Controller: orchestrates one tick of the state + action pipeline.

The Controller holds the latest immutable `State` (see `state.py`) and
runs a single pass per tick:

  1. Advance state via `step()` — snap to measurements when fresh,
     extrapolate when not. The Controller is the sole owner of the
     `state.step` -> `self.state` reassignment.
  2. Build an `ActionContext` from this tick's inputs (now, settings,
     dump window, forecasts, store references).
  3. Filter the registered actions to those whose `Settings`-enable flag
     is True and whose `applies(state, ctx)` predicate is satisfied.
  4. Pick the highest-priority survivor; ties at the top are logged as a
     warning (actions partition by construction — overlap is a smell).
  5. Return the winner's `Decision`.

No FSM, no transitions, no persistent mode flags. Every tick
re-evaluates from scratch. See `actions.py` for the `Action` Protocol
and the bundled `DEFAULT_ACTIONS` roster.

The Controller does not write to the DB and does not log per-tick
apply/decide rationale — those are the caller's job (`web.py`). The
only log line emitted on the normal path is the priority-tie warning.

A kill-switch is exposed for the dashboard. When engaged, the
Controller still advances state (so telemetry stays current) but
short-circuits action evaluation and returns a no-op `Decision`
reflecting the EVSE's last-known configured rate.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from loguru import logger

from .actions import DEFAULT_ACTIONS, Action, ActionContext
from .config import Settings
from .emporia import ChargerState
from .policy import Decision
from .powerwall import PowerReading
from .runtime_config import effective as _effective_settings
from .samples import Forecast, LoadStore, SampleStore
from .state import State, step
from .timewindow import next_dump_window


class Controller:
    """Orchestrates one tick of the control loop.

    Holds the latest immutable `State`. Each tick: step state forward
    incorporating measurements, then evaluate enabled actions and
    return the highest-priority winner's `Decision`. No FSM, no
    persistent mode flags — every tick re-evaluates from scratch.

    Kill-switch: setting `kill_switch=True` (or calling
    `engage_kill_switch()`) suppresses action dispatch while still
    advancing internal state from telemetry. The returned `Decision`
    in that case is a no-op that reflects the EVSE's last-known rate.
    """

    state: State
    settings: Settings
    actions: list[Action]
    kill_switch: bool
    last_decision: Decision | None

    def __init__(
        self,
        settings: Settings,
        actions: list[Action] | None = None,
    ) -> None:
        self.settings = settings
        self.actions = actions if actions is not None else list(DEFAULT_ACTIONS)
        self.state = State()
        self.kill_switch = False
        self.last_decision = None

    # --- kill-switch -------------------------------------------------------

    def engage_kill_switch(self) -> None:
        """Stop pushing decisions to the EVSE; state still advances."""
        self.kill_switch = True

    def release_kill_switch(self) -> None:
        """Resume normal action dispatch."""
        self.kill_switch = False

    # --- tick --------------------------------------------------------------

    def tick(
        self,
        now: datetime,
        *,
        pw: PowerReading | None,
        em_load_w: float | None,
        ev: ChargerState | None,
        pv_forecasts: list[Forecast],
        sample_store: SampleStore | None = None,
        load_store: LoadStore | None = None,
        ev_circuit_name: str = "EV Charger",
        ev_circuit_w: float | None = None,
    ) -> Decision:
        # 1. Advance state from this tick's measurements. Look up the
        # forecasted PV at `now` so step() can derive battery_w from
        # the power balance when PW3 is dark — letting Emporia load
        # spikes and forecast solar inform the dead-reckoning instead
        # of blindly extrapolating from the held PW3 rate.
        from .forecast import pv_w_at
        solar_forecast_w = pv_w_at(pv_forecasts, int(now.timestamp()))
        self.state = step(
            self.state,
            now.timestamp(),
            pw=pw,
            em_load_w=em_load_w,
            solar_forecast_w=solar_forecast_w,
            ev=ev,
            ev_circuit_w=ev_circuit_w,
            settings=self.settings,
        )

        decision = self._decide(now, pv_forecasts, sample_store, load_store,
                                 ev_circuit_name)
        self.last_decision = decision
        return decision

    def _decide(
        self,
        now: datetime,
        pv_forecasts: list[Forecast],
        sample_store: SampleStore | None,
        load_store: LoadStore | None,
        ev_circuit_name: str,
    ) -> Decision:
        # Kill-switch short-circuit. Telemetry already updated; we just
        # refuse to act. Reflect the EVSE's current configured rate so
        # the dashboard renders something meaningful.
        if self.kill_switch:
            return Decision(
                self.state.ev_amps or 0,
                "kill switch engaged",
                on=self.state.ev_on or False,
                action_name="kill_switch",
            )

        # Apply today's UI-set overrides on top of the persisted Settings.
        # Overrides live in state/config_overrides.json and expire at
        # local midnight — see elec_auto.runtime_config.
        eff = _effective_settings(self.settings)
        dump_start, dump_end = next_dump_window(now, eff)
        ctx = ActionContext(
            now=now,
            settings=eff,
            dump_start=dump_start,
            dump_end=dump_end,
            pv_forecasts=pv_forecasts,
            sample_store=sample_store,
            load_store=load_store,
            ev_circuit_name=ev_circuit_name,
        )

        candidates = [
            a for a in self.actions
            if getattr(eff, a.enabled_setting, True)
            and a.applies(self.state, ctx)
        ]
        if not candidates:
            return Decision(0, "no action applies", on=False, action_name="none")

        # Highest priority wins. Ties at the top are a design smell
        # (actions are supposed to partition by predicate) — warn but
        # still pick deterministically by the actions' list order
        # (Python's sort is stable, so the first registered wins).
        candidates.sort(key=lambda a: a.priority, reverse=True)
        if len(candidates) > 1 and candidates[0].priority == candidates[1].priority:
            tied = [a.name for a in candidates if a.priority == candidates[0].priority]
            logger.warning(
                "multiple actions tied at priority {}: {}",
                candidates[0].priority,
                tied,
            )
        winner = candidates[0]
        decision = winner.decide(self.state, ctx)
        # Stamp the action's name so downstream (logging, sample
        # recording, dashboard) can identify what fired without
        # sniffing the reason string.
        return replace(decision, action_name=winner.name)

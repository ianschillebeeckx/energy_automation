"""Observed state of the home-energy system.

`State` is an immutable snapshot of what the system *is* at a moment in
time. It carries no control intent (no charge mode, no enabled actions,
no dump-window bookkeeping) and no cumulative totals — only
instantaneous quantities. `soc_pct` is the one apparent exception, and
it is: an inherently cumulative quantity that we maintain as a
forward-integrated estimate when PW3 is dark.

The single entry point is `step(state, now, ...)` — Kalman-shaped in
spirit (snap to measurement when present, extrapolate from dynamics when
not), one pass per tick, returns a new State. The Controller holds a
mutable reference to the latest immutable State.

Source preference for the effective `load_w`:

  fresh PW3   > fresh Emporia   > stale PW3   > stale Emporia

PW3 sees the gateway-measured true total (including untracked circuits
like the HVAC condenser); Emporia's panel sum is an imperfect but useful
fallback. Freshness threshold lives in `Settings`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .config import Settings
from .emporia import ChargerState
from .powerwall import PowerReading

# Legacy fallback set. Named topline labels this home has historically
# used ("Garage Subpanel" was the panel-monitor's `device_name` before
# being renamed to "Garage Subpanel A"; "EV Charger" is the EVSE's
# name). Kept only so tests + old-DB queries stay meaningful when no
# live `Emporia.aggregate_labels()` is available. Production always
# passes the live set through `em_panel_sum(..., aggregate_labels=...)`.
TOPLINE_CIRCUITS = frozenset({
    "Garage Subpanel", "Garage Subpanel A", "EV Charger",
})


def em_panel_sum(
    circuits: dict[str, float] | None,
    aggregate_labels: set[str] | frozenset[str] | None = None,
) -> float | None:
    """Whole-house load estimate from an Emporia reading.

    Sums the device-topline channels — not the individual named
    circuits, because each named circuit is a child of one of those
    toplines and would double-count.

    `aggregate_labels` is the live set from
    `Emporia.aggregate_labels()`; passing it in makes renames in the
    Emporia app safe (the old TOPLINE_CIRCUITS constant broke silently
    when the user renamed "Garage Subpanel" → "Garage Subpanel A").
    When None, falls back to TOPLINE_CIRCUITS for tests that stub the
    Emporia layer.

    Returns None when the reading is missing or contains no toplines
    this tick, matching `step()`'s `em_load_w=None` semantics so the
    SoC dead-reckoning falls back rather than treating "we measured
    nothing" as "load = 0 W".
    """
    if not circuits:
        return None
    labels = aggregate_labels if aggregate_labels else TOPLINE_CIRCUITS
    toplines = [w for name, w in circuits.items() if name in labels]
    return sum(toplines) if toplines else None


@dataclass(frozen=True, slots=True)
class State:
    """Immutable snapshot of the system at one moment.

    All fields default to "unknown / zero" so an empty `State()` is a
    valid starting point for the very first tick.
    """

    ts: float = 0.0

    # Battery state of charge (displayed-%, Tesla-app scale).
    # Forward-integrated from held battery_w when PW3 is dark;
    # snapped when fresh.
    soc_pct: float | None = None

    # Instantaneous rates (W). Snapped when telemetry arrives; held
    # last-known otherwise. battery_w is also used by `step()` to
    # forward-integrate soc_pct during PW3 outages.
    solar_w: float | None = None
    battery_w: float | None = None    # > 0 discharging (Tesla convention)
    grid_w: float | None = None       # > 0 importing
    pw_load_w: float | None = None
    em_load_w: float | None = None
    load_w: float | None = None       # derived: best of {pw_load_w, em_load_w}
    # Non-EV whole-house load — same as load_w minus the EV's contribution,
    # BUT computed with a source-matched pair to avoid time-base mismatch.
    # When load_source is "pw3" (instantaneous), subtract the configured
    # EV rate proxy (also instantaneous). When "emporia" (1-min avg),
    # subtract the measured `ev_circuit_w` (also 1-min avg — cancels the
    # lag). Naive `load_w - configured_proxy` was racey during EV ramps:
    # em_load hadn't caught up yet, so subtraction underflowed to
    # negative and Surplus computed phantom PV. Now computed inside
    # `step()` so consumers can't reintroduce the mismatch by
    # re-deriving it locally.
    non_ev_load_w: float | None = None

    # EVSE last-known state (configured rate, not measured current).
    ev_amps: int | None = None
    ev_on: bool | None = None
    ev_status: str | None = None      # "Charging" / "Standby" / "Disconnected"

    # Measured EV-circuit watts from Emporia. Distinct from ev_amps: when
    # the EVSE is configured ON at 40 A but the car isn't drawing (Standby /
    # Disconnected), the proxy ev_amps × voltage is a phantom; this field
    # carries reality. Snapped from the per-tick circuits dict, held across
    # ticks where Emporia is dark (same pattern as em_load_w).
    ev_circuit_w: float | None = None

    # Per-source last-fresh-read anchors (unix seconds). Distinct from
    # `ts` so a stale reading can't masquerade as fresh.
    pw_last_ts: float | None = None
    em_last_ts: float | None = None
    ev_last_ts: float | None = None

    # Provenance — set by step() based on what happened this tick.
    # Lets consumers (dashboard, actions, sample logger) distinguish
    # measured values from estimates without re-deriving from timestamps.
    soc_source: str | None = None     # "pw3" | "estimated" | None
    load_source: str | None = None    # "pw3" | "emporia" | None


def step(
    state: State,
    now: float,
    *,
    pw: PowerReading | None,
    em_load_w: float | None,
    ev: ChargerState | None,
    settings: Settings,
    solar_forecast_w: float | None = None,
    ev_circuit_w: float | None = None,
) -> State:
    """Advance `state` to `now`, incorporating this tick's measurements.

    One pass per tick. Returns a new immutable State. The Controller's
    pattern is `self.state = step(self.state, now, ...)`.

    Per-field behavior:

      `soc_pct`              snap to `pw.battery_soc_pct` if fresh;
                             else dead-reckon using a battery_w derived
                             from the Tesla power balance:

                               battery_w = load_w − solar_w − grid_w

                             with each term picked from the freshest
                             source available this tick (see below).
      `solar_w` / `battery_w` / `grid_w` / `pw_load_w`
                             snap from `pw` if fresh; else hold.
      `em_load_w`            snap if fresh; else hold.
      `ev_*`                 snap from `ev` if fresh; else hold.
      `load_w`               derived: best of fresh-pw > fresh-em >
                             stale-pw > stale-em.

    Source preference for the SoC dead-reckoning's energy balance:
      load_w  : em_load_w (fresh) > state.pw_load_w (held) > None
      solar_w : solar_forecast_w  > state.solar_w  (held) > None
      grid_w  : state.grid_w (held) (no fallback — usually small and
                we have no surrogate)
    If any of those are None, we fall back to pure held-battery_w
    extrapolation (the prior behavior) so callers without forecasts
    still produce a value.
    """
    dt = max(0.0, now - state.ts)
    j_per_kwh = 3_600_000.0

    # SoC: snap to fresh PW3 or dead-reckon from energy-balance battery_w.
    if pw is not None:
        soc_pct: float | None = pw.battery_soc_pct
        soc_source: str | None = "pw3"
    elif state.soc_pct is not None:
        # Pick the freshest available source for each balance term.
        bal_load = (
            em_load_w if em_load_w is not None
            else state.pw_load_w
        )
        bal_solar = (
            solar_forecast_w if solar_forecast_w is not None
            else state.solar_w
        )
        bal_grid = state.grid_w
        if bal_load is not None and bal_solar is not None and bal_grid is not None:
            # Tesla balance: battery = load - solar - grid.
            battery_w_est: float | None = bal_load - bal_solar - bal_grid
        else:
            battery_w_est = state.battery_w  # fall back to held
        if battery_w_est is not None:
            # Add the always-on DC draw inside the Powerwall (gateway,
            # BMS, thermal mgmt). Never crosses the inverter so it's
            # invisible in the AC balance, but it does drain SoC and
            # shows up empirically as a ~50-80 W bias on AC-derived
            # extrapolation across long outages.
            battery_w_est += settings.battery_vampire_w
            usable_kwh = settings.battery_capacity_kwh * (
                1.0 - settings.battery_raw_floor_pct / 100.0
            )
            if usable_kwh > 0:
                delta_pct = -battery_w_est * dt / j_per_kwh / usable_kwh * 100.0
                soc_pct = max(0.0, min(100.0, state.soc_pct + delta_pct))
            else:
                soc_pct = state.soc_pct
            soc_source = "estimated"
        else:
            soc_pct = state.soc_pct
            soc_source = state.soc_source
    else:
        soc_pct = state.soc_pct
        soc_source = state.soc_source

    # PW3 instantaneous rates.
    if pw is not None:
        solar_w = pw.solar_w
        battery_w = pw.battery_w
        grid_w = pw.grid_w
        pw_load_w: float | None = pw.load_w
        pw_last_ts: float | None = now
    else:
        solar_w = state.solar_w
        battery_w = state.battery_w
        grid_w = state.grid_w
        pw_load_w = state.pw_load_w
        pw_last_ts = state.pw_last_ts

    # Emporia load surrogate.
    if em_load_w is not None:
        em_load = em_load_w
        em_last_ts: float | None = now
    else:
        em_load = state.em_load_w
        em_last_ts = state.em_last_ts

    # Measured EV-circuit watts. Same source as em_load_w (Emporia); snap
    # when present, hold last-known otherwise.
    if ev_circuit_w is not None:
        ev_circ_w = ev_circuit_w
    else:
        ev_circ_w = state.ev_circuit_w

    # EVSE.
    if ev is not None:
        ev_amps: int | None = ev.charge_rate_a
        ev_on: bool | None = ev.on
        ev_status: str | None = ev.status
        ev_last_ts: float | None = now
    else:
        ev_amps = state.ev_amps
        ev_on = state.ev_on
        ev_status = state.ev_status
        ev_last_ts = state.ev_last_ts

    load_w, load_source = _pick_load(
        pw_load_w=pw_load_w, em_load_w=em_load,
        pw_last_ts=pw_last_ts, em_last_ts=em_last_ts,
        now=now, settings=settings,
    )

    non_ev_load_w = _compute_non_ev_load(
        load_source=load_source,
        pw_load_w=pw_load_w,
        em_load_w=em_load,
        ev_circuit_w=ev_circ_w,
        ev_amps=ev_amps, ev_on=ev_on, ev_status=ev_status,
        settings=settings,
    )

    return State(
        ts=now,
        soc_pct=soc_pct,
        solar_w=solar_w, battery_w=battery_w, grid_w=grid_w,
        pw_load_w=pw_load_w, em_load_w=em_load, load_w=load_w,
        non_ev_load_w=non_ev_load_w,
        ev_amps=ev_amps, ev_on=ev_on, ev_status=ev_status,
        ev_circuit_w=ev_circ_w,
        pw_last_ts=pw_last_ts, em_last_ts=em_last_ts, ev_last_ts=ev_last_ts,
        soc_source=soc_source, load_source=load_source,
    )


def _compute_non_ev_load(
    *,
    load_source: str | None,
    pw_load_w: float | None,
    em_load_w: float | None,
    ev_circuit_w: float | None,
    ev_amps: int | None,
    ev_on: bool | None,
    ev_status: str | None,
    settings: Settings,
) -> float | None:
    """Whole-house load minus the EV, time-base-matched to `load_source`.

    Two rails, one per source, so the subtracted EV value always has
    the same lag as the load it's being subtracted from:

      pw3      : pw_load (instantaneous) − configured proxy
                 (ev_amps × voltage, only when EVSE reports "Charging"
                 — Standby/Disconnected means contactor is open and the
                 car is drawing ~0 regardless of the configured rate).
      emporia  : em_load (1-min avg) − ev_circuit_w (1-min avg).
                 Falls back to config proxy if the EV channel is dark.

    Clamped at 0 (a negative here means our EV estimate exceeded the
    load measurement — sensor noise or brief lag mismatch, not a real
    negative house load).
    """
    if load_source == "pw3" and pw_load_w is not None:
        # Trust the configured rate when the EVSE is on AND not
        # explicitly Standby/Disconnected (either of those means the
        # contactor is open — the car is drawing ~0 regardless of the
        # configured amps). Unknown status (None) counts as "maybe
        # charging" — safer to over-subtract slightly than to leave
        # phantom EV in the non-EV total.
        if ev_on and ev_status not in ("Standby", "Disconnected"):
            ev_w = (ev_amps or 0) * settings.ev_voltage
        else:
            ev_w = 0.0
        return max(0.0, pw_load_w - ev_w)
    if load_source == "emporia" and em_load_w is not None:
        if ev_circuit_w is not None:
            return max(0.0, em_load_w - ev_circuit_w)
        # No Emporia EV reading — degrade gracefully with the config
        # proxy. Not perfect (mismatched time base) but bounded at 0.
        # Trust the configured rate when the EVSE is on AND not
        # explicitly Standby/Disconnected (either of those means the
        # contactor is open — the car is drawing ~0 regardless of the
        # configured amps). Unknown status (None) counts as "maybe
        # charging" — safer to over-subtract slightly than to leave
        # phantom EV in the non-EV total.
        if ev_on and ev_status not in ("Standby", "Disconnected"):
            ev_w = (ev_amps or 0) * settings.ev_voltage
        else:
            ev_w = 0.0
        return max(0.0, em_load_w - ev_w)
    return None


def _pick_load(
    *,
    pw_load_w: float | None,
    em_load_w: float | None,
    pw_last_ts: float | None,
    em_last_ts: float | None,
    now: float,
    settings: Settings,
) -> tuple[float | None, str | None]:
    """Best available load reading and the source it came from.

    Returns (value, source) where source is "pw3" / "emporia" / None.
    Source identifies which channel's reading was picked; freshness is
    separately observable via the corresponding `*_last_ts` field.
    """
    max_age = settings.telemetry_fresh_sec
    pw_fresh = pw_last_ts is not None and (now - pw_last_ts) <= max_age
    em_fresh = em_last_ts is not None and (now - em_last_ts) <= max_age
    if pw_fresh and pw_load_w is not None:
        return pw_load_w, "pw3"
    if em_fresh and em_load_w is not None:
        return em_load_w, "emporia"
    if pw_load_w is not None:
        return pw_load_w, "pw3"
    if em_load_w is not None:
        return em_load_w, "emporia"
    return None, None

"""Minimal browser UI for manual charger control and live state display.

Binds to localhost by default. There is no auth — do not expose this to the
open LAN without putting a reverse proxy / password in front of it.
"""

from __future__ import annotations

import asyncio
import html
import math
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

from .config import settings
from .controller import compute_target
from .emporia import ChargerState, Emporia
from .flow import Flows, decompose
from .nest import Nest
from .policy import Decision
from .powerwall import Powerwall, PowerReading


@asynccontextmanager
async def _lifespan(app_: FastAPI):
    loop = asyncio.get_running_loop()
    task = loop.create_task(_control_loop())
    logger.info("control loop started (mode={}, interval={}s)",
                _charge_mode, settings.poll_interval_sec)
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="elec_auto", docs_url=None, redoc_url=None, lifespan=_lifespan)

_pw: Powerwall | None = None
_em: Emporia | None = None
_nest: Nest | None = None

_CHARGE_MODES: dict[str, str] = {
    "surplus":      "Surplus energy",
    "morning_dump": "Morning dump",
    "trickle":      f"Trickle ({settings.trickle_kw:.0f} kW)",
    "manual":       "Manual",
    "off":          "Off",
}
_charge_mode: str = "surplus"
# Tracks whether morning_dump has been actively pushing this window. Used to
# trigger an auto-flip to "surplus" once the dump completes (floor reached
# or window closed after activity).
_dump_was_active: bool = False
# Counts consecutive ticks where the Powerwall read failed. Crosses
# `pw_fail_safe_ticks` => charger off until reads recover.
_pw_fail_count: int = 0
# Latches once the EV is detected as "plugged but not drawing" (full / faulted
# / paused). Resets on mode change or unplug → replug. While set, surplus mode
# treats the EV as unavailable and routes power to the Nest path instead.
_ev_not_accepting: bool = False
# Tracks plugged_in transitions so we can reset _ev_not_accepting when the
# user unplugs and replugs the car (a clean signal of "I want to retest").
_ev_was_plugged_in: bool = False


def _powerwall() -> Powerwall:
    global _pw
    if _pw is None:
        _pw = Powerwall(settings)
    return _pw


def _emporia() -> Emporia:
    global _em
    if _em is None:
        _em = Emporia(settings)
    return _em


def _nest_client() -> Nest:
    global _nest
    if _nest is None:
        _nest = Nest(settings)
    return _nest


def _safe_pw() -> tuple[PowerReading | None, str | None]:
    try:
        return _powerwall().read(), None
    except Exception as e:
        logger.warning("powerwall read failed: {}", e)
        return None, str(e)


def _safe_em() -> tuple[ChargerState | None, str | None]:
    try:
        em = _emporia()
        ev = em.read()
    except Exception as e:
        logger.exception("emporia read failed")
        return None, str(e)
    try:
        ev.actual_watts = em.actual_watts()
    except Exception:
        logger.warning("emporia draw read failed", exc_info=False)
        ev.actual_watts = None
    return ev, None


def _safe_top_consumers(n: int = 3) -> list[tuple[str, float]] | None:
    try:
        return _emporia().top_consumers(n)
    except Exception:
        logger.exception("emporia top_consumers failed")
        return None


def _next_dump_start(s) -> datetime:
    """Wall-clock moment when the next morning-dump window opens."""
    from zoneinfo import ZoneInfo

    from .controller import _next_dump_window
    return _next_dump_window(datetime.now(ZoneInfo(s.timezone)), s)[0]


def _surplus_watts(pw: PowerReading) -> float:
    """Same surplus formula the policy uses, for the Nest dispatch decision."""
    if pw.battery_soc_pct < settings.battery_reserve_pct:
        battery_reserve_w = settings.battery_max_charge_kw * 1000
    else:
        battery_reserve_w = 0
    return pw.solar_w - pw.load_w - battery_reserve_w


def _restore_nest_default() -> None:
    """Push the Nest setpoint back to the default target when leaving
    surplus mode. Only acts if the current setpoint is still the surplus
    value we'd previously written, so a manual change you make mid-window
    survives the mode switch.
    """
    try:
        nest = _nest_client()
    except Exception:
        return
    if not nest.enabled:
        return
    try:
        state = nest.read()
    except Exception:
        logger.warning("nest read failed during cleanup", exc_info=False)
        return
    if state.mode == "HEAT":
        cur, surplus_t, default_t = (
            state.heat_setpoint_f,
            settings.nest_surplus_heat_f,
            settings.nest_default_heat_f,
        )
        if cur is None or abs(cur - surplus_t) > 0.5:
            return  # not at our surplus target — leave alone
        try:
            nest.set_heat_target_f(default_t)
            logger.info("nest cleanup: HEAT {:.1f} F -> {} F", cur, default_t)
        except Exception:
            logger.warning("nest cleanup set_heat failed", exc_info=False)
    elif state.mode == "COOL":
        cur, surplus_t, default_t = (
            state.cool_setpoint_f,
            settings.nest_surplus_cool_f,
            settings.nest_default_cool_f,
        )
        if cur is None or abs(cur - surplus_t) > 0.5:
            return
        try:
            nest.set_cool_target_f(default_t)
            logger.info("nest cleanup: COOL {:.1f} F -> {} F", cur, default_t)
        except Exception:
            logger.warning("nest cleanup set_cool failed", exc_info=False)


def _run_nest_path(pw: PowerReading) -> None:
    """Manage the Nest setpoint while we're in surplus mode and the EV
    isn't accepting energy. Picks heat-up vs cool-down based on the
    thermostat's current operating mode and only writes when the target
    differs from the current setpoint by more than 0.5 °F.
    """
    try:
        nest = _nest_client()
    except Exception:
        logger.exception("nest init failed")
        return
    if not nest.enabled:
        return
    try:
        state = nest.read()
    except Exception:
        logger.warning("nest read failed", exc_info=False)
        return

    has_surplus = _surplus_watts(pw) > 0

    if state.mode == "HEAT":
        target = (settings.nest_surplus_heat_f if has_surplus
                  else settings.nest_default_heat_f)
        cur = state.heat_setpoint_f
        if cur is None or abs(cur - target) > 0.5:
            try:
                nest.set_heat_target_f(target)
                logger.info(
                    "nest: HEAT {:.1f} F -> {} F (surplus={:.0f} W)",
                    cur if cur is not None else float("nan"), target, _surplus_watts(pw),
                )
            except Exception:
                logger.warning("nest set_heat failed", exc_info=False)
    elif state.mode == "COOL":
        target = (settings.nest_surplus_cool_f if has_surplus
                  else settings.nest_default_cool_f)
        cur = state.cool_setpoint_f
        if cur is None or abs(cur - target) > 0.5:
            try:
                nest.set_cool_target_f(target)
                logger.info(
                    "nest: COOL {:.1f} F -> {} F (surplus={:.0f} W)",
                    cur if cur is not None else float("nan"), target, _surplus_watts(pw),
                )
            except Exception:
                logger.warning("nest set_cool failed", exc_info=False)
    # HEATCOOL / OFF / ECO: don't push, the Nest's own logic owns it.


def _apply_target(decision: Decision) -> None:
    """Push a target decision to the EVSE.

    If the controller wants the charger on, push (amps, on=True). If it
    wants off but we still have a preview amperage (e.g. scheduled
    morning_dump), push (amps, on=False) so the dashboard reflects the
    intended rate. If there's no meaningful rate, just flip the switch
    off and leave whatever amperage the user configured manually.
    """
    try:
        em = _emporia()
    except Exception:
        logger.exception("apply: emporia init failed")
        return
    try:
        if decision.on and decision.target_amps >= settings.ev_min_amps:
            em.set_amps(decision.target_amps, on=True)
            logger.info("apply: mode={} set {} A on ({})",
                        _charge_mode, decision.target_amps, decision.reason)
        elif decision.target_amps >= settings.ev_min_amps:
            em.set_amps(decision.target_amps, on=False)
            logger.info("apply: mode={} set {} A off ({})",
                        _charge_mode, decision.target_amps, decision.reason)
        else:
            em.set_on(False)
            logger.info("apply: mode={} off ({})", _charge_mode, decision.reason)
    except Exception:
        logger.exception("apply: push to EVSE failed")


def _control_tick() -> None:
    global _charge_mode, _dump_was_active, _pw_fail_count
    global _ev_not_accepting, _ev_was_plugged_in
    if _charge_mode == "manual":
        return  # hands off: leave the EVSE entirely under manual control
    pw, _ = _safe_pw()
    ev, _ = _safe_em()

    # Update the "not accepting charge" latch using actual EV draw.
    # Reset on plug → replug; once we conclude the car isn't accepting, stay
    # in that state to avoid flapping the charger on/off to retest.
    if ev is not None:
        if ev.plugged_in and not _ev_was_plugged_in:
            _ev_not_accepting = False  # fresh plug; give it a chance
        _ev_was_plugged_in = ev.plugged_in
        if not ev.plugged_in:
            _ev_not_accepting = False
        elif (ev.on and ev.actual_watts is not None
                and ev.actual_watts < settings.ev_accepting_threshold_w):
            if not _ev_not_accepting:
                logger.info(
                    "EV draw {:.0f} W < {} W threshold — treating as not accepting",
                    ev.actual_watts, settings.ev_accepting_threshold_w,
                )
            _ev_not_accepting = True

    # If we've decided the car isn't accepting, present it to the policy as
    # "Disconnected" so the surplus path bails out (and, once Nest is wired,
    # the controller will hand off to the heating path).
    ev_for_decision = ev
    if ev is not None and _ev_not_accepting:
        from dataclasses import replace
        ev_for_decision = replace(ev, status="Disconnected")

    # Modes that need a Powerwall reading: ride out brief outages by
    # skipping the tick, but fail safe to off after `pw_fail_safe_ticks`
    # consecutive failures so a stuck gateway doesn't leave the charger
    # blasting indefinitely.
    if _charge_mode in {"surplus", "morning_dump"}:
        if pw is None:
            _pw_fail_count += 1
            threshold = settings.pw_fail_safe_ticks
            if _pw_fail_count == threshold:
                logger.warning(
                    "telemetry stale {} ticks — failing safe to off", threshold,
                )
                _apply_target(Decision(0, "telemetry stale", on=False))
            return
        if _pw_fail_count > 0:
            logger.info("telemetry recovered after {} failures", _pw_fail_count)
            _pw_fail_count = 0

    decision = compute_target(_charge_mode, pw, ev_for_decision, settings)

    # Surplus mode + EV unavailable => let Nest take the surplus.
    if (_charge_mode == "surplus" and pw is not None
            and (ev is None or not ev.plugged_in or _ev_not_accepting)):
        _run_nest_path(pw)

    # Auto-switch morning_dump -> surplus when the dump is done, so the
    # car keeps catching daytime surplus once the battery is drained.
    if _charge_mode == "morning_dump":
        is_active = decision.on and "dump " in decision.reason
        floor_reached = "at/below floor" in decision.reason
        # "scheduled" means we're outside the window. If we'd been active in
        # this morning's window, that means the window just closed.
        window_closed = "scheduled" in decision.reason and _dump_was_active
        if is_active:
            _dump_was_active = True
        elif floor_reached or window_closed:
            logger.info("morning_dump complete ({}) -> surplus", decision.reason)
            _charge_mode = "surplus"
            _dump_was_active = False
            decision = compute_target(_charge_mode, pw, ev, settings)

    if ev is None:
        return
    has_rate = decision.target_amps >= settings.ev_min_amps
    # Skip the network write when the EVSE already matches the target.
    if decision.on:
        if ev.on and has_rate and ev.charge_rate_a == decision.target_amps:
            return
    else:
        if not ev.on and (not has_rate or ev.charge_rate_a == decision.target_amps):
            return
    _apply_target(decision)


async def _control_loop() -> None:
    loop = asyncio.get_running_loop()
    while True:
        try:
            await loop.run_in_executor(None, _control_tick)
        except Exception:
            logger.exception("control loop tick failed")
        await asyncio.sleep(settings.poll_interval_sec)


_FO_NS = 'xmlns="http://www.w3.org/1999/xhtml"'

# Right-column panel geometry — same coordinate system as the SVG nodes.
_PANEL_X = 640
_PANEL_W = 210
_LOADS_PANEL = (_PANEL_X, 150, _PANEL_W, 120)   # aligns with Home (y=180..240)
_MODES_PANEL = (_PANEL_X, 250, _PANEL_W, 200)   # aligns with Car  (y=340..400), tall enough for 5 buttons


def _loads_foreign(consumers: list[tuple[str, float]] | None) -> str:
    if consumers is None:
        body = '<li class="muted">—</li>'
    elif not consumers:
        body = '<li class="muted">all idle</li>'
    else:
        body = "".join(
            f'<li>{html.escape(name)}<span>{watts:.0f} W</span></li>'
            for name, watts in consumers
        )
    x, y, w, h = _LOADS_PANEL
    return (
        f'<foreignObject x="{x}" y="{y}" width="{w}" height="{h}">'
        f'<div {_FO_NS} class="panel"><h3>Top loads</h3>'
        f'<ul class="loads">{body}</ul></div></foreignObject>'
    )


def _modes_foreign(pw: PowerReading | None, ev: ChargerState | None) -> str:
    # Two pairs of buttons sit side-by-side at the bottom of the panel so
    # the column doesn't have to widen. Anything in `_PAIRED_KEYS` collapses
    # into a flex row.
    paired = {"off", "manual"}

    def _hint_for(key: str) -> str:
        if key == "trickle":
            d = compute_target(key, pw, None, settings)
            return f'<small>&rarr; {d.target_amps} A</small>'
        if key == "surplus":
            d = compute_target(key, pw, ev, settings)
            return (f'<small>&rarr; {d.target_amps} A</small>'
                    if d.target_amps else f'<small>{html.escape(d.reason)}</small>')
        if key == "morning_dump":
            preview_now = _next_dump_start(settings)
            d = compute_target(key, pw, None, settings, now=preview_now)
            return (f'<small>{preview_now:%H:%M} &rarr; {d.target_amps} A</small>'
                    if d.target_amps else f'<small>{html.escape(d.reason)}</small>')
        return ""

    def _btn(key: str, label: str) -> str:
        cls = "mode-btn active" if key == _charge_mode else "mode-btn"
        return (
            f'<button type="submit" name="mode" value="{key}" class="{cls}">'
            f'{label}{_hint_for(key)}</button>'
        )

    rows: list[str] = []
    pair_buf: list[str] = []
    for key, label in _CHARGE_MODES.items():
        if key in paired:
            pair_buf.append(_btn(key, label))
        else:
            rows.append(_btn(key, label))
    if pair_buf:
        rows.append(f'<div class="mode-row">{"".join(pair_buf)}</div>')
    x, y, w, h = _MODES_PANEL
    return (
        f'<foreignObject x="{x}" y="{y}" width="{w}" height="{h}">'
        f'<div {_FO_NS} class="panel"><h3>Charge mode</h3>'
        f'<form method="post" action="/mode">' + "".join(rows) +
        '</form></div></foreignObject>'
    )


def _demo_state(scenario: str) -> tuple[PowerReading, ChargerState, list[tuple[str, float]]]:
    if scenario in ("export", "4"):
        # Battery full, no EV draw, solar covers the modest home load and
        # the rest flows out to the grid.
        pw = PowerReading(
            solar_w=6000, load_w=600, battery_w=0, grid_w=-5400, battery_soc_pct=100,
        )
        ev = ChargerState(
            gid=0, name="Demo EV", on=True,
            charge_rate_a=40, max_charge_rate_a=40, status="Standby",
        )
        consumers = [("HVAC", 400), ("Fridge", 120), ("Water Heater", 80)]
        return pw, ev, consumers

    if scenario in ("surplus", "3"):
        # Battery full, excess solar diverts to EV (the project's core goal).
        # Solar 6 kW covers a 480 W base load + 5520 W (23 A × 240 V) for
        # the car, with zero battery flow and zero grid.
        pw = PowerReading(
            solar_w=6000, load_w=6000, battery_w=0, grid_w=0, battery_soc_pct=98,
        )
        ev = ChargerState(
            gid=0, name="Demo EV", on=True,
            charge_rate_a=23, max_charge_rate_a=40, status="Charging",
        )
        consumers = [("Water Heater", 200), ("Internet & Garage Plugs", 200), ("Fridge", 80)]
        return pw, ev, consumers

    if scenario in ("sunny", "2"):
        # Solar at its 6 kW ceiling, covering a 3 kW house and pushing the
        # remaining 3 kW into the battery. No grid, no car.
        pw = PowerReading(
            solar_w=6000, load_w=3000, battery_w=-3000, grid_w=0, battery_soc_pct=68,
        )
        ev = ChargerState(
            gid=0, name="Demo EV", on=True,
            charge_rate_a=40, max_charge_rate_a=40, status="Standby",
        )
        consumers = [("HVAC", 1800), ("Fridge", 600), ("Water Heater", 400)]
        return pw, ev, consumers

    # Default "peak": worst-case draw. The PW3 inverter caps AC output at
    # 11.5 kW combined (solar + battery), so with solar at its 6 kW ceiling
    # the battery can only supply the remaining 5.5 kW. House draws oven
    # 3.8 + HVAC 3.6 + water heater 0.4 + EV 9.6 = 17.4 kW, so the grid
    # covers the balance: 17.4 − 11.5 = 5.9 kW import.
    pw = PowerReading(
        solar_w=6000, load_w=17400, battery_w=5500, grid_w=5900, battery_soc_pct=55,
    )
    ev = ChargerState(
        gid=0, name="Demo EV", on=True,
        charge_rate_a=40, max_charge_rate_a=40, status="Charging",
    )
    consumers = [("Oven", 3800), ("HVAC", 3600), ("Water Heater", 400)]
    return pw, ev, consumers


# Node geometry for the flow SVG. Anchor points are on the box edges so arrows
# terminate flush against them.
_NODES = {
    "solar":   {"x": 250, "y":  20, "w": 140, "h": 60, "color": "#e8a33d"},
    "grid":    {"x":  10, "y": 180, "w": 140, "h": 60, "color": "#4b8fd4"},
    "home":    {"x": 490, "y": 180, "w": 140, "h": 60, "color": "#888888"},
    "battery": {"x": 250, "y": 340, "w": 140, "h": 60, "color": "#2ea56a"},
    "car":     {"x": 490, "y": 340, "w": 140, "h": 60, "color": "#3aa5c7"},
}

# (src, dst, (sx, sy), (ex, ey), (label_x, label_y))
_EDGES: list[tuple[str, str, tuple[int, int], tuple[int, int], tuple[int, int]]] = [
    ("solar",   "home",    (385,  75), (500, 195), (465, 125)),
    ("solar",   "battery", (320,  80), (320, 340), (347, 150)),
    ("solar",   "grid",    (255,  75), (140, 195), (175, 125)),
    ("grid",    "home",    (150, 210), (490, 210), (395, 225)),
    ("battery", "home",    (385, 365), (500, 225), (475, 305)),
    ("battery", "grid",    (265, 345), (130, 240), (175, 305)),
    ("home",    "car",     (560, 240), (560, 340), (585, 295)),
]

# Viewbox reserves a 210-unit column to the right of the five nodes for the
# Top-loads and Charge-mode panels (embedded via foreignObject so they sit
# exactly beside Home and Car respectively). 40 extra vertical units give
# the charge-mode form room to extend below the car node.
_VIEWBOX_W = 860
_VIEWBOX_H = 460

# Minimum watts to draw an edge at full opacity (below this it's a ghost line).
_FLOW_VISIBLE_W = 50.0


# Uniform stroke for every edge — magnitude lives in the kW label, not the
# line thickness (which otherwise "funnels" at high watts vs the arrowhead).
_STROKE_W = 3.0


def _fmt_kw(watts: float) -> str:
    return f"{watts / 1000:.1f} kW"


def _clip_to_node(sx: float, sy: float, ex: float, ey: float,
                  rect: dict, margin: float = 4.0) -> tuple[float, float]:
    """Clip the (sx,sy)->(ex,ey) segment to enter `rect` at its boundary.

    Pulls the endpoint `margin` pixels back along the segment so the arrowhead
    sits outside the node box instead of being covered by it.
    """
    left, top = rect["x"], rect["y"]
    right, bottom = left + rect["w"], top + rect["h"]
    dx, dy = ex - sx, ey - sy
    ts: list[float] = []
    if dx:
        for x in (left, right):
            t = (x - sx) / dx
            if 0 < t <= 1 and top <= sy + t * dy <= bottom:
                ts.append(t)
    if dy:
        for y in (top, bottom):
            t = (y - sy) / dy
            if 0 < t <= 1 and left <= sx + t * dx <= right:
                ts.append(t)
    if not ts:
        return ex, ey
    t_enter = min(ts)
    length = (dx * dx + dy * dy) ** 0.5
    if length:
        t_enter -= margin / length
    return sx + t_enter * dx, sy + t_enter * dy


def _edge_watts(src: str, dst: str, flows: Flows | None, ev: ChargerState | None) -> float:
    # Home -> car is a sub-flow of home's total load, not a meter edge.
    if src == "home" and dst == "car":
        if ev is None or not ev.charging:
            return 0.0
        return ev.charge_rate_a * settings.ev_voltage
    if flows is None:
        return 0.0
    return float(getattr(flows, f"{src}_to_{dst}", 0.0))


def _node_value_label(name: str, pw: PowerReading | None, ev: ChargerState | None) -> str:
    if name == "car":
        if ev is None:
            return "—"
        if not ev.on:
            return f"disabled &middot; ready {ev.charge_rate_a} A"
        if not ev.charging:
            return f"{ev.status.lower() or 'idle'} &middot; ready {ev.charge_rate_a} A"
        kw = ev.charge_rate_a * settings.ev_voltage / 1000
        return f"{kw:.1f} kW &middot; {ev.charge_rate_a} A"
    if pw is None:
        return "—"
    if name == "solar":
        return _fmt_kw(pw.solar_w)
    if name == "home":
        return _fmt_kw(pw.load_w)
    if name == "battery":
        verb = "charging" if pw.battery_w < 0 else "discharging" if pw.battery_w > 0 else "idle"
        label = f"{_fmt_kw(abs(pw.battery_w))} {verb}"
        if not math.isnan(pw.battery_soc_pct):
            label += f" &middot; {pw.battery_soc_pct:.0f}%"
        return label
    if name == "grid":
        verb = "import" if pw.grid_w > 0 else "export" if pw.grid_w < 0 else "idle"
        return f"{_fmt_kw(abs(pw.grid_w))} {verb}"
    return ""


def _flow_svg(
    pw: PowerReading | None,
    ev: ChargerState | None = None,
    consumers: list[tuple[str, float]] | None = None,
) -> str:
    flows = decompose(pw) if pw is not None else None

    # Arrowhead markers, one per source color, since we color arrows by origin.
    markers = "".join(
        f'<marker id="arrow-{name}" viewBox="0 0 10 10" refX="9" refY="5" '
        f'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        f'<path d="M0,0 L10,5 L0,10 z" fill="{n["color"]}"/></marker>'
        for name, n in _NODES.items()
    )
    # Clip path for the battery SoC fill, respects the box's rounded corners.
    bn = _NODES["battery"]
    battery_clip = (
        '<clipPath id="node-battery-clip">'
        f'<rect x="{bn["x"]}" y="{bn["y"]}" width="{bn["w"]}" height="{bn["h"]}" rx="10"/>'
        '</clipPath>'
    )

    edges = []
    for src, dst, (sx, sy), (ex, ey), (lx, ly) in _EDGES:
        w = _edge_watts(src, dst, flows, ev)
        active = w >= _FLOW_VISIBLE_W
        color = _NODES[src]["color"]
        opacity = 1.0 if active else 0.12
        cx, cy = _clip_to_node(sx, sy, ex, ey, _NODES[dst])
        dash = ' stroke-dasharray="8 6"' if active else ""
        # Decreasing dashoffset shifts the pattern forward along the line —
        # faster dots for heavier flows, clamped so tiny flows still tick.
        dur = max(0.375, min(1.875, 1875.0 / max(w, 800.0))) if active else 0
        anim = (
            f'<animate attributeName="stroke-dashoffset" values="14;0" '
            f'dur="{dur:.1f}s" repeatCount="indefinite"/>'
            if active else ""
        )
        edges.append(
            f'<line x1="{sx}" y1="{sy}" x2="{cx:.1f}" y2="{cy:.1f}" '
            f'stroke="{color}" stroke-width="{_STROKE_W}" '
            f'stroke-linecap="round" opacity="{opacity:.2f}"'
            f'{dash} marker-end="url(#arrow-{src})">{anim}</line>'
        )
        if active:
            edges.append(
                f'<text x="{lx}" y="{ly}" text-anchor="middle" '
                f'class="flow-label" fill="{color}">{_fmt_kw(w)}</text>'
            )

    nodes = []
    for name, n in _NODES.items():
        cx = n["x"] + n["w"] // 2
        title_y = n["y"] + 26
        value_y = n["y"] + 46
        fill = ""
        if name == "battery" and pw is not None and not math.isnan(pw.battery_soc_pct):
            soc = max(0.0, min(100.0, pw.battery_soc_pct))
            fill_w = n["w"] * soc / 100.0
            fill = (
                f'<rect x="{n["x"]}" y="{n["y"]}" width="{fill_w:.1f}" height="{n["h"]}" '
                f'fill="{n["color"]}" fill-opacity="0.22" clip-path="url(#node-battery-clip)"/>'
            )
        nodes.append(
            f'<g><rect x="{n["x"]}" y="{n["y"]}" width="{n["w"]}" height="{n["h"]}" '
            f'rx="10" fill="var(--node-bg)" stroke="{n["color"]}" stroke-width="2"/>'
            f'{fill}'
            f'<text x="{cx}" y="{title_y}" text-anchor="middle" class="node-title">'
            f'{name.capitalize()}</text>'
            f'<text x="{cx}" y="{value_y}" text-anchor="middle" class="node-value">'
            f'{_node_value_label(name, pw, ev)}</text></g>'
        )

    return (
        f'<svg viewBox="0 0 {_VIEWBOX_W} {_VIEWBOX_H}" class="flow" '
        'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Power flow diagram">'
        f'<defs>{markers}{battery_clip}</defs>'
        + "".join(edges)
        + "".join(nodes)
        + _loads_foreign(consumers)
        + _modes_foreign(pw, ev)
        + "</svg>"
    )


def _render(flash: str = "", flash_ok: bool = True, demo: str = "") -> str:
    if demo:
        pw, ev, consumers = _demo_state(demo)
        pw_err = ev_err = None
    else:
        pw, pw_err = _safe_pw()
        ev, ev_err = _safe_em()
        consumers = _safe_top_consumers()
    try:
        decision = compute_target(_charge_mode, pw, ev, settings)
    except Exception as e:
        logger.exception("compute_target failed")
        decision = Decision(0, f"policy error: {e}")

    def fmt_w(v: float) -> str:
        return f"{v:+.0f} W" if v else "0 W"

    pw_rows = (
        f"<tr><td>solar</td><td>{fmt_w(pw.solar_w)}</td></tr>"
        f"<tr><td>home load</td><td>{fmt_w(pw.load_w)}</td></tr>"
        f"<tr><td>battery</td><td>{fmt_w(pw.battery_w)} "
        f"<small>({'discharging' if pw.battery_w > 0 else 'charging' if pw.battery_w < 0 else 'idle'})</small></td></tr>"
        f"<tr><td>grid</td><td>{fmt_w(pw.grid_w)} "
        f"<small>({'importing' if pw.grid_w > 0 else 'exporting' if pw.grid_w < 0 else 'balanced'})</small></td></tr>"
        f"<tr><td>SoC</td><td><b>{pw.battery_soc_pct:.1f} %</b></td></tr>"
        if pw
        else f"<tr><td colspan=2 class=err>Powerwall unavailable: {html.escape(pw_err or '')}</td></tr>"
    )

    if ev:
        amps_value = ev.charge_rate_a
        on_checked = "checked" if ev.on else ""
        ev_rows = (
            f"<tr><td>name</td><td>{html.escape(ev.name)} <small>(gid {ev.gid})</small></td></tr>"
            f"<tr><td>state</td><td><b>{'ON' if ev.on else 'OFF'}</b></td></tr>"
            f"<tr><td>rate</td><td>{ev.charge_rate_a} A <small>(max {ev.max_charge_rate_a} A)</small></td></tr>"
        )
    else:
        amps_value = settings.ev_min_amps
        on_checked = ""
        ev_rows = f"<tr><td colspan=2 class=err>Emporia unavailable: {html.escape(ev_err or '')}</td></tr>"

    decision_html = (
        f'<p class=decision><b>{html.escape(_CHARGE_MODES[_charge_mode])}:</b> '
        f'{decision.target_amps} A <small>({html.escape(decision.reason)})</small></p>'
    )

    flash_html = ""
    if flash:
        cls = "ok" if flash_ok else "err"
        flash_html = f"<p class='flash {cls}'>{html.escape(flash)}</p>"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>elec_auto</title>
<style>
  :root {{ color-scheme: light dark; --muted:#888; --ok:#2a7; --err:#c33; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 900px; margin: 2em auto; padding: 0 1em; --node-bg: #fff; }}
  @media (prefers-color-scheme: dark) {{ body {{ --node-bg: #1a1a1a; }} }}
  svg.flow {{ width: 100%; height: auto; max-width: 860px; display: block; margin: .5em auto 1.5em; }}
  svg.flow .node-title {{ font-size: 14px; font-weight: 600; fill: currentColor; }}
  svg.flow .node-value {{ font-size: 12px; fill: var(--muted); }}
  svg.flow .flow-label {{ font-size: 12px; font-weight: 600; font-variant-numeric: tabular-nums; paint-order: stroke; stroke: var(--node-bg); stroke-width: 3px; }}
  /* Panels embedded in the SVG via <foreignObject>. They use the same
     coordinate system as the nodes so they stay aligned at every zoom.
     Flex column + justify-content centers the content vertically inside
     the box so it lines up with its companion node. */
  svg.flow .panel {{ font-size: 12px; color: currentColor; height: 100%; box-sizing: border-box; display: flex; flex-direction: column; justify-content: center; }}
  svg.flow .panel h3 {{ font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin: 0 0 .4em 0; }}
  svg.flow .panel ul.loads {{ list-style: none; padding: 0; margin: 0; }}
  svg.flow .panel ul.loads li {{ display: flex; justify-content: space-between; padding: .2em 0; }}
  svg.flow .panel ul.loads li span {{ color: var(--muted); font-variant-numeric: tabular-nums; }}
  svg.flow .panel ul.loads li.muted {{ color: var(--muted); justify-content: center; }}
  svg.flow .panel form {{ margin: 0; padding: 0; }}
  svg.flow .panel .mode-btn {{ display: flex; justify-content: space-between; align-items: baseline; width: 100%; text-align: left; padding: .4em .6em; margin-bottom: .3em; border: 1px solid #8884; border-radius: 6px; background: transparent; cursor: pointer; font: inherit; color: inherit; }}
  svg.flow .panel .mode-row {{ display: flex; gap: .3em; margin-bottom: .3em; }}
  svg.flow .panel .mode-row .mode-btn {{ flex: 1 1 0; min-width: 0; justify-content: center; margin-bottom: 0; }}
  svg.flow .panel .mode-btn:hover {{ background: #0001; }}
  svg.flow .panel .mode-btn.active {{ border-color: var(--ok); background: color-mix(in srgb, var(--ok) 14%, transparent); }}
  svg.flow .panel .mode-btn small {{ color: var(--muted); font-variant-numeric: tabular-nums; }}
  h1 {{ margin: 0 0 .2em 0; font-size: 1.3em; }}
  .sub {{ color: var(--muted); margin: 0 0 1.5em 0; font-size: .85em; }}
  h2 {{ margin: 1.5em 0 .4em 0; font-size: 1em; border-bottom: 1px solid #8882; padding-bottom: .2em; }}
  table {{ width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }}
  td {{ padding: .3em .2em; }}
  td:first-child {{ color: var(--muted); width: 40%; }}
  small {{ color: var(--muted); }}
  .decision {{ background: #0001; padding: .6em .8em; border-radius: 6px; }}
  form label {{ display: block; margin: .8em 0 .3em 0; }}
  form input[type=number] {{ width: 6em; font-size: 1em; padding: .3em; }}
  form input[type=range] {{ width: 100%; }}
  form button {{ margin-top: 1em; padding: .5em 1.2em; font-size: 1em; }}
  .flash {{ padding: .6em .8em; border-radius: 6px; margin: 1em 0; }}
  .flash.ok {{ background: #2a71; color: var(--ok); }}
  .flash.err {{ background: #c331; color: var(--err); }}
  .err {{ color: var(--err); }}
</style></head><body>
<p class="sub">{now} &middot; auto-refresh 15s</p>
{flash_html}

{_flow_svg(pw, ev, consumers)}

<h2>Powerwall</h2>
<table>{pw_rows}</table>

<h2>EV Charger</h2>
<table>{ev_rows}</table>

{decision_html}

<h2>Manual override</h2>
<form method="post" action="/set">
  <label><input type="checkbox" name="on" value="on" {on_checked}> Charger enabled</label>
  <label>Charge current (A):
    <input type="number" name="amps" min="{settings.ev_min_amps}" max="{settings.ev_max_amps}" value="{amps_value}">
  </label>
  <button type="submit">Apply</button>
</form>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index(demo: str = "") -> str:
    return _render(demo=demo)


@app.post("/mode")
def set_mode(mode: Annotated[str, Form()]) -> RedirectResponse:
    global _charge_mode, _dump_was_active, _pw_fail_count, _ev_not_accepting
    if mode in _CHARGE_MODES:
        previous = _charge_mode
        _charge_mode = mode
        _dump_was_active = False
        _pw_fail_count = 0
        _ev_not_accepting = False
        logger.info("charge mode -> {}", mode)
        # Leaving surplus: undo our setpoint nudge if it's still in place.
        if previous == "surplus" and mode != "surplus":
            _restore_nest_default()
        if mode != "manual":
            pw, _ = _safe_pw()
            ev, _ = _safe_em()
            _apply_target(compute_target(mode, pw, ev, settings))
    return RedirectResponse("/", status_code=303)


@app.post("/set")
def set_charger(
    amps: Annotated[int, Form()],
    on: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    desired_on = on == "on"
    try:
        new_state = _emporia().set_amps(amps, on=desired_on)
        flash = f"Set charger {'ON' if new_state.on else 'OFF'} @ {new_state.charge_rate_a} A"
        return HTMLResponse(_render(flash=flash, flash_ok=True))
    except Exception as e:
        logger.exception("set_amps failed")
        return HTMLResponse(_render(flash=f"Failed: {e}", flash_ok=False), status_code=500)


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")

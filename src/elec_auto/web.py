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
from .policy import Decision
from .powerwall import Powerwall, PowerReading
from .samples import Sample, SampleStore
from .solar import theoretical_w


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
_samples: SampleStore | None = None


def _sample_store() -> SampleStore:
    global _samples
    if _samples is None:
        from pathlib import Path
        _samples = SampleStore(Path("state") / "samples.db")
    return _samples

_CHARGE_MODES: dict[str, str] = {
    "surplus":      "Surplus solar",
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


def _safe_pw() -> tuple[PowerReading | None, str | None]:
    try:
        return _powerwall().read(), None
    except Exception as e:
        logger.exception("powerwall read failed")
        return None, str(e)


def _safe_em() -> tuple[ChargerState | None, str | None]:
    try:
        return _emporia().read(), None
    except Exception as e:
        logger.exception("emporia read failed")
        return None, str(e)


def _safe_top_consumers(n: int = 3) -> list[tuple[str, float]] | None:
    try:
        return _emporia().top_consumers(n)
    except Exception:
        logger.exception("emporia top_consumers failed")
        return None


_sunset_cache: tuple[object, datetime] | None = None


def _todays_sunset() -> datetime | None:
    """Astronomical sunset for today at the configured location.

    Returns None if latitude/longitude aren't set. Cached for the day so we
    don't recompute on every tick.
    """
    global _sunset_cache
    if settings.latitude is None or settings.longitude is None:
        return None
    from zoneinfo import ZoneInfo

    from astral import Observer
    from astral.sun import sun
    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).date()
    if _sunset_cache is not None and _sunset_cache[0] == today:
        return _sunset_cache[1]
    obs = Observer(latitude=settings.latitude, longitude=settings.longitude)
    sunset = sun(obs, date=today, tzinfo=tz)["sunset"]
    _sunset_cache = (today, sunset)
    return sunset


def _is_past_sunset() -> bool:
    """True iff local time is at or past today's sunset (and we have coords)."""
    from zoneinfo import ZoneInfo

    sunset = _todays_sunset()
    if sunset is None:
        return False
    return datetime.now(ZoneInfo(settings.timezone)) >= sunset


def _next_dump_start(s) -> datetime:
    """Wall-clock moment when the next morning-dump window opens."""
    from zoneinfo import ZoneInfo

    from .controller import _next_dump_window
    return _next_dump_window(datetime.now(ZoneInfo(s.timezone)), s)[0]


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
        if decision.on:
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
    global _charge_mode, _dump_was_active
    if _charge_mode == "manual":
        return  # hands off: leave the EVSE entirely under manual control
    pw, _ = _safe_pw()
    ev, _ = _safe_em()
    decision = compute_target(_charge_mode, pw, ev, settings)

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

    # Auto-switch surplus -> morning_dump once we cross today's astronomical
    # sunset, queueing the next morning's scheduled charge.
    if _charge_mode == "surplus" and _is_past_sunset():
        logger.info("past sunset -> morning_dump")
        _charge_mode = "morning_dump"
        _dump_was_active = False
        decision = compute_target(_charge_mode, pw, ev, settings)

    _record_sample(pw, ev, _charge_mode, decision)

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


def _chart_svg() -> str:
    """24-hour rolling chart: actual solar production vs clear-sky theoretical."""
    import time as _time
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(settings.timezone)
    now_dt = datetime.now(tz)
    now_ts = int(_time.time())
    # 24-hour window centered on now: 12 h past, 12 h future. The
    # theoretical curve extends into the future (it's just astronomy);
    # actual/SoC stop at now since we don't have future telemetry.
    start_ts = now_ts - 12 * 3600
    end_ts = now_ts + 12 * 3600

    samples = _sample_store().read_range(start_ts, now_ts)

    W, H = 900, 280
    # Right pad widened to fit the "%" axis labels.
    PAD_L, PAD_R, PAD_T, PAD_B = 50, 40, 12, 30
    plot_w = W - PAD_L - PAD_R
    plot_h = H - PAD_T - PAD_B

    if not samples:
        return (
            f'<svg viewBox="0 0 {W} {H}" class="chart" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f'<text x="{W//2}" y="{H//2}" text-anchor="middle" '
            f'fill="var(--muted)" font-size="14">no data yet — gathering…</text>'
            f'</svg>'
        )

    max_solar = max((s.solar_w or 0) for s in samples)
    # Use the rated array capacity as a y-axis floor so the theoretical
    # curve always fits even when DB samples are sparse.
    rated_w = settings.solar_array_max_kw * 1000.0
    y_max_kw = max(1, int(max(max_solar, rated_w) // 1000) + 1)

    def x_for(ts: int) -> float:
        return PAD_L + (ts - start_ts) / (end_ts - start_ts) * plot_w

    def y_for(w: float) -> float:
        return PAD_T + plot_h - (w / (y_max_kw * 1000)) * plot_h

    def y_for_pct(pct: float) -> float:
        return PAD_T + plot_h - (pct / 100.0) * plot_h

    parts: list[str] = []

    # Horizontal grid + LEFT Y labels (every 1 kW).
    for kw in range(y_max_kw + 1):
        y = y_for(kw * 1000)
        parts.append(
            f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W-PAD_R}" y2="{y:.1f}" '
            f'stroke="#8884" stroke-dasharray="2 2"/>'
            f'<text x="{PAD_L-6}" y="{y+4:.1f}" text-anchor="end" '
            f'font-size="10" fill="var(--muted)">{kw} kW</text>'
        )
    # RIGHT Y labels (SOC %) — every 25%.
    for pct in (0, 25, 50, 75, 100):
        y = y_for_pct(pct)
        parts.append(
            f'<text x="{W-PAD_R+6}" y="{y+4:.1f}" text-anchor="start" '
            f'font-size="10" fill="var(--muted)">{pct}%</text>'
        )

    # Vertical grid + X labels every 3 hours, aligned to clock. Spans the
    # full 24 h window (12 h past + 12 h future).
    aligned = now_dt.replace(minute=0, second=0, microsecond=0)
    while aligned.hour % 3 != 0:
        aligned -= timedelta(hours=1)
    cur = aligned - timedelta(hours=12)
    end_dt = now_dt + timedelta(hours=12)
    while cur <= end_dt:
        cur_ts = int(cur.timestamp())
        if start_ts <= cur_ts <= end_ts:
            x = x_for(cur_ts)
            parts.append(
                f'<line x1="{x:.1f}" y1="{PAD_T}" x2="{x:.1f}" y2="{H-PAD_B}" '
                f'stroke="#8884" stroke-dasharray="2 2"/>'
                f'<text x="{x:.1f}" y="{H-PAD_B+14}" text-anchor="middle" '
                f'font-size="10" fill="var(--muted)">{cur:%H:%M}</text>'
            )
        cur += timedelta(hours=3)
    # "Now" marker line in the middle of the chart.
    now_x = x_for(now_ts)
    parts.append(
        f'<line x1="{now_x:.1f}" y1="{PAD_T}" x2="{now_x:.1f}" y2="{H-PAD_B}" '
        f'stroke="currentColor" stroke-width="1" opacity="0.35"/>'
    )

    def series_polyline(getter, stroke: str) -> str:
        # Break the polyline at gaps (NULL readings) so we don't connect
        # across telemetry outages.
        segments: list[list[tuple[float, float]]] = []
        current: list[tuple[float, float]] = []
        for s in samples:
            v = getter(s)
            if v is None or v < 0:
                if current:
                    segments.append(current)
                    current = []
            else:
                current.append((x_for(s.ts), y_for(v)))
        if current:
            segments.append(current)
        return "".join(
            f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in seg)}" '
            f'fill="none" stroke="{stroke}" stroke-width="1.5"/>'
            for seg in segments if len(seg) >= 2
        )

    # Theoretical curve: compute live across the full 24 h window so it's
    # always visible regardless of how much real data we've accumulated.
    # 5-minute granularity → 288 points, plenty smooth, microseconds to draw.
    from datetime import timedelta as _td
    from zoneinfo import ZoneInfo as _Z

    tz_for_theo = _Z(settings.timezone)
    step = 5 * 60
    theo_pts: list[tuple[float, float]] = []
    t = start_ts
    while t <= end_ts:
        w = theoretical_w(datetime.fromtimestamp(t, tz_for_theo), settings)
        if w > 0:
            theo_pts.append((x_for(t), y_for(w)))
        elif theo_pts:
            theo_pts.append((x_for(t), y_for(0.0)))
        t += step
    if len(theo_pts) >= 2:
        pts_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in theo_pts)
        parts.append(
            f'<polyline points="{pts_str}" fill="none" '
            f'stroke="#d04545" stroke-width="1.5"/>'
        )

    # Actual solar: orange to match the solar node in the diagram above.
    parts.append(series_polyline(lambda s: s.solar_w, "#e8a33d"))

    # SOC: scaled to the right-side 0–100% axis, green to match battery node.
    soc_segments: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    for s in samples:
        if s.soc_pct is None:
            if cur:
                soc_segments.append(cur)
                cur = []
        else:
            cur.append((x_for(s.ts), y_for_pct(s.soc_pct)))
    if cur:
        soc_segments.append(cur)
    for seg in soc_segments:
        if len(seg) < 2:
            continue
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in seg)
        parts.append(
            f'<polyline points="{pts}" fill="none" '
            f'stroke="#2ea56a" stroke-width="1.5"/>'
        )

    parts.append(
        f'<text x="{PAD_L+8}" y="{PAD_T+14}" font-size="11">'
        '<tspan fill="#d04545">━━ </tspan>'
        '<tspan fill="currentColor">theoretical</tspan>'
        '<tspan dx="14" fill="#e8a33d">━━ </tspan>'
        '<tspan fill="currentColor">actual</tspan>'
        '<tspan dx="14" fill="#2ea56a">━━ </tspan>'
        '<tspan fill="currentColor">SoC</tspan>'
        '</text>'
    )

    return (
        f'<svg viewBox="0 0 {W} {H}" class="chart" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="Solar production over the last 24 hours">'
        + "".join(parts)
        + '</svg>'
    )


def _record_sample(
    pw: PowerReading | None,
    ev: ChargerState | None,
    mode: str,
    decision: Decision,
) -> None:
    """Persist a telemetry sample for the chart and analytics queries."""
    import time as _time
    from zoneinfo import ZoneInfo

    now_local = datetime.now(ZoneInfo(settings.timezone))
    try:
        theoretical = theoretical_w(now_local, settings)
    except Exception:
        theoretical = None
    soc = pw.battery_soc_pct if pw else None
    if soc is not None and math.isnan(soc):
        soc = None
    sample = Sample(
        ts=int(_time.time()),
        solar_w=pw.solar_w if pw else None,
        load_w=pw.load_w if pw else None,
        battery_w=pw.battery_w if pw else None,
        grid_w=pw.grid_w if pw else None,
        soc_pct=soc,
        theoretical_w=theoretical,
        charger_amps=ev.charge_rate_a if ev else None,
        charger_on=ev.on if ev else None,
        pw_ok=pw is not None,
        em_ok=ev is not None,
        mode=mode,
        decision_amps=decision.target_amps,
        decision_on=decision.on,
        decision_reason=decision.reason,
    )
    try:
        _sample_store().insert(sample)
    except Exception:
        logger.warning("sample insert failed", exc_info=False)


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
  svg.chart {{ width: 100%; height: auto; max-width: 900px; display: block; margin: .25em auto 1em; }}
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

<h2>Solar — 24 h window (now centered)</h2>
{_chart_svg()}

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
    global _charge_mode, _dump_was_active
    if mode in _CHARGE_MODES:
        _charge_mode = mode
        _dump_was_active = False
        logger.info("charge mode -> {}", mode)
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

"""Minimal browser UI for manual charger control and live state display.

Binds to localhost by default. There is no auth — do not expose this to the
open LAN without putting a reverse proxy / password in front of it.
"""

from __future__ import annotations

import html
import math
from datetime import datetime
from typing import Annotated

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

from .config import settings
from .emporia import ChargerState, Emporia
from .flow import Flows, decompose
from .policy import Decision, decide_ev_amps
from .powerwall import Powerwall, PowerReading

app = FastAPI(title="elec_auto", docs_url=None, redoc_url=None)

_pw: Powerwall | None = None
_em: Emporia | None = None


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

# Viewbox extends right of Home node to fit the top-consumers list.
_VIEWBOX_W = 820
_VIEWBOX_H = 420
_CONSUMERS_X = 650  # left edge of the consumer list column

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
        if ev is None or not ev.on:
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
            return "idle"
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


def _consumer_rows(consumers: list[tuple[str, float]] | None) -> str:
    header = (
        f'<text x="{_CONSUMERS_X}" y="190" class="consumers-head" '
        f'fill="var(--muted)">Top loads</text>'
    )
    if not consumers:
        note = "—" if consumers is None else "all idle"
        return header + (
            f'<text x="{_CONSUMERS_X}" y="215" class="consumer-row" '
            f'fill="var(--muted)">{note}</text>'
        )
    rows = [header]
    for i, (name, watts) in enumerate(consumers):
        y = 215 + i * 22
        rows.append(
            f'<text x="{_CONSUMERS_X}" y="{y}" class="consumer-row">'
            f'{html.escape(name)}'
            f'<tspan class="consumer-watts" dx="8">{watts:.0f} W</tspan></text>'
        )
    return "".join(rows)


def _flow_svg(
    pw: PowerReading | None,
    consumers: list[tuple[str, float]] | None = None,
    ev: ChargerState | None = None,
) -> str:
    flows = decompose(pw) if pw is not None else None

    # Arrowhead markers, one per source color, since we color arrows by origin.
    markers = "".join(
        f'<marker id="arrow-{name}" viewBox="0 0 10 10" refX="9" refY="5" '
        f'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        f'<path d="M0,0 L10,5 L0,10 z" fill="{n["color"]}"/></marker>'
        for name, n in _NODES.items()
    )

    edges = []
    for src, dst, (sx, sy), (ex, ey), (lx, ly) in _EDGES:
        w = _edge_watts(src, dst, flows, ev)
        active = w >= _FLOW_VISIBLE_W
        color = _NODES[src]["color"]
        opacity = 1.0 if active else 0.12
        cx, cy = _clip_to_node(sx, sy, ex, ey, _NODES[dst])
        edges.append(
            f'<line x1="{sx}" y1="{sy}" x2="{cx:.1f}" y2="{cy:.1f}" '
            f'stroke="{color}" stroke-width="{_STROKE_W}" '
            f'stroke-linecap="round" opacity="{opacity:.2f}" '
            f'marker-end="url(#arrow-{src})"/>'
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
        nodes.append(
            f'<g><rect x="{n["x"]}" y="{n["y"]}" width="{n["w"]}" height="{n["h"]}" '
            f'rx="10" fill="var(--node-bg)" stroke="{n["color"]}" stroke-width="2"/>'
            f'<text x="{cx}" y="{title_y}" text-anchor="middle" class="node-title">'
            f'{name.capitalize()}</text>'
            f'<text x="{cx}" y="{value_y}" text-anchor="middle" class="node-value">'
            f'{_node_value_label(name, pw, ev)}</text></g>'
        )

    return (
        f'<svg viewBox="0 0 {_VIEWBOX_W} {_VIEWBOX_H}" class="flow" '
        'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Power flow diagram">'
        f'<defs>{markers}</defs>'
        + "".join(edges)
        + "".join(nodes)
        + _consumer_rows(consumers)
        + "</svg>"
    )


def _render(flash: str = "", flash_ok: bool = True) -> str:
    pw, pw_err = _safe_pw()
    ev, ev_err = _safe_em()
    consumers = _safe_top_consumers()
    decision: Decision | None = None
    if pw is not None and ev is not None:
        try:
            decision = decide_ev_amps(pw, ev, settings)
        except Exception as e:
            logger.exception("policy decide failed")
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
        f"<p class=decision><b>Policy would set:</b> {decision.target_amps} A <small>({html.escape(decision.reason)})</small></p>"
        if decision is not None
        else ""
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
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 860px; margin: 2em auto; padding: 0 1em; --node-bg: #fff; }}
  @media (prefers-color-scheme: dark) {{ body {{ --node-bg: #1a1a1a; }} }}
  svg.flow {{ width: 100%; height: auto; max-width: 820px; display: block; margin: .5em auto 1.5em; }}
  svg.flow .node-title {{ font-size: 14px; font-weight: 600; fill: currentColor; }}
  svg.flow .node-value {{ font-size: 12px; fill: var(--muted); }}
  svg.flow .flow-label {{ font-size: 12px; font-weight: 600; font-variant-numeric: tabular-nums; paint-order: stroke; stroke: var(--node-bg); stroke-width: 3px; }}
  svg.flow .consumers-head {{ font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .08em; }}
  svg.flow .consumer-row {{ font-size: 13px; fill: currentColor; }}
  svg.flow .consumer-watts {{ fill: var(--muted); font-variant-numeric: tabular-nums; }}
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
<h1>elec_auto</h1>
<p class="sub">{now} &middot; auto-refresh 15s</p>
{flash_html}

{_flow_svg(pw, consumers, ev)}

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
def index() -> str:
    return _render()


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

"""Minimal browser UI for manual charger control and live state display.

Binds to localhost by default. There is no auth — do not expose this to the
open LAN without putting a reverse proxy / password in front of it.
"""

from __future__ import annotations

import asyncio
import html
import math
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

from .config import settings
from .control import Controller
from .emporia import ChargerState, Emporia
from .flow import Flows, decompose
from .timewindow import next_dump_window
from .forecast import load_forecast as _load_forecast
from .forecast import non_ev_load_kwh_in_window as _non_ev_load_kwh
from .forecast import soc_forecast as _soc_forecast
from .nws import NWS
from .state import em_panel_sum
from .policy import Decision
from .powerwall import Powerwall, PowerReading, PowerwallUnavailable
from .samples import (
    Forecast, ForecastStore, LoadStore, ObservationStore, Sample, SampleStore,
    WeatherStore,
)
from .solar import theoretical_w
from .solcast import Solcast, daily_schedule


@asynccontextmanager
async def _lifespan(app_: FastAPI):
    loop = asyncio.get_running_loop()
    # Seed the dashboard cache from the most recent DB sample so the very
    # first page load has real (if stale) numbers instead of "no data
    # yet". The control loop will overwrite within poll_interval_sec.
    _warm_cache_from_db()
    tasks = [loop.create_task(_control_loop())]
    logger.info("control loop started (interval={}s)", settings.poll_interval_sec)
    if settings.solcast_api_key and settings.solcast_resource_id:
        tasks.append(loop.create_task(_forecast_loop()))
        logger.info("solcast forecast loop started")
    if settings.latitude is not None and settings.longitude is not None:
        tasks.append(loop.create_task(_weather_loop()))
        logger.info("nws weather loop started")
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass


app = FastAPI(title="elec_auto", docs_url=None, redoc_url=None, lifespan=_lifespan)

_pw: Powerwall | None = None
_em: Emporia | None = None
# Last good readings, populated by _control_tick and read by _render so
# the dashboard never blocks on a network call. Decoupling render from
# fetch is what makes the page feel instant on mobile — Emporia's cloud
# in particular can take 5–10 s on a slow LTE link. The control loop
# already runs every poll_interval_sec, so cache age is bounded by that.
_last_pw_reading: PowerReading | None = None
_last_pw_ts: float = 0.0
_last_ev_state: ChargerState | None = None
_last_ev_ts: float = 0.0
_samples: SampleStore | None = None
_forecasts: ForecastStore | None = None
_loads: LoadStore | None = None
_weather: WeatherStore | None = None
_observations: ObservationStore | None = None
_controller: Controller | None = None


def _ctl() -> Controller:
    """The module-level Controller singleton. Built on first access."""
    global _controller
    if _controller is None:
        _controller = Controller(settings)
    return _controller


def _db_path():
    from pathlib import Path
    return Path("state") / "samples.db"


def _sample_store() -> SampleStore:
    global _samples
    if _samples is None:
        _samples = SampleStore(_db_path())
    return _samples


def _forecast_store() -> ForecastStore:
    global _forecasts
    if _forecasts is None:
        _forecasts = ForecastStore(_db_path())
    return _forecasts


def _load_store() -> LoadStore:
    global _loads
    if _loads is None:
        _loads = LoadStore(_db_path())
    return _loads


def _weather_store() -> WeatherStore:
    global _weather
    if _weather is None:
        _weather = WeatherStore(_db_path())
    return _weather


def _observation_store() -> ObservationStore:
    global _observations
    if _observations is None:
        _observations = ObservationStore(_db_path())
    return _observations

def _mode_label(ctl: Controller) -> str:
    """Human-readable summary of the controller's current state.

    Returned to the dashboard heading. Maps the action_name on the last
    Decision (or the kill-switch state) onto a friendly label.
    """
    if ctl.kill_switch:
        return "Disabled"
    d = ctl.last_decision
    if d is None:
        return "Starting…"
    if d.action_name == "surplus":
        return "Surplus solar"
    if d.action_name == "solar_passthrough":
        return "Solar → EV"
    if d.action_name == "morning_dump":
        return "Morning dump"
    if d.action_name == "kill_switch":
        return "Disabled"
    return "Idle"


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


# Module-level counter for PW3 read-failure log throttling. The gateway
# client supplies the rate limit (its own exponential back-off, capped at
# 5 min between real attempts), so we just need to skip the per-tick
# in-back-off raises and log each real attempt that fails. Net effect:
# a sustained outage produces one traceback at the start and one short
# heartbeat per cap-period (~5 min) thereafter, plus a recovery line.
_pw_failure_streak = 0


def _safe_pw() -> tuple[PowerReading | None, str | None]:
    global _pw_failure_streak
    try:
        reading = _powerwall().read()
    except PowerwallUnavailable as e:
        # Inside the gateway's back-off window — not a fresh attempt,
        # nothing new to say. Stay silent so the log stays readable.
        return None, str(e)
    except Exception as e:
        _pw_failure_streak += 1
        if _pw_failure_streak == 1:
            # First failure: full traceback so we can see what broke.
            logger.exception("powerwall read failed (entering back-off)")
        else:
            # Subsequent real attempts (gated by the gateway back-off,
            # so at most one per ~5 min). One-liner — the originating
            # traceback is already in the log a few minutes up.
            logger.warning(
                "powerwall still failing ({} consecutive): {}",
                _pw_failure_streak, e,
            )
        return None, str(e)
    if _pw_failure_streak > 0:
        logger.info(
            "powerwall recovered after {} failed read(s)", _pw_failure_streak,
        )
        _pw_failure_streak = 0
    return reading, None


def _safe_em() -> tuple[ChargerState | None, str | None]:
    try:
        return _emporia().read(), None
    except Exception as e:
        logger.exception("emporia read failed")
        return None, str(e)


def _warm_cache_from_db() -> None:
    """Seed `_last_pw_reading` / `_last_ev_state` from the newest sample row.

    Bridges the gap between server start and the first network tick (10–15 s).
    The dashboard renders this immediately; the staleness banner uses the
    sample's real timestamp so it's obvious the data is from before the
    restart, not live. Best-effort — any failure is swallowed because
    "no warm data" is a survivable fallback.
    """
    global _last_pw_reading, _last_pw_ts, _last_ev_state, _last_ev_ts
    try:
        # The most recent ~60 s of samples — enough to find one non-empty
        # row even if the latest tick was missing telemetry.
        import time as _time
        now = int(_time.time())
        rows = _sample_store().read_range(now - 300, now)
    except Exception:
        logger.exception("warm cache from db: query failed")
        return
    if not rows:
        return
    latest = max(rows, key=lambda r: r.ts)

    # PowerReading: needs all four watts + SoC to be useful. Missing
    # values are fine to substitute 0 — the staleness banner will flag
    # this isn't live anyway.
    if any(v is not None for v in
           (latest.solar_w, latest.load_w, latest.battery_w, latest.grid_w)):
        _last_pw_reading = PowerReading(
            solar_w=latest.solar_w or 0.0,
            load_w=latest.load_w or 0.0,
            battery_w=latest.battery_w or 0.0,
            grid_w=latest.grid_w or 0.0,
            battery_soc_pct=latest.soc_pct if latest.soc_pct is not None else float("nan"),
        )
        _last_pw_ts = float(latest.ts)

    # ChargerState: gid / name / max_charge_rate_a aren't persisted in
    # Sample, so we substitute neutral placeholders. The real values
    # land the moment _control_tick fetches Emporia for the first time.
    if latest.charger_amps is not None or latest.charger_on is not None:
        _last_ev_state = ChargerState(
            gid=0,
            name="EV Charger",
            on=bool(latest.charger_on) if latest.charger_on is not None else False,
            charge_rate_a=latest.charger_amps or 0,
            max_charge_rate_a=settings.ev_max_amps,
            status=latest.charger_status or "",
        )
        _last_ev_ts = float(latest.ts)


def _staleness_msg(last_ts: float, source: str) -> str | None:
    """Build the dashboard's "stale Ns ago" hint, or None if fresh.

    Threshold is 3× the poll interval — enough headroom that one missed
    tick doesn't flag, but a real outage does.
    """
    if last_ts == 0.0:
        return f"{source} unavailable (no data yet)"
    age = time.time() - last_ts
    if age > settings.poll_interval_sec * 3:
        return f"{source} stale ({age:.0f}s ago)"
    return None


def _cached_pw() -> tuple[PowerReading | None, str | None]:
    """Last good PW3 reading + a staleness hint. Never touches the network."""
    return _last_pw_reading, _staleness_msg(_last_pw_ts, "Powerwall")


def _cached_ev() -> tuple[ChargerState | None, str | None]:
    """Last good Emporia reading + a staleness hint. Never touches the network."""
    return _last_ev_state, _staleness_msg(_last_ev_ts, "Emporia")


# Channels we never want to show in "Top loads": the synthetic Main /
# Balance / sub-panel roll-ups (double-counting) and the EV Charger
# (which already has its own dashboard node).
_TOP_CONSUMERS_EXCLUDE = {
    "Main", "Balance", "Garage Subpanel", "EV Charger", "EV", "Car", "Tesla",
}


def _safe_top_consumers(n: int = 3) -> list[tuple[str, float]] | None:
    """Top-N non-aggregate circuits at the most recent recorded tick.

    Reads from the `loads` table the control loop populates each tick —
    avoids hitting Emporia synchronously from the request handler, which
    can be slow when their cloud is sluggish.
    """
    try:
        import time as _time
        now = int(_time.time())
        recent = _load_store().read_range(now - 120, now)
        if not recent:
            return None
        latest_ts = max(r.ts for r in recent)
        rows = [
            (r.circuit, r.watts)
            for r in recent
            if r.ts == latest_ts
            and r.circuit not in _TOP_CONSUMERS_EXCLUDE
            and r.watts > 0
        ]
        rows.sort(key=lambda x: x[1], reverse=True)
        return rows[:n]
    except Exception:
        logger.exception("top_consumers from loads failed")
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

    return next_dump_window(datetime.now(ZoneInfo(s.timezone)), s)[0]


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
        action = decision.action_name or "—"
        if decision.on:
            em.set_amps(decision.target_amps, on=True)
            logger.info("apply: action={} set {} A on ({})",
                        action, decision.target_amps, decision.reason)
        elif decision.target_amps >= settings.ev_min_amps:
            em.set_amps(decision.target_amps, on=False)
            logger.info("apply: action={} set {} A off ({})",
                        action, decision.target_amps, decision.reason)
        else:
            em.set_on(False)
            logger.info("apply: action={} off ({})", action, decision.reason)
    except Exception:
        logger.exception("apply: push to EVSE failed")


def _control_tick() -> None:
    """One tick of the control loop.

    The Controller owns state and action selection. We:
      1. Read raw telemetry (best-effort; None on failure).
      2. Hand it to Controller.tick(), which updates state and returns
         the Decision for this tick.
      3. Persist a Sample row, EV-circuit row, and apply the Decision
         to the EVSE unless we're missing the EV side.

    No mode globals, no string-sniffing auto-flips — actions partition
    by predicate inside the Controller.
    """
    from zoneinfo import ZoneInfo

    global _last_pw_reading, _last_pw_ts, _last_ev_state, _last_ev_ts

    pw, _ = _safe_pw()
    ev, _ = _safe_em()
    # Stash the latest good readings for the dashboard. Hold the prior
    # value on failure so the page still has something to render during
    # transient outages — _render uses the timestamp to flag staleness.
    if pw is not None:
        _last_pw_reading = pw
        _last_pw_ts = time.time()
    if ev is not None:
        _last_ev_state = ev
        _last_ev_ts = time.time()

    now_ts = int(time.time())
    pv_forecasts = _forecast_store().operational_in_range(
        now_ts, now_ts + 24 * 3600,
    )
    em_load_w, ev_circuit_w = _em_loads(ev.name if ev else "EV Charger")
    tz = ZoneInfo(settings.timezone)
    ctl = _ctl()
    decision = ctl.tick(
        datetime.now(tz),
        pw=pw, em_load_w=em_load_w, ev=ev,
        ev_circuit_w=ev_circuit_w,
        pv_forecasts=pv_forecasts,
        sample_store=_sample_store(),
        load_store=_load_store(),
        ev_circuit_name=(ev.name if ev else "EV Charger"),
    )

    _record_sample_from_state(ctl, decision)
    _record_loads(int(time.time()))

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


def _em_loads(ev_circuit_name: str) -> tuple[float | None, float | None]:
    """Pull one Emporia circuit snapshot and split it into:

      - `em_load_w`   : whole-house load via `em_panel_sum` (toplines)
      - `ev_circuit_w`: measured EV draw from the EVSE's own Vue device

    Threshold is forced to 0 W so the EV channel is never dropped when
    the car is in Standby (Surplus.decide needs the real value, not a
    None that falls back to the phantom `ev_amps × voltage` proxy).
    """
    try:
        circuits = _emporia().all_circuit_loads(min_threshold_w=0.0)
    except Exception:
        return None, None
    return em_panel_sum(circuits), circuits.get(ev_circuit_name)


# Channels we treat as roll-ups, not individual circuits. The per-circuit
# chart skips these because they double-count (Main = sum of everything;
# Garage Subpanel = sum of its branches) and they'd otherwise dominate
# the y-axis.
_AGGREGATE_CIRCUITS = {"Main", "Garage Subpanel", "Balance"}

# Color palette for circuit polylines. Assigned in sorted-name order so
# the same circuit gets the same color across page reloads. Sourced from
# matplotlib's tab20 + a couple of extras for 17 distinct colors.
_CIRCUIT_PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
    "#aec7e8", "#ffbb78", "#98df8a", "#ff9896", "#c5b0d5",
    "#c49c94", "#f7b6d2",
]


def _nice_y_axis(max_value: float, target_ticks: int = 8) -> tuple[float, float]:
    """Pick a 'nice' step (1, 2, 2.5, 5 × 10^n) and an axis max above `max_value`.

    Returns (step, axis_max) in the same units as `max_value`. Standard chart
    axis algorithm — yields steps like 100, 200, 250, 500, 1000, 2500, ...
    """
    if max_value <= 0:
        return 100.0, 100.0
    rough = max_value / target_ticks
    magnitude = 10 ** math.floor(math.log10(rough))
    norm = rough / magnitude
    if norm < 1.5:
        nice = 1.0
    elif norm < 2.25:
        nice = 2.0
    elif norm < 3.75:
        nice = 2.5
    elif norm < 7.5:
        nice = 5.0
    else:
        nice = 10.0
    step = nice * magnitude
    axis_max = math.ceil(max_value / step) * step
    return step, axis_max


def _fmt_y_label(w: float, step_w: float) -> str:
    """Numeric Y-axis tick label (unit goes in the axis title)."""
    if step_w >= 1000:
        return f"{w/1000:g}"
    return f"{int(round(w))}"


def _pack_legend_rows(
    names: list[str], available_px: float,
    char_px: float = 6.0, dash_px: float = 20.0, gap_px: float = 10.0,
) -> list[list[str]]:
    """Greedy-pack circuit names into rows that fit `available_px` wide.

    Width estimates are rough — SVG text width depends on the font and
    glyph mix, but ~6px per char at 11pt matches typical UI fonts well
    enough for this layout.
    """
    rows: list[list[str]] = []
    current: list[str] = []
    width = 0.0
    for name in names:
        item_w = dash_px + len(name) * char_px + (gap_px if current else 0)
        if current and width + item_w > available_px:
            rows.append(current)
            current = []
            width = 0
            item_w = dash_px + len(name) * char_px
        current.append(name)
        width += item_w
    if current:
        rows.append(current)
    return rows


def _circuits_section() -> str:
    """SVG chart + legend for per-circuit usage. Same 24 h window centered on
    "now" as the system chart, so the two line up visually."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    now_ts = int(time.time())
    start_ts = now_ts - 12 * 3600
    end_ts = now_ts + 12 * 3600

    DAY_SEC = 24 * 3600
    rows = [
        r for r in _load_store().read_range(start_ts, now_ts)
        if r.circuit not in _AGGREGATE_CIRCUITS
    ]
    # Dumb forecast: yesterday's data for the same wall-clock hours we'd be
    # showing in the future half, shifted +24 h so it lands there.
    forecast_rows = [
        r for r in _load_store().read_range(now_ts - DAY_SEC, end_ts - DAY_SEC)
        if r.circuit not in _AGGREGATE_CIRCUITS
    ]

    W, H = 900, 240
    PAD_L, PAD_R, PAD_B = 50, 12, 30
    plot_w = W - PAD_L - PAD_R

    if not rows and not forecast_rows:
        PAD_T = 26
        plot_h = H - PAD_T - PAD_B
        return (
            f'<svg viewBox="0 0 {W} {H}" class="chart" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f'<text x="{W//2}" y="{H//2}" text-anchor="middle" '
            f'fill="var(--muted)" font-size="14">'
            f'no circuit data yet — gathering…</text></svg>'
        )

    by_circuit: dict[str, list[tuple[int, float]]] = {}
    for r in rows:
        by_circuit.setdefault(r.circuit, []).append((r.ts, r.watts))
    by_circuit_fc: dict[str, list[tuple[int, float]]] = {}
    for r in forecast_rows:
        by_circuit_fc.setdefault(r.circuit, []).append((r.ts + DAY_SEC, r.watts))

    all_circuits = set(by_circuit) | set(by_circuit_fc)
    max_w = max(
        max((w for pts in by_circuit.values() for _, w in pts), default=0.0),
        max((w for pts in by_circuit_fc.values() for _, w in pts), default=0.0),
    )
    step_w, y_max_w = _nice_y_axis(max_w, target_ticks=8)

    name_color = {
        name: _CIRCUIT_PALETTE[i % len(_CIRCUIT_PALETTE)]
        for i, name in enumerate(sorted(all_circuits))
    }

    # Pack legend into rows that fit the chart width. Sorted by total energy
    # (today + yesterday-forecast) so the heaviest circuits appear first.
    energy = {
        n: sum(w for _, w in by_circuit.get(n, []))
        + sum(w for _, w in by_circuit_fc.get(n, []))
        for n in all_circuits
    }
    legend_order = sorted(all_circuits, key=lambda n: energy[n], reverse=True)
    legend_rows = _pack_legend_rows(legend_order, available_px=plot_w - 8)

    LEGEND_LINE_PX = 14
    AXIS_TITLE_ROW_PX = 16
    PAD_T = AXIS_TITLE_ROW_PX + len(legend_rows) * LEGEND_LINE_PX + 6
    plot_h = H - PAD_T - PAD_B

    def x_for(ts: int) -> float:
        return PAD_L + (ts - start_ts) / (end_ts - start_ts) * plot_w

    def y_for(w: float) -> float:
        return PAD_T + plot_h - (w / y_max_w) * plot_h

    parts: list[str] = []

    # Axis title (unit only) at the top.
    unit_label = "kW" if step_w >= 1000 else "W"
    parts.append(
        f'<text x="{PAD_L-6}" y="10" text-anchor="end" '
        f'font-size="10" fill="var(--muted)">{unit_label}</text>'
    )
    # In-plot legend rows, one <text> per row.
    for row_idx, row in enumerate(legend_rows):
        y = AXIS_TITLE_ROW_PX + 4 + row_idx * LEGEND_LINE_PX
        tspans = []
        for i, name in enumerate(row):
            spacer = '<tspan dx="10"> </tspan>' if i > 0 else ""
            tspans.append(
                f'{spacer}<tspan fill="{name_color[name]}">━━ </tspan>'
                f'<tspan fill="currentColor">{html.escape(name)}</tspan>'
            )
        parts.append(
            f'<text x="{PAD_L+4}" y="{y}" font-size="11">'
            + "".join(tspans) + '</text>'
        )
    # Y gridlines + labels using the nice step.
    n_ticks = int(y_max_w / step_w)
    for i in range(n_ticks + 1):
        w = i * step_w
        y = y_for(w)
        parts.append(
            f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W-PAD_R}" y2="{y:.1f}" '
            f'stroke="#8884" stroke-dasharray="2 2"/>'
            f'<text x="{PAD_L-6}" y="{y+4:.1f}" text-anchor="end" '
            f'font-size="10" fill="var(--muted)">{_fmt_y_label(w, step_w)}</text>'
        )

    # X grid + labels every 3 hours, full 24 h centered on now.
    tz = ZoneInfo(settings.timezone)
    now_dt = datetime.fromtimestamp(now_ts, tz)
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
    # "Now" marker matching the system chart.
    now_x = x_for(now_ts)
    parts.append(
        f'<line x1="{now_x:.1f}" y1="{PAD_T}" x2="{now_x:.1f}" y2="{H-PAD_B}" '
        f'stroke="currentColor" stroke-width="1" opacity="0.35"/>'
    )

    # One polyline per circuit, broken at telemetry gaps (>90 s between
    # samples). Today's data is drawn solid in the past half; yesterday's
    # data is drawn dashed/faded in the future half as a "dumb forecast".
    # Each segment is bracketed with a (first_x, 0) and (last_x, 0) point
    # so it renders as a bounded "spike" with visible vertical edges at
    # both ends, rather than a line that hangs in mid-air.
    y0 = y_for(0)
    def draw(data: dict[str, list[tuple[int, float]]], extra_attrs: str) -> None:
        for circuit in sorted(data):
            pts = sorted(data[circuit])
            color = name_color[circuit]
            segments: list[list[tuple[float, float]]] = []
            cur_seg: list[tuple[float, float]] = []
            prev_ts = None
            for ts, w in pts:
                if prev_ts is not None and ts - prev_ts > 90:
                    if len(cur_seg) >= 2:
                        segments.append(cur_seg)
                    cur_seg = []
                cur_seg.append((x_for(ts), y_for(w)))
                prev_ts = ts
            if cur_seg:
                segments.append(cur_seg)
            for seg in segments:
                if len(seg) < 2:
                    continue
                edged = [(seg[0][0], y0)] + seg + [(seg[-1][0], y0)]
                pts_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in edged)
                parts.append(
                    f'<polyline points="{pts_str}" fill="none" stroke="{color}" '
                    f'stroke-width="1.5"{extra_attrs}/>'
                )

    draw(by_circuit, "")
    draw(by_circuit_fc, ' stroke-dasharray="4 3" opacity="0.45"')

    return (
        f'<svg viewBox="0 0 {W} {H}" class="chart" '
        f'xmlns="http://www.w3.org/2000/svg">'
        + "".join(parts) + '</svg>'
    )


def _chart_heading() -> str:
    """`<h2>` for the system chart, appending the current qualitative weather."""
    try:
        label = _forecast_store().current_qualitative()
    except Exception:
        label = None
    if label:
        return f"<h2>System ({html.escape(label)})</h2>"
    return "<h2>System</h2>"


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
    # Top pad widened to give the axis-title row room above the gridlines.
    PAD_L, PAD_R, PAD_T, PAD_B = 50, 40, 26, 30
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
    max_load = max((s.load_w or 0) for s in samples)
    # Use the rated array capacity as a y-axis floor so the theoretical
    # curve always fits even when DB samples are sparse. Also include
    # home load — a Tesla pulling 9.6 kW from grid can outstrip rated PV.
    rated_w = settings.solar_array_max_kw * 1000.0
    y_max_kw = max(1, int(max(max_solar, max_load, rated_w) // 1000) + 1)

    def x_for(ts: int) -> float:
        return PAD_L + (ts - start_ts) / (end_ts - start_ts) * plot_w

    def y_for(w: float) -> float:
        return PAD_T + plot_h - (w / (y_max_kw * 1000)) * plot_h

    def y_for_pct(pct: float) -> float:
        return PAD_T + plot_h - (pct / 100.0) * plot_h

    parts: list[str] = []

    # Axis titles (units), placed above the top gridline.
    parts.append(
        f'<text x="{PAD_L-6}" y="{PAD_T-8}" text-anchor="end" '
        f'font-size="10" fill="var(--muted)">kW</text>'
        f'<text x="{W-PAD_R+6}" y="{PAD_T-8}" text-anchor="start" '
        f'font-size="10" fill="var(--muted)">%</text>'
    )
    # Horizontal grid + LEFT (kW) + RIGHT (SoC %) labels.
    # Both axes share the same gridlines: 0..y_max_kw kW maps linearly to
    # 0..100% SoC, so the right-axis label at each gridline is just the
    # gridline's fraction of full scale.
    for kw in range(y_max_kw + 1):
        y = y_for(kw * 1000)
        pct = (kw / y_max_kw) * 100.0
        parts.append(
            f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W-PAD_R}" y2="{y:.1f}" '
            f'stroke="#8884" stroke-dasharray="2 2"/>'
            f'<text x="{PAD_L-6}" y="{y+4:.1f}" text-anchor="end" '
            f'font-size="10" fill="var(--muted)">{kw}</text>'
            f'<text x="{W-PAD_R+6}" y="{y+4:.1f}" text-anchor="start" '
            f'font-size="10" fill="var(--muted)">{pct:.0f}</text>'
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

    def series_polyline(
        getter, stroke: str, smooth: int = 1, dasharray: str | None = None,
    ) -> str:
        # Break the polyline at gaps (NULL readings) so we don't connect
        # across telemetry outages. With smooth>1, apply a centered moving
        # average within each segment before mapping to chart coords.
        segments: list[list[tuple[int, float]]] = []
        current: list[tuple[int, float]] = []
        for s in samples:
            v = getter(s)
            if v is None or v < 0:
                if current:
                    segments.append(current)
                    current = []
            else:
                current.append((s.ts, v))
        if current:
            segments.append(current)

        if smooth > 1:
            half = smooth // 2
            smoothed: list[list[tuple[int, float]]] = []
            for seg in segments:
                sm: list[tuple[int, float]] = []
                for i in range(len(seg)):
                    # Only smooth where a full centered window fits.
                    # Endpoints stay raw so the most recent sample isn't
                    # dragged toward stale values by an asymmetric window.
                    if i < half or i > len(seg) - 1 - half:
                        sm.append(seg[i])
                    else:
                        window = seg[i - half : i + half + 1]
                        sm.append((seg[i][0], sum(v for _, v in window) / len(window)))
                smoothed.append(sm)
            segments = smoothed

        dash_attr = f' stroke-dasharray="{dasharray}"' if dasharray else ""
        return "".join(
            f'<polyline points="{" ".join(f"{x_for(ts):.1f},{y_for(v):.1f}" for ts, v in seg)}" '
            f'fill="none" stroke="{stroke}" stroke-width="1.5"{dash_attr}/>'
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

    # Home load: grey to match the home node in the diagram above.
    parts.append(series_polyline(lambda s: s.load_w, "#888888", smooth=3))
    # Actual solar: orange to match the solar node in the diagram above.
    # 3-point centered moving average smooths the 30 s sample jitter.
    parts.append(series_polyline(lambda s: s.solar_w, "#e8a33d", smooth=3))
    # Grid: one trace on the positive axis (|grid_w|). Export = solid,
    # import = dashed; segments break naturally at the sign change.
    parts.append(series_polyline(
        lambda s: (-s.grid_w if s.grid_w is not None and s.grid_w < 0 else None),
        "#9b6dc7", smooth=3,
    ))
    parts.append(series_polyline(
        lambda s: (s.grid_w if s.grid_w is not None and s.grid_w > 0 else None),
        "#9b6dc7", smooth=3, dasharray="4 3",
    ))
    # Solcast forecast: dashed teal so it reads as "predicted" rather than
    # measured. Spans the full window (past forecasts that we kept + future
    # predictions for the right half).
    store = _forecast_store()
    # Use the *operational* view: at each period, the forecast that was
    # most recent as of that period's own timestamp. For future periods
    # this is the latest fetch; for past periods it's the version that was
    # active at the time — without the benefit of later refinements.
    forecasts = store.operational_in_range(start_ts, end_ts)
    if len(forecasts) >= 2:
        pts = " ".join(
            f"{x_for(f.period_ts):.1f},{y_for(f.pv_w_p50):.1f}"
            for f in forecasts if f.pv_w_p50 is not None
        )
        if pts:
            parts.append(
                f'<polyline points="{pts}" fill="none" stroke="#3aa5c7" '
                f'stroke-width="1.5" stroke-dasharray="4 3"/>'
            )
    # Markers at each refresh event so we can eyeball how often the forecast
    # actually updated. Y comes from the forecast itself at fetch time.
    for fetched_at, pv in store.fetch_events(start_ts, end_ts):
        if pv is None:
            continue
        parts.append(
            f'<circle cx="{x_for(fetched_at):.1f}" cy="{y_for(pv):.1f}" '
            f'r="3" fill="#3aa5c7"/>'
        )

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

    # Heuristic forecasts for the future half: dashed load (grey) and SoC
    # (green) extensions matching the colors of the solid past-half traces.
    load_fc = _load_forecast(_sample_store(), now_ts, end_ts)
    last_soc = next(
        (s.soc_pct for s in reversed(samples) if s.soc_pct is not None), None,
    )
    soc_fc = _soc_forecast(
        now_ts=now_ts, end_ts=end_ts, current_soc_pct=last_soc,
        pv_forecasts=forecasts, load_forecasts=load_fc,
        settings=settings,
    )

    def _dashed(pts: list[tuple[float, float]], stroke: str) -> str:
        if len(pts) < 2:
            return ""
        s = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        return (
            f'<polyline points="{s}" fill="none" stroke="{stroke}" '
            f'stroke-width="1.5" stroke-dasharray="4 3" opacity="0.65"/>'
        )

    parts.append(_dashed(
        [(x_for(lf.ts), y_for(lf.load_w)) for lf in load_fc], "#888888",
    ))
    parts.append(_dashed(
        [(x_for(sf.ts), y_for_pct(sf.soc_pct)) for sf in soc_fc], "#2ea56a",
    ))

    parts.append(
        f'<text x="{PAD_L+8}" y="{PAD_T+14}" font-size="11">'
        '<tspan fill="#d04545">━━ </tspan>'
        '<tspan fill="currentColor">theoretical</tspan>'
        '<tspan dx="14" fill="#e8a33d">━━ </tspan>'
        '<tspan fill="currentColor">actual</tspan>'
        '<tspan dx="14" fill="#3aa5c7">┅┅ </tspan>'
        '<tspan fill="currentColor">forecast</tspan>'
        '<tspan dx="14" fill="#888888">━━ </tspan>'
        '<tspan fill="currentColor">load</tspan>'
        '<tspan dx="14" fill="#2ea56a">━━ </tspan>'
        '<tspan fill="currentColor">SoC</tspan>'
        '<tspan dx="14" fill="#9b6dc7">━ </tspan>'
        '<tspan fill="currentColor">export / </tspan>'
        '<tspan fill="#9b6dc7">┅ </tspan>'
        '<tspan fill="currentColor">import</tspan>'
        '</text>'
    )

    return (
        f'<svg viewBox="0 0 {W} {H}" class="chart" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="Solar production over the last 24 hours">'
        + "".join(parts)
        + '</svg>'
    )


def _record_loads(ts: int) -> None:
    """Snapshot non-zero per-circuit loads from Emporia and write to the DB.

    Independent of `_record_sample` so a brief Emporia outage doesn't
    block the main telemetry write. Best-effort: errors are logged and
    swallowed.
    """
    try:
        em = _emporia()
        loads = em.all_circuit_loads(min_threshold_w=settings.load_log_threshold_w)
    except Exception:
        logger.warning("emporia circuit loads read failed", exc_info=False)
        return
    if not loads:
        return
    try:
        _load_store().insert_tick(ts, loads)
    except Exception:
        logger.warning("loads insert failed", exc_info=False)


def _record_sample_from_state(ctl: Controller, decision: Decision) -> None:
    """Persist a telemetry row built from the Controller's current state.

    Reads exclusively from `ctl.state` (the post-step() snapshot) rather
    than the raw pw/ev telemetry, so dead-reckoned SoC values during
    brief PW3 outages still get persisted (instead of writing NULL).
    The Decision's action_name and reason go into their respective
    columns for log-free auditability.
    """
    from zoneinfo import ZoneInfo

    s = ctl.state
    now_local = datetime.now(ZoneInfo(settings.timezone))
    try:
        theoretical = theoretical_w(now_local, settings)
    except Exception:
        theoretical = None
    soc = s.soc_pct
    if soc is not None and math.isnan(soc):
        soc = None
    sample = Sample(
        ts=int(s.ts),
        solar_w=s.solar_w,
        load_w=s.load_w,
        battery_w=s.battery_w,
        grid_w=s.grid_w,
        soc_pct=soc,
        theoretical_w=theoretical,
        charger_amps=s.ev_amps,
        charger_on=s.ev_on,
        charger_status=s.ev_status,
        pw_ok=s.soc_source == "pw3",
        em_ok=s.em_last_ts is not None,
        # Legacy column kept populated for charts that group by it.
        mode=("disabled" if ctl.kill_switch else (decision.action_name or "idle")),
        action_name=decision.action_name or None,
        decision_amps=decision.target_amps,
        decision_on=decision.on,
        decision_reason=decision.reason,
    )
    try:
        _sample_store().insert(sample)
    except Exception:
        logger.warning("sample insert failed", exc_info=False)


def _fetch_forecast() -> None:
    try:
        client = Solcast(settings)
        rows = client.fetch(hours=settings.solcast_forecast_horizon_hours)
    except Exception:
        logger.exception("solcast forecast fetch failed")
        return
    _forecast_store().insert_many(rows)
    logger.info("solcast forecast: stored {} periods ({}h horizon)",
                len(rows), settings.solcast_forecast_horizon_hours)


async def _forecast_loop() -> None:
    """Strategically scheduled Solcast fetches.

    Plan (per day): one fetch at 05:00 local + seven evenly spaced between
    sunrise and sunset = 8 total. The 10/day hobbyist budget leaves 2
    calls in reserve for retries / debugging. On startup we skip the
    initial fetch if the most recent stored fetch is younger than
    `solcast_skip_recent_minutes` — guards the budget across rapid
    restart cycles.
    """
    import time as _time
    from zoneinfo import ZoneInfo

    loop = asyncio.get_running_loop()
    tz = ZoneInfo(settings.timezone)

    last = _forecast_store().last_fetched_at()
    if last is not None:
        age_min = (_time.time() - last) / 60.0
        if age_min < settings.solcast_skip_recent_minutes:
            logger.info(
                "solcast: skipping startup fetch (last was {:.0f} min ago < {} min)",
                age_min, settings.solcast_skip_recent_minutes,
            )
        else:
            await loop.run_in_executor(None, _fetch_forecast)
    else:
        await loop.run_in_executor(None, _fetch_forecast)

    while True:
        now = datetime.now(tz)
        slots = daily_schedule(now, settings.latitude, settings.longitude)
        future = [t for t in slots if t > now]
        if future:
            next_t = min(future)
        else:
            # All of today's slots have passed; sleep until tomorrow's
            # pre-dawn slot (04:50, 10 min before the morning_dump start).
            from datetime import timedelta
            next_t = (now + timedelta(days=1)).replace(
                hour=4, minute=50, second=0, microsecond=0,
            )
        sleep_sec = max(1.0, (next_t - now).total_seconds())
        logger.info(
            "solcast: next fetch at {} (in {:.0f} min)",
            next_t.strftime("%H:%M"), sleep_sec / 60.0,
        )
        await asyncio.sleep(sleep_sec)
        await loop.run_in_executor(None, _fetch_forecast)


def _fetch_weather() -> None:
    client = NWS(settings)
    try:
        rows = client.fetch(horizon_hours=settings.nws_forecast_horizon_hours)
        _weather_store().insert_many(rows)
        logger.info("nws forecast: stored {} hourly rows ({}h horizon)",
                    len(rows), settings.nws_forecast_horizon_hours)
    except Exception:
        logger.exception("nws forecast fetch failed")
    try:
        obs = client.fetch_observations(
            hours=settings.nws_obs_hours,
            station_id=settings.nws_obs_station_id,
        )
        _observation_store().insert_many(obs)
        logger.info("nws obs ({}): stored {} hourly rows",
                    settings.nws_obs_station_id, len(obs))
    except Exception:
        logger.exception("nws observations fetch failed")


async def _weather_loop() -> None:
    """Refresh the NWS hourly forecast once a day at 05:00 local.

    Mirrors the Solcast 05:00 slot so the morning_dump window (06:00)
    always sees a fresh weather forecast. On startup, fetch immediately
    if we haven't fetched yet today.
    """
    from datetime import timedelta as _td
    from zoneinfo import ZoneInfo

    loop = asyncio.get_running_loop()
    tz = ZoneInfo(settings.timezone)

    last = _weather_store().last_fetched_at()
    today_5am_ts = int(datetime.now(tz).replace(
        hour=5, minute=0, second=0, microsecond=0,
    ).timestamp())
    if last is None or last < today_5am_ts:
        await loop.run_in_executor(None, _fetch_weather)
    else:
        logger.info("nws: skipping startup fetch (already fetched today)")

    while True:
        now = datetime.now(tz)
        next_t = now.replace(hour=5, minute=0, second=0, microsecond=0)
        if next_t <= now:
            next_t += _td(days=1)
        sleep_sec = (next_t - now).total_seconds()
        logger.info(
            "nws: next fetch at {} (in {:.0f} min)",
            next_t.strftime("%a %H:%M"), sleep_sec / 60.0,
        )
        await asyncio.sleep(sleep_sec)
        await loop.run_in_executor(None, _fetch_weather)


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
    """Dashboard control panel: per-action enable toggles + kill switch.

    Four buttons stacked vertically:
      1. Morning Dump   — toggles `settings.morning_dump_enabled`
      2. Surplus        — toggles `settings.surplus_enabled`
      3. Solar → EV     — toggles `settings.solar_passthrough_enabled`
      4. Disable All / Enable All — engages/releases the kill switch.

    Per-action buttons show as active only when their flag is True AND
    the kill switch is not engaged (the kill switch overrides). EVSE
    amperage during a kill is set from the Emporia app directly.
    """
    ctl = _ctl()
    last = ctl.last_decision
    current_action = last.action_name if last else None

    def _btn(value: str, label: str, sub: str, active: bool) -> str:
        cls = "mode-btn active" if active else "mode-btn"
        return (
            f'<button type="submit" name="action" value="{value}" class="{cls}">'
            f'{label}<small>{sub}</small></button>'
        )

    def _action_sub(active: bool, kill: bool, current: str, name: str) -> str:
        """Subtitle text for an action button. Single source of truth for
        the four states: firing / enabled-idle / kill-overridden / disabled."""
        if active:
            return "firing" if current == name else "enabled / idle"
        return "kill switch on" if kill else "disabled"

    # Morning Dump
    md_enabled = bool(getattr(ctl.settings, "morning_dump_enabled", True))
    md_active = md_enabled and not ctl.kill_switch
    md_sub = _action_sub(md_active, ctl.kill_switch, current_action or "", "morning_dump")

    # Surplus
    sp_enabled = bool(getattr(ctl.settings, "surplus_enabled", True))
    sp_active = sp_enabled and not ctl.kill_switch
    sp_sub = _action_sub(sp_active, ctl.kill_switch, current_action or "", "surplus")

    # Solar → EV (SolarPassthrough)
    spt_enabled = bool(getattr(ctl.settings, "solar_passthrough_enabled", False))
    spt_active = spt_enabled and not ctl.kill_switch
    spt_sub = _action_sub(
        spt_active, ctl.kill_switch, current_action or "", "solar_passthrough",
    )

    # Kill switch button
    if ctl.kill_switch:
        ks_value = "release_kill_switch"
        ks_label = "Enable All"
        ks_sub = "kill switch on &middot; click to release"
        ks_active = True
    else:
        ks_value = "engage_kill_switch"
        ks_label = "Disable All"
        ks_sub = "kill switch off"
        ks_active = False

    rows = [
        _btn("toggle_morning_dump", "Morning Dump", md_sub, md_active),
        _btn("toggle_surplus", "Surplus", sp_sub, sp_active),
        _btn("toggle_solar_passthrough", "Solar → EV", spt_sub, spt_active),
        _btn(ks_value, ks_label, ks_sub, ks_active),
    ]
    x, y, w, h = _MODES_PANEL
    return (
        f'<foreignObject x="{x}" y="{y}" width="{w}" height="{h}">'
        f'<div {_FO_NS} class="panel"><h3>Automation</h3>'
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
        # Read from cache, not the network. The control loop refreshes
        # these every poll_interval_sec; mobile page loads stay instant
        # even when Emporia's cloud is sluggish.
        pw, pw_err = _cached_pw()
        ev, ev_err = _cached_ev()
        consumers = _safe_top_consumers()
    # Use the Controller's most recent decision rather than re-running it
    # synchronously per page render. If we don't have one yet (first
    # request before the control loop has ticked), fall back to a
    # placeholder so the dashboard still draws.
    decision = _ctl().last_decision or Decision(
        0, "waiting on first tick", on=False, action_name="none",
    )

    def fmt_w(v: float) -> str:
        return f"{v:+.0f} W" if v else "0 W"

    # `pw_err` / `ev_err` may be non-None even when the reading itself is
    # present — that means "we have last-known data but it's stale." Show
    # the data plus a staleness banner; only fall back to "unavailable"
    # when we've literally never had a reading.
    def _stale_banner(err: str | None) -> str:
        if not err:
            return ""
        return f"<tr><td colspan=2 class=err>{html.escape(err)}</td></tr>"

    if pw:
        pw_rows = (
            _stale_banner(pw_err)
            + f"<tr><td>solar</td><td>{fmt_w(pw.solar_w)}</td></tr>"
            f"<tr><td>home load</td><td>{fmt_w(pw.load_w)}</td></tr>"
            f"<tr><td>battery</td><td>{fmt_w(pw.battery_w)} "
            f"<small>({'discharging' if pw.battery_w > 0 else 'charging' if pw.battery_w < 0 else 'idle'})</small></td></tr>"
            f"<tr><td>grid</td><td>{fmt_w(pw.grid_w)} "
            f"<small>({'importing' if pw.grid_w > 0 else 'exporting' if pw.grid_w < 0 else 'balanced'})</small></td></tr>"
            f"<tr><td>SoC</td><td><b>{pw.battery_soc_pct:.1f} %</b></td></tr>"
        )
    else:
        pw_rows = f"<tr><td colspan=2 class=err>Powerwall unavailable: {html.escape(pw_err or '')}</td></tr>"

    if ev:
        amps_value = ev.charge_rate_a
        on_checked = "checked" if ev.on else ""
        ev_rows = (
            _stale_banner(ev_err)
            + f"<tr><td>name</td><td>{html.escape(ev.name)} <small>(gid {ev.gid})</small></td></tr>"
            f"<tr><td>state</td><td><b>{'ON' if ev.on else 'OFF'}</b></td></tr>"
            f"<tr><td>rate</td><td>{ev.charge_rate_a} A <small>(max {ev.max_charge_rate_a} A)</small></td></tr>"
        )
    else:
        amps_value = settings.ev_min_amps
        on_checked = ""
        ev_rows = f"<tr><td colspan=2 class=err>Emporia unavailable: {html.escape(ev_err or '')}</td></tr>"

    decision_html = (
        f'<p class=decision><b>{html.escape(_mode_label(_ctl()))}:</b> '
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
  .circuits-legend {{ display: flex; flex-wrap: wrap; gap: .35em 1em; font-size: 12px; margin: 0 .5em 1.5em; max-width: 900px; }}
  .circuits-legend .leg-item {{ white-space: nowrap; color: var(--muted); }}
  .circuits-legend .leg-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: .35em; vertical-align: middle; }}
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

{_chart_heading()}
{_chart_svg()}

<h2>Usage</h2>
{_circuits_section()}

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
def set_mode(action: Annotated[str, Form()]) -> RedirectResponse:
    """Per-action enable toggles + kill switch.

    `action` is one of:
      - "toggle_morning_dump" / "toggle_surplus" /
        "toggle_solar_passthrough" — flip the matching Settings flag
        in place (in-memory only; not persisted).
      - "engage_kill_switch" / "release_kill_switch" — kill switch.

    The next `_control_tick` picks up the change naturally — no need
    to push anything to the EVSE here.
    """
    ctl = _ctl()
    if action == "toggle_morning_dump":
        new_state = not bool(getattr(ctl.settings, "morning_dump_enabled", True))
        ctl.settings.morning_dump_enabled = new_state
        logger.info("morning_dump_enabled -> {} (user)", new_state)
    elif action == "toggle_surplus":
        new_state = not bool(getattr(ctl.settings, "surplus_enabled", True))
        ctl.settings.surplus_enabled = new_state
        logger.info("surplus_enabled -> {} (user)", new_state)
    elif action == "toggle_solar_passthrough":
        new_state = not bool(getattr(ctl.settings, "solar_passthrough_enabled", False))
        ctl.settings.solar_passthrough_enabled = new_state
        logger.info("solar_passthrough_enabled -> {} (user)", new_state)
    elif action == "engage_kill_switch":
        ctl.engage_kill_switch()
        logger.info("automation disabled by user (kill switch engaged)")
    elif action == "release_kill_switch":
        ctl.release_kill_switch()
        logger.info("automation enabled by user (kill switch released)")
    else:
        logger.warning("/mode: ignoring unknown action {!r}", action)
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

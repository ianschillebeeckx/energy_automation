"""Map a user-selected charge mode to a concrete target current.

`compute_target(mode, pw, ev, settings)` returns the `Decision` the control
loop should push to the EVSE. It never calls Emporia or Powerwall itself —
pure function of the inputs, easy to test.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Settings
from .emporia import ChargerState
from .policy import Decision, decide_ev_amps
from .powerwall import PowerReading


def _next_dump_window(now: datetime, settings: Settings) -> tuple[datetime, datetime]:
    """Return the [start, end) window for the next upcoming morning-dump.

    If today's window is still ahead or currently active, returns today's.
    If today's window has already ended, returns tomorrow's. This lets the
    user click the button in the evening and have the schedule fire at
    the next morning's start time.
    """
    start_today = now.replace(
        hour=settings.morning_dump_start_hour,
        minute=settings.morning_dump_start_minute,
        second=0, microsecond=0,
    )
    end_today = start_today + timedelta(hours=settings.morning_dump_hours)
    if now < end_today:
        return start_today, end_today
    start_tomorrow = start_today + timedelta(days=1)
    return start_tomorrow, start_tomorrow + timedelta(hours=settings.morning_dump_hours)


def compute_target(
    mode: str,
    pw: PowerReading | None,
    ev: ChargerState | None,
    settings: Settings,
    now: datetime | None = None,
) -> Decision:
    if mode == "off":
        # Charger off; keep whatever amperage was last configured so the
        # manual slider value survives for debugging.
        return Decision(0, "charging disabled", on=False)

    if mode == "manual":
        # Hands off — the control loop won't push to the EVSE in this mode,
        # so this Decision is only used for the dashboard's status line.
        return Decision(
            ev.charge_rate_a if ev else 0,
            "manual control (loop paused)",
            on=bool(ev and ev.on),
        )

    if mode == "surplus":
        if pw is None or ev is None:
            return Decision(0, "waiting on telemetry")
        return decide_ev_amps(pw, ev, settings)

    if mode == "trickle":
        amps = int(settings.trickle_kw * 1000 / settings.ev_voltage)
        amps = max(settings.ev_min_amps, min(settings.ev_max_amps, amps))
        return Decision(
            amps, f"trickle {settings.trickle_kw:.1f} kW -> {amps} A", on=True,
        )

    if mode == "morning_dump":
        if pw is None or math.isnan(pw.battery_soc_pct):
            return Decision(0, "no battery reading", on=False)

        tz = ZoneInfo(settings.timezone)
        now_local = (now.astimezone(tz) if now is not None else datetime.now(tz))
        start, end = _next_dump_window(now_local, settings)

        if now_local < start:
            # Scheduled — preview the rate at full-window duration so the
            # dashboard shows what the charger is configured for.
            amps, _ = _dump_amps_for(pw, settings.morning_dump_hours, settings)
            return Decision(amps, f"scheduled {start:%a %H:%M}", on=False)

        # In the window: base the rate on the time we have left so we
        # converge on the floor even if discharge was faster/slower than
        # estimated at the start.
        remaining_hr = max((end - now_local).total_seconds() / 3600.0, 0.05)
        amps, reason = _dump_amps_for(pw, remaining_hr, settings)
        return Decision(amps, reason, on=(amps > 0))

    return Decision(0, f"unknown mode {mode!r}", on=False)


def _dump_amps_for(
    pw: PowerReading, window_hours: float, settings: Settings,
) -> tuple[int, str]:
    """Return (amps, reason) for draining the battery to the floor in `window_hours`."""
    headroom = (
        (pw.battery_soc_pct - settings.morning_dump_floor_pct) / 100.0
        * settings.battery_capacity_kwh
    )
    if headroom <= 0:
        return 0, (
            f"SoC {pw.battery_soc_pct:.0f}% at/below floor "
            f"{settings.morning_dump_floor_pct}%"
        )
    kw = headroom / window_hours
    amps = int(kw * 1000 / settings.ev_voltage)
    amps = max(settings.ev_min_amps, min(settings.ev_max_amps, amps))
    return amps, f"dump {headroom:.1f} kWh in {window_hours:.2f} h -> {amps} A"

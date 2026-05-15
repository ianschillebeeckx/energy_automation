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
from .forecast import pv_kwh_in_range
from .policy import Decision, decide_ev_amps
from .powerwall import PowerReading
from .samples import Forecast
from .solar import theoretical_day_kwh


def _sunny_floor_pct(
    pv_forecasts: list[Forecast] | None,
    dump_start: datetime,
    settings: Settings,
) -> int:
    """Lower the morning_dump floor when today's forecast looks clear.

    Returns `morning_dump_sunny_floor_pct` if forecast PV for the day of
    `dump_start` reaches `morning_dump_sunny_threshold_pct` of the
    theoretical clear-sky integral, otherwise the normal floor.

    TODO: replace this PV-only ratio with a simulation-based test using
    forecast.soc_forecast(): start from the post-dump floor, integrate
    (PV − load) forecast across the day, and lower the floor only when
    SoC is projected to reach 100% by sunset (with a configurable
    headroom buffer). That handles cloudy days with low load, sunny
    days with heavy load (HVAC, EV trickle), and seasonal variations
    in one principled rule instead of a static %-of-clear-sky threshold.
    """
    if not pv_forecasts:
        return settings.morning_dump_floor_pct
    day_start = dump_start.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    forecast_kwh = pv_kwh_in_range(
        pv_forecasts, int(day_start.timestamp()), int(day_end.timestamp()),
    )
    theoretical_kwh = theoretical_day_kwh(day_start, settings)
    if theoretical_kwh <= 0:
        return settings.morning_dump_floor_pct
    ratio = forecast_kwh / theoretical_kwh
    if ratio >= settings.morning_dump_sunny_threshold_pct / 100.0:
        return settings.morning_dump_sunny_floor_pct
    return settings.morning_dump_floor_pct


def _next_dump_window(now: datetime, settings: Settings) -> tuple[datetime, datetime]:
    """Return the [start, end) window for the next upcoming morning-dump.

    If today's window is still ahead or currently active, returns today's.
    If today's window has already ended, returns tomorrow's. This lets the
    user click the button in the evening and have the schedule fire at
    the next morning's start time.

    Start and end are both wall-clock anchored, so they roll together
    across midnight if the user ever configures an overnight window.
    """
    start_today = now.replace(
        hour=settings.morning_dump_start_hour,
        minute=settings.morning_dump_start_minute,
        second=0, microsecond=0,
    )
    end_today = now.replace(
        hour=settings.morning_dump_end_hour,
        minute=settings.morning_dump_end_minute,
        second=0, microsecond=0,
    )
    if now < end_today:
        return start_today, end_today
    return (
        start_today + timedelta(days=1),
        end_today + timedelta(days=1),
    )


def compute_target(
    mode: str,
    pw: PowerReading | None,
    ev: ChargerState | None,
    settings: Settings,
    now: datetime | None = None,
    pv_forecasts: list[Forecast] | None = None,
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

        # Forecast PV between now and the window end adds to the headroom
        # we have to drain. Credit only `morning_dump_pv_credit_pct` of the
        # raw forecast so the rate stays slightly conservative.
        credit = settings.morning_dump_pv_credit_pct / 100.0
        raw_pv_kwh = pv_kwh_in_range(
            pv_forecasts or [],
            int(max(now_local, start).timestamp()),
            int(end.timestamp()),
        )
        forecast_kwh = raw_pv_kwh * credit

        # Sunny-day deep dump: when today's full-day forecast clears the
        # threshold of the theoretical max, drain to a lower floor so the
        # battery has more room for incoming generation.
        floor_pct = _sunny_floor_pct(pv_forecasts, start, settings)
        sunny = floor_pct != settings.morning_dump_floor_pct

        if now_local < start:
            # Scheduled — preview the rate at full-window duration so the
            # dashboard shows what the charger is configured for.
            full_window_hr = (end - start).total_seconds() / 3600.0
            amps, _ = _dump_amps_for(
                pw, full_window_hr, settings, forecast_kwh,
                floor_pct=floor_pct,
            )
            reason = f"scheduled {start:%a %H:%M}"
            if sunny:
                reason = f"sunny: {reason}"
            return Decision(amps, reason, on=False)

        # In the window: base the rate on the time we have left so we
        # converge on the floor even if discharge was faster/slower than
        # estimated at the start.
        remaining_hr = max((end - now_local).total_seconds() / 3600.0, 0.05)
        amps, reason = _dump_amps_for(
            pw, remaining_hr, settings, forecast_kwh, floor_pct=floor_pct,
        )
        if sunny:
            reason = f"sunny: {reason}"
        return Decision(amps, reason, on=(amps > 0))

    return Decision(0, f"unknown mode {mode!r}", on=False)


def _dump_amps_for(
    pw: PowerReading,
    window_hours: float,
    settings: Settings,
    forecast_kwh: float = 0.0,
    floor_pct: int | None = None,
) -> tuple[int, str]:
    """Return (amps, reason) for draining the battery to `floor_pct` in `window_hours`.

    `forecast_kwh` is the (already-discounted) PV energy expected to arrive
    during the window — added to the SoC-derived headroom so the EV can
    absorb both the existing charge and the incoming generation.
    `floor_pct` defaults to `settings.morning_dump_floor_pct`; the caller
    overrides it for the sunny-day deeper dump.
    """
    floor = floor_pct if floor_pct is not None else settings.morning_dump_floor_pct
    battery_kwh = (
        (pw.battery_soc_pct - floor) / 100.0
        * settings.battery_capacity_kwh
    )
    if battery_kwh <= 0:
        return 0, f"SoC {pw.battery_soc_pct:.0f}% at/below floor {floor}%"
    headroom = battery_kwh + forecast_kwh
    kw = headroom / window_hours
    raw_amps = int(kw * 1000 / settings.ev_voltage)
    # Round *down* to off rather than up to ev_min_amps. Pulling 6 A out
    # of a battery whose natural rate would be 2 A drains it faster than
    # the dump's intended pace — better to idle and save the headroom for
    # HVAC / base load. As the remaining window shrinks the natural rate
    # rises, and the dump fires once it reaches the minimum.
    if raw_amps < settings.ev_min_amps:
        return 0, (
            f"hold: natural {raw_amps} A < min {settings.ev_min_amps} A "
            f"(SoC {pw.battery_soc_pct:.0f}% > floor {floor}%)"
        )
    amps = min(raw_amps, settings.ev_max_amps, settings.morning_dump_max_amps)
    reason = (
        f"dump {battery_kwh:.1f}+{forecast_kwh:.1f} kWh in "
        f"{window_hours:.2f} h -> {amps} A"
    )
    return amps, reason

"""Clear-sky theoretical PV output model.

A simple geometric model: given the sun's altitude and azimuth (from
astral), the panel orientation (azimuth + tilt from settings), and the
array's rated capacity, returns the watts the array *would* produce
under ideal conditions. The actual output divided by this gives a
measure of how much the system is losing to clouds, soiling, age, etc.

We don't model atmospheric scattering, panel temperature derate, or
spectral effects — that's what pvlib is for. The intent here is a
sanity-check overlay, not a forecast.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from .config import Settings


def theoretical_w(when: datetime, settings: Settings) -> float:
    """Theoretical instantaneous PV output (watts).

    Returns 0 when the sun is below the horizon, latitude/longitude are
    unset, or the angle of incidence on the panel face is non-positive.
    """
    if settings.latitude is None or settings.longitude is None:
        return 0.0

    # Deferred import: astral is only needed when we actually compute.
    from astral import Observer
    from astral.sun import azimuth, elevation

    obs = Observer(latitude=settings.latitude, longitude=settings.longitude)
    sun_alt_deg = elevation(obs, when)
    sun_az_deg = azimuth(obs, when)

    if sun_alt_deg <= 0.0:
        return 0.0

    alt = math.radians(sun_alt_deg)
    az = math.radians(sun_az_deg)
    p_tilt = math.radians(settings.solar_panel_tilt_deg)
    p_az = math.radians(settings.solar_panel_azimuth_deg)

    # Cosine of the angle of incidence between the sun vector and the
    # panel surface normal. Standard solar geometry formula.
    cos_theta = (
        math.sin(p_tilt) * math.cos(alt) * math.cos(az - p_az)
        + math.cos(p_tilt) * math.sin(alt)
    )
    if cos_theta <= 0.0:
        return 0.0

    return (
        settings.solar_array_max_kw * 1000.0
        * cos_theta
        * (1.0 - settings.solar_system_loss_factor)
    )


def theoretical_day_kwh(
    day_start: datetime, settings: Settings, step_sec: int = 300,
) -> float:
    """Integrate the clear-sky model over the 24 h starting at `day_start`.

    `day_start` must be timezone-aware. Used to gauge how close a real
    or forecast day comes to the geometric maximum — e.g., to decide
    whether to deepen the morning dump when a clear day is expected.
    """
    if day_start.tzinfo is None:
        raise ValueError("day_start must be timezone-aware")
    day_end = day_start + timedelta(days=1)
    total = 0.0
    t = day_start
    while t < day_end:
        w0 = theoretical_w(t, settings)
        w1 = theoretical_w(t + timedelta(seconds=step_sec), settings)
        total += (w0 + w1) / 2.0 * step_sec / 3600.0 / 1000.0
        t += timedelta(seconds=step_sec)
    return total

"""Pure helpers for control-loop time windows.

Carved out of the old `controller.py` so the action layer can use
`next_dump_window` without pulling in the legacy mode-dispatch logic
that's being replaced.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .config import Settings


def next_dump_window(now: datetime, settings: Settings) -> tuple[datetime, datetime]:
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

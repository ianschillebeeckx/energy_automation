"""Command-line entry points."""

from __future__ import annotations

import typer
from loguru import logger

from .config import settings
from .emporia import Emporia
from .policy import decide_ev_amps
from .powerwall import Powerwall

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.command()
def probe() -> None:
    """Read current state from Powerwall and Emporia and print it."""
    try:
        pw = Powerwall(settings).read()
        logger.info(
            "powerwall: solar={:.0f}W load={:.0f}W battery={:.0f}W grid={:.0f}W soc={:.1f}%",
            pw.solar_w, pw.load_w, pw.battery_w, pw.grid_w, pw.battery_soc_pct,
        )
    except Exception as e:
        logger.warning("powerwall read failed: {}", e)
        pw = None

    try:
        em = Emporia(settings)
        devices = em.list_chargers()
        logger.info("emporia: found {} charger(s)", len(devices))
        for d in devices:
            c = d.ev_charger
            logger.info(
                "  gid={} name={!r} on={} rate={}A max={}A status={!r} msg={!r}",
                d.device_gid, d.device_name or d.display_name,
                c.charger_on, c.charging_rate, c.max_charging_rate,
                c.status, c.message,
            )
        ev = em.read() if devices else None
    except Exception as e:
        logger.warning("emporia read failed: {}", e)
        ev = None

    if pw and ev:
        decision = decide_ev_amps(pw, ev, settings)
        logger.info("decision: set EV to {}A ({})", decision.target_amps, decision.reason)


@app.command()
def run() -> None:
    """Run the control loop. Not implemented yet."""
    typer.echo("run loop not implemented yet; use `probe` to inspect state.")
    raise typer.Exit(code=1)


@app.command()
def backfill(
    days: int = typer.Option(2, help="Past days to backfill (e.g. 2 = yesterday + today)."),
    step_sec: int = typer.Option(300, help="Sample interval in seconds (5 min default)."),
) -> None:
    """Seed `samples.theoretical_w` for past days from the astronomical model.

    Useful for an empty/new DB so the chart and SQL queries have a
    reference curve before real telemetry has accumulated. Doesn't touch
    any other columns on existing rows.
    """
    from datetime import datetime, timedelta
    from pathlib import Path
    from zoneinfo import ZoneInfo

    from .samples import SampleStore
    from .solar import theoretical_w

    if settings.latitude is None or settings.longitude is None:
        typer.echo("LATITUDE / LONGITUDE not set in .env", err=True)
        raise typer.Exit(code=1)

    store = SampleStore(Path("state") / "samples.db")
    tz = ZoneInfo(settings.timezone)
    end = datetime.now(tz)
    start = end - timedelta(days=days)

    count = 0
    t = start
    while t <= end:
        store.backfill_theoretical(int(t.timestamp()), theoretical_w(t, settings))
        t += timedelta(seconds=step_sec)
        count += 1
    typer.echo(f"backfilled {count} theoretical samples ({days} days @ {step_sec}s)")


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", help="Bind address. Default exposes to LAN; no auth — keep off untrusted networks."),
    port: int = typer.Option(8000, help="Port for the web UI."),
) -> None:
    """Start the browser UI for manual charger control + live state."""
    from .web import serve as _serve

    if host == "0.0.0.0":
        logger.warning("serving on 0.0.0.0:{} with NO AUTH — LAN-only, no reverse proxy", port)
    _serve(host=host, port=port)


def main() -> None:
    app()

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


def main() -> None:
    app()

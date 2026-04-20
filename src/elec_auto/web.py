"""Minimal browser UI for manual charger control and live state display.

Binds to localhost by default. There is no auth — do not expose this to the
open LAN without putting a reverse proxy / password in front of it.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Annotated

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

from .config import settings
from .emporia import ChargerState, Emporia
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


def _render(flash: str = "", flash_ok: bool = True) -> str:
    pw, pw_err = _safe_pw()
    ev, ev_err = _safe_em()
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
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 520px; margin: 2em auto; padding: 0 1em; }}
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

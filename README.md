# elec_auto

Home energy automation. Routes surplus solar PV to the appropriate sink based on
a configurable priority policy, starting with EV charging against a Tesla
Powerwall 3 + Emporia EV Charger Classic.

## Priority policy (default)

From highest to lowest priority, PV generation feeds:

1. Real-time home consumption (heat, oven, etc.)
2. Powerwall (battery) charging
3. EV charging (surplus only)
4. Export to grid

Overrides can be scheduled (e.g. pre-heat house between 15:00 and 16:00 in
winter, bumping #1 ahead of the defaults during that window).

## Layout

```
src/elec_auto/
├── config.py      # Settings loaded from .env
├── powerwall.py   # Tesla Powerwall 3 local client (pypowerwall / TEDAPI)
├── emporia.py     # Emporia EV Charger Classic client (pyemvue cloud)
├── policy.py      # Priority / allocation logic
└── cli.py         # Entry points: `elec-auto probe`, `elec-auto run`
```

## Getting started

```bash
uv sync                         # install deps into .venv
cp .env.example .env            # then fill in credentials
uv run elec-auto probe          # one-shot: print current PV/battery/EV state
```

## Deployment

Prototyped on a MacBook Pro. Production target is a MacBook Air running on the
home LAN. Both machines use `uv sync` from this repo's lockfile to recreate the
environment.

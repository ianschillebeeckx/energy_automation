# analyses/

One-off scripts that replay or analyze the system's recorded data.
Output goes into `analyses/output/` and is committed (the images are
small) so a reader can see what was found without re-running.

Each script is self-contained and is run via `uv run python -m
analyses.<name>` from the repo root. They read `state/samples.db`
directly and never touch live telemetry.

| Script | What it does |
|---|---|
| `replay_soc.py` | Dead-reckons SoC through a chosen window using only the first sample's recorded SoC as the anchor and each subsequent tick's recorded `battery_w` as the rate. Compares the dead-reckoned trace to the actually-recorded SoC — measures how much our state model would drift if PW3's SoC readings disappeared. |

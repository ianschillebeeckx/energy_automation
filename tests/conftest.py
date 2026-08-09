"""Global test isolation.

The Controller reads live tunable overrides from
`state/config_overrides.json` via `runtime_config.effective(...)`. That
file is real user state (last dashboard "Apply") and will absolutely
be non-empty on a running instance. Tests that construct their own
Settings must not have those overrides silently mixed in — otherwise
you get "why is battery_reserve_pct=20 in a test that set it to 80"
kinds of surprises. Neutralize by monkeypatching the two entry points
`Controller._decide` uses to a pass-through.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_runtime_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from elec_auto import control, runtime_config
    pass_through = lambda s: s
    monkeypatch.setattr(runtime_config, "effective", pass_through)
    monkeypatch.setattr(runtime_config, "read", dict)
    monkeypatch.setattr(runtime_config, "read_with_expiry", dict)
    # `from .runtime_config import effective as _effective_settings`
    # binds a local alias in control's namespace — patch it too, or
    # the Controller keeps calling the real (file-reading) version.
    monkeypatch.setattr(control, "_effective_settings", pass_through)

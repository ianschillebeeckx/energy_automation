"""Tests for `PW3CloudClient` — auth session recycling on stuck reads.

Regression coverage for the 2026-07-13→17 incident: the long-running
service's cached `PyPowerwallFleetAPI` started returning `mode=None
reserve=None` for every read, `apply: push to PW3 cloud failed` fired
on every peak_export tick, and zero energy was exported that week.
Fresh subprocesses worked fine — the fix recycles the cached client
after RESET_STREAK consecutive invalid reads.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from elec_auto.pw3_cloud import PW3CloudClient


def _install_fake_fleet(client: PW3CloudClient, mode_seq, reserve_seq) -> list[int]:
    """Wire a fake `fleet` client that returns values from the given
    iterables per call. Returns a call-count container so tests can
    assert how many API round-trips happened.
    """
    counter = [0]

    def _get_mode(force: bool = False):
        v = mode_seq[min(counter[0], len(mode_seq) - 1)]
        counter[0] += 1
        return v

    def _get_reserve(force: bool = False):
        # Reserve is read in the same tick as mode; advance by pairing.
        # (Tests can just track calls via `counter` if they need it.)
        return reserve_seq[min(counter[0] - 1, len(reserve_seq) - 1)]

    fleet = SimpleNamespace(
        get_operating_mode=_get_mode,
        get_battery_reserve=_get_reserve,
    )
    client._client = SimpleNamespace(fleet=fleet)
    return counter


def test_read_state_valid_clears_invalid_streak(monkeypatch) -> None:
    c = PW3CloudClient()
    _install_fake_fleet(c, ["self_consumption"], [20])
    c._invalid_streak = 2  # simulate prior failures
    state = c.read_state()
    assert state.mode == "self_consumption"
    assert state.reserve_pct == 20
    assert c._invalid_streak == 0


def test_read_state_recycles_client_after_reset_streak(monkeypatch) -> None:
    """After RESET_STREAK consecutive invalid reads, the cached client
    is dropped so the next call rebuilds it via `connect()`."""
    c = PW3CloudClient()
    # Every read returns garbage. `read_state` retries once internally
    # and both attempts fail — so a single call bumps the streak by 1.
    _install_fake_fleet(c, [None] * 20, [None] * 20)
    monkeypatch.setattr("time.sleep", lambda _s: None)  # skip the 1s retry sleep

    initial_client = c._client
    assert initial_client is not None

    # RESET_STREAK is 3 — first two calls just bump the streak.
    for i in range(c._RESET_STREAK - 1):
        with pytest.raises(RuntimeError, match="invalid mode"):
            c.read_state()
        assert c._invalid_streak == i + 1
        assert c._client is initial_client  # not yet recycled

    # RESET_STREAK-th call: recycle fires.
    with pytest.raises(RuntimeError, match="invalid mode"):
        c.read_state()
    assert c._client is None  # recycled — next call will connect() fresh
    assert c._invalid_streak == 0  # counter reset after recycle

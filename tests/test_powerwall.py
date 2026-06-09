"""Tests for the raw -> displayed SoC scaling used by the PW3 local client."""

from __future__ import annotations

import math

import pytest

from elec_auto.powerwall import (
    PowerwallUnavailable,
    _LocalGateway,
    _to_displayed_soc,
)


@pytest.mark.parametrize(
    "raw,floor,expected",
    [
        (100.0, 5.0, 100.0),         # full pack
        (52.5, 5.0, 50.0),           # midpoint of operating range
        (5.0, 5.0, 0.0),             # at the floor
        (2.0, 5.0, 0.0),             # below the floor -> clamped
        (7.32, 5.0, pytest.approx(2.44, abs=0.01)),  # the case we hit live
        (50.0, 0.0, 50.0),           # zero floor: identity
        (50.0, 20.0, pytest.approx(37.5, abs=0.01)),  # higher floor
    ],
)
def test_to_displayed_soc(raw: float, floor: float, expected: float) -> None:
    assert _to_displayed_soc(raw, floor) == expected


def test_nan_passes_through() -> None:
    # When the API is unreachable we may end up with NaN; the helper must
    # not crash and must not silently fabricate a number.
    assert math.isnan(_to_displayed_soc(float("nan"), 5.0))


def test_clamps_above_100() -> None:
    # Shouldn't happen, but guard against weird firmware reporting >100%.
    assert _to_displayed_soc(105.0, 5.0) == 100.0


# --- back-off behavior ------------------------------------------------------


def _make_gateway() -> _LocalGateway:
    return _LocalGateway(host="gw.local", customer_password="abcde",
                         raw_floor_pct=5.0)


def test_backoff_short_circuits_within_window(monkeypatch) -> None:
    """A failure schedules back-off; subsequent read() inside it raises
    PowerwallUnavailable without touching the network."""
    gw = _make_gateway()
    calls = {"n": 0}

    def fail(_path: str) -> dict:
        calls["n"] += 1
        raise RuntimeError("simulated 502")

    monkeypatch.setattr(gw, "_get", fail)
    with pytest.raises(RuntimeError, match="simulated 502"):
        gw.read()
    assert calls["n"] == 1
    assert gw._failure_streak == 1
    assert gw._backoff_until > 0

    # Second call inside the back-off window: no network attempt.
    with pytest.raises(PowerwallUnavailable):
        gw.read()
    assert calls["n"] == 1  # unchanged — _get not called


def test_backoff_escalates_then_caps(monkeypatch) -> None:
    """Streak picks successively longer back-off, capped at the last entry."""
    import time as _time
    gw = _make_gateway()
    monkeypatch.setattr(gw, "_get", lambda _p: (_ for _ in ()).throw(RuntimeError("x")))

    waits: list[float] = []
    for _ in range(len(gw._BACKOFF_SCHEDULE_SEC) + 2):
        t0 = _time.monotonic()
        with pytest.raises(Exception):
            gw.read()
        waits.append(gw._backoff_until - t0)
        # Pretend the back-off elapsed so the next read() attempts the network.
        gw._backoff_until = 0.0

    # Strictly increasing through the schedule, then flat at the cap.
    sched = gw._BACKOFF_SCHEDULE_SEC
    for i, expected in enumerate(sched):
        assert waits[i] == pytest.approx(expected, abs=0.5)
    for w in waits[len(sched):]:
        assert w == pytest.approx(sched[-1], abs=0.5)


def test_success_clears_streak(monkeypatch) -> None:
    gw = _make_gateway()
    monkeypatch.setattr(gw, "_get", lambda _p: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(Exception):
        gw.read()
    assert gw._failure_streak == 1

    # Swap to a healthy gateway response.
    aggs = {
        "solar": {"instant_power": 1000.0},
        "load": {"instant_power": 800.0},
        "battery": {"instant_power": -200.0},
        "site": {"instant_power": 0.0},
    }
    soe = {"percentage": 52.5}
    monkeypatch.setattr(
        gw, "_get",
        lambda path: aggs if path.endswith("aggregates") else soe,
    )
    gw._backoff_until = 0.0  # pretend the back-off window elapsed
    reading = gw.read()
    assert reading.solar_w == 1000.0
    assert gw._failure_streak == 0
    assert gw._backoff_until == 0.0


def test_session_and_token_recycle_after_reset_streak(monkeypatch) -> None:
    """After RESET_STREAK consecutive failures, the cached bearer and
    pooled Session are dropped so the next attempt starts clean."""
    gw = _make_gateway()
    monkeypatch.setattr(gw, "_get", lambda _p: (_ for _ in ()).throw(RuntimeError("x")))
    gw._token = "stale-token"
    initial_session = gw._session

    for _ in range(gw._RESET_STREAK):
        with pytest.raises(Exception):
            gw.read()
        gw._backoff_until = 0.0  # bypass back-off to keep failing

    assert gw._token is None
    assert gw._session is not initial_session

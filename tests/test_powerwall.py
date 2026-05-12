"""Tests for the raw -> displayed SoC scaling used by the PW3 local client."""

from __future__ import annotations

import math

import pytest

from elec_auto.powerwall import _to_displayed_soc


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

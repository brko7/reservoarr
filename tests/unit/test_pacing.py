"""Pacing controller unit tests: the clamp behavior + the grace-period floor
flip of release_factor() — the v5 cushion bug came from the floor cancelling
out, so the math has to stay exactly as specced.

  r = rate * clamp(1 + 0.3 * level_err, floor, 1.15)
  level_err = (cushion - TARGET_S) / TARGET_S
  floor = 1.0 for the first GRACE_S, 0.97 after
"""
from __future__ import annotations

import pytest

IN_GRACE = 0.0
AFTER_GRACE = 1e6


@pytest.fixture
def pacing_factor(resv):
    def factor(cushion_s, target_s, in_grace):
        assert target_s == resv.TARGET_S
        return resv.release_factor(cushion_s, IN_GRACE if in_grace else AFTER_GRACE)
    return factor


def test_at_target_factor_is_one(pacing_factor):
    # cushion=30 == TARGET_S(30) -> factor exactly 1.0 -> realtime release
    assert pacing_factor(30, 30, in_grace=False) == 1.0


def test_above_target_speeds_up(pacing_factor):
    # cushion 45, target 30 -> err=0.5 -> 1 + 0.15 = 1.15 (hits ceiling)
    assert pacing_factor(45, 30, in_grace=False) == 1.15
    # cushion 35 -> err=0.167 -> 1.05
    assert abs(pacing_factor(35, 30, in_grace=False) - 1.05) < 1e-6


def test_below_target_slows_down_after_grace(pacing_factor):
    # cushion 15, target 30 -> err=-0.5 -> 1 - 0.15 = 0.85 -> floored to 0.97
    assert pacing_factor(15, 30, in_grace=False) == 0.97


def test_below_target_holds_realtime_during_grace(pacing_factor):
    """The first GRACE_S after release-start, the player owns only the small
    headstart — sub-realtime would drain it before the bank settles. Floor=1.0."""
    assert pacing_factor(15, 30, in_grace=True) == 1.0
    assert pacing_factor(5, 30, in_grace=True) == 1.0


def test_clamp_ceiling_never_exceeds_1_15(pacing_factor):
    """Bigger-than-target cushion can't bleed faster than 1.15× content rate —
    bleeding too fast IS the v5 bug (chasing arrival rate ate the surplus)."""
    assert pacing_factor(300, 30, in_grace=False) == 1.15
    assert pacing_factor(300, 30, in_grace=True) == 1.15


def test_grace_constants_match_script(resv):
    """Sanity: the constants in the script are what we expect. If someone bumps
    TARGET_S or GRACE_S, these tests need to retune alongside it."""
    assert resv.TARGET_S == 30.0
    assert resv.GRACE_S == 45.0
    assert resv.RATE_FLOOR == 125_000   # 1 Mbps


def test_the_floor_flips_exactly_at_grace_end(resv):
    assert resv.release_factor(15, resv.GRACE_S - 0.01) == 1.0
    assert resv.release_factor(15, resv.GRACE_S) == 0.97

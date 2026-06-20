"""Pacing controller unit tests. The release rate is computed inline in main(),
so these tests replicate the formula and assert its clamp behavior + the
grace-period floor flip — the v5 cushion bug came from the floor cancelling out,
so the math has to stay exactly as specced.

  r = rate * clamp(1 + 0.3 * level_err, floor, 1.15)
  level_err = (cushion - TARGET_S) / TARGET_S
  floor = 1.0 for the first GRACE_S, 0.97 after
"""
from __future__ import annotations


def pacing_factor(cushion_s, target_s, in_grace, floor_after_grace=0.97, gain=0.3, ceiling=1.15):
    """Mirror of reservoarr.py main()'s pacing math, kept identical."""
    level_err = (cushion_s - target_s) / target_s
    floor = 1.0 if in_grace else floor_after_grace
    return min(max(1.0 + gain * level_err, floor), ceiling)


def test_at_target_factor_is_one(resv):
    # cushion=30 == TARGET_S(30) -> factor exactly 1.0 -> realtime release
    assert pacing_factor(30, 30, in_grace=False) == 1.0


def test_above_target_speeds_up(resv):
    # cushion 45, target 30 -> err=0.5 -> 1 + 0.15 = 1.15 (hits ceiling)
    assert pacing_factor(45, 30, in_grace=False) == 1.15
    # cushion 35 -> err=0.167 -> 1.05
    assert abs(pacing_factor(35, 30, in_grace=False) - 1.05) < 1e-6


def test_below_target_slows_down_after_grace(resv):
    # cushion 15, target 30 -> err=-0.5 -> 1 - 0.15 = 0.85 -> floored to 0.97
    assert pacing_factor(15, 30, in_grace=False) == 0.97


def test_below_target_holds_realtime_during_grace(resv):
    """The first GRACE_S after release-start, the player owns only the small
    headstart — sub-realtime would drain it before the bank settles. Floor=1.0."""
    assert pacing_factor(15, 30, in_grace=True) == 1.0
    assert pacing_factor(5, 30, in_grace=True) == 1.0


def test_clamp_ceiling_never_exceeds_1_15(resv):
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

"""Release-rate e2e: with the cushion under the target, the release holds the
content rate (RESV_FLOOR=1.0, the default) or 3% under it (RESV_FLOOR=0.97)
once the grace is over, on the real pipeline. It catches the fixed 0.97 floor
of discussion #46 everywhere. The pacer dropping every oversleep shows here
only on macOS (9% under at this 2.4 Mbps); on Linux it costs ~0.6% at this
rate, inside the bands, and test_the_pacer_pays_back_oversleep catches it."""
from __future__ import annotations

import pytest

from .harness import run_pipeline


@pytest.mark.e2e
@pytest.mark.parametrize("env, low, high", [
    ({}, 0.985, 1.015),
    ({"RESV_FLOOR": "0.97"}, 0.955, 0.985),
], ids=["default", "floor-0.97"])
def test_release_rate_under_the_target(tmp_path, synth_ts, env, low, high):
    # A 10s front-load leaves ~5s after the 5s headstart, far under the 30s target.
    run = run_pipeline(tmp_path, synth_ts, rate_bps=300_010, duration_s=100, front_s=10, extra_env=env)
    stats = run.stats_lines()
    after_grace = stats[3:]                                           # windows end ~15, 30, 45, 60... s in
    assert len(after_grace) >= 2, "no telemetry after the grace:\n  " + "\n  ".join(s["raw"] for s in stats)
    # Over all the windows together: a write that stalls across a window
    # boundary shifts bytes into the next window without changing the rate.
    ratio = sum(s["out_mbps"] for s in after_grace) / sum(s["crate_mbps"] for s in after_grace)
    assert low <= ratio <= high, (
        f"out/crate={ratio:.3f}, expected {low}-{high}:\n  " + "\n  ".join(s["raw"] for s in stats)
    )

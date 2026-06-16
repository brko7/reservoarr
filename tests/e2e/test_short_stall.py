"""Short-stall e2e: 12s CDN stall absorbed by the cushion. This is the
core invariant — v5 sessions died inside 15s of stall; v6 must absorb a
12s stall with output never starving and the stall watchdog NOT firing."""
from __future__ import annotations

import pytest

from .harness import run_pipeline


@pytest.mark.e2e
def test_12s_stall_absorbed(tmp_path, synth_ts):
    """Introduce a 12s stall at t=30s; cushion should dip but output keeps flowing.
    The #4 watchdog must NOT fire (STALL_S=25 > 12)."""
    run = run_pipeline(
        tmp_path, synth_ts, rate_bps=300_010, duration_s=75,
        stalls=[(30.0, 12.0)],
    )
    log = run.log_text()
    assert "no data" not in log, f"stall watchdog #4 should not fire on 12s stall:\n{log}"
    assert "buffer kept" not in log, f"stall reconnect should not have happened:\n{log}"

    stats = run.stats_lines()
    assert stats, "no telemetry"
    # No reconnects across the absorbed stall.
    final = stats[-1]
    assert final["reconnects"] == 0, f"unexpected reconnects={final['reconnects']}"
    # No corruption counted — the stream isn't corrupt, just delayed.
    assert final["ccerr"] == 0
    assert final["pcrrej"] == 0

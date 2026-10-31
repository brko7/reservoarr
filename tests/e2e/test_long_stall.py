"""Long-stall e2e: a >25s stall trips #4 (stall watchdog). The reservoir
forces a reconnect WITHOUT flushing (buffer is good — preserve the
cushion the player is draining) and rebuilds front-load on the fresh edge."""
from __future__ import annotations

import pytest

from .harness import run_pipeline


@pytest.mark.e2e
def test_30s_stall_trips_watchdog_no_flush(tmp_path, synth_ts):
    """Stall for 30s starting at edge time 55 (~10s wall — the edge clock
    starts at front=45); #4 should fire, log a 'reconnecting, buffer kept'
    line, and the fetcher should not flush the reservoir. The 45s front banks
    more than the 25s it takes #4 to fire: with less, the reservoir runs dry
    first and the process exits (RESV_DRY_EXIT_S), see test_dry_exit.py."""
    run = run_pipeline(
        tmp_path, synth_ts, rate_bps=300_010, duration_s=85, front_s=45.0,
        stalls=[(55.0, 30.0)],
    )
    log = run.log_text()
    assert "upstream stalled" in log and "buffer kept" in log, (
        f"#4 watchdog should fire on 30s stall (RESV_STALL_S=25):\n{log}"
    )
    # The flush message belongs to corrupt-loop / #5 (flush=True). Must NOT appear.
    assert "flushed reservoir" not in log, (
        f"stall watchdog must NOT flush the reservoir (it's reconnect-without-flush):\n{log}"
    )

    stats = run.stats_lines()
    # At least one reconnect should be observed.
    assert stats and stats[-1]["reconnects"] >= 1, (
        f"expected reconnect after stall watchdog; final stats: {stats[-1]['raw'] if stats else 'none'}"
    )

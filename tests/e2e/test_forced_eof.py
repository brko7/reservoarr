"""Forced-EOF e2e: the CDN closes the connection mid-stream. The fetcher
must reconnect WITHOUT producing an AttributeError / 1s backoff (the v6.1
#6 fix), and the cushion should be preserved through the seam."""
from __future__ import annotations

import pytest

from .harness import run_pipeline


@pytest.mark.e2e
def test_eof_clean_reconnect(tmp_path, synth_ts):
    """Force EOF at t=40s. We should see a clean 'upstream EOF' followed by
    'upstream connected'; no AttributeError, no NoneType errors."""
    run = run_pipeline(
        tmp_path, synth_ts, rate_bps=300_010, duration_s=75, eof_at=40.0,
    )
    log = run.log_text()
    assert "upstream EOF" in log, f"forced EOF should be logged:\n{log}"
    assert "upstream connected" in log, f"reconnect should be logged:\n{log}"
    # The v6.1 #6 fix: this exact error class must never appear.
    assert "AttributeError" not in log, (
        f"#6 regression: AttributeError during forced reconnect:\n{log}"
    )
    assert "NoneType" not in log, (
        f"#6 regression: NoneType error during forced reconnect:\n{log}"
    )

    stats = run.stats_lines()
    assert stats, "no telemetry"
    # An EOF that the fetcher handles via normal upstream-EOF (not force_reconnect)
    # still counts as a reconnect in the telemetry counter.
    assert stats[-1]["reconnects"] >= 1, (
        f"expected reconnect after forced EOF; final stats: {stats[-1]['raw']}"
    )

"""CC-corruption e2e: cdn_sim injects CC-field corruption into served
packets. The #5 detector (log-only by default) must produce 'would-fire:'
lines on the corruption window, and zero false-fires on the clean prefix
or any clean window. Output stays continuous (log-only = no reconnect)."""
from __future__ import annotations

import pytest

from .harness import run_pipeline


@pytest.mark.e2e
def test_cc_corruption_triggers_would_fire(tmp_path, synth_ts):
    """Inject ~5 CC errors per 15s starting at t=20s. By t=50s (two 15s sustain
    windows past corruption start), #5 should log a `would-fire:` line. Output
    must keep flowing (default RESV_TS_RECONNECT=0)."""
    run = run_pipeline(
        tmp_path, synth_ts, rate_bps=300_010, duration_s=80,
        corrupt_from=20.0, corrupt_rate=6,                    # rate >= CC_ERR_PER_WIN(3)
    )
    log = run.log_text()
    assert "would-fire" in log, (
        f"#5 detector should log 'would-fire:' on sustained CC corruption:\n{log}"
    )

    # No actual forced reconnect (log-only is the default). The armed-mode
    # message ends with 'forcing upstream reconnect + buffer flush'; the
    # log-only message contains 'RESV_TS_RECONNECT=0 (log-only)'. Check the
    # armed trailer specifically — the words 'TS corruption detected' appear
    # in both messages, so we can't grep on that.
    assert "forcing upstream reconnect + buffer flush" not in log, (
        f"#5 default is log-only; RESV_TS_RECONNECT=0 must not arm:\n{log}"
    )
    assert "flushed reservoir" not in log, (
        f"#5 default is log-only — no reservoir flush should occur:\n{log}"
    )

    stats = run.stats_lines()
    assert stats, "no telemetry"
    # ccerr must have climbed materially.
    assert stats[-1]["ccerr"] >= 6, (
        f"expected accumulated CC errors after corruption injection; "
        f"final ccerr={stats[-1]['ccerr']}\nlog:\n{log}"
    )


@pytest.mark.e2e
def test_clean_prefix_has_no_false_fires(tmp_path, synth_ts):
    """Run with corruption-from=20s; the windows BEFORE t=20 must not log
    any 'would-fire' line. False positives are what kept #5 log-only this long;
    the test enforces zero of them."""
    run = run_pipeline(
        tmp_path, synth_ts, rate_bps=300_010, duration_s=70,
        corrupt_from=20.0, corrupt_rate=6,
    )
    log_lines = run.log_text().splitlines()
    # First stats line lands ~15s after release-start. Check the first two.
    early_stats = [ln for ln in log_lines if " cushion=" in ln][:2]
    for ln in early_stats:
        # No 'would-fire' should land at or before the corruption window.
        assert "would-fire" not in ln, (
            f"#5 false-fire on clean prefix line:\n{ln}\n\nfull log:\n{run.log_text()}"
        )

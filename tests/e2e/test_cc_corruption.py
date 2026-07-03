"""CC-corruption e2e: cdn_sim injects CC-field corruption into served
packets. The #5 detector (log-only by default) must produce 'would-fire:'
lines on the corruption window, and zero false-fires on the clean prefix.
Output stays continuous (log-only = no reconnect).

Times passed to cdn_sim are on its edge clock (starts at --front=25) —
see the TIMEBASE note in harness.run_pipeline."""
from __future__ import annotations

from datetime import datetime

import pytest

from .harness import run_pipeline

FRONT_S = 25.0


def _line_ts(line: str) -> datetime:
    """Parse the leading '%Y-%m-%dT%H:%M:%S%z' timestamp of a delaybuf.log line."""
    return datetime.strptime(line.split(" ", 1)[0], "%Y-%m-%dT%H:%M:%S%z")


@pytest.mark.e2e
def test_cc_corruption_triggers_would_fire(tmp_path, synth_ts):
    """Inject ~6 CC errors per 15s from the first served byte (corrupt-from=20
    is below the edge clock's start of 25). After two 15s sustain windows, #5
    must log a `would-fire:` line. Output keeps flowing (default
    RESV_TS_RECONNECT=0)."""
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
    """Corruption starts 40 wall-seconds in (edge time front+40). The clean
    prefix must show zero ccerr and zero would-fire lines, and the corrupt
    suffix must still trip the detector — that last assert keeps this test
    from passing vacuously if the injection or the detector silently breaks.
    False positives are what kept #5 log-only this long; the prefix check
    enforces zero of them."""
    onset_wall = 40.0
    run = run_pipeline(
        tmp_path, synth_ts, rate_bps=300_010, duration_s=80,
        front_s=FRONT_S,
        corrupt_from=FRONT_S + onset_wall, corrupt_rate=6,
    )
    log_lines = run.log_text().splitlines()
    assert log_lines, "empty log"
    t_start = _line_ts(log_lines[0])

    fires = [ln for ln in log_lines if "would-fire" in ln]
    assert fires, (
        f"#5 never fired on the corrupt suffix — the test would be vacuous:\n{run.log_text()}"
    )
    for ln in fires:
        fired_after_s = (_line_ts(ln) - t_start).total_seconds()
        assert fired_after_s > onset_wall, (
            f"#5 false-fire at +{fired_after_s:.0f}s, before corruption onset "
            f"(+{onset_wall:.0f}s):\n{ln}\n\nfull log:\n{run.log_text()}"
        )

    clean_stats = [s for s in run.stats_lines()
                   if (_line_ts(s["raw"]) - t_start).total_seconds() <= onset_wall]
    assert clean_stats, "no stats lines landed in the clean prefix"
    assert all(s["ccerr"] == 0 for s in clean_stats), (
        "CC errors counted before corruption onset:\n"
        + "\n".join(s["raw"] for s in clean_stats)
    )

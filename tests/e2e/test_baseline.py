"""Baseline e2e: no stalls, no EOF, no corruption. The pipeline should
build a cushion and hold it, output should be valid H.264+AC3, telemetry
should report zero anomalies. This is the regression check for v6+ —
v5 rode 0-16s cushion under exactly these conditions."""
from __future__ import annotations

import pytest

from .harness import ffprobe_streams, run_pipeline


@pytest.mark.e2e
def test_baseline_builds_cushion(tmp_path, synth_ts):
    """Run for 60s on a clean stream. By the second stats window, cushion
    should be close to TARGET_S (30s), out_ts should be playable H.264+AC3."""
    run = run_pipeline(tmp_path, synth_ts, rate_bps=300_010, duration_s=65)

    stats = run.stats_lines()
    assert stats, f"no telemetry produced\nstderr:\n{run.resv_stderr}\nlog:\n{run.log_text()}"

    # Cushion: by the LAST stats window the script logged, we should be near
    # TARGET_S. Allow generous slack: the controller takes a few seconds to
    # settle, and the test wall-clock might end mid-build. Floor: must be
    # better than v5's ceiling (16s) by a clear margin.
    final = stats[-1]
    assert final["cushion"] >= 20, (
        f"cushion={final['cushion']}s — v6 should reliably hit >=20s. "
        f"All stats:\n  " + "\n  ".join(s["raw"] for s in stats)
    )

    # No anomalies on a clean synthetic stream.
    assert final["ccerr"] == 0, f"ccerr={final['ccerr']} on clean stream"
    assert final["pcrrej"] == 0, f"pcrrej={final['pcrrej']} on clean stream"
    assert final["sync"] == 0, f"sync={final['sync']} on clean stream"
    assert final["reconnects"] == 0, f"reconnects={final['reconnects']} on clean stream"

    # Output must be valid MPEG-TS with H.264 video and AC3 audio.
    streams = ffprobe_streams(run.out_ts)
    codecs = {s.get("codec_name") for s in streams}
    assert "h264" in codecs, f"expected H.264 video, got {codecs}"
    assert "ac3" in codecs, f"expected AC3 audio (per Dispatcharr #1122), got {codecs}"


@pytest.mark.e2e
def test_baseline_uses_pcr_clock(tmp_path, synth_ts):
    """After ~30s the cushion should be reported off the PCR clock, not the
    byte-rate fallback. (pcr) means the parser locked successfully."""
    run = run_pipeline(tmp_path, synth_ts, rate_bps=300_010, duration_s=45)
    stats = run.stats_lines()
    assert stats, "no telemetry produced"
    # Last window must be PCR-sourced — byte fallback is a degraded state.
    assert stats[-1]["src"] == "pcr", (
        f"cushion still byte-sourced at end: {stats[-1]['raw']}\n"
        f"all:\n  " + "\n  ".join(s["raw"] for s in stats)
    )

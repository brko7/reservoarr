from __future__ import annotations

import pytest

from .harness import ffprobe_streams, run_pipeline

RATE_BPS = 300_010


@pytest.mark.e2e
def test_aac_output_is_stereo_48k(tmp_path, synth_ts):
    run = run_pipeline(tmp_path, synth_ts, rate_bps=RATE_BPS, duration_s=40, extra_env={"RESV_AUDIO_CODEC": "aac"})

    audio = [s for s in ffprobe_streams(run.out_ts) if s.get("codec_type") == "audio"]
    assert len(audio) == 1, f"expected one audio stream, got {audio}\nstderr:\n{run.resv_stderr}"
    assert audio[0]["codec_name"] == "aac"
    assert audio[0]["sample_rate"] == "48000"
    assert audio[0]["channels"] == 2

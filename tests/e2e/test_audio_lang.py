"""RESV_AUDIO_LANG end to end: the language tag reaches the output TS."""
from __future__ import annotations

import pytest

from .harness import ffprobe_streams, run_pipeline


@pytest.mark.e2e
def test_audio_language_tag_reaches_the_output(tmp_path, synth_ts):
    run = run_pipeline(tmp_path, synth_ts, rate_bps=300_010, duration_s=20,
                       extra_env={"RESV_AUDIO_LANG": "fra"})
    audio = [s for s in ffprobe_streams(run.out_ts) if s.get("codec_type") == "audio"]
    assert audio, f"no audio in output\nstderr:\n{run.resv_stderr}"
    assert audio[0].get("tags", {}).get("language") == "fra"
    assert audio[0].get("codec_name") == "ac3"

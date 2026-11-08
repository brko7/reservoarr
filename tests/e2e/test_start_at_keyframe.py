"""RESV_START_AT_KEYFRAME (#32) through the real ffmpeg: on an 8s-GOP channel a
tune that lands just after a keyframe gives ffmpeg's 500KB probe no SPS, and
the output carries video with no size (ffprobe width=0; a Jellyfin remux keeps
the audio only). Started at the next keyframe, the same tune probes 1280x720."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from .harness import run_pipeline

TOOLS = Path(__file__).resolve().parents[2] / "tools"


@pytest.fixture(scope="module")
def long_gop_ts(tmp_path_factory):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not found")
    out = tmp_path_factory.mktemp("gop") / "gop8.ts"
    env = {**os.environ, "SYNTH_DUR": "60", "SYNTH_GOP": "200"}
    subprocess.run(["bash", str(TOOLS / "make_synth_ts.sh"), str(out)], env=env, check=True,
                   stdout=subprocess.DEVNULL)
    return out


def probed_size(path):
    """What a downstream probe with default settings sees, as Jellyfin's does."""
    out = subprocess.run(["ffprobe", "-v", "quiet", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout
    return out.strip().splitlines()[0] if out.strip() else ""


# Off, the same tune probes 0,0 on this fixture today. Strict xfail, not an
# assert on 0,0: an ffmpeg that copes with it should fail the run here, so the
# tunable can be reconsidered. Invariant #12's 8.3s-GOP channel opened with video
# without +nobuffer: whether the probe finds an SPS depends on how much of the GOP
# 500KB covers, so on the bitrate as much as on the GOP length.
OFF = pytest.mark.xfail(strict=True, reason="#32: released mid-GOP, the 500KB probe has no SPS")


@pytest.mark.e2e
@pytest.mark.parametrize("on", [pytest.param("0", marks=OFF), "1"])
def test_long_gop_tune_probes_only_from_a_keyframe(tmp_path, long_gop_ts, on):
    run = run_pipeline(tmp_path, long_gop_ts, rate_bps=300_000, duration_s=25, front_s=10.0,
                       extra_env={"RESV_START_AT_KEYFRAME": on})
    log = run.log_text()
    assert ("starting at the first video keyframe" in log) == (on == "1"), log
    assert probed_size(run.out_ts) == "1280,720", log
    assert "Traceback" not in run.resv_stderr

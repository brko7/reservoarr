from __future__ import annotations

import pytest

from .harness import ffprobe_video_pts, run_pipeline


def two_processes(tmp_path, synth_ts, ts_channel):
    logs = tmp_path / "logs"
    runs = []
    for name in ("first", "second"):
        work = tmp_path / name
        work.mkdir()
        runs.append(run_pipeline(work, synth_ts, rate_bps=300_010, duration_s=25,
                                 ts_channel=ts_channel, log_dir=logs))
    return runs


@pytest.mark.e2e
def test_the_next_process_continues_the_timeline(tmp_path, synth_ts):
    first, second = two_processes(tmp_path, synth_ts, "9")
    before, after = ffprobe_video_pts(first.out_ts), ffprobe_video_pts(second.out_ts)
    assert before and after, f"no video out\n{first.resv_stderr}\n{second.resv_stderr}"

    assert min(after) > max(before), (
        f"the second process went back in time: {max(before):.3f} -> {min(after):.3f}"
    )
    assert min(after) - max(before) < 10, (
        f"the seam left a gap: {max(before):.3f} -> {min(after):.3f}"
    )
    assert "ts timeline: channel 9 starts at" in second.log_text()


@pytest.mark.e2e
def test_without_the_tunable_every_process_starts_again_from_zero(tmp_path, synth_ts):
    first, second = two_processes(tmp_path, synth_ts, None)
    before, after = ffprobe_video_pts(first.out_ts), ffprobe_video_pts(second.out_ts)
    assert before and after

    assert min(after) < max(before)
    assert "ts timeline" not in second.log_text()

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from .harness import RESERVOIR, ffprobe_video_pts, run_pipeline

TS_WRAP_S = (1 << 33) / 90000.0


def raw_pes_pts(path, video):
    """Every PES PTS as written, in seconds, in file order: ffprobe would hide
    the 33-bit wrap from us. The muxer's first PIDs: video 0x100, audio 0x101."""
    want = 0x100 if video else 0x101
    data = path.read_bytes()
    out = []
    for i in range(0, len(data) - 187, 188):
        pid = (data[i + 1] & 0x1F) << 8 | data[i + 2]
        if pid != want or not data[i + 1] & 0x40 or not data[i + 3] & 0x10:
            continue
        p = i + 4 + (1 + data[i + 4] if data[i + 3] & 0x20 else 0)
        if data[p:p + 3] != b"\x00\x00\x01" or not data[p + 7] & 0x80:
            continue
        b = data[p + 9:p + 14]
        pts = ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1
        out.append(pts / 90000.0)
    return out


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


def raw_pcr(path):
    """Every PCR on the video PID, as written, in seconds."""
    data = path.read_bytes()
    out = []
    for i in range(0, len(data) - 187, 188):
        pid = (data[i + 1] & 0x1F) << 8 | data[i + 2]
        if pid != 0x100 or not data[i + 3] & 0x20 or data[i + 4] < 7 or not data[i + 5] & 0x10:
            continue
        b = data[i + 6:i + 11]
        out.append((b[0] << 25 | b[1] << 17 | b[2] << 9 | b[3] << 1 | b[4] >> 7) / 90000.0)
    return out


def wrapped(seconds):
    return (seconds + TS_WRAP_S / 2) % TS_WRAP_S - TS_WRAP_S / 2


@pytest.mark.e2e
def test_without_a_channel_every_process_starts_on_the_wall_clock(tmp_path, synth_ts):
    """No state file and no channel id. The next process of an immediate switch
    steps back by no more than what the old output ran ahead of the wall clock:
    the 5s head start, against the whole session length before 6.7.0."""
    first, second = two_processes(tmp_path, synth_ts, None)
    before, after = ffprobe_video_pts(first.out_ts), ffprobe_video_pts(second.out_ts)
    assert before and after

    step = wrapped(min(after) - max(before))
    assert -8 < step < 3, f"seam {max(before):.3f} -> {min(after):.3f}"
    assert "output timestamps start on the wall clock" in second.log_text()
    assert list((tmp_path / "logs").glob("pts-*")) == [], "no state file without a channel"


@pytest.mark.e2e
def test_after_an_outage_the_next_process_moves_time_forward(tmp_path, synth_ts):
    logs = tmp_path / "logs"
    runs = []
    for name, outages in (("first", [(20.0, 100.0)]), ("second", None)):
        work = tmp_path / name
        work.mkdir()
        runs.append(run_pipeline(work, synth_ts, rate_bps=300_010, duration_s=40 if outages else 15,
                                 front_s=0.0, outages=outages, log_dir=logs))
    assert runs[0].exited_by_itself, runs[0].log_text()
    before, after = ffprobe_video_pts(runs[0].out_ts), ffprobe_video_pts(runs[1].out_ts)
    step = wrapped(min(after) - max(before))
    assert 0 < step < 15, f"seam {max(before):.3f} -> {min(after):.3f}"


@pytest.mark.e2e
def test_the_off_switch_starts_every_process_from_zero_again(tmp_path, synth_ts):
    logs = tmp_path / "logs"
    runs = []
    for name in ("first", "second"):
        work = tmp_path / name
        work.mkdir()
        runs.append(run_pipeline(work, synth_ts, rate_bps=300_010, duration_s=25, log_dir=logs,
                                 extra_env={"RESV_TS_WALLCLOCK": "0"}))
    before, after = ffprobe_video_pts(runs[0].out_ts), ffprobe_video_pts(runs[1].out_ts)
    assert before and after
    assert min(after) < max(before)
    assert min(after) < 10
    assert "wall clock" not in runs[1].log_text()
    assert "ts timeline" not in runs[1].log_text()


@pytest.mark.e2e
@pytest.mark.parametrize("codec", ["ac3", "aac"])
def test_output_crossing_the_33_bit_wrap_keeps_video_and_audio_together(tmp_path, synth_ts, codec):
    """Sessions now cross the wrap within 26.5h. Same ffmpeg arguments the
    process uses, offset 8s under the wrap, on 25s of content."""
    env = {**os.environ, "RESV_AUDIO_CODEC": codec, "RESV_LOG_DIR": str(tmp_path)}
    env.setdefault("RESV_FFMPEG_BIN", shutil.which("ffmpeg") or "ffmpeg")
    code = ("import runpy,sys,json;sys.argv=['r','http://offline/x'];"
            f"m=runpy.run_path({str(RESERVOIR)!r},run_name='loaded');"
            f"print(json.dumps(m['ffmpeg_cmd']({TS_WRAP_S - 8.0!r})))")
    cmd = json.loads(subprocess.check_output([sys.executable, "-c", code], env=env, text=True))
    assert cmd[-3:-1] == ["-output_ts_offset", f"{TS_WRAP_S - 8.0:.6f}"]
    out = tmp_path / "wrapped.ts"
    with open(synth_ts, "rb") as src, open(out, "wb") as dst:
        subprocess.run(cmd, input=src.read(300_010 * 25), stdout=dst, stderr=subprocess.DEVNULL, check=True)

    video, audio, pcr = raw_pes_pts(out, video=True), raw_pes_pts(out, video=False), raw_pcr(out)
    assert video and audio and pcr
    for name, stamps in (("video", video), ("audio", audio), ("pcr", pcr)):
        assert max(stamps) > TS_WRAP_S - 8.0 - 1, name                 # starts just under the wrap
        assert min(stamps) < 16.0, f"{name} never wrapped"
        unwrapped = [wrapped(t - stamps[0]) + stamps[0] for t in stamps]
        steps = [b - a for a, b in zip(unwrapped, unwrapped[1:], strict=False)]
        assert max(abs(x) for x in steps) < 1.0, f"{name} jumped at the wrap"
    assert abs(wrapped(video[0] - audio[0])) < 3.0                     # still the same timeline
    assert abs(wrapped(video[-1] - audio[-1])) < 3.0

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from .harness import ffprobe_audio_packets, ffprobe_streams, ffprobe_video_pts, run_pipeline

RATE_BPS = 300_010
AUDIO_PID = 0x101
GAP_FROM_S, GAP_TO_S = 20, 22
JUMP_AT_S, JUMP_S = 40, 300
STOP_AT_S = 41
FLOWING_PAST_S = 8


def derive(synth_ts: Path, out: Path, audio_filter: str) -> Path:
    ffmpeg = os.environ.get("RESV_FFMPEG_BIN") or shutil.which("ffmpeg") or "ffmpeg"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-t", "120", "-i", str(synth_ts),
         "-map", "0:v", "-map", "0:a", "-c:v", "copy", "-af", audio_filter,
         "-c:a", "aac", "-b:a", "128k", "-muxrate", "2400k", "-f", "mpegts", str(out)],
        check=True,
    )
    return out


def read_pes_time(b: bytes) -> int:
    return ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1


def write_pes_time(prefix: int, value: int) -> bytes:
    value &= (1 << 33) - 1
    return bytes([
        (prefix & 0xF0) | ((value >> 30) & 7) << 1 | 1,
        (value >> 22) & 0xFF,
        ((value >> 15) & 0x7F) << 1 | 1,
        (value >> 7) & 0xFF,
        (value & 0x7F) << 1 | 1,
    ])


def shift_audio_in_place(src: Path, dst: Path, after_s: float, shift_s: float) -> int:
    data = bytearray(src.read_bytes())
    first = None
    shifted = 0
    for off in range(0, len(data) - 187, 188):
        packet = data[off:off + 188]
        if packet[0] != 0x47 or not packet[1] & 0x40 or ((packet[1] & 0x1F) << 8 | packet[2]) != AUDIO_PID:
            continue
        start = 4 + (1 + packet[4] if (packet[3] >> 4) & 2 else 0)
        if packet[start:start + 3] != b"\x00\x00\x01" or not packet[start + 7] & 0x80:
            continue
        pts_at = off + start + 9
        pts = read_pes_time(data[pts_at:pts_at + 5])
        first = pts if first is None else first
        if pts - first < after_s * 90000:
            continue
        data[pts_at:pts_at + 5] = write_pes_time(data[pts_at], pts + int(shift_s * 90000))
        if packet[start + 7] & 0x40:
            dts_at = pts_at + 5
            data[dts_at:dts_at + 5] = write_pes_time(data[dts_at], read_pes_time(data[dts_at:dts_at + 5]) + int(shift_s * 90000))
        shifted += 1
    dst.write_bytes(bytes(data))
    return shifted


@pytest.fixture(scope="module")
def gap_ts(synth_ts, tmp_path_factory):
    return derive(synth_ts, tmp_path_factory.mktemp("audio") / "gap.ts",
                  f"aselect='not(between(t,{GAP_FROM_S},{GAP_TO_S}))'")


@pytest.fixture(scope="module")
def stop_ts(synth_ts, tmp_path_factory):
    return derive(synth_ts, tmp_path_factory.mktemp("audio") / "stop.ts", f"atrim=end={STOP_AT_S}")


@pytest.fixture(scope="module")
def jump_ts(synth_ts, tmp_path_factory):
    folder = tmp_path_factory.mktemp("audio")
    plain = derive(synth_ts, folder / "plain.ts", "anull")
    jump = folder / "jump.ts"
    assert shift_audio_in_place(plain, jump, JUMP_AT_S, JUMP_S) > 0
    return jump


def largest_gap(packets: list[tuple[float, float]]) -> float:
    return max(b[0] - (a[0] + a[1]) for a, b in zip(packets, packets[1:], strict=False))


@pytest.mark.e2e
def test_aac_output_is_stereo_48k(tmp_path, synth_ts):
    run = run_pipeline(tmp_path, synth_ts, rate_bps=RATE_BPS, duration_s=40, extra_env={"RESV_AUDIO_CODEC": "aac"})

    audio = [s for s in ffprobe_streams(run.out_ts) if s.get("codec_type") == "audio"]
    assert len(audio) == 1, f"expected one audio stream, got {audio}\nstderr:\n{run.resv_stderr}"
    assert audio[0]["codec_name"] == "aac"
    assert audio[0]["sample_rate"] == "48000"
    assert audio[0]["channels"] == 2


@pytest.mark.e2e
def test_an_audio_gap_reaches_the_output_without_fill(tmp_path, gap_ts):
    run = run_pipeline(tmp_path, gap_ts, rate_bps=RATE_BPS, duration_s=50)

    gap = largest_gap(ffprobe_audio_packets(run.out_ts))
    assert gap > 1.5, f"the {GAP_TO_S - GAP_FROM_S}s hole should pass through untouched, largest gap {gap:.3f}s"


@pytest.mark.e2e
@pytest.mark.parametrize("codec", ["ac3", "aac"])
def test_fill_gaps_closes_the_hole(tmp_path, gap_ts, codec):
    run = run_pipeline(
        tmp_path, gap_ts, rate_bps=RATE_BPS, duration_s=50,
        extra_env={"RESV_AUDIO_CODEC": codec, "RESV_AUDIO_FILL_GAPS": "1"},
    )

    gap = largest_gap(ffprobe_audio_packets(run.out_ts))
    assert gap < 0.1, f"{codec}: largest gap {gap:.3f}s\nstderr:\n{run.resv_stderr}"


@pytest.mark.e2e
def test_fill_gaps_keeps_video_flowing_when_audio_stops(tmp_path, stop_ts):
    run = run_pipeline(
        tmp_path, stop_ts, rate_bps=RATE_BPS, duration_s=60,
        extra_env={"RESV_AUDIO_CODEC": "aac", "RESV_AUDIO_FILL_GAPS": "1"},
    )

    video = ffprobe_video_pts(run.out_ts)
    assert video[-1] > STOP_AT_S + FLOWING_PAST_S, f"video stopped at {video[-1]:.1f}s, audio ended at {STOP_AT_S}s"


@pytest.mark.e2e
def test_fill_gaps_does_not_flood_a_far_jump_with_silence(tmp_path, jump_ts):
    run = run_pipeline(
        tmp_path, jump_ts, rate_bps=RATE_BPS, duration_s=60,
        extra_env={"RESV_AUDIO_CODEC": "aac", "RESV_AUDIO_FILL_GAPS": "1"},
    )

    packets = ffprobe_audio_packets(run.out_ts)
    video = ffprobe_video_pts(run.out_ts)
    audio_end = packets[-1][0] + packets[-1][1]
    assert video[-1] > JUMP_AT_S + FLOWING_PAST_S, f"video stopped at {video[-1]:.1f}s"
    assert abs(audio_end - video[-1]) < 1.0, (
        f"audio ends at {audio_end:.1f}s, video at {video[-1]:.1f}s after a {JUMP_S}s jump"
    )
    assert largest_gap(packets) < 0.1

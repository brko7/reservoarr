from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RESERVOIR_PATH = REPO_ROOT / "reservoarr.py"


def load(tmp_log_dir, **env):
    os.environ["RESV_LOG_DIR"] = str(tmp_log_dir)
    os.environ.update(env)
    sys.argv = ["reservoarr", "http://offline/unit-test"]
    spec = importlib.util.spec_from_file_location("resv_timeline", str(RESERVOIR_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for key in env:
        os.environ.pop(key, None)
    return mod


def pes_packet(pts, pid=0x100, adaptation=0):
    header = bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x30 if adaptation else 0x10])
    field = bytes([adaptation]) + b"\x00" * adaptation if adaptation else b""
    stamp = bytes([
        0x21 | ((pts >> 29) & 0x0E),
        (pts >> 22) & 0xFF,
        0x01 | ((pts >> 14) & 0xFE),
        (pts >> 7) & 0xFF,
        0x01 | ((pts << 1) & 0xFE),
    ])
    pes = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + stamp
    return (header + field + pes).ljust(188, b"\xff")


def plain_packet(pid=0x100):
    return bytes([0x47, pid >> 8, pid & 0xFF, 0x10]).ljust(188, b"\xff")


def write_state(resv, end, wall):
    Path(resv.timeline_path()).write_text(json.dumps({"end": end, "wall": wall}))


class FakeFfmpeg:
    def __init__(self, tmp_path, data):
        source = tmp_path / "ffmpeg-stdout.ts"
        source.write_bytes(data)
        self.stdout = io.FileIO(str(source), "r")


def test_timeline_is_opt_in(tmp_path):
    resv = load(tmp_path)
    assert resv.TS_CHANNEL == ""
    assert resv.ffmpeg_cmd() == resv.FFMPEG_CMD
    assert "-output_ts_offset" not in resv.FFMPEG_CMD


def test_offset_goes_right_before_the_output(tmp_path):
    resv = load(tmp_path, RESV_TS_CHANNEL="42")
    cmd = resv.ffmpeg_cmd(1234.5)
    assert cmd[-3:] == ["-output_ts_offset", "1234.500000", "pipe:1"]
    assert cmd[:-3] == resv.FFMPEG_CMD[:-1]


def test_channel_id_cannot_leave_the_log_dir(tmp_path):
    resv = load(tmp_path, RESV_TS_CHANNEL="../12/x y")
    assert resv.TS_CHANNEL == "12xy"
    assert Path(resv.timeline_path()).parent == tmp_path


def test_first_process_starts_on_the_wall_clock(tmp_path):
    resv = load(tmp_path, RESV_TS_CHANNEL="7")
    timeline = resv.Timeline(resv.timeline_path(), 1_000_000.0)
    assert timeline.origin == 1_000_000.0
    assert timeline.offset == 1_000_000.0 % resv.TS_WRAP_S


def test_next_process_continues_after_the_projected_end(tmp_path):
    resv = load(tmp_path, RESV_TS_CHANNEL="7")
    write_state(resv, end=1_000_130.0, wall=1_000_120.0)
    timeline = resv.Timeline(resv.timeline_path(), 1_000_125.0)
    assert timeline.origin == 1_000_130.0 + 5.0 + resv.TS_SEAM_S


def test_a_stale_state_never_pulls_the_origin_back(tmp_path):
    resv = load(tmp_path, RESV_TS_CHANNEL="7")
    write_state(resv, end=10.0, wall=1_000_000.0)
    timeline = resv.Timeline(resv.timeline_path(), 1_000_000.0)
    assert timeline.origin == 1_000_000.0


def test_a_corrupt_state_falls_back_to_the_wall_clock(tmp_path):
    resv = load(tmp_path, RESV_TS_CHANNEL="7")
    Path(resv.timeline_path()).write_text("{not json")
    assert resv.Timeline(resv.timeline_path(), 500.0).origin == 500.0


def test_pes_pts_reads_the_stamp(tmp_path):
    resv = load(tmp_path)
    for pts in (0, 90000, 123456789, resv.PTS_WRAP - 1):
        assert resv.pes_pts(pes_packet(pts), 0) == pts
    assert resv.pes_pts(pes_packet(90000, adaptation=7), 0) == 90000


def test_pes_pts_ignores_packets_without_a_pes_start(tmp_path):
    resv = load(tmp_path)
    assert resv.pes_pts(plain_packet(), 0) is None
    pat = bytes([0x47, 0x40, 0x00, 0x10, 0x00, 0x00, 0xB0, 0x0D]).ljust(188, b"\xff")
    assert resv.pes_pts(pat, 0) is None


def test_end_follows_the_highest_output_pts(tmp_path):
    resv = load(tmp_path, RESV_TS_CHANNEL="7")
    timeline = resv.Timeline(resv.timeline_path(), 200_000.0)
    base = int(timeline.offset * 90000)
    for delta in (0, 90000, 45000, 180000):
        timeline.observe(base + delta)
    assert abs(timeline.end() - (200_000.0 + 2.0)) < 1e-4


def test_end_survives_the_33_bit_wrap(tmp_path):
    resv = load(tmp_path, RESV_TS_CHANNEL="7")
    now = resv.TS_WRAP_S * 3 - 1.0
    timeline = resv.Timeline(resv.timeline_path(), now)
    timeline.observe(int(timeline.offset * 90000))
    timeline.observe(90000)
    assert abs(timeline.end() - (now + 2.0)) < 1e-3


def test_a_first_pts_already_past_the_wrap_counts_as_wrapped(tmp_path):
    resv = load(tmp_path, RESV_TS_CHANNEL="7")
    now = resv.TS_WRAP_S * 3 - 0.5
    timeline = resv.Timeline(resv.timeline_path(), now)
    timeline.observe(45000)
    assert abs(timeline.end() - (now + 1.0)) < 1e-3


def test_save_keeps_the_later_of_two_writers(tmp_path):
    resv = load(tmp_path, RESV_TS_CHANNEL="7")
    newer = resv.Timeline(resv.timeline_path(), 1000.0)
    newer.observe(int(newer.offset * 90000) + 90000 * 30)
    newer.save(1000.0)
    older = resv.Timeline(resv.timeline_path(), 1000.0)
    older.origin, older.offset = 900.0, 900.0
    older.observe(int(900.0 * 90000) + 90000 * 10)
    older.save(1001.0)
    state = json.loads(Path(resv.timeline_path()).read_text())
    assert state == {"end": 1030.0, "wall": 1000.0}


def test_relay_passes_every_byte_and_tracks_pts(tmp_path):
    resv = load(tmp_path, RESV_TS_CHANNEL="7")
    timeline = resv.Timeline(resv.timeline_path(), 5000.0)
    base = int(timeline.offset * 90000)
    stream = b"".join([pes_packet(base), plain_packet(), pes_packet(base + 900000), plain_packet()])
    out = io.BytesIO()

    resv.stdout_relay(FakeFfmpeg(tmp_path, stream), timeline, out)

    assert out.getvalue() == stream
    assert abs(timeline.end() - 5010.0) < 1e-4


def test_relay_finds_pts_across_read_boundaries(tmp_path):
    resv = load(tmp_path, RESV_TS_CHANNEL="7")
    timeline = resv.Timeline(resv.timeline_path(), 5000.0)
    base = int(timeline.offset * 90000)
    stream = b"".join([plain_packet()] * 600 + [pes_packet(base + 90000 * 4)])
    out = io.BytesIO()

    resv.stdout_relay(FakeFfmpeg(tmp_path, stream), timeline, out)

    assert out.getvalue() == stream
    assert abs(timeline.end() - 5004.0) < 1e-4

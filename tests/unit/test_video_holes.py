from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE = Path(__file__).resolve().parent.parent.parent / "fixtures" / "synth.ts"
VIDEO_PID = 0x100
PMT_PID = 0x1000


def ts_packet(pid, payload, pusi=True, cc=0, discontinuity=False):
    header = bytes([0x47, (0x40 if pusi else 0) | pid >> 8, pid & 0xFF])
    if discontinuity:
        body = bytes([1, 0x80]) + payload
        header += bytes([0x30 | cc])
    else:
        body = payload
        header += bytes([0x10 | cc])
    packet = header + body
    return packet + b"\xff" * (188 - len(packet))


def pat():
    section = bytes([0x00, 0xB0, 13, 0, 1, 0xC1, 0, 0, 0, 1, 0xE0 | PMT_PID >> 8, PMT_PID & 0xFF]) + b"\0\0\0\0"
    return ts_packet(0, b"\x00" + section)


def pmt(stream_type=0x1B):
    es = bytes([0x0F, 0xE1, 0x01, 0xF0, 0x00, stream_type, 0xE0 | VIDEO_PID >> 8, VIDEO_PID & 0xFF, 0xF0, 0x00])
    body = bytes([0, 1, 0xC1, 0, 0, 0xE0 | VIDEO_PID >> 8, VIDEO_PID & 0xFF, 0xF0, 0x00]) + es
    section = bytes([0x02, 0xB0, len(body) + 4]) + body + b"\0\0\0\0"
    return ts_packet(PMT_PID, b"\x00" + section)


def pes_time_bytes(prefix, ticks):
    return bytes([
        prefix << 4 | (ticks >> 30 & 0x7) << 1 | 1,
        ticks >> 22 & 0xFF,
        (ticks >> 15 & 0x7F) << 1 | 1,
        ticks >> 7 & 0xFF,
        (ticks & 0x7F) << 1 | 1,
    ])


def video_pes(seconds, with_dts=True, **kwargs):
    ticks = int(seconds * 90000)
    if with_dts:
        header = bytes([0x80, 0xC0, 10]) + pes_time_bytes(3, ticks + 3600) + pes_time_bytes(1, ticks)
    else:
        header = bytes([0x80, 0x80, 5]) + pes_time_bytes(2, ticks)
    return ts_packet(VIDEO_PID, b"\x00\x00\x01\xE0\x00\x00" + header + b"\x00\x00\x00\x01", **kwargs)


def stream(times, **kwargs):
    return pat() + pmt() + b"".join(video_pes(t, **kwargs) for t in times)


def frames(start, end, fps=25):
    return [start + n / fps for n in range(round((end - start) * fps))]


def test_the_video_pid_comes_from_the_pmt(resv):
    resv.parser.feed(pat() + pmt() + ts_packet(0x1FFF, b""))

    assert resv.parser.pmt_pid == PMT_PID
    assert resv.parser.video_pid == VIDEO_PID


def test_a_continuous_stream_has_no_holes(resv):
    resv.parser.feed(stream(frames(100, 115)))

    assert resv.parser.take_video_holes_pct() == 0


def test_four_seconds_missing_out_of_eight_is_about_half(resv):
    times = [t for t in frames(100, 132) if (t - 100) % 8 < 4]
    resv.parser.feed(stream(times))

    assert 40 <= resv.parser.take_video_holes_pct() <= 50


def test_the_window_resets_after_each_read(resv):
    resv.parser.feed(stream(frames(100, 104) + frames(106, 110)))
    assert resv.parser.take_video_holes_pct() > 0

    resv.parser.feed(b"".join(video_pes(t) for t in frames(110, 125)))

    assert resv.parser.take_video_holes_pct() == 0


def test_pts_is_used_when_there_is_no_dts(resv):
    resv.parser.feed(stream(frames(100, 104) + frames(106, 110), with_dts=False))

    assert resv.parser.take_video_holes_pct() == 20


def test_a_reconnect_seam_is_not_a_hole(resv):
    resv.parser.feed(stream(frames(100, 110)))
    resv.parser.on_reconnect()
    resv.parser.feed(b"".join(video_pes(t) for t in frames(130, 140)))

    assert resv.parser.take_video_holes_pct() == 0


def test_a_flagged_discontinuity_is_not_a_hole(resv):
    resv.parser.feed(stream(frames(100, 110)))
    resv.parser.feed(video_pes(200, discontinuity=True) + b"".join(video_pes(t) for t in frames(200.04, 210)))

    assert resv.parser.take_video_holes_pct() == 0


def test_a_far_jump_is_not_a_hole(resv):
    resv.parser.feed(stream(frames(100, 110) + frames(400, 410)))

    assert resv.parser.take_video_holes_pct() == 0


def test_no_video_reads_zero(resv):
    resv.parser.feed(pat() + pmt() + ts_packet(0x1FFF, b""))

    assert resv.parser.take_video_holes_pct() == 0


def without_video(data, video_pid, start_s, end_s, resv):
    out = bytearray()
    first = None
    dropping = False
    for off in range(0, len(data) - 187, 188):
        packet = data[off:off + 188]
        pid = (packet[1] & 0x1F) << 8 | packet[2]
        if pid == video_pid and packet[1] & 0x40:
            afc = packet[3] >> 4 & 0x3
            start = 4 + (1 + packet[4] if afc & 0x2 else 0)
            t = resv.pes_time(packet, start + 9) / 90000.0
            first = t if first is None else first
            dropping = start_s <= t - first < end_s
        if not (pid == video_pid and dropping):
            out += packet
    return bytes(out)


def test_the_synthetic_fixture_has_no_holes(resv):
    if not FIXTURE.exists():
        pytest.skip(f"{FIXTURE} not generated; run `just fixture` first")
    resv.parser.feed(FIXTURE.read_bytes()[: 188 * 60000])

    assert resv.parser.video_pid == VIDEO_PID
    assert resv.parser.take_video_holes_pct() == 0


def test_dropping_six_seconds_of_video_from_the_fixture_shows_up(resv):
    if not FIXTURE.exists():
        pytest.skip(f"{FIXTURE} not generated; run `just fixture` first")
    data = without_video(FIXTURE.read_bytes()[: 188 * 60000], VIDEO_PID, 10, 16, resv)

    resv.parser.feed(data)

    assert resv.parser.take_video_holes_pct() >= 10

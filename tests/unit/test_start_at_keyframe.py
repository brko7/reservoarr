"""RESV_START_AT_KEYFRAME (#32): ffmpeg's input starts at the first video packet
with random_access_indicator, behind the last PAT and PMT. Released mid-GOP,
ffmpeg's probe window has no SPS on a long GOP and writes the video with no
size; a downstream remux (Jellyfin) then keeps the audio only."""
from __future__ import annotations

import time

NULL_PACKET = b"\x47\x1f\xff\x10" + b"\xff" * 184
VIDEO_PID = 0x100


def pid_of(packet):
    return (packet[1] & 0x1F) << 8 | packet[2]


def is_rap(packet):
    return bool(packet[1] & 0x40 and packet[3] & 0x20 and packet[4] and packet[5] & 0x40)


def mid_gop(synth_ts):
    """The fixture from one packet after its second keyframe: a tune that
    lands inside a GOP."""
    data = synth_ts.read_bytes()
    raps = [i for i in range(0, len(data) - 187, 188)
            if pid_of(data[i:i + 188]) == VIDEO_PID and is_rap(data[i:i + 188])]
    start = raps[1] + 188
    return data[start:start + 4 * 1024 * 1024], raps[2] - start


def test_parser_finds_the_first_keyframe_and_the_tables_ahead_of_it(resv, synth_ts):
    data, rap = mid_gop(synth_ts)
    p = resv.TsParser()
    for i in range(0, len(data), 188 * 512):
        p.feed(data[i:i + 188 * 512])
    assert p.first_rap == rap
    assert pid_of(p.tables[0]) == 0
    assert pid_of(p.tables[p.pmt_pid]) == p.pmt_pid
    assert all(len(t) == 188 for t in p.tables.values())


def test_the_release_starts_at_the_keyframe_behind_pat_and_pmt(resv, stream, synth_ts, capsys):
    data, rap = mid_gop(synth_ts)
    stream.upstream.ingest(data)
    head = stream.start_at_keyframe(time.monotonic())
    assert [pid_of(head[i:i + 188]) for i in range(0, len(head), 188)] == [0, stream.parser.pmt_pid]
    first, released = stream.reservoir.take(timeout=0)
    assert first[:188] == data[rap:rap + 188]
    assert released == rap + len(first)
    assert "starting at the first video keyframe" in capsys.readouterr().err


def test_no_keyframe_in_time_releases_from_the_start(resv, stream, capsys):
    resv.KEYFRAME_WAIT_S = 0.2
    stream.upstream.ingest(NULL_PACKET * 100)
    assert stream.start_at_keyframe(time.monotonic()) == b""
    first, _ = stream.reservoir.take(timeout=0)
    assert first[:188] == NULL_PACKET
    assert "no video keyframe" in capsys.readouterr().err


def test_off_by_default(resv):
    assert resv.START_AT_KEYFRAME is False

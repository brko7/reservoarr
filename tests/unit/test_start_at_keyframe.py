"""RESV_START_AT_KEYFRAME (#32): ffmpeg's input starts at the first video packet
with random_access_indicator, behind the last PAT and PMT. Released mid-GOP,
ffmpeg's probe window has no SPS on a long GOP and writes the video with no
size; a downstream remux (Jellyfin) then keeps the audio only."""
from __future__ import annotations

import io
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
    assert all(len(t) and len(t) % 188 == 0 for t in p.tables.values())


def test_the_release_starts_at_the_keyframe_behind_pat_and_pmt(resv, stream, synth_ts, capsys):
    data, rap = mid_gop(synth_ts)
    stream.upstream.ingest(data)
    head = stream.start_at_keyframe(time.monotonic())
    assert [pid_of(head[i:i + 188]) for i in range(0, len(head), 188)] == [0, stream.parser.pmt_pid]
    assert stream.start_s == stream.parser.released_pcr(rap) > 0
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


def test_ffmpeg_reads_pat_pmt_then_the_keyframe(resv, stream, synth_ts):
    """What reaches ffmpeg's stdin, not just what start_at_keyframe returns."""
    data, rap = mid_gop(synth_ts)
    data = data[:rap + 188 * 400]                                     # under the head start: no pacing sleep
    stream.upstream.ingest(data)
    head = stream.start_at_keyframe(time.monotonic())
    stream.reservoir.close()
    ff = FakeFfmpeg()
    stream.pump(ff, None, head)
    out = ff.stdin.getvalue()
    assert [pid_of(out[i:i + 188]) for i in range(0, 3 * 188, 188)] == [0, stream.parser.pmt_pid, VIDEO_PID]
    assert out[2 * 188:3 * 188] == data[rap:rap + 188]
    assert out[2 * 188:] == data[rap:]


def test_the_head_start_counts_from_the_keyframe(resv):
    """After a skip, released_s starts at the keyframe's content position, not
    at 0: the first HEADSTART_S of the release still go out unpaced."""
    pacer = resv.Pacer(resv.TsParser(), None, start_s=20.0)
    pacer.pace(1000, resv.Level(1e6, 1e6, 10.0, 10.0, released_s=22.0))
    assert pacer.headstart_bytes == 1000                              # head start, not paced
    pacer = resv.Pacer(resv.TsParser(), None)
    pacer.pace(1000, resv.Level(1e6, 1e6, 10.0, 10.0, released_s=22.0))
    assert pacer.headstart_bytes == 0                                 # default: unchanged


def test_a_flagged_packet_without_an_sps_is_not_a_keyframe(parser):
    """#32: a provider set random_access_indicator on packets with no IDR."""
    stream = pat() + pmt(1) + video(rap=True, nal=0x41) + video(rap=True, nal=0x67)
    parser.feed(stream)
    assert parser.first_rap == 3 * 188


def test_hevc_keyframes_open_on_a_vps(parser):
    parser.feed(pat() + pmt(1, vtype=0x24) + video(rap=True, nal=0x02) + video(rap=True, nal=0x40))
    assert parser.first_rap == 3 * 188


def test_a_pmt_longer_than_a_packet_is_kept_whole(parser):
    tables = pat() + pmt(60)                                          # 60 audio entries: two packets
    assert len(tables) == 3 * 188
    parser.feed(tables + video(rap=True, nal=0x67))
    assert parser.first_rap == len(tables)
    assert parser.tables[parser.pmt_pid] == tables[188:]


def test_a_shutdown_during_the_wait_is_not_a_missing_keyframe(resv, stream, capsys):
    stream.upstream.ingest(NULL_PACKET * 100)
    stream.stop.set()
    assert stream.start_at_keyframe(time.monotonic()) == b""
    assert "no video keyframe" not in capsys.readouterr().err


def test_skip_to_drops_only_what_is_still_buffered(resv, stream):
    res = stream.reservoir
    res.put(bytes(range(188)) * 10)
    first, released = res.take(timeout=0)
    res.skip_to(released + 188)                                       # an offset, not a count
    nxt, _ = res.take(timeout=0)
    assert nxt[:188] == (bytes(range(188)) * 10)[released + 188:released + 376]


class FakeFfmpeg:
    def __init__(self):
        self.stdin = io.BytesIO()


PMT_PID = 0x1000


def packet(pid, payload, pusi=False, af=b""):
    head = bytes([0x47, (0x40 if pusi else 0) | pid >> 8, pid & 0xFF, (0x30 if af else 0x10)])
    body = (bytes([len(af)]) + af if af else b"") + payload
    return head + body + b"\xff" * (184 - len(body))


def sections(pid, section):
    """A PSI section cut into packets: pointer field on the first."""
    data = b"\x00" + section
    return b"".join(packet(pid, data[i:i + 184], pusi=i == 0) for i in range(0, len(data), 184))


def psi(table_id, ext, body):
    length = 5 + len(body) + 4
    return (bytes([table_id, 0xB0 | length >> 8, length & 0xFF, ext >> 8, ext & 0xFF, 0xC1, 0, 0])
            + body + b"\x00" * 4)


def pat():
    return sections(0, psi(0x00, 1, bytes([0, 1, 0xE0 | PMT_PID >> 8, PMT_PID & 0xFF])))


def pmt(audio_tracks, vtype=0x1B):
    es = bytes([vtype, 0xE0 | VIDEO_PID >> 8, VIDEO_PID & 0xFF, 0xF0, 0])
    es += b"".join(bytes([0x0F, 0xE1, k, 0xF0, 0]) for k in range(audio_tracks))
    return sections(PMT_PID, psi(0x02, 1, bytes([0xE0 | VIDEO_PID >> 8, VIDEO_PID & 0xFF, 0xF0, 0]) + es))


def video(rap, nal):
    """A PES start with random_access_indicator, its first NAL type `nal`."""
    pes = b"\x00\x00\x01\xe0\x00\x00\x80\x00\x00" + b"\x00\x00\x00\x01" + bytes([nal]) + b"\x42" * 20
    return packet(VIDEO_PID, pes, pusi=True, af=b"\x40" if rap else b"\x00")

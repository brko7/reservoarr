from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE = Path(__file__).resolve().parent.parent.parent / "fixtures" / "synth.ts"
VIDEO_PID = 0x100
AUDIO_PID = 0x101
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


def null_packet():
    return ts_packet(0x1FFF, b"")


def pat():
    section = bytes([0x00, 0xB0, 13, 0, 1, 0xC1, 0, 0, 0, 1, 0xE0 | PMT_PID >> 8, PMT_PID & 0xFF]) + b"\0\0\0\0"
    return ts_packet(0, b"\x00" + section)


def elementary(stream_type, pid, descriptors=b""):
    return bytes([stream_type, 0xE0 | pid >> 8, pid & 0xFF, 0xF0 | len(descriptors) >> 8, len(descriptors) & 0xFF]) + descriptors


def pmt(*streams):
    streams = streams or (elementary(0x1B, VIDEO_PID), elementary(0x0F, AUDIO_PID))
    body = bytes([0, 1, 0xC1, 0, 0, 0xE0 | VIDEO_PID >> 8, VIDEO_PID & 0xFF, 0xF0, 0x00]) + b"".join(streams)
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


def audio_pes(seconds, **kwargs):
    header = bytes([0x80, 0x80, 5]) + pes_time_bytes(2, int(seconds * 90000))
    return ts_packet(AUDIO_PID, b"\x00\x00\x01\xC0\x00\x00" + header + b"\xff\xf1", **kwargs)


def frames(start, end, fps=25):
    return [start + n / fps for n in range(round((end - start) * fps))]


def audio_frames(start, end):
    return frames(start, end, fps=48000 / 1024)


def stream(video_times, audio_times=None, **kwargs):
    audio_times = video_times if audio_times is None else audio_times
    packets = [(t, video_pes(t, **kwargs)) for t in video_times]
    packets += [(t, audio_pes(t)) for t in audio_times]
    packets.sort(key=lambda item: item[0])
    return pat() + pmt() + b"".join(packet for _, packet in packets)


def test_the_video_and_audio_pids_come_from_the_pmt(resv):
    resv.parser.feed(pat() + pmt() + null_packet())

    assert resv.parser.pmt_pid == PMT_PID
    assert resv.parser.video.pid == VIDEO_PID
    assert resv.parser.audio.pid == AUDIO_PID


def test_private_data_with_an_ac3_descriptor_is_audio(resv):
    descriptor = bytes([0x6A, 1, 0x00])
    resv.parser.feed(pat() + pmt(elementary(0x1B, VIDEO_PID), elementary(0x06, AUDIO_PID, descriptor)) + null_packet())

    assert resv.parser.audio.pid == AUDIO_PID


def test_private_data_without_an_audio_descriptor_is_not_audio(resv):
    descriptor = bytes([0x59, 1, 0x00])
    resv.parser.feed(pat() + pmt(elementary(0x1B, VIDEO_PID), elementary(0x06, AUDIO_PID, descriptor)) + null_packet())

    assert resv.parser.audio.pid is None


def test_a_continuous_stream_has_no_holes(resv):
    resv.parser.feed(stream(frames(100, 115), audio_frames(100, 115)))

    assert resv.parser.take_holes_pct() == (0, 0)


def test_four_seconds_missing_out_of_eight_is_about_half(resv):
    video = [t for t in frames(100, 132) if (t - 100) % 8 < 4]
    audio = [t for t in audio_frames(100, 132) if (t - 100) % 8 < 4]
    resv.parser.feed(stream(video, audio))

    video_pct, audio_pct = resv.parser.take_holes_pct()
    assert 40 <= video_pct <= 50
    assert 40 <= audio_pct <= 50


def test_audio_holes_are_told_apart_from_video_holes(resv):
    audio = [t for t in audio_frames(100, 110) if not 104 <= t < 106]
    resv.parser.feed(stream(frames(100, 110), audio))

    video_pct, audio_pct = resv.parser.take_holes_pct()
    assert video_pct == 0
    assert audio_pct == 20


def test_audio_that_disappears_while_the_video_runs_is_all_missing(resv):
    resv.parser.feed(stream(frames(100, 110), audio_frames(100, 110)))
    resv.parser.take_holes_pct()

    resv.parser.feed(b"".join(video_pes(t) for t in frames(110, 125)))

    assert resv.parser.take_holes_pct() == (0, 100)


def test_video_that_disappears_while_the_audio_runs_is_all_missing(resv):
    resv.parser.feed(stream(frames(100, 110), audio_frames(100, 110)))
    resv.parser.take_holes_pct()

    resv.parser.feed(b"".join(audio_pes(t) for t in audio_frames(110, 125)))

    assert resv.parser.take_holes_pct() == (100, 0)


def test_a_source_without_audio_is_not_reported_as_missing_it(resv):
    resv.parser.feed(pat() + pmt(elementary(0x1B, VIDEO_PID)) + b"".join(video_pes(t) for t in frames(100, 110)))

    assert resv.parser.take_holes_pct() == (0, 0)


def test_the_window_resets_after_each_read(resv):
    resv.parser.feed(stream(frames(100, 104) + frames(106, 110)))
    assert resv.parser.take_holes_pct()[0] > 0

    resv.parser.feed(stream(frames(110, 125))[376:])

    assert resv.parser.take_holes_pct() == (0, 0)


def test_pts_is_used_when_there_is_no_dts(resv):
    resv.parser.feed(stream(frames(100, 104) + frames(106, 110), audio_times=[], with_dts=False))

    assert resv.parser.take_holes_pct()[0] == 20


def test_a_reconnect_seam_is_not_a_hole(resv):
    resv.parser.feed(stream(frames(100, 110)))
    resv.parser.on_reconnect()
    resv.parser.feed(stream(frames(130, 140))[376:])

    assert resv.parser.take_holes_pct() == (0, 0)


def test_a_flagged_discontinuity_is_not_a_hole(resv):
    resv.parser.feed(stream(frames(100, 110), audio_times=[]))
    resv.parser.feed(video_pes(200, discontinuity=True) + b"".join(video_pes(t) for t in frames(200.04, 210)))

    assert resv.parser.take_holes_pct()[0] == 0


def test_a_far_jump_is_not_a_hole(resv):
    resv.parser.feed(stream(frames(100, 110) + frames(400, 410)))

    assert resv.parser.take_holes_pct() == (0, 0)


def test_nothing_seen_reads_zero(resv):
    resv.parser.feed(pat() + pmt() + null_packet())

    assert resv.parser.take_holes_pct() == (0, 0)


def without(data, pid, start_s, end_s, resv, period_s=None):
    out = bytearray()
    first = None
    dropping = False
    for off in range(0, len(data) - 187, 188):
        packet = data[off:off + 188]
        packet_pid = (packet[1] & 0x1F) << 8 | packet[2]
        if packet_pid == pid and packet[1] & 0x40:
            start = 4 + (1 + packet[4] if (packet[3] >> 4) & 0x2 else 0)
            t = resv.pes_time(packet, start + 9) / 90000.0
            first = t if first is None else first
            at = t - first if period_s is None else (t - first) % period_s
            dropping = start_s <= at < end_s
        if not (packet_pid == pid and dropping):
            out += packet
    return bytes(out)


def fixture_bytes():
    if not FIXTURE.exists():
        pytest.skip(f"{FIXTURE} not generated; run `just fixture` first")
    return FIXTURE.read_bytes()[: 188 * 60000]


def test_the_synthetic_fixture_has_no_holes(resv):
    resv.parser.feed(fixture_bytes())

    assert resv.parser.video.pid == VIDEO_PID
    assert resv.parser.audio.pid == AUDIO_PID
    assert resv.parser.take_holes_pct() == (0, 0)


def test_dropping_six_seconds_of_video_from_the_fixture_shows_up(resv):
    resv.parser.feed(without(fixture_bytes(), VIDEO_PID, 10, 16, resv))

    video_pct, audio_pct = resv.parser.take_holes_pct()
    assert video_pct >= 10
    assert audio_pct == 0


def test_dropping_audio_four_seconds_in_eight_from_the_fixture_shows_up(resv):
    resv.parser.feed(without(fixture_bytes(), AUDIO_PID, 4, 8, resv, period_s=8))

    video_pct, audio_pct = resv.parser.take_holes_pct()
    assert video_pct == 0
    assert audio_pct >= 30


def test_a_sliver_of_video_without_audio_is_not_missing_sound(resv):
    resv.parser.feed(stream(frames(100, 110), audio_frames(100, 110)))
    resv.parser.take_holes_pct()

    resv.parser.feed(b"".join(video_pes(t) for t in frames(110, 112)))

    assert resv.parser.take_holes_pct() == (0, 0)


def test_five_seconds_of_video_without_audio_is_missing_sound(resv):
    resv.parser.feed(stream(frames(100, 110), audio_frames(100, 110)))
    resv.parser.take_holes_pct()

    resv.parser.feed(b"".join(video_pes(t) for t in frames(110, 116)))

    assert resv.parser.take_holes_pct() == (0, 100)


PCR_PID = 0x1FF


def pcr_packet(seconds, discontinuity=False):
    base = int(seconds * 90000)
    flags = 0x10 | (0x80 if discontinuity else 0)
    field = bytes([flags, base >> 25 & 0xFF, base >> 17 & 0xFF, base >> 9 & 0xFF, base >> 1 & 0xFF,
                   (base & 1) << 7 | 0x7E, 0x00])
    field += b"\xff" * (183 - len(field))
    return bytes([0x47, PCR_PID >> 8, PCR_PID & 0xFF, 0x20, 183]) + field


def split_pmt(*streams):
    body = bytes([0, 1, 0xC1, 0, 0, 0xE0 | VIDEO_PID >> 8, VIDEO_PID & 0xFF, 0xF0, 0x00]) + b"".join(streams)
    section = bytes([0x02, 0xB0 | (len(body) + 4) >> 8, (len(body) + 4) & 0xFF]) + body + b"\0\0\0\0"
    payload = b"\x00" + section
    assert len(payload) > 184
    first = bytes([0x47, 0x40 | PMT_PID >> 8, PMT_PID & 0xFF, 0x10]) + payload[:184]
    rest = payload[184:]
    second = bytes([0x47, PMT_PID >> 8, PMT_PID & 0xFF, 0x11]) + rest + b"\xff" * (184 - len(rest))
    return first, second


def long_pmt_streams():
    padding = bytes([0x05, 200]) + b"\x00" * 200
    return elementary(0x1B, VIDEO_PID, padding), elementary(0x0F, AUDIO_PID)


def test_a_pmt_spanning_two_packets_is_put_back_together(resv):
    first, second = split_pmt(*long_pmt_streams())

    resv.parser.feed(pat() + first + second + null_packet())

    assert resv.parser.video.pid == VIDEO_PID
    assert resv.parser.audio.pid == AUDIO_PID


def test_a_cut_off_pmt_clears_nothing(resv):
    resv.parser.feed(pat() + pmt() + null_packet())
    first, _ = split_pmt(elementary(0x1B, VIDEO_PID, bytes([0x05, 200]) + b"\x00" * 200))

    resv.parser.feed(first + null_packet() + null_packet())

    assert resv.parser.audio.pid == AUDIO_PID


def test_a_track_the_pmt_drops_is_forgotten(resv):
    resv.parser.feed(stream(frames(100, 110), audio_frames(100, 110)))
    resv.parser.take_holes_pct()

    resv.parser.feed(pmt(elementary(0x1B, VIDEO_PID)) + b"".join(video_pes(t) for t in frames(110, 125)))

    assert resv.parser.audio.pid is None
    assert resv.parser.take_holes_pct() == (0, 0)


def test_a_discontinuity_on_the_pcr_pid_covers_the_jump_on_both_tracks(resv):
    resv.parser.feed(pcr_packet(100) + stream(frames(100, 110), audio_frames(100, 110)) + pcr_packet(110))
    resv.parser.take_holes_pct()
    after = sorted([(t, video_pes(t)) for t in frames(113, 123)] + [(t, audio_pes(t)) for t in audio_frames(113, 123)])

    resv.parser.feed(pcr_packet(500, discontinuity=True) + b"".join(packet for _, packet in after))

    assert resv.parser.take_holes_pct() == (0, 0)


def test_the_same_jump_without_the_flag_is_a_hole(resv):
    resv.parser.feed(pcr_packet(100) + stream(frames(100, 110), audio_frames(100, 110)) + pcr_packet(110))
    resv.parser.take_holes_pct()
    after = sorted([(t, video_pes(t)) for t in frames(113, 123)] + [(t, audio_pes(t)) for t in audio_frames(113, 123)])

    resv.parser.feed(pcr_packet(110.5) + b"".join(packet for _, packet in after))

    video_pct, audio_pct = resv.parser.take_holes_pct()
    assert video_pct > 0
    assert audio_pct > 0


def test_reading_the_window_waits_for_the_lock(resv):
    import threading
    done = threading.Event()
    resv.parser.holes_lock.acquire()
    reader = threading.Thread(target=lambda: (resv.parser.take_holes_pct(), done.set()))
    reader.start()

    assert not done.wait(0.2)
    resv.parser.holes_lock.release()
    assert done.wait(2)


def test_counting_a_timestamp_waits_for_the_lock(resv):
    import threading
    resv.parser.feed(pat() + pmt() + null_packet())
    done = threading.Event()
    resv.parser.holes_lock.acquire()
    feeder = threading.Thread(target=lambda: (resv.parser.feed(b"".join(video_pes(t) for t in frames(100, 101))), done.set()))
    feeder.start()

    assert not done.wait(0.2)
    resv.parser.holes_lock.release()
    assert done.wait(2)


def pmt_not_yet_current(*streams):
    body = bytes([0, 1, 0xC2, 0, 0, 0xE0 | VIDEO_PID >> 8, VIDEO_PID & 0xFF, 0xF0, 0x00]) + b"".join(streams)
    section = bytes([0x02, 0xB0, len(body) + 4]) + body + b"\0\0\0\0"
    return ts_packet(PMT_PID, b"\x00" + section)


def split_video_pes(seconds, first_bytes=10):
    ticks = int(seconds * 90000)
    header = (b"\x00\x00\x01\xE0\x00\x00" + bytes([0x80, 0xC0, 10])
              + pes_time_bytes(3, ticks + 3600) + pes_time_bytes(1, ticks) + b"\x00\x00\x00\x01")
    stuffing = 184 - 1 - first_bytes
    first = (bytes([0x47, 0x40 | VIDEO_PID >> 8, VIDEO_PID & 0xFF, 0x30, stuffing, 0x00])
             + b"\xff" * (stuffing - 1) + header[:first_bytes])
    rest = header[first_bytes:]
    second = bytes([0x47, VIDEO_PID >> 8, VIDEO_PID & 0xFF, 0x11]) + rest + b"\xff" * (184 - len(rest))
    return first + second


def test_a_pmt_for_the_next_mapping_is_not_applied(resv):
    resv.parser.feed(pat() + pmt() + null_packet())

    resv.parser.feed(pmt_not_yet_current(elementary(0x1B, 0x200), elementary(0x0F, 0x201)) + null_packet() + null_packet())

    assert resv.parser.video.pid == VIDEO_PID
    assert resv.parser.audio.pid == AUDIO_PID


def test_a_pes_header_split_over_two_packets_is_still_read(resv):
    resv.parser.feed(pat() + pmt(elementary(0x1B, VIDEO_PID)) + null_packet())

    resv.parser.feed(b"".join(split_video_pes(t) for t in frames(100, 104) + frames(106, 110)))

    assert resv.parser.take_holes_pct() == (20, 0)


def test_a_reconnect_drops_a_half_read_pes_header(resv):
    resv.parser.feed(pat() + pmt(elementary(0x1B, VIDEO_PID)) + null_packet())
    resv.parser.feed(split_video_pes(100)[:188] + null_packet() + null_packet())
    assert resv.parser.video.head is not None

    resv.parser.on_reconnect()

    assert resv.parser.video.head is None

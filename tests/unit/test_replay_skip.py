"""RESV_REPLAY_SKIP: after a plain reconnect the edge resends seconds that were
already ingested. The gate drops packets up to the last PCR taken before the
seam and releases the rest untouched; anything it cannot place passes through
as before."""
from __future__ import annotations

import pytest

PID = 0x100
SEAM = 110.0


def packet(pid, pcr=None, disc=False):
    if pcr is None:
        return bytes([0x47, pid >> 8 & 0x1F, pid & 0xFF, 0x10]) + b"\xab" * 184
    base = round(pcr * 90000)
    flags = 0x10 | (0x80 if disc else 0)
    pcr_bytes = bytes([
        base >> 25 & 0xFF, base >> 17 & 0xFF, base >> 9 & 0xFF, base >> 1 & 0xFF,
        (base & 1) << 7 | 0x7E, 0x00,
    ])
    return (bytes([0x47, pid >> 8 & 0x1F, pid & 0xFF, 0x30, 7, flags]) + pcr_bytes
            + b"\xcd" * 176)


def ts_stream(start, end, step=0.1, disc_at=None):
    pat = packet(0)
    out = [pat]
    ticks = round((end - start) / step)
    for k in range(ticks + 1):
        t = round(start + k * step, 3)
        out.append(packet(PID, t, disc=disc_at is not None and abs(t - disc_at) < 1e-9))
        out.extend(packet(PID) for _ in range(3))
    return b"".join(out)


def offset_of_pcr(data, pcr):
    target = packet(PID, pcr)
    return data.index(target)


@pytest.fixture
def gate(resv, stream):
    replay = stream.upstream.replay

    def arm(enabled=True, allowed=True, seam=SEAM):
        resv.REPLAY_SKIP = enabled
        stream.parser.pcr_pid = PID
        stream.parser.last_pcr = seam
        replay.arm(allowed=allowed)

    def run(data, chunk=1000):
        return b"".join(replay.feed(data[i:i + chunk]) for i in range(0, len(data), chunk))

    return arm, run


def test_replayed_seconds_are_dropped_up_to_the_seam(resv, gate, tmp_path):
    arm, run = gate
    arm()
    data = ts_stream(101.0, 120.0)
    out = run(data)
    assert out == data[offset_of_pcr(data, 110.1):]
    log = (tmp_path / "delaybuf.log").read_text()
    assert "replay after reconnect: skipped 9.0s" in log


def test_a_reconnect_that_lands_ahead_passes_everything(gate):
    arm, run = gate
    arm()
    data = ts_stream(112.0, 118.0)
    assert run(data) == data


def test_a_jump_back_beyond_the_window_passes_everything(resv, gate, tmp_path):
    arm, run = gate
    arm()
    data = ts_stream(30.0, 40.0)
    assert run(data) == data
    assert "beyond RESV_REPLAY_MAX_S" in (tmp_path / "delaybuf.log").read_text()


def test_a_discontinuity_flag_passes_everything(gate):
    arm, run = gate
    arm()
    data = ts_stream(105.0, 115.0, disc_at=105.0)
    assert run(data) == data


def test_a_pcr_that_goes_back_releases_everything_held(gate):
    arm, run = gate
    arm()
    data = ts_stream(105.0, 106.0) + ts_stream(105.5, 115.0)
    assert run(data) == data


def test_a_duplicate_pcr_in_the_replay_is_still_skipped(resv, gate, tmp_path):
    arm, run = gate
    arm()
    data = ts_stream(101.0, 105.0) + packet(PID, 105.0) + ts_stream(105.1, 120.0)
    assert run(data) == data[offset_of_pcr(data, 110.1):]
    assert "skipped 9.0s" in (tmp_path / "delaybuf.log").read_text()


def test_a_jump_from_the_replay_straight_past_the_seam_passes_everything(gate):
    arm, run = gate
    arm()
    data = ts_stream(101.0, 103.0) + ts_stream(114.0, 118.0)
    assert run(data) == data


def test_a_discontinuity_inside_the_replay_gives_back_what_was_held(gate):
    arm, run = gate
    arm()
    data = ts_stream(101.0, 115.0, disc_at=106.0)
    assert run(data) == data


def test_a_replay_whose_pcr_stops_is_released_at_the_hold_limit(resv, gate, tmp_path):
    arm, run = gate
    arm()
    data = ts_stream(101.0, 102.0) + packet(PID) * (resv.REPLAY_HOLD_BYTES // 188 + 10)
    assert run(data, chunk=65536) == data
    assert "PCR not advancing" in (tmp_path / "delaybuf.log").read_text()


def test_a_first_pcr_beyond_the_hold_limit_is_not_taken(resv, gate, tmp_path):
    arm, run = gate
    arm()
    data = packet(PID) * (resv.REPLAY_HOLD_BYTES // 188 + 10) + ts_stream(101.0, 120.0)
    assert run(data, chunk=len(data)) == data
    assert "no PCR within the hold limit" in (tmp_path / "delaybuf.log").read_text()


def test_a_replay_that_does_not_fit_in_the_reservoir_passes_everything(resv, gate, tmp_path):
    arm, run = gate
    arm()
    resv.MAX_BYTES = 20_000
    data = ts_stream(101.0, 120.0)
    assert run(data) == data
    assert "does not fit in the reservoir" in (tmp_path / "delaybuf.log").read_text()


def test_a_step_of_exactly_ten_seconds_is_still_a_replay(resv, gate, tmp_path):
    arm, run = gate
    arm()
    data = ts_stream(100.0, 100.5) + ts_stream(110.5, 115.0)
    assert run(data) == data[offset_of_pcr(data, 110.5):]
    assert "skipped 10.0s" in (tmp_path / "delaybuf.log").read_text()


def test_a_reservoir_running_low_releases_the_replay(resv, stream, gate, tmp_path, monkeypatch):
    arm, run = gate
    arm()
    monkeypatch.setattr(stream.parser, "content_rate", lambda: 500_000.0)
    stream.reservoir.bytes = 500_000
    data = ts_stream(101.0, 120.0)
    assert run(data) == data
    assert "reservoir running low" in (tmp_path / "delaybuf.log").read_text()


def test_a_full_reservoir_keeps_holding(resv, stream, gate, monkeypatch):
    arm, run = gate
    arm()
    monkeypatch.setattr(stream.parser, "content_rate", lambda: 500_000.0)
    stream.reservoir.bytes = 500_000 * 12
    data = ts_stream(101.0, 120.0)
    assert run(data) == data[offset_of_pcr(data, 110.1):]


def test_ingest_queues_a_large_release_in_chunks(resv, stream):
    blob = bytes(resv.CHUNK * 3 + 10)
    stream.upstream.ingest(blob)
    assert [len(piece) for piece in stream.reservoir.chunks] == [resv.CHUNK] * 3 + [10]
    assert stream.reservoir.bytes == stream.reservoir.in_total == stream.parser.fed == len(blob)


def test_whole_packets_come_out_even_from_odd_chunks(gate):
    arm, run = gate
    arm()
    data = ts_stream(101.0, 120.0)
    out = run(data, chunk=777)
    assert len(out) % 188 == 0
    assert out[0] == 0x47


def test_disabled_changes_nothing(gate):
    arm, run = gate
    arm(enabled=False)
    data = ts_stream(101.0, 120.0)
    assert run(data) == data


def test_a_flushed_reservoir_keeps_the_replay(gate):
    arm, run = gate
    arm(allowed=False)
    data = ts_stream(101.0, 120.0)
    assert run(data) == data


def test_no_seam_on_the_first_connect(resv, stream):
    resv.REPLAY_SKIP = True
    stream.upstream.replay.arm()
    assert stream.upstream.replay.seam is None


def test_a_stream_without_pcr_is_released_at_the_hold_limit(resv, gate):
    arm, run = gate
    arm()
    data = packet(PID) * (resv.REPLAY_HOLD_BYTES // 188 + 10)
    assert run(data, chunk=65536) == data


class Replayed:
    def __init__(self, data, chunk=1000):
        self._chunks = [data[i:i + chunk] for i in range(0, len(data), chunk)]

    def read(self, _size):
        return self._chunks.pop(0) if self._chunks else b""

    def geturl(self):
        return "http://edge.test/live/stream.ts"


@pytest.fixture
def connect_once(resv, stream, monkeypatch):
    def go(response):
        resv.REPLAY_SKIP = True
        resv.GIVEUP_TRIES = 0
        stream.parser.pcr_pid = PID
        stream.parser.last_pcr = SEAM
        monkeypatch.setattr(resv.urllib.request, "urlopen", lambda *a, **k: response)
        monkeypatch.setattr(resv.time, "sleep", lambda _s: stream.stop.set())
        stream.upstream.run()

    return go


class Watched(Replayed):
    def __init__(self, stream, data):
        super().__init__(data)
        self.stream = stream
        self.seen = []

    def read(self, size):
        self.seen.append((self.stream.reservoir.in_total, self.stream.upstream.arrived_total))
        return super().read(size)


def test_a_held_replay_counts_as_arrival_while_it_is_held(resv, stream, connect_once):
    replay = ts_stream(101.0, 109.0)
    response = Watched(stream, replay)
    connect_once(response)
    assert response.seen[-1] == (0, len(replay))


def test_a_connection_that_ends_before_the_seam_gives_back_what_was_held(
    resv, stream, connect_once, tmp_path
):
    replay = ts_stream(101.0, 109.0)
    connect_once(Replayed(replay))
    assert stream.reservoir.in_total == len(replay)
    assert "connection ended before the seam" in (tmp_path / "delaybuf.log").read_text()


class Flushed(Replayed):
    def __init__(self, stream, data):
        super().__init__(data)
        self.stream = stream

    def read(self, size):
        chunk = super().read(size)
        if not chunk:
            self.stream.upstream.flush_pending = True
        return chunk


def test_a_held_replay_is_dropped_when_the_reservoir_is_being_flushed(resv, stream, monkeypatch, tmp_path):
    resv.REPLAY_SKIP = True
    resv.GIVEUP_TRIES = 0
    stream.parser.pcr_pid = PID
    stream.parser.last_pcr = SEAM
    answers = [Flushed(stream, ts_stream(101.0, 109.0))]

    def urlopen(*_a, **_k):
        if answers:
            return answers.pop()
        stream.stop.set()
        raise OSError("stop")

    monkeypatch.setattr(resv.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(resv.time, "sleep", lambda _s: None)
    stream.upstream.run()
    assert stream.reservoir.in_total == 0
    assert "connection ended before the seam" not in (tmp_path / "delaybuf.log").read_text()


class Ticks:
    def __init__(self, upstream, clock, count, arriving):
        self.upstream, self.clock, self.count, self.arriving = upstream, clock, count, arriving

    def wait(self, seconds):
        self.clock[0] += seconds
        if self.arriving:
            self.upstream.arrived_total += 188
        self.count -= 1
        return self.count < 0


def watch(resv, stream, monkeypatch, arriving):
    """Run the real stall watchdog on a fake clock; returns the reasons it fired."""
    clock = [1000.0]
    fired = []
    up = stream.upstream
    monkeypatch.setattr(resv.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(up, "request_reconnect", lambda reason, flush=True: fired.append(reason) or True)
    up.stop = Ticks(up, clock, round(resv.STALL_S) + 10, arriving)
    up.stall_watchdog()
    return fired


@pytest.mark.parametrize(("arriving", "fires"), [(True, False), (False, True)])
def test_the_stall_watchdog_follows_arrival_not_ingest(resv, stream, monkeypatch, arriving, fires):
    fired = watch(resv, stream, monkeypatch, arriving)
    assert stream.reservoir.in_total == 0
    assert bool(fired) is fires


def test_a_full_reservoir_is_not_a_stall(resv, stream, monkeypatch):
    """put() stops reading on purpose while the reservoir is full; arrival
    freezes, but the upstream is healthy and must not be reconnected every 90s."""
    stream.reservoir.bytes = resv.MAX_BYTES
    assert watch(resv, stream, monkeypatch, arriving=False) == []

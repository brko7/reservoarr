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


def stream(start, end, step=0.1, disc_at=None):
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
def gate(resv):
    def arm(enabled=True, allowed=True, seam=SEAM):
        resv.REPLAY_SKIP = enabled
        resv.parser.pcr_pid = PID
        resv.parser.last_pcr = seam
        resv.arm_replay_skip(allowed=allowed)

    def run(data, chunk=1000):
        return b"".join(resv.skip_replay(data[i:i + chunk]) for i in range(0, len(data), chunk))

    return arm, run


def test_replayed_seconds_are_dropped_up_to_the_seam(resv, gate, tmp_path):
    arm, run = gate
    arm()
    data = stream(101.0, 120.0)
    out = run(data)
    assert out == data[offset_of_pcr(data, 110.1):]
    log = (tmp_path / "delaybuf.log").read_text()
    assert "replay after reconnect: skipped 9.0s" in log


def test_a_reconnect_that_lands_ahead_passes_everything(gate):
    arm, run = gate
    arm()
    data = stream(112.0, 118.0)
    assert run(data) == data


def test_a_jump_back_beyond_the_window_passes_everything(resv, gate, tmp_path):
    arm, run = gate
    arm()
    data = stream(30.0, 40.0)
    assert run(data) == data
    assert "beyond RESV_REPLAY_MAX_S" in (tmp_path / "delaybuf.log").read_text()


def test_a_discontinuity_flag_passes_everything(gate):
    arm, run = gate
    arm()
    data = stream(105.0, 115.0, disc_at=105.0)
    assert run(data) == data


def test_a_pcr_that_goes_back_releases_everything_held(gate):
    arm, run = gate
    arm()
    data = stream(105.0, 106.0) + stream(105.5, 115.0)
    assert run(data) == data


def test_a_duplicate_pcr_in_the_replay_is_still_skipped(resv, gate, tmp_path):
    arm, run = gate
    arm()
    data = stream(101.0, 105.0) + packet(PID, 105.0) + stream(105.1, 120.0)
    assert run(data) == data[offset_of_pcr(data, 110.1):]
    assert "skipped 9.0s" in (tmp_path / "delaybuf.log").read_text()


def test_a_jump_from_the_replay_straight_past_the_seam_passes_everything(gate):
    arm, run = gate
    arm()
    data = stream(101.0, 103.0) + stream(114.0, 118.0)
    assert run(data) == data


def test_a_discontinuity_inside_the_replay_gives_back_what_was_held(gate):
    arm, run = gate
    arm()
    data = stream(101.0, 115.0, disc_at=106.0)
    assert run(data) == data


def test_a_replay_whose_pcr_stops_is_released_at_the_hold_limit(resv, gate, tmp_path):
    arm, run = gate
    arm()
    data = stream(101.0, 102.0) + packet(PID) * (resv.REPLAY_HOLD_BYTES // 188 + 10)
    assert run(data, chunk=65536) == data
    assert "PCR not advancing" in (tmp_path / "delaybuf.log").read_text()


def test_a_first_pcr_beyond_the_hold_limit_is_not_taken(resv, gate, tmp_path):
    arm, run = gate
    arm()
    data = packet(PID) * (resv.REPLAY_HOLD_BYTES // 188 + 10) + stream(101.0, 120.0)
    assert run(data, chunk=len(data)) == data
    assert "no PCR within the hold limit" in (tmp_path / "delaybuf.log").read_text()


def test_a_replay_larger_than_the_hold_cap_passes_everything(resv, gate, tmp_path):
    arm, run = gate
    arm()
    resv.REPLAY_HOLD_MAX_BYTES = 20_000
    data = stream(101.0, 120.0)
    assert run(data) == data
    assert "hold cap" in (tmp_path / "delaybuf.log").read_text()


def test_whole_packets_come_out_even_from_odd_chunks(gate):
    arm, run = gate
    arm()
    data = stream(101.0, 120.0)
    out = run(data, chunk=777)
    assert len(out) % 188 == 0
    assert out[0] == 0x47


def test_disabled_changes_nothing(gate):
    arm, run = gate
    arm(enabled=False)
    data = stream(101.0, 120.0)
    assert run(data) == data


def test_a_flushed_reservoir_keeps_the_replay(gate):
    arm, run = gate
    arm(allowed=False)
    data = stream(101.0, 120.0)
    assert run(data) == data


def test_no_seam_on_the_first_connect(resv):
    resv.REPLAY_SKIP = True
    resv.arm_replay_skip()
    assert resv.replay_seam is None


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
def connect_once(resv, monkeypatch):
    def go(response):
        resv.REPLAY_SKIP = True
        resv.GIVEUP_TRIES = 0
        resv.parser.pcr_pid = PID
        resv.parser.last_pcr = SEAM
        monkeypatch.setattr(resv.urllib.request, "urlopen", lambda *a, **k: response)
        monkeypatch.setattr(resv.time, "sleep", lambda _s: resv.stop.set())
        resv.fetcher()

    return go


class Watched(Replayed):
    def __init__(self, resv, data):
        super().__init__(data)
        self.resv = resv
        self.seen = []

    def read(self, size):
        self.seen.append((self.resv.in_total, self.resv.arrived_total))
        return super().read(size)


def test_a_held_replay_counts_as_arrival_while_it_is_held(resv, connect_once):
    replay = stream(101.0, 109.0)
    response = Watched(resv, replay)
    connect_once(response)
    assert response.seen[-1] == (0, len(replay))


def test_a_connection_that_ends_before_the_seam_gives_back_what_was_held(
    resv, connect_once, tmp_path
):
    replay = stream(101.0, 109.0)
    connect_once(Replayed(replay))
    assert resv.in_total == len(replay)
    assert "connection ended before the seam" in (tmp_path / "delaybuf.log").read_text()


class Ticks:
    def __init__(self, resv, clock, count, arriving):
        self.resv, self.clock, self.count, self.arriving = resv, clock, count, arriving

    def wait(self, seconds):
        self.clock[0] += seconds
        if self.arriving:
            self.resv.arrived_total += 188
        self.count -= 1
        return self.count < 0


@pytest.mark.parametrize(("arriving", "fires"), [(True, False), (False, True)])
def test_the_stall_watchdog_follows_arrival_not_ingest(resv, monkeypatch, arriving, fires):
    clock = [1000.0]
    fired = []
    monkeypatch.setattr(resv.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(resv, "force_upstream_reconnect",
                        lambda reason, flush=True: fired.append(reason) or True)
    monkeypatch.setattr(resv, "stop", Ticks(resv, clock, round(resv.STALL_S) + 10, arriving))
    resv.stall_watchdog()
    assert resv.in_total == 0
    assert bool(fired) is fires

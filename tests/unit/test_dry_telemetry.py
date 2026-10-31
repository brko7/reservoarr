"""6.7.0: `dry=` and `maxgap=` on the stats line. The 15s averages cannot show a
sub-second hole in delivery (discussion #46)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

PACKET = b"\x47\x1f\xff\x10" + b"\xff" * 184


class Clock:
    def __init__(self, resv, monkeypatch):
        self.now = 1000.0
        monkeypatch.setattr(resv.time, "monotonic", lambda: self.now)


@pytest.fixture
def clock(resv, monkeypatch):
    return Clock(resv, monkeypatch)


def spell(res, clock, seconds):
    res.put(PACKET)
    res.take(0)
    clock.now += seconds
    res.put(PACKET)
    res.take(0)


def test_a_spell_counts_once_it_is_long_enough(resv, stream, clock):
    res = stream.reservoir
    spell(res, clock, 1.2)
    res.dry_since = None
    assert res.take_dry(0.5) == pytest.approx(1.2)
    assert res.take_dry(0.5) == 0.0


def test_a_short_spell_is_not_counted(resv, stream, clock):
    res = stream.reservoir
    spell(res, clock, 0.3)
    res.dry_since = None
    assert res.take_dry(0.5) == 0.0


def test_the_threshold_applies_when_it_is_read(resv, stream, clock):
    """A chunk's time apart is healthy on a slow channel."""
    res = stream.reservoir
    spell(res, clock, 1.2)
    res.dry_since = None
    assert res.take_dry(2.0) == 0.0


def test_spells_add_up(resv, stream, clock):
    res = stream.reservoir
    spell(res, clock, 1.0)
    spell(res, clock, 0.6)
    res.dry_since = None
    assert res.take_dry(0.5) == pytest.approx(1.6)


def test_nothing_counts_before_the_first_release(resv, stream, clock):
    res = stream.reservoir
    clock.now += 30.0
    assert res.take_dry(0.5) == 0.0
    res.put(PACKET)
    assert res.take_dry(0.5) == 0.0


def test_a_spell_across_windows_is_counted_once(resv, stream, clock):
    res = stream.reservoir
    res.put(PACKET)
    res.take(0)
    clock.now += 10.0
    assert res.take_dry(0.5) == pytest.approx(10.0)
    clock.now += 5.0
    assert res.take_dry(0.5) == pytest.approx(5.0)
    clock.now += 2.0
    res.put(PACKET)
    assert res.take_dry(0.5) == pytest.approx(2.0)
    assert res.take_dry(0.5) == 0.0


def test_a_spell_too_short_to_report_in_one_window_counts_whole_in_the_next(resv, stream, clock):
    res = stream.reservoir
    res.put(PACKET)
    res.take(0)
    clock.now += 0.3
    assert res.take_dry(0.5) == 0.0
    clock.now += 0.3
    res.put(PACKET)
    assert res.take_dry(0.5) == pytest.approx(0.6)


def test_a_flush_starts_a_spell(resv, stream, clock):
    res = stream.reservoir
    res.put(PACKET)
    res.flush()
    clock.now += 1.0
    res.put(PACKET)
    assert res.take_dry(0.5) == pytest.approx(1.0)


def test_maxgap_is_the_longest_wait_and_resets(resv, stream, clock):
    up = stream.upstream
    assert up.take_max_gap() == 0.0                                # nothing has arrived: no gap to report
    up.last_arrival = clock.now
    clock.now += 0.2
    assert up.take_max_gap() == pytest.approx(0.2)                 # the wait still going
    up.max_gap = 3.0
    up.last_arrival = clock.now
    assert up.take_max_gap() == pytest.approx(3.0)
    assert up.take_max_gap() == pytest.approx(0.0)


def test_maxgap_ignores_a_reservoir_full_on_purpose(resv, stream, clock):
    up = stream.upstream
    up.last_arrival = clock.now
    clock.now += 40.0
    resv.MAX_BYTES = 1
    up.reservoir.bytes = 1
    assert up.take_max_gap() == 0.0


def test_read_gaps_span_a_reconnect(resv, stream):
    up = stream.upstream
    up.pass_on = lambda d: None

    class Response:
        def __init__(self, chunks):
            self.chunks = chunks

        def read(self, _size):
            return self.chunks.pop(0) if self.chunks else b""

        def close(self):
            pass

    up.drain(Response([PACKET]))                                   # first byte of the process is not a gap
    assert up.max_gap == 0.0
    time.sleep(0.1)
    up.drain(Response([PACKET]))
    assert up.max_gap >= 0.1


def test_stats_line_appends_both_fields(resv, stream):
    pacer = resv.Pacer(stream.parser, stream.reservoir)
    stream.report(pacer.measure(0), 0.0)
    line = Path(resv.LOG_FILE).read_text().strip().splitlines()[-1]
    assert line.endswith("vholes=0% aholes=0% dry=0.0s maxgap=0.0s")

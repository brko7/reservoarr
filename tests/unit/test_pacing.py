"""Pacing controller unit tests: the clamp behavior + the grace-period floor
flip of release_factor() — the v5 cushion bug came from the floor cancelling
out, so the math has to stay exactly as specced.

  r = rate * clamp(1 + 0.3 * level_err, floor, 1.15)
  level_err = (cushion - TARGET_S) / TARGET_S
  floor = 1.0 for the first GRACE_S, RESV_FLOOR (default 1.0) after
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
from pathlib import Path

import pytest

RESERVOIR_PATH = Path(__file__).resolve().parent.parent.parent / "reservoarr.py"

IN_GRACE = 0.0
AFTER_GRACE = 1e6


def load(tmp_path, **env):
    os.environ["RESV_LOG_DIR"] = str(tmp_path)
    os.environ.update(env)
    sys.argv = ["reservoarr", "http://offline/unit-test"]
    try:
        spec = importlib.util.spec_from_file_location("resv_pacing", str(RESERVOIR_PATH))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for key in env:
            os.environ.pop(key, None)
    return mod


@pytest.fixture
def pacing_factor(resv):
    def factor(cushion_s, target_s, in_grace):
        assert target_s == resv.TARGET_S
        return resv.release_factor(cushion_s, IN_GRACE if in_grace else AFTER_GRACE)
    return factor


def test_at_target_factor_is_one(pacing_factor):
    # cushion=30 == TARGET_S(30) -> factor exactly 1.0 -> realtime release
    assert pacing_factor(30, 30, in_grace=False) == 1.0


def test_above_target_speeds_up(pacing_factor):
    # cushion 45, target 30 -> err=0.5 -> 1 + 0.15 = 1.15 (hits ceiling)
    assert pacing_factor(45, 30, in_grace=False) == 1.15
    # cushion 35 -> err=0.167 -> 1.05
    assert abs(pacing_factor(35, 30, in_grace=False) - 1.05) < 1e-6


def test_below_target_never_goes_below_realtime_by_default(pacing_factor):
    """A release under 1.0 banks cushion out of the player's own buffer: a
    client with a small live buffer stutters every few tens of seconds until
    the cushion reaches the target (discussion #46)."""
    # cushion 15, target 30 -> err=-0.5 -> 1 - 0.15 = 0.85 -> floored to 1.0
    assert pacing_factor(15, 30, in_grace=False) == 1.0
    assert pacing_factor(0, 30, in_grace=False) == 1.0
    assert pacing_factor(29.9, 30, in_grace=False) == 1.0


def test_a_floor_under_one_slows_down_after_grace(tmp_path):
    resv = load(tmp_path, RESV_FLOOR="0.97")
    # cushion 15 -> 0.85 -> floored to 0.97; cushion 28.5 -> err=-0.05 -> 0.985
    assert resv.release_factor(15, AFTER_GRACE) == 0.97
    assert abs(resv.release_factor(28.5, AFTER_GRACE) - 0.985) < 1e-9
    assert resv.release_factor(15, IN_GRACE) == 1.0
    assert resv.release_factor(45, AFTER_GRACE) == 1.15


def test_the_floor_cannot_go_below_the_controller(tmp_path):
    # an empty cushion asks for 1 - 0.3 = 0.7; a lower floor changes nothing
    resv = load(tmp_path, RESV_FLOOR="0.5")
    assert abs(resv.release_factor(0, AFTER_GRACE) - 0.7) < 1e-9


@pytest.mark.parametrize("raw, value", [
    ("0.97", 0.97), (" 0.97 ", 0.97), (".97", 0.97), ("1", 1.0), ("1.0", 1.0), ("0.5", 0.5),
])
def test_floor_values(tmp_path, raw, value):
    resv = load(tmp_path, RESV_FLOOR=raw)
    assert resv.FLOOR_OK
    assert value == resv.FLOOR


@pytest.mark.parametrize("raw", ["", "0", "0.0", "1.5", "1.01", "-0.97", "97", "0,97", "inf", "nan", "abc", ".\u0669\u0667"])
def test_anything_else_is_realtime(tmp_path, raw):
    resv = load(tmp_path, RESV_FLOOR=raw)
    assert resv.FLOOR == 1.0
    assert resv.release_factor(15, AFTER_GRACE) == 1.0


def test_an_invalid_floor_is_logged_once(tmp_path, capsys):
    resv = load(tmp_path, RESV_FLOOR="1.5")
    resv.Upstream.run = lambda self: self.reservoir.close()
    resv.main()
    assert capsys.readouterr().err.count("RESV_FLOOR='1.5' is not a decimal above 0 and at most 1") == 1


@pytest.mark.parametrize("env", [{}, {"RESV_FLOOR": ""}, {"RESV_FLOOR": "0.97"}])
def test_an_unset_or_valid_floor_logs_nothing(tmp_path, capsys, env):
    resv = load(tmp_path, **env)
    resv.Upstream.run = lambda self: self.reservoir.close()
    resv.main()
    assert "RESV_FLOOR" not in capsys.readouterr().err


def test_below_target_holds_realtime_during_grace(pacing_factor):
    """The first GRACE_S after release-start, the player owns only the small
    headstart — sub-realtime would drain it before the bank settles. Floor=1.0."""
    assert pacing_factor(15, 30, in_grace=True) == 1.0
    assert pacing_factor(5, 30, in_grace=True) == 1.0


def test_clamp_ceiling_never_exceeds_1_15(pacing_factor):
    """Bigger-than-target cushion can't bleed faster than 1.15× content rate —
    bleeding too fast IS the v5 bug (chasing arrival rate ate the surplus)."""
    assert pacing_factor(300, 30, in_grace=False) == 1.15
    assert pacing_factor(300, 30, in_grace=True) == 1.15


def test_grace_constants_match_script(resv):
    """Sanity: the constants in the script are what we expect. If someone bumps
    TARGET_S, GRACE_S or the FLOOR default, these tests need to retune alongside it."""
    assert resv.TARGET_S == 30.0
    assert resv.GRACE_S == 45.0
    assert resv.FLOOR == 1.0
    assert resv.RATE_FLOOR == 125_000   # 1 Mbps


def test_the_floor_flips_exactly_at_grace_end(tmp_path):
    resv = load(tmp_path, RESV_FLOOR="0.97")
    assert resv.release_factor(15, resv.GRACE_S - 0.01) == 1.0
    assert resv.release_factor(15, resv.GRACE_S) == 0.97


class FakeClock:
    """Stands in for the time module: every sleep runs `over` long."""

    def __init__(self, over):
        self.now = 1000.0
        self.over = over

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += s + self.over


@pytest.mark.parametrize("over", [0.0, 0.00025, 0.0036])
def test_the_pacer_pays_back_oversleep(resv, monkeypatch, over):
    """time.sleep() runs long, ~0.23ms on Linux and ~3.6ms on macOS. The pacer
    used to drop the overrun after every sleep: 2% under the target rate on an
    8 Mbps channel on Linux, 9% at 2.4 Mbps on macOS."""
    clock = FakeClock(over)
    monkeypatch.setattr(resv, "time", clock)
    pacer = resv.Pacer(resv.TsParser(), resv.Reservoir(threading.Event()))
    clock.now += 20                                                   # past the headstart window
    rate = 1_000_000                                                  # 8 Mbps
    level = resv.Level(crate=rate, rate=rate, cush_pcr=None, cush=resv.TARGET_S)
    pacer.pace(resv.PACE_SLICE, level)                                # drops the 20s idle
    start = clock.now
    for _ in range(1000):
        pacer.pace(resv.PACE_SLICE, level)
    assert clock.now - start == pytest.approx(1000 * resv.PACE_SLICE / rate, rel=0.005)


class Content:
    """Feeds the pacer what Stream.pump() would: after each slice, the parser's
    PCR clock and the cushion behind it, for a stream whose content carries
    `density` bytes per content second, with a PCR mark every 40ms."""

    MARK_S = 0.04

    def __init__(self, resv, rate, density):
        self.resv = resv
        self.rate = rate
        self.density = density
        self.parser = resv.TsParser()
        self.released = 0.0                                           # content seconds released
        self.lead = 10.0                                              # content seconds buffered past the release

    def level(self, released_s):
        """`released_s`: the content position of the last PCR mark released."""
        self.parser.cum_pcr = released_s + self.lead
        return self.resv.Level(crate=self.rate, rate=self.rate, cush_pcr=self.lead, cush=self.lead,
                               released_s=released_s)

    def run(self, pacer, seconds, jump=0.0):
        """Release `seconds` of content slice by slice. `jump` is added to the
        PCR clock halfway through, as a garbage sample the parser let through."""
        end = self.released + seconds
        half = self.released + seconds / 2
        jumped = 0.0
        while self.released < end:
            self.released += self.resv.PACE_SLICE / self.density
            if jump and not jumped and self.released >= half:
                jumped = jump
            mark = (self.released // self.MARK_S) * self.MARK_S + jumped
            pacer.pace(self.resv.PACE_SLICE, self.level(mark))


def content_pacer(resv, monkeypatch, rate, density):
    clock = FakeClock(0.0)
    monkeypatch.setattr(resv, "time", clock)
    content = Content(resv, rate, density)
    pacer = resv.Pacer(content.parser, resv.Reservoir(threading.Event()))
    clock.now += 20                                                   # past the headstart window
    content.run(pacer, 1.0)                                           # anchors, drops the 20s idle
    return clock, content, pacer


@pytest.mark.parametrize("density", [0.5, 1.0, 1.3, 2.0])
def test_the_release_follows_content_time_not_the_average_rate(resv, monkeypatch, density):
    """The content rate is a 90s average. Paced on bytes, content denser than
    that average went out under realtime: after a drop in bitrate ended, the
    average still held the low rate and the player was fed 40% of realtime
    (discussion #46). The release now takes as long as the content it carries."""
    rate = 1_000_000
    clock, content, pacer = content_pacer(resv, monkeypatch, rate, density * rate)
    start = clock.now
    content.run(pacer, 60.0)
    assert clock.now - start == pytest.approx(60.0, abs=0.05)


@pytest.mark.parametrize("density, expected", [(10.0, 60.0 * 10 / 4), (0.1, 60.0 * 0.1 * 4)])
def test_content_time_is_trusted_within_four_times_the_byte_time(resv, monkeypatch, density, expected):
    """Far outside 1/4..4x the byte-rate time, the PCR is not believed: a
    stretch that sparse or that dense is a broken clock, not content."""
    rate = 1_000_000
    clock, content, pacer = content_pacer(resv, monkeypatch, rate, density * rate)
    start = clock.now
    content.run(pacer, 60.0)
    assert clock.now - start == pytest.approx(expected, rel=0.01)


def test_a_garbage_pcr_jump_costs_a_fraction_of_a_second(resv, monkeypatch):
    """A forward jump under 10s passes the parser's plausibility window. Slept
    on, it would freeze the output for the whole jump: the -re failure that
    invariant #1 exists for."""
    rate = 1_000_000
    clock, content, pacer = content_pacer(resv, monkeypatch, rate, rate)
    start = clock.now
    content.run(pacer, 20.0, jump=9.0)
    assert 20.0 <= clock.now - start < 20.2


class LongestSleep(FakeClock):
    def __init__(self):
        super().__init__(0.0)
        self.longest = 0.0

    def sleep(self, s):
        self.longest = max(self.longest, s)
        super().sleep(s)


def test_a_garbage_pcr_after_a_run_of_rejects_costs_a_quarter_second(resv, monkeypatch):
    """The parser re-anchors after 25 rejected PCRs, ~1s of content: up to
    then nothing advances, and one garbage sample accepted at the end of the
    run used to be trued up to 4x all those bytes, a ~3s sleep."""
    rate = 1_000_000
    clock = LongestSleep()
    monkeypatch.setattr(resv, "time", clock)
    content = Content(resv, rate, rate)
    pacer = resv.Pacer(content.parser, resv.Reservoir(threading.Event()))
    clock.now += 20
    content.run(pacer, 1.0)
    clock.longest = 0.0
    frozen = content.level(content.released // Content.MARK_S * Content.MARK_S)
    for _ in range(int(0.95 * rate / resv.PACE_SLICE)):              # 0.95s of rejects
        pacer.pace(resv.PACE_SLICE, frozen)
    pacer.pace(resv.PACE_SLICE, content.level(frozen.released_s + 9.0))
    assert clock.longest < 0.3


def test_a_flush_is_not_trued_up(resv, monkeypatch):
    """A flush empties the reservoir between two slices: the content position
    jumps by everything flushed. None of it went out, so none of it is owed."""
    rate = 1_000_000
    clock = LongestSleep()
    monkeypatch.setattr(resv, "time", clock)
    content = Content(resv, rate, rate)
    pacer = resv.Pacer(content.parser, resv.Reservoir(threading.Event()))
    clock.now += 20
    released = 0
    for _ in range(200):                                              # ~2.4s, through measure() as pump() does
        released += resv.PACE_SLICE
        pacer.measure(released)
        content.released = released / rate
        pacer.pace(resv.PACE_SLICE, content.level(content.released // Content.MARK_S * Content.MARK_S))
    clock.longest = 0.0
    released += 30 * rate                                             # 30s flushed
    pacer.measure(released)
    content.released = released / rate
    pacer.pace(resv.PACE_SLICE, content.level(content.released // Content.MARK_S * Content.MARK_S))
    assert clock.longest < 0.03                                       # one slice is 12ms


def test_a_stuck_pcr_falls_back_to_the_byte_rate(resv, monkeypatch):
    """No PCR advance (a run of rejects, a lost PCR PID): the release keeps
    the byte rate instead of going out unpaced, and does not burst when the
    clock moves again."""
    rate = 1_000_000
    clock, content, pacer = content_pacer(resv, monkeypatch, rate, rate)
    start = clock.now
    frozen = content.level(content.released // Content.MARK_S * Content.MARK_S)
    slices = int(5.0 * rate / resv.PACE_SLICE)
    for _ in range(slices):                                           # 5s of bytes, clock frozen
        pacer.pace(resv.PACE_SLICE, frozen)
        content.released += resv.PACE_SLICE / rate
    assert clock.now - start == pytest.approx(slices * resv.PACE_SLICE / rate, abs=0.05)
    mid = clock.now                                                   # the clock resumes 5s on
    content.run(pacer, 10.0)
    assert clock.now - mid == pytest.approx(10.0, abs=0.1)


def test_the_release_factor_applies_to_content_time(resv, monkeypatch):
    rate = 1_000_000
    clock, content, pacer = content_pacer(resv, monkeypatch, rate, 2 * rate)
    content.lead = 45.0                                               # over the 30s target: 1.15x
    start = clock.now
    content.run(pacer, 60.0)
    assert clock.now - start == pytest.approx(60.0 / 1.15, abs=0.05)

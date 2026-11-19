"""RESV_DECODE_MONITOR: count decoder error bursts on a copy of the released
video and log a would-fire line; never touch the stream itself."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RESERVOIR_PATH = REPO_ROOT / "reservoarr.py"

CONCEAL = "[h264 @ 0x1] concealing 1620 DC, 1620 AC, 1620 MV errors in P frame\n"
MB = "[h264 @ 0x1] error while decoding MB 33 21, bytestream -7\n"


def load(tmp_log_dir, **env):
    os.environ["RESV_LOG_DIR"] = str(tmp_log_dir)
    os.environ.update(env)
    sys.argv = ["reservoarr", "http://offline/unit-test"]
    spec = importlib.util.spec_from_file_location("resv_decode", str(RESERVOIR_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for key in env:
        os.environ.pop(key, None)
    return mod


def test_off_by_default(tmp_path):
    assert load(tmp_path).DECODE_MONITOR is False
    assert load(tmp_path, RESV_DECODE_MONITOR="1").DECODE_MONITOR is True
    assert load(tmp_path).Stream("http://offline/x", "ua").decode is None


def test_three_bursts_in_window_fire_once(tmp_path):
    resv = load(tmp_path)
    m = resv.DecodeMonitor(now=0.0)
    t = resv.DECODE_GRACE_S + 1
    assert m.observe(CONCEAL, t) is None
    assert m.observe(MB, t + 0.5) is None                             # same burst
    assert m.observe(CONCEAL, t + 10) is None
    detail = m.observe(CONCEAL, t + 20)
    assert detail and detail.startswith("3 bursts in 20s")
    assert m.fired == 1 and m.errors == 4
    assert m.observe(CONCEAL, t + 30) is None                         # counting restarts after a fire


def test_lines_within_the_gap_are_one_burst(tmp_path):
    resv = load(tmp_path)
    m = resv.DecodeMonitor(now=0.0)
    t = resv.DECODE_GRACE_S + 1
    for i in range(50):
        assert m.observe(CONCEAL, t + i * 1.5) is None                # each line inside the 2s gap
    assert m.fired == 0


def test_bursts_outside_the_window_do_not_add_up(tmp_path):
    resv = load(tmp_path)
    m = resv.DecodeMonitor(now=0.0)
    t = resv.DECODE_GRACE_S + 1
    for i in range(10):
        assert m.observe(CONCEAL, t + i * 31) is None                 # at most 2 per 60s
    assert m.fired == 0


def test_grace_and_other_lines_are_ignored(tmp_path):
    resv = load(tmp_path)
    m = resv.DecodeMonitor(now=0.0)
    for i in range(5):
        assert m.observe(CONCEAL, i * 3.0) is None                    # tune-in, mid-GOP
    t = resv.DECODE_GRACE_S + 1
    for i in range(5):
        assert m.observe("[mpegts @ 0x1] PES packet size mismatch\n", t + i * 3.0) is None
    assert m.errors == 0


def test_full_queue_drops_and_quiets_without_blocking(tmp_path):
    resv = load(tmp_path)
    m = resv.DecodeMonitor(now=0.0)
    for _ in range(resv.DECODE_QUEUE_CHUNKS + 3):
        m.feed(b"\x47" * 188)                                         # no analyzer draining it
    assert m.dropped == 3
    assert m.quiet_until > 0 and m.observe(CONCEAL, m.quiet_until - 1) is None


def test_missing_analyzer_turns_it_off(tmp_path):
    resv = load(tmp_path, RESV_FFMPEG_BIN=str(tmp_path / "no-ffmpeg"))
    assert resv.DecodeMonitor().start() is False
    assert "analyzer did not start" in (tmp_path / "delaybuf.log").read_text()

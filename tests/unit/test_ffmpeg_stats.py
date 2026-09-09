"""RESV_FFMPEG_STATS: hand Dispatcharr's buffering watchdog the line it reads,
without letting the progress line rotate delaybuf.log away.

The watchdog parses `speed=` only off a stderr line that also carries `frame=`,
and ffmpeg terminates that line with \\r — which readline() never yielded.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RESERVOIR_PATH = REPO_ROOT / "reservoarr.py"

PROGRESS = b"frame=  100 fps= 25 q=-1.0 size=N/A time=00:00:04.00 bitrate=N/A speed=0.25x\r"


def load(tmp_log_dir, **env):
    """Fresh module with the environment this test needs. Mirrors conftest's
    loader; the env keys are removed again so the next load starts clean."""
    os.environ["RESV_LOG_DIR"] = str(tmp_log_dir)
    os.environ.update(env)
    sys.argv = ["reservoarr", "http://offline/unit-test"]
    spec = importlib.util.spec_from_file_location("resv_stats", str(RESERVOIR_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for key in env:
        os.environ.pop(key, None)
    return mod


class FakeStderr:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def read(self, _size):
        return self._chunks.pop(0) if self._chunks else b""


class FakeFfmpeg:
    def __init__(self, chunks):
        self.stderr = FakeStderr(chunks)


def test_stats_flag_is_opt_in(tmp_path):
    assert "-stats" not in load(tmp_path).FFMPEG_CMD
    assert "-stats" in load(tmp_path, RESV_FFMPEG_STATS="1").FFMPEG_CMD


def test_progress_lines_reach_stderr_split_on_cr(tmp_path, capsys):
    resv = load(tmp_path, RESV_FFMPEG_STATS="1")

    resv.stderr_watcher(FakeFfmpeg([PROGRESS + PROGRESS, b"[mpegts @ 0x1] pes packet size mismatch\n"]))

    err = capsys.readouterr().err
    assert err.count("speed=0.25x") == 2
    assert "pes packet size mismatch" in err


def test_progress_lines_stay_out_of_the_journal(tmp_path, capsys):
    resv = load(tmp_path, RESV_FFMPEG_STATS="1")

    resv.stderr_watcher(FakeFfmpeg([PROGRESS, b"[mpegts @ 0x1] pes packet size mismatch\n"]))
    capsys.readouterr()

    journal = Path(resv.LOG_FILE).read_text()
    assert "speed=" not in journal
    assert "pes packet size mismatch" in journal


def test_corruption_detector_still_sees_its_line(tmp_path, capsys):
    resv = load(tmp_path, RESV_FFMPEG_STATS="1")
    seen = []
    resv.register_corrupt = lambda dts: seen.append(dts)

    resv.stderr_watcher(FakeFfmpeg([PROGRESS + b"Packet corrupt (stream = 0, dts = 501541200).\n"]))
    capsys.readouterr()

    assert seen == [501541200]

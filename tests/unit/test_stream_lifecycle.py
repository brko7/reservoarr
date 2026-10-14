"""6.4.0: the release side of a Stream.

- Telemetry keeps coming while the reservoir is dry. The stats line was
  written only after a slice was released, so an upstream outage that drained
  the cushion to zero logged nothing at all - no `cushion=0s` line, exactly
  when it mattered.
- A dead ffmpeg ends the stream. Its death was noticed only by the next write
  to its stdin; with the reservoir empty there is none, and the channel sat on
  dead air instead of ending so Dispatcharr could retry.
"""
from __future__ import annotations

import io
import subprocess
import sys
import threading
import time
from pathlib import Path


class FakeFfmpeg:
    def __init__(self):
        self.stdin = io.BytesIO()


def run_pump(stream, ff, seconds):
    t = threading.Thread(target=stream.pump, args=(ff, None), daemon=True)
    t.start()
    time.sleep(seconds)
    stream.shutdown()
    t.join(3)
    assert not t.is_alive()


def stats_lines(resv):
    return [line for line in Path(resv.LOG_FILE).read_text().splitlines() if "cushion=" in line]


def test_a_dry_reservoir_still_reports_its_cushion(resv, stream):
    resv.STATS_EVERY_S = 0.2
    run_pump(stream, FakeFfmpeg(), 1.8)          # pump wakes every 0.5s when dry
    lines = stats_lines(resv)
    assert len(lines) >= 2, lines
    assert "cushion=0s(byte) buf=0.0MB out=0.00Mbps" in lines[-1]


def test_a_dry_window_resets_the_corruption_count(resv, stream):
    """#5 counts windows 'while bytes still flow'; a dry window breaks the run."""
    det = stream.detector
    assert det.window(10, 0, 100) is None
    assert det.window(10, 0, 100) is None                 # dry: nothing ingested
    assert det.window(20, 0, 200) is None
    assert det.window(30, 0, 300) is not None


def test_everything_in_the_reservoir_reaches_ffmpeg_in_order(resv, stream):
    data = bytes(range(256)) * 188 * 3
    for i in range(0, len(data), resv.CHUNK):
        stream.reservoir.put(data[i:i + resv.CHUNK])
    stream.reservoir.close()
    ff = FakeFfmpeg()
    stream.pump(ff, None)                                 # returns once drained after EOF
    assert ff.stdin.getvalue() == data


def spawn(code):
    return subprocess.Popen([sys.executable, "-c", code], stderr=subprocess.PIPE, bufsize=0)


def test_ffmpeg_dying_on_its_own_ends_the_stream(resv, stream, capsys):
    stream.watch_ffmpeg(spawn("import sys; sys.stderr.write('Killed\\n'); sys.exit(137 - 128)"))
    assert stream.stop.is_set()
    err = capsys.readouterr().err
    assert "ffmpeg: Killed" in err
    assert "ffmpeg exited on its own (rc=9); shutting down" in err


def test_ffmpeg_stopped_by_us_is_not_reported(resv, stream, capsys):
    stream.stop.set()
    stream.watch_ffmpeg(spawn("import sys; sys.exit(255)"))
    assert "exited on its own" not in capsys.readouterr().err


def test_a_dead_ffmpeg_wakes_a_pump_waiting_on_a_dry_reservoir(resv, stream):
    t = threading.Thread(target=stream.pump, args=(FakeFfmpeg(), None), daemon=True)
    t.start()
    time.sleep(0.2)
    stream.watch_ffmpeg(spawn("import sys; sys.exit(1)"))
    t0 = time.monotonic()
    t.join(3)
    assert not t.is_alive()
    assert time.monotonic() - t0 < 0.3, "woken by its own 0.5s timeout, not by the shutdown"

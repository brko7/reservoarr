from __future__ import annotations

from pathlib import Path

import pytest

from .harness import run_pipeline

RATE_BPS = 300_010
VIDEO_PID = 0x100
SYNTH_DURATION_S = 180


def pes_seconds(packet: bytes, start: int) -> float:
    b = packet[start + 9:start + 14]
    return (((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1) / 90000.0


def drop_video_half_the_time(src: Path, dst: Path, period_s: float = 8.0, missing_s: float = 4.0) -> None:
    data = src.read_bytes()
    out = bytearray()
    first = None
    dropping = False
    for off in range(0, len(data) - 187, 188):
        packet = data[off:off + 188]
        pid = (packet[1] & 0x1F) << 8 | packet[2]
        if pid == VIDEO_PID and packet[1] & 0x40:
            start = 4 + (1 + packet[4] if (packet[3] >> 4) & 2 else 0)
            t = pes_seconds(packet, start)
            first = t if first is None else first
            dropping = (t - first) % period_s >= period_s - missing_s
        if not (pid == VIDEO_PID and dropping):
            out += packet
    dst.write_bytes(bytes(out))


@pytest.fixture(scope="module")
def holed_ts(synth_ts, tmp_path_factory):
    dst = tmp_path_factory.mktemp("holes") / "holed.ts"
    drop_video_half_the_time(synth_ts, dst)
    return dst


@pytest.mark.e2e
def test_a_clean_source_reports_no_holes(tmp_path, synth_ts):
    run = run_pipeline(tmp_path, synth_ts, rate_bps=RATE_BPS, duration_s=50)

    holes = [s["holes"] for s in run.stats_lines()]
    assert holes, f"no telemetry\nstderr:\n{run.resv_stderr}"
    assert set(holes) == {0}, f"holes on a clean source: {holes}"


@pytest.mark.e2e
def test_a_source_missing_half_its_video_reports_it(tmp_path, holed_ts):
    run = run_pipeline(tmp_path, holed_ts, rate_bps=holed_ts.stat().st_size / SYNTH_DURATION_S, duration_s=50)

    holes = [s["holes"] for s in run.stats_lines()]
    assert holes, f"no telemetry\nstderr:\n{run.resv_stderr}"
    assert max(holes) >= 30, f"holes per window: {holes}"

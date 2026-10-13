from __future__ import annotations

from pathlib import Path

import pytest

from .harness import run_pipeline

VIDEO_PID = 0x100
AUDIO_PID = 0x101
SYNTH_DURATION_S = 180


def pes_seconds(packet: bytes, start: int) -> float:
    b = packet[start + 9:start + 14]
    return (((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1) / 90000.0


def in_gap(at: float, period_s: float | None, missing_s: float, after_s: float) -> bool:
    if period_s is None:
        return at >= after_s
    return at % period_s >= period_s - missing_s


def drop(
    src: Path, dst: Path, pid: int, period_s: float | None = 8.0, missing_s: float = 4.0,
    after_s: float = 0.0,
) -> None:
    data = src.read_bytes()
    out = bytearray()
    first = None
    dropping = False
    for off in range(0, len(data) - 187, 188):
        packet = data[off:off + 188]
        packet_pid = (packet[1] & 0x1F) << 8 | packet[2]
        if packet_pid == pid and packet[1] & 0x40:
            start = 4 + (1 + packet[4] if (packet[3] >> 4) & 2 else 0)
            t = pes_seconds(packet, start)
            first = t if first is None else first
            dropping = in_gap(t - first, period_s, missing_s, after_s)
        if not (packet_pid == pid and dropping):
            out += packet
    dst.write_bytes(bytes(out))


def derived(synth_ts: Path, folder: Path, name: str, **kwargs) -> Path:
    dst = folder / name
    drop(synth_ts, dst, **kwargs)
    return dst


@pytest.fixture(scope="module")
def holed(synth_ts, tmp_path_factory):
    folder = tmp_path_factory.mktemp("holes")
    return {
        "video": derived(synth_ts, folder, "video.ts", pid=VIDEO_PID),
        "audio": derived(synth_ts, folder, "audio.ts", pid=AUDIO_PID),
        "mute": derived(synth_ts, folder, "mute.ts", pid=AUDIO_PID, period_s=None, after_s=20),
    }


def telemetry(tmp_path: Path, capture: Path) -> list[tuple[int, int]]:
    run = run_pipeline(tmp_path, capture, rate_bps=capture.stat().st_size / SYNTH_DURATION_S, duration_s=50)
    lines = [(s["vholes"], s["aholes"]) for s in run.stats_lines()]
    assert lines, f"no telemetry\nstderr:\n{run.resv_stderr}"
    return lines


@pytest.mark.e2e
def test_a_clean_source_reports_no_holes(tmp_path, synth_ts):
    assert set(telemetry(tmp_path, synth_ts)) == {(0, 0)}


@pytest.mark.e2e
def test_a_source_missing_half_its_video_reports_it(tmp_path, holed):
    lines = telemetry(tmp_path, holed["video"])
    assert max(v for v, _ in lines) >= 30, f"per window: {lines}"
    assert {a for _, a in lines} == {0}, f"per window: {lines}"


@pytest.mark.e2e
def test_a_source_missing_half_its_sound_reports_it(tmp_path, holed):
    lines = telemetry(tmp_path, holed["audio"])
    assert max(a for _, a in lines) >= 30, f"per window: {lines}"
    assert {v for v, _ in lines} == {0}, f"per window: {lines}"


@pytest.mark.e2e
def test_sound_that_stops_while_the_picture_runs_reports_all_of_it_missing(tmp_path, holed):
    lines = telemetry(tmp_path, holed["mute"])
    assert (0, 100) in lines, f"per window: {lines}"


@pytest.mark.e2e
def test_a_reconnect_does_not_read_as_a_missing_track(tmp_path, synth_ts):
    run = run_pipeline(tmp_path, synth_ts, rate_bps=300_010, duration_s=75, eof_at=40.0)
    lines = [(s["vholes"], s["aholes"]) for s in run.stats_lines()]
    assert "upstream EOF" in run.log_text(), f"no EOF happened\nstderr:\n{run.resv_stderr}"
    assert all(v < 100 and a < 100 for v, a in lines), f"per window: {lines}"

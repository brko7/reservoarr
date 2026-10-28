"""End-to-end harness. Spawns cdn_sim + reservoarr.py as real subprocesses and
collects: the output TS (stdout of reservoarr.py = ffmpeg's stdout = the pipe
Dispatcharr would consume), reservoarr.py's stderr (lifecycle + ffmpeg warnings),
and delaybuf.log (15s telemetry stats).

This is the same shape as the in-container smoke harness — the only difference
is that the upstream is a local cdn_sim instead of the real CDN, and ffmpeg
is the system binary. Everything else (stdlib-only reservoarr.py, real ffmpeg
subprocess, real urllib HTTP client) is the production code path."""
from __future__ import annotations

import bisect
import contextlib
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RESERVOIR = REPO_ROOT / "reservoarr.py"
CDN_SIM = REPO_ROOT / "tools" / "cdn_sim.py"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait_listening(port: int, deadline_s: float = 5.0) -> None:
    end = time.time() + deadline_s
    while time.time() < end:
        with contextlib.suppress(OSError), socket.socket() as s:
            s.settimeout(0.2)
            s.connect(("127.0.0.1", port))
            return
        time.sleep(0.05)
    raise RuntimeError(f"cdn_sim never started listening on :{port}")


@dataclass
class Run:
    out_ts: Path
    log_file: Path
    cdn_stdout: str
    resv_stderr: str
    duration_s: float
    arrivals: list[tuple[float, int]] = field(default_factory=list)   # (wall s, output bytes so far)

    def stats_lines(self) -> list[dict]:
        """Parse the delaybuf.log telemetry lines into dicts.
        Format: cushion=Ns(pcr) buf=…MB out=…Mbps in=…Mbps crate=…Mbps in_total=…MB
                reconnects=N ccerr=N pcrrej=N disc=N sync=N pcr_back=N
        """
        pat = re.compile(
            r"cushion=(?P<cushion>\d+)s\((?P<src>pcr|byte)\)\s+"
            r"buf=(?P<buf_mb>[\d.]+)MB\s+"
            r"out=(?P<out_mbps>[\d.]+)Mbps\s+"
            r"in=(?P<in_mbps>[\d.]+)Mbps\s+"
            r"crate=(?P<crate_mbps>[\d.]+)Mbps\s+"
            r"in_total=(?P<in_total_mb>\d+)MB\s+"
            r"reconnects=(?P<reconnects>\d+)\s+"
            r"ccerr=(?P<ccerr>\d+)\s+"
            r"pcrrej=(?P<pcrrej>\d+)\s+"
            r"disc=(?P<disc>\d+)\s+"
            r"sync=(?P<sync>\d+)\s+"
            r"pcr_back=(?P<pcr_back>\d+)"
            r"(?:\s+vholes=(?P<vholes>\d+)%\s+aholes=(?P<aholes>\d+)%)?"
        )
        out = []
        for line in self.log_file.read_text().splitlines():
            m = pat.search(line)
            if m:
                d = {k: float(v) if "." in v else int(v) for k, v in m.groupdict().items()
                     if k not in ("src",) and v is not None}
                d["src"] = m.group("src")
                d["raw"] = line
                out.append(d)
        return out

    def log_text(self) -> str:
        return self.log_file.read_text()


def run_pipeline(
    tmp_path: Path,
    capture: Path,
    rate_bps: float,
    duration_s: float = 75.0,
    front_s: float = 25.0,
    stalls: list[tuple[float, float]] | None = None,
    eof_at: float | None = None,
    corrupt_from: float | None = None,
    corrupt_rate: int = 5,
    replay_skip: bool = False,
    pcr_paced: bool = False,
    ts_channel: str | None = None,
    log_dir: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> Run:
    """Run cdn_sim + reservoarr.py for duration_s seconds; return collected
    artifacts. Caller asserts on Run.stats_lines() / Run.log_text() / out_ts.

    TIMEBASE: cdn_sim event times (stalls, eof_at, corrupt_from) are on the
    sim's *edge clock*, which starts at front_s — the edge pretends front_s
    seconds of content already exist when the server starts. Wall-clock time
    of an event ≈ its edge time minus front_s. E.g. with front_s=25, a stall
    at 30 begins ~5s after the client connects."""
    port = _free_port()
    out_ts = tmp_path / "out.ts"
    log_dir = log_dir or tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "delaybuf.log"

    cdn_argv = [sys.executable, str(CDN_SIM), str(capture), str(rate_bps),
                "--port", str(port), "--front", str(front_s)]
    if pcr_paced:
        cdn_argv.append("--pcr")
    for s, d in stalls or []:
        cdn_argv += ["--stall", f"{s}:{d}"]
    if eof_at:
        cdn_argv += ["--eof-at", str(eof_at)]
    if corrupt_from is not None:
        cdn_argv += ["--corrupt-from", str(corrupt_from), "--corrupt-rate", str(corrupt_rate)]

    env = os.environ.copy()
    # Strip any RESV_* tuning from the developer's shell so a stray export
    # (e.g. RESV_TS_RECONNECT=1, RESV_STALL_S) can't change test behaviour.
    # RESV_FFMPEG_BIN is kept: it's a path, not a tunable, and the justfile
    # sets it deliberately.
    for k in [k for k in env if k.startswith("RESV_") and k != "RESV_FFMPEG_BIN"]:
        env.pop(k)
    env["RESV_LOG_DIR"] = str(log_dir)
    if replay_skip:
        env["RESV_REPLAY_SKIP"] = "1"
    if ts_channel:
        env["RESV_TS_CHANNEL"] = ts_channel
    env.update(extra_env or {})
    # Use the ffmpeg path the test runner picked (mac vs linux differ).
    if "RESV_FFMPEG_BIN" not in env:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            env["RESV_FFMPEG_BIN"] = ffmpeg
    # Speed up unit-level checks of the stats loop by sampling more often:
    # the script logs every STATS_EVERY_S = 15.0 (hardcoded). Tests rely on
    # the 15s cadence — DON'T env-override it; the test durations are sized
    # around it. (If a future tweak adds an env knob, document and use it.)

    cdn = subprocess.Popen(cdn_argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        _wait_listening(port)
        with open(out_ts, "wb") as f:
            resv = subprocess.Popen(
                [sys.executable, str(RESERVOIR),
                 f"http://127.0.0.1:{port}/sim-test", "Mozilla/5.0"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            )
            arrivals: list[tuple[float, int]] = []
            # The player's side: the output lands in out_ts, and when each
            # byte arrived is kept for player_lead().
            copier = threading.Thread(target=_copy_out, args=(resv.stdout, f, arrivals), daemon=True)
            copier.start()
            try:
                t0 = time.time()
                resv.wait(timeout=duration_s)
                wall = time.time() - t0
            except subprocess.TimeoutExpired:
                resv.terminate()
                try:
                    resv.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    resv.kill()
                    resv.wait()
                wall = duration_s
            copier.join(timeout=10)
            if copier.is_alive():                                     # ffmpeg outlived reservoarr, holding stdout
                raise RuntimeError("reservoarr's output pipe stayed open 10s after it exited; out_ts is incomplete")
            resv_err = resv.stderr.read().decode("utf-8", "replace")
    finally:
        cdn.terminate()
        try:
            cdn_out = cdn.stdout.read() if cdn.stdout else ""
            cdn.wait(timeout=3)
        except subprocess.TimeoutExpired:
            cdn.kill()
            cdn.wait()
            cdn_out = ""

    return Run(out_ts=out_ts, log_file=log_file, cdn_stdout=cdn_out or "",
               resv_stderr=resv_err, duration_s=wall, arrivals=arrivals)


def _copy_out(src, dst, arrivals):
    t0 = time.monotonic()
    total = 0
    for chunk in iter(lambda: src.read1(65536), b""):
        dst.write(chunk)
        total += len(chunk)
        arrivals.append((time.monotonic() - t0, total))


def player_lead(run: Run) -> list[tuple[float, float]]:
    """(wall s, lead s) for every PCR of the output: how far the content
    that has reached the player runs ahead of realtime, both clocks counted
    from the first output PCR. A player that starts with B seconds buffered
    stalls once the lead falls more than B under where it started."""
    data = run.out_ts.read_bytes()
    ends = [n for _, n in run.arrivals]
    lead, first, pid0 = [], None, None
    for i in range(0, len(data) - 187, 188):
        if data[i] != 0x47 or not data[i + 3] & 0x20 or data[i + 4] < 7 or not data[i + 5] & 0x10:
            continue
        pid = (data[i + 1] & 0x1F) << 8 | data[i + 2]
        pid0 = pid if pid0 is None else pid0
        if pid != pid0:
            continue
        b = data[i + 6:i + 11]
        pcr = (b[0] << 25 | b[1] << 17 | b[2] << 9 | b[3] << 1 | b[4] >> 7) / 90000.0
        k = bisect.bisect_left(ends, i + 188)
        if k == len(ends):
            break
        wall = run.arrivals[k][0]
        if first is None:
            first = (wall, pcr)
        lead.append((wall - first[0], (pcr - first[1]) - (wall - first[0])))
    return lead


def ffprobe_streams(path: Path) -> list[dict]:
    """Return the streams from `ffprobe -show_streams` as a list of dicts.
    Uses JSON output to avoid the wrapper-vs-no-wrapper parsing gotcha.
    Honours FFPROBE_BIN (exported by the justfile's e2e recipe) so sandboxed
    runs with a stripped PATH still find the binary."""
    import json
    ffprobe = os.environ.get("FFPROBE_BIN") or shutil.which("ffprobe") or "ffprobe"
    out = subprocess.check_output(
        [ffprobe, "-v", "error", "-show_streams", "-of", "json", str(path)],
        text=True,
    )
    return json.loads(out).get("streams", [])


def ffprobe_video_pts(path: Path) -> list[float]:
    ffprobe = os.environ.get("FFPROBE_BIN") or shutil.which("ffprobe") or "ffprobe"
    out = subprocess.check_output(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
         "-of", "csv=p=0", str(path)],
        text=True,
    )
    values = (line.split(",")[0].strip() for line in out.splitlines())
    return [float(v) for v in values if v and v != "N/A"]


def ffprobe_audio_packets(path: Path) -> list[tuple[float, float]]:
    ffprobe = os.environ.get("FFPROBE_BIN") or shutil.which("ffprobe") or "ffprobe"
    out = subprocess.check_output(
        [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
         "packet=pts_time,duration_time", "-of", "csv=p=0", str(path)],
        text=True,
    )
    packets = []
    for line in out.splitlines():
        fields = [f.strip() for f in line.split(",")]
        if len(fields) >= 2 and "N/A" not in fields[:2] and all(fields[:2]):
            packets.append((float(fields[0]), float(fields[1])))
    return packets

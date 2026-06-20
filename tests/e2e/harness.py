"""End-to-end harness. Spawns cdn_sim + reservoarr.py as real subprocesses and
collects: the output TS (stdout of reservoarr.py = ffmpeg's stdout = the pipe
Dispatcharr would consume), reservoarr.py's stderr (lifecycle + ffmpeg warnings),
and delaybuf.log (15s telemetry stats).

This is the same shape as the in-container smoke harness — the only difference
is that the upstream is a local cdn_sim instead of the real CDN, and ffmpeg
is the system binary. Everything else (stdlib-only reservoarr.py, real ffmpeg
subprocess, real urllib HTTP client) is the production code path."""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
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

    def stats_lines(self) -> list[dict]:
        """Parse the delaybuf.log telemetry lines into dicts.
        Format: cushion=Ns(pcr) buf=…MB out=…Mbps in=…Mbps crate=…Mbps in_total=…MB
                reconnects=N ccerr=N pcrrej=N disc=N sync=N
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
            r"sync=(?P<sync>\d+)"
        )
        out = []
        for line in self.log_file.read_text().splitlines():
            m = pat.search(line)
            if m:
                d = {k: float(v) if "." in v else int(v) for k, v in m.groupdict().items()
                     if k not in ("src",)}
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
) -> Run:
    """Run cdn_sim + reservoarr.py for duration_s seconds; return collected
    artifacts. Caller asserts on Run.stats_lines() / Run.log_text() / out_ts."""
    port = _free_port()
    out_ts = tmp_path / "out.ts"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = log_dir / "delaybuf.log"

    cdn_argv = [sys.executable, str(CDN_SIM), str(capture), str(rate_bps),
                "--port", str(port), "--front", str(front_s)]
    for s, d in stalls or []:
        cdn_argv += ["--stall", f"{s}:{d}"]
    if eof_at:
        cdn_argv += ["--eof-at", str(eof_at)]
    if corrupt_from is not None:
        cdn_argv += ["--corrupt-from", str(corrupt_from), "--corrupt-rate", str(corrupt_rate)]

    env = os.environ.copy()
    env["RESV_LOG_DIR"] = str(log_dir)
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
                stdout=f, stderr=subprocess.PIPE, env=env,
            )
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
               resv_stderr=resv_err, duration_s=wall)


def ffprobe_streams(path: Path) -> list[dict]:
    """Return the streams from `ffprobe -show_streams` as a list of dicts.
    Uses JSON output to avoid the wrapper-vs-no-wrapper parsing gotcha."""
    import json
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        text=True,
    )
    return json.loads(out).get("streams", [])

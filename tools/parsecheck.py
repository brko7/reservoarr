#!/usr/bin/env python3
"""Offline TsParser validation. Feeds a captured (or synthetic) TS file
chunk-by-chunk the way the fetcher does, then compares the PCR-derived
content rate / clock against ffprobe ground truth.

Useful for:
- spot-checking that a real CDN capture parses cleanly (ccerr/sync should be 0)
- confirming the synthetic fixture's PCR matches its muxrate within ~1%

Usage: parsecheck.py <capture.ts> [<path/to/reservoir.py>]
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path


def load_reservoir(path):
    # The script reads sys.argv at import time; give it a safe placeholder.
    sys.argv = ["reservoir", "http://offline/parser-test"]
    # Point its log dir away from /data/scripts/logs so dev machines work.
    os.environ.setdefault("RESV_LOG_DIR", "/tmp/reservoarr-parsecheck")
    spec = importlib.util.spec_from_file_location("resv", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ffprobe_duration(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        text=True,
    ).strip()
    return float(out)


def main():
    cap = Path(sys.argv[1])
    script = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parent.parent / "reservoir.py"

    mod = load_reservoir(str(script))
    data = cap.read_bytes()
    p = mod.TsParser()
    t0 = time.time()
    for i in range(0, len(data), mod.CHUNK):
        c = data[i:i + mod.CHUNK]
        mod.in_total += len(c)                                # the parser keys cushion marks on this global
        p.feed(c)
    dt = time.time() - t0

    rate = p.content_rate()
    print(f"bytes={len(data)} parse_time={dt:.2f}s throughput={len(data) / 1e6 / dt:.0f}MB/s")
    if rate:
        print(f"content_rate={rate * 8 / 1e6:.3f}Mbps")
    print(f"cum_pcr={p.cum_pcr:.1f}s")
    try:
        truth = ffprobe_duration(str(cap))
        diff_pct = abs(p.cum_pcr - truth) / truth * 100
        print(f"ffprobe duration={truth:.1f}s  diff={diff_pct:.2f}%")
    except FileNotFoundError:
        print("(ffprobe not on PATH; skipping ground-truth comparison)")
    except subprocess.CalledProcessError as e:
        print(f"(ffprobe failed: {e})")
    print(f"ccerr={p.cc_errors} pcrrej={p.pcr_rejects} disc={p.pcr_disc} "
          f"sync={p.sync_losses} pids={len(p.cc)} pcr_pid={p.pcr_pid}")


if __name__ == "__main__":
    main()

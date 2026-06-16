#!/usr/bin/env python3
"""Take a clean MPEG-TS file and produce a copy with CC-field corruption
injected at a specified rate, starting at an offset. This is the standalone
generator used to seed the corruption-detector e2e test independently of
cdn_sim (which has its own in-band injector).

The corruption shape matches the real 2026-06-16 incident: a sustained low
rate of CC errors on payload packets, with DIFFERENT dts values each time
(so the old same-dts-3x stderr detector can't catch it).

Usage: make_corrupt_ts.py <in.ts> <out.ts> [--from BYTE_OFFSET] [--every N]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inp")
    ap.add_argument("outp")
    ap.add_argument("--from", dest="offset", type=int, default=0,
                    help="byte offset to start corruption (0 = whole file)")
    ap.add_argument("--every", type=int, default=2000,
                    help="corrupt one CC field per N packets")
    args = ap.parse_args()

    data = bytearray(Path(args.inp).read_bytes())
    n = len(data)

    # Sync to the first 0x47 boundary at or after `offset`.
    start = args.offset
    while start < n and data[start] != 0x47:
        start += 1

    corrupted = 0
    seen = 0
    for p in range(start, n - 188, 188):
        if data[p] != 0x47:
            return                                            # lost sync; bail rather than smear
        afc = (data[p + 3] >> 4) & 0x3
        if not (afc & 0x1):                                   # payload only
            continue
        seen += 1
        if seen % args.every == 0:
            data[p + 3] ^= 0x05                               # jump CC by 5 — guaranteed discontinuity
            corrupted += 1

    Path(args.outp).write_bytes(bytes(data))
    print(f"corrupted {corrupted} CC fields out of {seen} payload packets in {args.outp}")


if __name__ == "__main__":
    sys.exit(main() or 0)

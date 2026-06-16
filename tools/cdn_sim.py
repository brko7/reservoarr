#!/usr/bin/env python3
"""CDN simulator for reservoir testing. Replays a captured (or synthetic) TS
file over HTTP with the IPTorrents-IPTV delivery shape:

- a live edge that advances at the file's real byte rate
- each connection instantly receives FRONT_S seconds of backlog (front-load
  burst — the per-connection grace the reservoir's pacing depends on)
- STALLS: windows where the server withholds output while the edge keeps
  advancing; on resume the held backlog flushes at line speed (catch-up burst)
- optional one-shot forced EOF mid-test (exercises the reconnect path)
- optional CC-corruption injection in served packets (exercises detector #5)

Usage:
    cdn_sim.py <capture.ts> <rate_bytes_per_s> [--port N] [--front S]
               [--stall START:DURATION ...] [--eof-at S]
               [--corrupt-from S --corrupt-rate N]

The defaults reproduce the v6.1 e2e shape (12s stall absorbed, 30s trips #4,
forced EOF at 120s). Tests pass explicit flags.
"""
from __future__ import annotations

import argparse
import contextlib
import http.server
import sys
import time


def parse_stalls(values):
    out = []
    for v in values or []:
        s, d = v.split(":")
        out.append((float(s), float(d)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("rate", type=float, help="bytes/second the edge advances at")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--front", type=float, default=25.0,
                    help="seconds of front-load backlog per connection")
    ap.add_argument("--stall", action="append", default=[],
                    help="stall window 'start:duration' in seconds since server start; repeatable")
    ap.add_argument("--eof-at", type=float, default=0.0,
                    help="force one EOF at this many seconds (0 = never)")
    ap.add_argument("--corrupt-from", type=float, default=0.0,
                    help="start CC-field corruption injection at this many seconds (0 = off)")
    ap.add_argument("--corrupt-rate", type=int, default=5,
                    help="how many packets per 15s to corrupt (matches real-incident rate)")
    args = ap.parse_args()

    with open(args.capture, "rb") as f:
        data = bytearray(f.read())
    rate = args.rate
    front_bytes = int(args.front * rate)
    stalls = parse_stalls(args.stall)
    t0 = time.time() - args.front
    eof_done = False
    corrupt_done = 0
    last_corrupt_window = -1.0

    def edge():
        return min(int((time.time() - t0) * rate), len(data))

    def stalled(t):
        return any(s <= t < s + d for s, d in stalls)

    def maybe_corrupt(byte_pos, byte_end, t):
        """Flip CC fields in payload packets within [byte_pos, byte_end) at the
        requested rate. Mutates the served buffer in-place. CC corruption is
        what the v6 stderr detector (same-dts-3x) MISSES — exactly the class of
        damage detector #5 exists to catch."""
        nonlocal corrupt_done, last_corrupt_window
        if args.corrupt_from <= 0 or t < args.corrupt_from:
            return
        # Reset budget each 15s window so it tracks the per-stats-window rate.
        win = (t - args.corrupt_from) // 15
        if win != last_corrupt_window:
            last_corrupt_window = win
            corrupt_done = 0
        budget = args.corrupt_rate - corrupt_done
        if budget <= 0:
            return
        # Walk TS packets in this slice; corrupt the first `budget` payload packets
        # by flipping the low nibble of byte 3 (the CC field).
        # Sync to the next 188 boundary first.
        start = byte_pos + (-byte_pos % 188)
        for p in range(start, byte_end - 188, 188):
            if data[p] != 0x47:
                continue
            afc = (data[p + 3] >> 4) & 0x3
            if not (afc & 0x1):                     # CC only meaningful on payload packets
                continue
            data[p + 3] ^= 0x05                     # jump CC by 5 — guaranteed discontinuity
            corrupt_done += 1
            budget -= 1
            if budget <= 0:
                return

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self):
            nonlocal eof_done
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")    # CDN mislabels TS; client must not care
            self.end_headers()
            if edge() >= len(data):
                print(f"connect after exhaustion t={time.time() - t0:.0f}s -> empty", flush=True)
                return
            pos = max(0, edge() - front_bytes)
            pos -= pos % 188                                  # packet-aligned start
            print(f"client connect t={time.time() - t0:.0f}s front={(edge() - pos) / 1e6:.1f}MB",
                  flush=True)
            while True:
                t = time.time() - t0
                if args.eof_at > 0 and not eof_done and t >= args.eof_at:
                    eof_done = True
                    print(f"forced EOF t={t:.0f}s", flush=True)
                    return
                e = edge()
                if stalled(t) or pos >= e:
                    if pos >= len(data):
                        print(f"capture exhausted t={t:.0f}s", flush=True)
                        return
                    time.sleep(0.2)
                    continue
                n = min(e - pos, 65536)
                maybe_corrupt(pos, pos + n, t)
                try:
                    self.wfile.write(bytes(data[pos:pos + n]))
                except Exception:
                    print(f"client gone t={t:.0f}s", flush=True)
                    return
                pos += n

        def log_message(self, *a):                            # silence default access log
            pass

    http.server.ThreadingHTTPServer.allow_reuse_address = True
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), H)
    print(f"cdn_sim :{args.port} rate={rate:.0f}B/s capture={len(data) / 1e6:.0f}MB "
          f"dur={len(data) / rate:.0f}s stalls={stalls} eof_at={args.eof_at} "
          f"corrupt_from={args.corrupt_from} corrupt_rate={args.corrupt_rate}",
          flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main() or 0)

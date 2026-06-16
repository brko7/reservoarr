#!/usr/bin/env bash
# Generate the deterministic synthetic MPEG-TS fixture used by the e2e suite.
#
# Why synthetic: the production provider (IPTorrents IPTV) is unreliable at
# prime-time, so CI cannot depend on capturing a live stream. testsrc2 + sine
# through ffmpeg produces a deterministic 180s H.264+AC3 mpegts with PCR — the
# same shape reservoir.py parses from the CDN, but reproducible byte-for-byte.
#
# Usage: make_synth_ts.sh <out.ts>
set -euo pipefail

OUT=${1:?usage: make_synth_ts.sh <out.ts>}
DUR=${SYNTH_DUR:-180}      # seconds
BV=${SYNTH_BV:-2000k}      # video bitrate
BA=${SYNTH_BA:-192k}       # audio bitrate
MUX=${SYNTH_MUX:-2400k}    # muxrate

# -preset ultrafast keeps gen time <2s; -pix_fmt yuv420p for broad compat;
# -muxrate forces a stable byte rate so cdn_sim can pace deterministically;
# explicit -g 50 (1 keyframe/2s) so dump_extra has something to repeat against.
# -fflags +bitexact strips encoder timestamps for reproducible output across hosts.
ffmpeg -hide_banner -loglevel error -y \
    -fflags +bitexact \
    -f lavfi -i "testsrc2=size=1280x720:rate=25" \
    -f lavfi -i "sine=frequency=440:sample_rate=48000" \
    -t "$DUR" \
    -c:v libx264 -preset ultrafast -tune zerolatency -b:v "$BV" -g 50 -pix_fmt yuv420p \
    -c:a ac3 -b:a "$BA" \
    -muxrate "$MUX" -f mpegts "$OUT"

BYTES=$(wc -c <"$OUT" | tr -d ' ')
RATE=$((BYTES / DUR))
echo "fixture: $OUT  size=${BYTES}B  duration=${DUR}s  byte_rate=${RATE}B/s ($((RATE * 8 / 1000 / 1000))Mbps)"

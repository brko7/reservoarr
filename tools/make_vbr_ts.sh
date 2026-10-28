#!/usr/bin/env bash
# Generate the VBR fixture for the e2e suite: one stream whose bitrate drops
# from 3 to 1 Mbps for 40s and comes back, with continuous timestamps.
#
# Why: the content rate reservoarr measures is a 90s average, and a stream
# that changes bitrate is where pacing on that average and pacing on content
# time part ways (discussion #46). synth.ts has a constant byte rate
# (-muxrate) and cannot show it. Serve this one with `cdn_sim.py --pcr`: at a
# fixed byte rate its 3 Mbps stretches would arrive slower than realtime.
#
# nal-hrd=cbr makes x264 hold each segment's bitrate on testsrc2, which needs
# far less; the concat demuxer joins the segments on one timeline.
#
# Usage: make_vbr_ts.sh <out.ts>
set -euo pipefail

OUT=${1:?usage: make_vbr_ts.sh <out.ts>}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

segment() {  # <bitrate> <seconds> <out>
    ffmpeg -hide_banner -loglevel error -y \
        -fflags +bitexact \
        -f lavfi -i "testsrc2=size=1280x720:rate=25" \
        -f lavfi -i "sine=frequency=440:sample_rate=48000" \
        -t "$2" \
        -c:v libx264 -preset ultrafast -tune zerolatency -b:v "$1" -minrate "$1" -maxrate "$1" -bufsize "$1" \
        -x264-params nal-hrd=cbr -g 50 -pix_fmt yuv420p \
        -c:a ac3 -b:a 192k -f mpegts "$3"
}

segment 3000k 45 "$TMP/a.ts"
segment 1000k 40 "$TMP/b.ts"
segment 3000k 60 "$TMP/c.ts"
printf "file '%s'\n" "$TMP/a.ts" "$TMP/b.ts" "$TMP/c.ts" >"$TMP/list.txt"
ffmpeg -hide_banner -loglevel error -y -f concat -safe 0 -i "$TMP/list.txt" -c copy -f mpegts "$OUT"

echo "fixture: $OUT  size=$(wc -c <"$OUT" | tr -d ' ')B  3 Mbps 45s, 1 Mbps 40s, 3 Mbps 60s"

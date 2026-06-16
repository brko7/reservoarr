#!/usr/bin/env bash
# Smoke-test a Live TV channel through the Dispatcharr delay-buffer end-to-end.
# Tunes the channel via the Dispatcharr proxy exactly like Plex does, streams it
# for a while, then prints the reservoir telemetry from the log.
#
# This is the manual/live ops tool — it talks to a running Dispatcharr. The CI
# e2e suite covers the same paths with the synthetic fixture (no network).
#
# Configure via env (or pass on the command line):
#   HOST            SSH target running Dispatcharr (default: $RESERVOARR_HOST or "localhost")
#   CONTAINER       Container name to docker-exec into (default: dispatcharr)
#   PROXY_BASE      Dispatcharr proxy base URL inside the container (default: http://127.0.0.1:9191)
#   LOG_PATH        Path inside the container to delaybuf.log (default: /data/scripts/logs/delaybuf.log)
#
# Usage: smoke_channel.sh <channel-number|channel-uuid> [duration-seconds]
#   smoke_channel.sh 1005          # 120s
#   smoke_channel.sh 1005 300      # 5 min
#
# Healthy: cushion=NNs(pcr) reaching ~25-30s within a minute and holding;
# ccerr/pcrrej/sync near zero.
#
# WARNING: a smoke consumes one of the provider's concurrent connection slots
# while it runs. Don't run two at once.
set -eu

CH=${1:?usage: smoke_channel.sh <channel-number|uuid> [seconds]}
DUR=${2:-120}
HOST=${HOST:-${RESERVOARR_HOST:-localhost}}
CONTAINER=${CONTAINER:-dispatcharr}
PROXY_BASE=${PROXY_BASE:-http://127.0.0.1:9191}
LOG_PATH=${LOG_PATH:-/data/scripts/logs/delaybuf.log}

# Resolve channel number -> UUID via the HDHR lineup (UUIDs pass through).
case "$CH" in
  *-*-*) UUID=$CH ;;
  *)
    UUID=$(ssh "$HOST" "docker exec $CONTAINER curl -s $PROXY_BASE/hdhr/lineup.json" \
      | python3 -c "
import json, sys
ch = sys.argv[1]
for e in json.load(sys.stdin):
    if e.get('GuideNumber') == ch:
        print(e['URL'].rsplit('/', 1)[-1]); break
else:
    sys.exit(f'channel {ch} not found in lineup')
" "$CH") ;;
esac

ACTIVE=$(ssh "$HOST" "docker exec $CONTAINER sh -c 'pgrep -af scripts/reservoir.py | grep -v pgrep | wc -l'" || echo 0)
if [ "$ACTIVE" -gt 0 ]; then
  echo "WARNING: $ACTIVE stream(s) already active — provider connection cap may apply."
  echo "         Ctrl-C within 5s if someone is watching."
  sleep 5
fi

echo "channel $CH -> $UUID, streaming ${DUR}s through the proxy (default stream profile)..."
ssh "$HOST" "docker exec $CONTAINER curl -s -m $DUR -o /dev/null $PROXY_BASE/proxy/ts/stream/$UUID" || true

echo "--- reservoir telemetry (lifecycle + stats, ffmpeg noise stripped) ---"
ssh "$HOST" "tail -60 $LOG_PATH" | grep -v "ffmpeg:"

# Tunables

All defaults reproduce the v6.1 production behaviour. **Override only with evidence** — the defaults are calibrated against real incidents (see the [CHANGELOG](../CHANGELOG.md)).

| Env var | Default | What it does |
|---|---|---|
| `RESV_PREFILL_BYTES` | `1572864` (1.5 MB) | Sniff target before releasing to ffmpeg. **NOT** a reservoir fill — Plex's tuner times out around 15s, so a full prefill caused "won't start, then loads on retry". |
| `RESV_PREFILL_MAX_S` | `3.0` | Sniff timeout. Must stay well under Dispatcharr's `CONNECTION_TIMEOUT` (~10s). |
| `RESV_HEADSTART_S` | `5.0` | Seconds of content released unpaced after prefill. Gives Plex its startup buffer fast; the rest of the CDN's front-load burst stays banked. |
| `RESV_TARGET_S` | `30.0` | Cushion level the controller holds. Tune higher only if your CDN's worst gaps are longer. |
| `RESV_GRACE_S` | `45.0` | First N seconds use floor `1.0` (realtime release); after, floor drops to `0.97`. Prevents starving the player before the bank settles. |
| `RESV_MAX_BYTES` | `268435456` (256 MB) | Reservoir hard cap. Worst-case RAM per concurrent stream. |
| `RESV_STALL_S` | `25.0` | No-ingest watchdog (#4). If no bytes arrive for this long while running, force a reconnect WITHOUT flushing the buffer. `0` disables. |
| `RESV_TS_RECONNECT` | `0` | #5 ingest-corruption detector action mode. `0` = log-only ("would-fire"). `1` = arm the forced reconnect+flush. **See arming guidance in [CHANGELOG](../CHANGELOG.md).** |
| `RESV_CC_ERR_PER_WIN` | `3` | #5 trigger: CC errors per 15s window to flag. |
| `RESV_SYNC_ERR_PER_WIN` | `2` | #5 trigger: sync losses per 15s window to flag. |
| `RESV_TS_SUSTAIN_WINS` | `2` | #5: consecutive flagged windows before action. |
| `RESV_LOG_DIR` | `/data/scripts/logs` | Where `delaybuf.log` lives. Set to a writable dir if not running under the AIO container. |
| `RESV_FFMPEG_BIN` | `/usr/local/bin/ffmpeg` | ffmpeg path. Override on dev hosts (`/opt/homebrew/bin/ffmpeg`, `/usr/bin/ffmpeg`). |

## When to bump what

- **Cushion too small for your CDN's prime-time gaps** → raise `RESV_TARGET_S` (cushion target) and `RESV_MAX_BYTES` (RAM cap) together. Each +10s of cushion is ~6–8 MB of extra RAM at typical HD bitrates.
- **Stall watchdog firing on benign gaps** → raise `RESV_STALL_S`. The default 25s sits above the CDN's normal burst-gap; if yours is longer, set it slightly above your observed worst-case.
- **Arming the #5 detector** → see the arming-test recipe in the [CHANGELOG](../CHANGELOG.md) v6.1.0 entry. Don't flip `RESV_TS_RECONNECT=1` blindly; it's gated on evidence that fresh-edge reconnects actually serve clean content for your provider.

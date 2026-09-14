# Tunables

Defaults are unchanged since v6.1 and calibrated against real incidents (see the [CHANGELOG](../CHANGELOG.md)). **Override only with evidence.**

| Env var | Default | What it does |
|---|---|---|
| `RESV_PREFILL_BYTES` | `1572864` (1.5 MB) | Sniff target before releasing to ffmpeg. **NOT** a reservoir fill — Plex's tuner times out around 15s, so a full prefill caused "won't start, then loads on retry". |
| `RESV_PREFILL_MAX_S` | `3.0` | Sniff timeout. Must stay well under Dispatcharr's `CONNECTION_TIMEOUT` (~10s). |
| `RESV_HEADSTART_S` | `5.0` | Seconds of content released unpaced after prefill. Gives Plex its startup buffer fast; the rest of the CDN's front-load burst stays banked. |
| `RESV_TARGET_S` | `30.0` | Cushion level the controller holds. Tune higher only if your CDN's worst gaps are longer. |
| `RESV_GRACE_S` | `45.0` | First N seconds use floor `1.0` (realtime release); after, floor drops to `0.97`. Prevents starving the player before the bank settles. |
| `RESV_MAX_BYTES` | `268435456` (256 MB) | Reservoir hard cap. Worst-case RAM per concurrent stream. |
| `RESV_STALL_S` | `25.0` | No-ingest watchdog (#4). If no bytes arrive for this long while running, force a reconnect WITHOUT flushing the buffer. `0` disables. |
| `RESV_GIVEUP_TRIES` | `0` | Upstream attempts that may end without a single byte before the process exits, so Dispatcharr counts a failed attempt and, after its own retries, moves the channel to the next stream. Errors and connects that close empty both count; once any byte has arrived it never fires. `0` retries forever, as before. Without it, a stream the edge answers `403` keeps the channel "connecting" until the client gives up, and the chain is never walked. |
| `RESV_REPLAY_SKIP` | `0` | `1` drops what an edge resends after a plain reconnect: the fetcher remembers the last PCR taken before the seam and discards packets of the new connection until the PCR passes it, so the same seconds are not played twice and the replay is not banked as new cushion. The new connection's packets are held until the seam is confirmed, so a seam it gives up on passes through whole, as before: a replay that starts further back than `RESV_REPLAY_MAX_S`, carries a discontinuity flag, jumps back or more than 10s forward, shows no advancing PCR within 1 MB (a repeated PCR is a legal duplicate), or ends with the connection before the seam. The held replay counts against `RESV_MAX_BYTES` together with the reservoir, and the gate also gives up when the reservoir falls below 3s of content while it holds, so a replay arriving at realtime never stalls playback. Never after a flushing reconnect, and a replay still held when a flush is requested is dropped with the reservoir. |
| `RESV_REPLAY_MAX_S` | `60.0` | Longest replay `RESV_REPLAY_SKIP` will drop. A new connection that starts further back than this is treated as a new timeline and passed through. |
| `RESV_TS_RECONNECT` | `0` | #5 ingest-corruption detector action mode. `0` = log-only ("would-fire"). `1` = arm the forced reconnect+flush. **See arming guidance in [CHANGELOG](../CHANGELOG.md).** |
| `RESV_CC_ERR_PER_WIN` | `3` | #5 trigger: CC errors per 15s window to flag. |
| `RESV_SYNC_ERR_PER_WIN` | `2` | #5 trigger: sync losses per 15s window to flag. |
| `RESV_TS_SUSTAIN_WINS` | `2` | #5: consecutive flagged windows before action. |
| `RESV_LOG_DIR` | `/data/scripts/logs` | Where `delaybuf.log` lives. Set to a writable dir if not running under the AIO container. |
| `RESV_FFMPEG_BIN` | `/usr/local/bin/ffmpeg` | ffmpeg path. Override on dev hosts (`/opt/homebrew/bin/ffmpeg`, `/usr/bin/ffmpeg`). |
| `RESV_FFMPEG_STATS` | `0` | `1` adds `-stats`, so ffmpeg's `frame=… speed=…` line reaches Dispatcharr and its buffering failover can move a starving feed to the next stream. `-loglevel warning` is unchanged (invariant #9), and the progress line goes to stderr only, never to `delaybuf.log`. |

## When to bump what

- **Cushion too small for your CDN's prime-time gaps** → raise `RESV_TARGET_S` (cushion target) and `RESV_MAX_BYTES` (RAM cap) together. Each +10s of cushion is ~6–8 MB of extra RAM at typical HD bitrates.
- **Stall watchdog firing on benign gaps** → raise `RESV_STALL_S`. The default 25s sits above the CDN's normal burst-gap; if yours is longer, set it slightly above your observed worst-case.
- **Arming the #5 detector** → see the arming-test recipe in the [CHANGELOG](../CHANGELOG.md) v6.1.0 entry. Don't flip `RESV_TS_RECONNECT=1` blindly; it's gated on evidence that fresh-edge reconnects actually serve clean content for your provider.

# Telemetry

`{RESV_LOG_DIR}/delaybuf.log` (self-rotates at 10 MB, keeps one `.1`; checked at stream start and every ~512 log lines while running). One stats line every 15s per active stream — also while the reservoir is empty (6.4.0; before, an outage that drained it went silent) — plus lifecycle events.

## Stats line

```
2026-06-14T10:03:33+0200 [500004175] cushion=27s(pcr) buf=15.5MB out=4.66Mbps in=4.96Mbps crate=4.80Mbps in_total=1843MB reconnects=0 ccerr=0 pcrrej=0 disc=0 sync=0 pcr_back=0 vholes=0% aholes=0%
```

| Field | Meaning |
|---|---|
| `cushion=Ns(pcr)` | Seconds of media buffered ahead of the player. `(pcr)` = measured off the PCR clock (good); `(byte)` = fallback while PCR is unlocked. |
| `buf=…MB` | Reservoir size in bytes. |
| `out=…Mbps` | Output byte rate to ffmpeg. |
| `in=…Mbps` | Byte rate ingested from upstream. It is also the pacing reference while PCR is unlocked, so a replay dropped by `RESV_REPLAY_SKIP` is not counted. |
| `crate=…Mbps` | PCR-derived content rate: the bytes per second of content over the last ~90s of content. The pacer charges each slice its byte-rate time at this rate and trues it up to the content time it carries at every PCR (6.6.0; before, it paced on this rate alone). A steady slide over 90s with `vholes=0%` is the channel's own bitrate changing, not the feed slowing down. |
| `in_total=…MB` | Lifetime bytes ingested. Bytes dropped by `RESV_REPLAY_SKIP`, and the partial packets dropped at a connection seam, are not counted. |
| `reconnects=N` | Upstream reconnects this session. |
| `ccerr/pcrrej/disc/sync` | TS continuity-counter errors / rejected PCR samples (garbage timestamps) / spec-legal discontinuities / sync losses. Healthy = flat zero. |
| `pcr_back=N` | Backward PCR jumps > 0.5s (CDN overlap-replay signal — provider re-served N seconds of content). Each event also emits a `pcr backward jump: -Xs (last=… cur=…)` log line. Log-only; pacing is unchanged. Healthy = flat zero. |
| `vholes=N%` `aholes=N%` | Share of the window's content that the video, and the sound, is missing: for each the gaps longer than 0.5s between consecutive timestamps (DTS for video when present, else PTS), over the span those timestamps cover since the previous line. The PIDs come from the PAT and PMT (audio: MPEG audio, AAC, AC-3, E-AC-3, or private data with an AC-3, E-AC-3 or AAC descriptor). A track that sends nothing in the window while the other one covers at least 5s of content reads `100`; a shorter sliver, as around a reconnect, does not count. The PAT and PMT are put back together when a section spans packets, sections not yet current are ignored, and a track that a complete PMT no longer lists is dropped; a PES header split over two packets is read whole. A reconnect seam, a discontinuity flag (on the PCR PID or either track, it excuses the next timestamp of both tracks) and a jump of 10s or more are not counted. A source that arrives at full rate but drops whole segments shows up here and nowhere else: `in` matches `crate` because the PCR jumps with the content. Measured on the input, so `RESV_AUDIO_FILL_GAPS` does not hide it. Healthy = flat zero; `0` also while a track has no timestamps yet. |

## Lifecycle events

Same file, free-form lines:

| Event | When |
|---|---|
| `upstream connected edge=<host>` | Each successful upstream HTTP connect. Includes the CDN edge `host[:port]` so you can correlate problems with a specific edge; any `user:password@` in the URL is left out (6.4.0). |
| `upstream EOF` | Upstream closed the connection cleanly. Not logged when a forced reconnect cut the connection. |
| `upstream connection ended: dropped AB before its first TS packet and BB of an unfinished last packet` | 6.4.0: the connection started (A) or ended (B) mid-packet. Those bytes are kept out of the reservoir, so the writes to ffmpeg stay on the 188-byte grid across the seam. File only. Seen on every reconnect means the edge cuts connections mid-packet; harmless either way. |
| `upstream connection: no TS sync in its first NKB; passing it on unaligned` | The connection's first chunk holds no MPEG-TS at all; it goes to ffmpeg as before 6.4.0, padded with `0xFF` to the next packet boundary when the connection ends, and ffmpeg's own error follows. Set `RESV_REJECT_MP4=1` if it is a provider's MP4 clip. |
| `upstream error <type>: <msg>; retry in Ns` | Upstream read/connect failed; exponential backoff active. |
| `upstream stalled (no data Ns) - reconnecting, buffer kept` | #4 watchdog: no byte has arrived from upstream for `RESV_STALL_S`; reconnect WITHOUT flushing. Since 6.4.0 a silent connection is cut at once; before, the reconnect waited for the 30s socket timeout. A connect that hangs before the first byte still waits out urlopen's 30s. A replay being dropped by `RESV_REPLAY_SKIP` counts as arriving, so a slow replay never trips it; a full reservoir (`RESV_MAX_BYTES`, reading paused on purpose) is not a stall. |
| `corrupt-loop detected in stream (dts=N x3) - forcing upstream reconnect + buffer flush` | ffmpeg-stderr-side detector: same `dts` reported 3× in 120s. |
| `TS corruption detected (...)` | #5 ingest-side detector, ARMED (`RESV_TS_RECONNECT=1`). |
| `would-fire: TS corruption detected (...)` | #5 ingest-side detector, log-only (`RESV_TS_RECONNECT=0` — default). |
| `pcr backward jump: -Xs (last=A cur=B)` | Upstream PCR went backward by more than 0.5s — usually a CDN serving overlapping content. Log-only signal; pacing unchanged. The corresponding telemetry-line counter is `pcr_back=N`. With `RESV_REPLAY_SKIP=1` a replay after a plain reconnect is dropped before the parser sees it, so it no longer shows up here. |
| `ts timeline: channel <id> starts at Xs (output offset Ys)` | `RESV_TS_CHANNEL` set: the absolute point the output timeline resumes from, and the 33-bit offset handed to ffmpeg. Consecutive processes of one channel should show X a few seconds past where the previous one ended. |
| `ts timeline: ignored N output PTS more than 30s ahead of the wall clock` | On exit, `RESV_TS_CHANNEL` set: N PES timestamps landed too far ahead of the expected position to be real and were left out of the saved timeline. Occasional ones are corrupt headers; a steady count means the output timeline itself runs ahead. |
| `replay after reconnect: skipped Xs (YMB) already delivered` | `RESV_REPLAY_SKIP=1`: the new connection started X seconds behind where the old one ended; those Y MB were dropped up to the last PCR before the seam, and the stream resumes on the first packet past it. |
| `replay after reconnect: <reason>; passing through` | `RESV_REPLAY_SKIP=1`, but the seam could not be placed, so everything held since the reconnect is released, as with `=0`. `<reason>` is one of `Ns back is beyond RESV_REPLAY_MAX_S`, `discontinuity flag`, `PCR not advancing through the replay`, `lost TS sync`, `no TS sync within the hold limit`, `no PCR within the hold limit`, `replay does not fit in the reservoir`, `reservoir running low`, `connection ended before the seam`. A connection that lands ahead of the seam passes through without a line. |
| `flushed reservoir after corrupt-loop reconnect` | Confirms the buffer was emptied (poisoned content discarded). Emitted after any flush-requesting reconnect — the corrupt-loop detector or an armed #5 firing. |
| `ffmpeg: <line>` | ffmpeg's stderr, relayed line-by-line (this is what the `grep -v "ffmpeg:"` recipes strip). |
| `stream consumer gone (<Type>); shutting down` | Dispatcharr closed our stdout (viewer stopped the channel); clean exit follows. |
| `prefill done: NMB in Ns, releasing stream to ffmpeg` | Once-per-stream startup line. |
| `no data before the fetcher gave up (Ns); exiting without ffmpeg` | `RESV_GIVEUP_TRIES` fired before any byte arrived: the process ends here, without the prefill wait and without ffmpeg, so Dispatcharr counts the failed attempt at once. |
| `upstream sent an MP4 file, not a live TS; counted as an attempt without data` | `RESV_REJECT_MP4=1` and the edge answered with an MP4 file: nothing was ingested from this connection. With `RESV_GIVEUP_TRIES` set, `giving up` follows. |
| `upstream sent an HLS playlist, not a live TS; counted as an attempt without data` (or `an HTML page` / `an XML document`) | Always on (6.3.9): the edge answered with something ffmpeg can never play as live TS — typically the stream URL is an `.m3u8` playlist or the edge returned an error page. Nothing was ingested; the backoff keeps growing. With `RESV_GIVEUP_TRIES` set, `giving up` follows. For an HLS playlist the line adds `(reservoarr needs a raw MPEG-TS URL, see README)`. |
| `ffmpeg exited on its own (rc=N); shutting down` | 6.4.0: ffmpeg ended without being asked to (OOM kill, crash, or its output pipe closed under it); the process ends so Dispatcharr can retry or fail over. `rc=-9` is a SIGKILL. Before, with the reservoir empty, nothing noticed and the channel sat on dead air. |
| `stream wrapper exit (ffmpeg rc=N)` | Final line on shutdown. |

## What healthy looks like

After the first ~60s of a stream:

- `cushion=` should reach `~25–30s(pcr)` and hold there, on a CDN that front-loads that much plus the 5s headstart; one that front-loads less leaves it lower (README, "Cushion never reaches the target"). `(byte)` is OK during the first 15s; if it stays `(byte)` for minutes the PCR clock never locked — check `pcrrej`.
- `out=` ≈ `crate=` on average over several minutes while the cushion is at or under the target. Since 6.6.0 `out` follows the content, so on a channel whose bitrate varies a single line can sit well over or under `crate`: over while the bitrate runs above its 90s average, under while it runs below. On a constant-bitrate channel they match within a percent or two. Over several minutes, `out` higher than `crate` means the controller is bleeding the cushion (transient OK after reconnect; sustained = misconfigured), and `out` held a few percent under means the player is fed below realtime to bank cushion, which a client with a small live buffer shows as stutters: ~3% with `RESV_FLOOR=0.97` since 6.5.0, and 3–5% on every release before it while the cushion was under the target after the first 45s (the 0.97 floor plus the pacer's lost oversleep). Releases before 6.6.0 also fed a channel below realtime while its bitrate ran above the average, with `out` matching `crate`: there, a cushion that falls to 0 while `crate` slides over 90s is followed by the player starving when the bitrate comes back.
- `ccerr`, `pcrrej`, `disc`, `sync`, `pcr_back` flat at 0. Any non-zero is a corruption signal worth investigating. `pcr_back` specifically points at CDN overlap-replay (some edges send the same N seconds twice); the player perceives this as a "rewind".
- `reconnects=0` for the first hour or so unless your provider is one of the unreliable ones; an occasional reconnect is fine, a reconnect every few minutes is not.

## Reading the log in production

```bash
# Last 60 lines, ffmpeg noise stripped:
tail -60 /data/scripts/logs/delaybuf.log | grep -v "ffmpeg:"

# Live stats for the active stream:
tail -F /data/scripts/logs/delaybuf.log | grep "cushion="
```

For ops-level smoke-testing through a running Dispatcharr instance, see `tools/smoke_channel.sh`.

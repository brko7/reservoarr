# Telemetry

`{RESV_LOG_DIR}/delaybuf.log` (self-rotates at 10 MB, keeps one `.1`; checked at stream start and every ~512 log lines while running). One stats line every 15s per active stream, plus lifecycle events.

## Stats line

```
2026-06-14T10:03:33+0200 [500004175] cushion=27s(pcr) buf=15.5MB out=4.66Mbps in=4.96Mbps crate=4.80Mbps in_total=1843MB reconnects=0 ccerr=0 pcrrej=0 disc=0 sync=0 pcr_back=0
```

| Field | Meaning |
|---|---|
| `cushion=Ns(pcr)` | Seconds of media buffered ahead of the player. `(pcr)` = measured off the PCR clock (good); `(byte)` = fallback while PCR is unlocked. |
| `buf=…MB` | Reservoir size in bytes. |
| `out=…Mbps` | Output byte rate to ffmpeg. |
| `in=…Mbps` | Byte rate ingested from upstream. It is also the pacing reference while PCR is unlocked, so a replay dropped by `RESV_REPLAY_SKIP` is not counted. |
| `crate=…Mbps` | PCR-derived content rate (the pacing reference). |
| `in_total=…MB` | Lifetime bytes ingested. Bytes dropped by `RESV_REPLAY_SKIP` are not counted. |
| `reconnects=N` | Upstream reconnects this session. |
| `ccerr/pcrrej/disc/sync` | TS continuity-counter errors / rejected PCR samples (garbage timestamps) / spec-legal discontinuities / sync losses. Healthy = flat zero. |
| `pcr_back=N` | Backward PCR jumps > 0.5s (CDN overlap-replay signal — provider re-served N seconds of content). Each event also emits a `pcr backward jump: -Xs (last=… cur=…)` log line. Log-only; pacing is unchanged. Healthy = flat zero. |

## Lifecycle events

Same file, free-form lines:

| Event | When |
|---|---|
| `upstream connected edge=<host>` | Each successful upstream HTTP connect. Includes the CDN edge host so you can correlate problems with a specific edge. |
| `upstream EOF` | Upstream closed the connection cleanly. |
| `upstream error <type>: <msg>; retry in Ns` | Upstream read/connect failed; exponential backoff active. |
| `upstream stalled (no data Ns) - reconnecting, buffer kept` | #4 watchdog: no byte has arrived from upstream for `RESV_STALL_S`; reconnect WITHOUT flushing. A replay being dropped by `RESV_REPLAY_SKIP` counts as arriving, so a slow replay never trips it. |
| `corrupt-loop detected in stream (dts=N x3) - forcing upstream reconnect + buffer flush` | ffmpeg-stderr-side detector: same `dts` reported 3× in 120s. |
| `TS corruption detected (...)` | #5 ingest-side detector, ARMED (`RESV_TS_RECONNECT=1`). |
| `would-fire: TS corruption detected (...)` | #5 ingest-side detector, log-only (`RESV_TS_RECONNECT=0` — default). |
| `pcr backward jump: -Xs (last=A cur=B)` | Upstream PCR went backward by more than 0.5s — usually a CDN serving overlapping content. Log-only signal; pacing unchanged. The corresponding telemetry-line counter is `pcr_back=N`. With `RESV_REPLAY_SKIP=1` a replay after a plain reconnect is dropped before the parser sees it, so it no longer shows up here. |
| `replay after reconnect: skipped Xs (YMB) already delivered` | `RESV_REPLAY_SKIP=1`: the new connection started X seconds behind where the old one ended; those Y MB were dropped up to the last PCR before the seam, and the stream resumes on the first packet past it. |
| `replay after reconnect: <reason>; passing through` | `RESV_REPLAY_SKIP=1`, but the seam could not be placed, so everything held since the reconnect is released, as with `=0`. `<reason>` is one of `Ns back is beyond RESV_REPLAY_MAX_S`, `discontinuity flag`, `PCR not advancing through the replay`, `lost TS sync`, `no TS sync within the hold limit`, `no PCR within the hold limit`, `replay longer than the hold cap`, `connection ended before the seam`. A connection that lands ahead of the seam passes through without a line. |
| `flushed reservoir after corrupt-loop reconnect` | Confirms the buffer was emptied (poisoned content discarded). Emitted after any flush-requesting reconnect — the corrupt-loop detector or an armed #5 firing. |
| `ffmpeg: <line>` | ffmpeg's stderr, relayed line-by-line (this is what the `grep -v "ffmpeg:"` recipes strip). |
| `stream consumer gone (<Type>); shutting down` | Dispatcharr closed our stdout (viewer stopped the channel); clean exit follows. |
| `prefill done: NMB in Ns, releasing stream to ffmpeg` | Once-per-stream startup line. |
| `stream wrapper exit (ffmpeg rc=N)` | Final line on shutdown. |

## What healthy looks like

After the first ~60s of a stream:

- `cushion=` should reach `~25–30s(pcr)` and hold there. `(byte)` is OK during the first 15s; if it stays `(byte)` for minutes the PCR clock never locked — check `pcrrej`.
- `out=` ≈ `crate=` ± a few percent. `out` higher than `crate` means the controller is bleeding the cushion (transient OK after reconnect; sustained = misconfigured).
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

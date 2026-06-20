# Hard invariants — DO NOT regress

Every entry below is earned by a real production failure. The
[CHANGELOG](../CHANGELOG.md) carries the incident history; this file is the
short list of rules anyone editing `reservoarr.py` must respect.

| # | Invariant | Why |
|---|---|---|
| 1 | **Byte-rate pacing**, NEVER `ffmpeg -re` / `-readrate` | Corrupt packets with garbage DTS make `-re` sleep for >25s with a full reservoir. PCR is a *measurement* input — a garbage sample is dropped, never slept on. |
| 2 | **stdout carries only the TS stream** | Dispatcharr's relay pipe consumes stdout. Logging goes to stderr + the log file. |
| 3 | **Audio re-encoded to AC3** (`-c:a ac3 -b:a 192k -ac 2`) | Upstream-blessed fix for [Dispatcharr #1122](https://github.com/Dispatcharr/Dispatcharr/issues/1122) (Plex MDE failures on AAC streams). `-c:a copy` causes A/V desync. |
| 4 | **`-bsf:v dump_extra=freq=keyframe`** | Re-injects SPS/PPS at every keyframe so mid-stream tune-in (channel switch) doesn't go black. Required in ffmpeg 7.x and 8.x. |
| 5 | **No `-copyts`** | Causes A/V desync on these streams. |
| 6 | **stdlib-only at runtime** | Dispatcharr's container has no pip available for end-users. Dev/test deps live in `pyproject.toml`'s `[dev]` extra. |
| 7 | **Single deployable file** | Spawned per channel-tune as `reservoarr.py {streamUrl} {userAgent}`. |
| 8 | **#5 detector defaults to log-only** (`RESV_TS_RECONNECT=0`) | The ingest-side corruption detector ships in observe mode pending more arming evidence (see [CHANGELOG](../CHANGELOG.md)). Setting `=1` arms the forced reconnect. |
| 9 | **ffmpeg at `-loglevel warning`** | The corrupt-loop stderr watcher parses `Packet corrupt (… dts = N)` lines. A speed-watchdog on a quieter setting can't see the corruption. |

## Decisions deliberately NOT taken

Each was considered, evidence-evaluated, and rejected. Don't reopen without new evidence.

| Rejected | Why |
|---|---|
| EOF-reconnect overlap-replay dedup | The controller self-drains the 1.15× post-reconnect bloat in ~2 min; the event is rare; a PCR-splice dedup risks dropping live content on a garbage seam. Add a log marker + gather evidence first. |
| `ffmpeg -readrate` | Timestamp-driven → same garbage-DTS stall as `-re`. |
| Output null-stuffing | Breaks Emby / Jellyfin per the Dispatcharr v0.26.0 changelog. |
| asyncio rewrite | No benefit at this bitrate; loses the simple stdlib-thread model. |
| tmpfs ring buffer | No benefit at this bitrate; complicates deployment. |

# CHANGELOG

All notable changes to `reservoir.py`. Each version's invariants are earned by a real production failure — read this before changing the script.

## [6.1.0] — 2026-06-16

Hardening release after the v6 wild corrupt-loop episode (2026-06-14, 08:03–08:06 UTC).

- **#1 lock-snapshot `released_total`** in `next_slice()`. `in_total` and `buf_bytes` both move under `cond` in the fetcher; reading them outside the lock was a torn-read source — telemetry-only impact today, but a sharp edge worth removing.
- **#2 sanitize `STREAM_ID`** — strip query strings and cap length, so a future provider URL shape can't leak an embedded API key into log filenames.
- **#3 nits** — argv guard with a clear usage message; `GRACE_S` made env-configurable as `RESV_GRACE_S`.
- **#4 silent-stall watchdog** (`RESV_STALL_S=25`, `0` to disable). No ingest progress for 25s while running → force a reconnect WITHOUT flushing the reservoir (buffer is good — grab a fresh front-load while the cushion keeps draining). Catches an open-but-silent socket that urlopen's 30s timeout would only surface after the cushion has drained.
- **#5 ingest-side TS-corruption detector** (`RESV_TS_RECONNECT`, default `0` = log-only). A sustained CC/sync error rate while bytes flow (`≥3 ccerr` per 15s for 2 consecutive windows) indicates a wedged/corrupt upstream; the rule fires ~30s before the ffmpeg-stderr corrupt-loop detector would. Calibrated on the 2026-06-14 incident: errors ramped from flat-zero to sustained `+5–10 ccerr/15s` 45s before the stderr detector. Critically, the rule catches *different-dts* corruption that the stderr same-dts-3× heuristic misses entirely (proven on 2026-06-16 when #5 fired 8× on a different-dts corruption episode the stderr detector would have ignored).
  - **Default is log-only** pending more arming evidence. Open question: does reconnecting to a fresh edge serve clean content (→ arming wins) or is the corruption source-wide (→ reconnect just adds blips)?
  - Arming test: set `RESV_TS_RECONNECT=1`, watch `ccerr` after a forced reconnect. Stops climbing → fresh edge was clean → keep armed. Continues → revert.
- **#6 clean forced-reconnect**. The previous `cur_response.close()` raised `AttributeError 'NoneType' has no 'read'` under TOCTOU → generic except → 1s backoff at the worst moment. Fixed: check `force_reconnect` inside the read loop, break clean.
- **F1 (pre-deploy review)** — per-class debounce (`last_forced_flush` / `last_forced_stall`) + sticky `flush_pending` under `cond`. A stall (no-flush) within the 90s debounce window must never suppress or downgrade a corrupt flush. 10/10 reconnect-precedence test.

Real-data validation: rule replay vs the real 2026-06-14 signature (fires 30s early, 0 false-positive on baseline/reconnect/gap); TsParser within 0.9% on a real TLC PCR capture; synthetic + real-capture cdn_sim e2e (no-regression cushion build, 12s stall absorbed, #4 fires at 25s no-flush, #6 clean reconnect, #5 no-FP, valid h264+ac3, controller self-drains EOF-overlap at 1.15×); 10/10 reconnect-precedence unit test.

**Deferred:**

- **#7 protect `crate` during corruption bursts.** During the 2026-06-14 burst the PCR-rate estimate degraded (2.16 → 1.28 Mbps), self-throttling recovery. The depression mechanism isn't reverse-engineerable from counters alone — needs a raw *corrupt* capture to build + validate. Grab raw bytes during the next corruption episode.

## [6.0.0] — 2026-06-13

**Headline rewrite.** v5's 30s cushion never actually built in production (logs showed 0–16s).

- **PCR content-rate pacing.** v5 paced against the measured *arrival* rate, which squandered the CDN's per-connection front-load burst (~30s of backlog served at line speed got released downstream at 5–7 Mbps chasing the inflated arrival rate) and could never rebuild the cushion at steady state (arrival averages exactly realtime; the rate window ended at the last arrival — excluding in-progress gaps, a ~4% overestimate that cancelled the 0.97 floor). v6 measures bytes-per-PCR-second on ingest with outlier rejection — banks the front-load surplus by construction on every connect, including reconnects after a corrupt-loop flush.
- **`TsParser`** on ingest: PCR clock (wrap-aware delta gate `0 < Δ < 10s`, re-anchor on discontinuity flags / reject runs / reconnects) and per-PID continuity counters (telemetry only in v6 — actions added in v6.1's #5).
- **Backoff-on-data fix.** v5 reset the upstream backoff on every `urlopen()` success — an empty-but-connectable upstream (dead channel) was hammered at 60 conn/min. v6 resets backoff only once bytes actually flow.
- **`cushion_s()` measured in PCR seconds**, not byte estimates.

Validated by 8-min live smoke (cushion 29s built in 15s, held 23–36s, `crate` pinned to true 5.37 Mbps), real-CDN capture parser within 0.2% of ffprobe truth, and the cdn_sim e2e absorbing 12s stalls.

## [5.0.0] — 2026-06-12

Initial delay buffer. The corrupt-loop detector was the highlight.

- **Eager fetcher + RAM reservoir + paced release** + **ffmpeg remux** (video copy + `dump_extra`, audio → AC3 per Dispatcharr #1122).
- **Auto-reconnect on upstream EOF.**
- **Corrupt-loop detector via ffmpeg-stderr watcher**: a wedged CDN connection can serve the same corrupt packet in a loop forever while a fresh connection is clean. Same `dts` reported 3× in 120s → force a reconnect + flush the poisoned reservoir (≤1/90s).
- **1.5 MB / 3s prefill sniff** (NOT a reservoir fill — Plex's tuner timeout is ~15s and isn't configurable).

Pacing was against the arrival rate — see v6.0.0 for why that didn't hold up.

## Pre-history

The script went through five offline prototypes before v5, killed by:

- ffmpeg `-re` sleeping on garbage DTS (25s output freeze with a full reservoir).
- Plex tuner bailing after ~15s initial silence (a 10s prefill caused "won't start, loads on retry").
- Pacing floor 0.85 starving the player to feed the reservoir.
- Buffering Timeout / Speed tuning being inert in Dispatcharr Proxy mode (stderr-parse-only detection).

## Decided NOT to build

Each was considered, evidence-evaluated, and rejected.

| Rejected | Why |
|---|---|
| EOF-reconnect overlap-replay dedup | The controller self-drains the 1.15× post-reconnect bloat in ~2 min; the event is rare; a PCR-splice dedup risks dropping live content on a garbage seam. Add a log marker + gather evidence first. |
| `ffmpeg -readrate` | Timestamp-driven → same garbage-DTS stall as `-re`. |
| Output null-stuffing | Breaks Emby / Jellyfin per the Dispatcharr v0.26.0 changelog. |
| asyncio rewrite | No benefit at this bitrate; loses the simple stdlib-thread model. |
| tmpfs ring buffer | No benefit at this bitrate; complicates deployment. |

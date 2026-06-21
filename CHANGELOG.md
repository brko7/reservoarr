# CHANGELOG

All notable changes to `reservoarr.py`. Each version's invariants are earned by a real production failure — read this before changing the script.

## [6.2.1] — 2026-06-21

Bug fix: TS-packet alignment on writes to ffmpeg's stdin.

- **`align_to_188(tail, chunk)`** module-level helper. The pacing loop now feeds ffmpeg only 188-byte multiples, carrying any unaligned tail across iterations. Previously, the loop wrote each `next_slice()` result `d` to `ff.stdin` directly. Because `r.read()` on the upstream HTTP socket returns arbitrary-length byte runs and the deque accumulates them whole, those writes did not reliably start at 188-byte TS-packet boundaries. ffmpeg's mpegts demuxer assumes they do, so partial-packet writes were detected as `timestamp discontinuity` events and (when the misalignment landed on AAC PES headers) as `channel element X.Y is not allocated` decoder errors that silently discarded audio frames. The viewer experienced the cumulative AAC frame discards as audio drifting out of sync with video over the course of a session.

  **Discovered 2026-06-16 evening** while debugging audio desync on channel 500157163 (kids' viewing). Diagnosis came from comparing live ffmpeg session telemetry to ffmpeg consuming the same captured upstream as a regular file: the offline run, which naturally reads in 188-aligned blocks, produced 0 disc lines / 30s; the live run, paced through the reservoir with arbitrary-length writes, produced 100+ disc lines / 30s on identical bytes. The provider's stream itself was clean.

  **Soak result:** deployed to tigar at 16:30 UTC on 2026-06-16; ran clean across multiple kid viewings (ccerr=0, pcrrej=0, no `would-fire` events, no audio desync) for 5 days before this release. The latent bug was present in every reservoir version since v5; it manifested as user-visible failures only when an unrelated provider-side stream variation (post-2026-06-16 source had AAC PES boundaries that landed on misaligned write boundaries more often) crossed the bug.

- **`tests/unit/test_ts_alignment.py`** — 17 regression tests covering: function existence (a revert that drops `align_to_188` fails loudly), every output is a 188-multiple, byte-perfect reconstruction across arbitrary chunk sequences, pathological one-byte chunks, empty inputs, parametrized realistic and adversarial chunk sizes, and a 2000-iteration randomized property check that the carried tail stays bounded below 188.

  No existing test would have caught this: the e2e suite uses synthetic fixtures whose AAC PES headers don't happen to land on the bug, and ground-truth comparisons key off ccerr/sync counters that are themselves fed misaligned bytes. The new unit tests target the alignment invariant directly.

**Implication for prior incident analysis:** earlier `iptv-incidents.md` entries that attributed AAC `channel element` errors and timestamp-discontinuity floods to provider-side corruption may have been measuring the bug's effects rather than (or as well as) real upstream damage. The 2026-06-16 #5-arming experiment in particular ran on bug-induced ccerr inflation; its "source-wide corruption, reconnect doesn't help" conclusion is therefore not reliable evidence against arming, and the open question remains open. Real provider corruption of course still exists — but it should be re-baselined against post-fix telemetry.

## [6.2.0] — 2026-06-16

Packaging-only release ahead of the public flip. **No runtime behaviour changes** — `reservoarr.py` is byte-equivalent to v6.1.0 modulo the rename in its docstring and usage string.

- **Rename** `reservoir.py` → `reservoarr.py`. Single name across repo, package, plugin, and brand. v6.1.0 was never tagged or released, so impact is theoretical; anyone vendoring from this repo's `main` before today should update their Stream Profile command path.
- **Dispatcharr plugin** (`plugin/plugin.py` + `plugin/plugin.json`). Installs `reservoarr.py` under `/data/reservoarr/`, exposes a "Generate Stream Profile" action, and optionally sets it as the instance-wide default Stream Profile. Same shape as the upstream [Dispatchwrapparr](https://github.com/jordandalley/dispatchwrapparr) precedent. `min_dispatcharr_version: v0.25.0`. Intentionally does NOT expose tuning fields — `RESV_*` env vars on the container remain the override mechanism, keeping the plugin minimal and the script's design untouched.
- **Release workflow** (`.github/workflows/release.yml`). On `v*` tag push, builds `reservoarr-<version>.zip` (plugin bundle with `reservoarr.py` adjacent to `plugin.py`) and attaches both the zip and standalone script to the GitHub Release with SHA256 in the body.
- **Docs reorg.** README front-loads What/Who/Install; the hard-invariants table, env-var reference, and telemetry schema moved to dedicated files under `docs/`. `CONTRIBUTING.md` and `SECURITY.md` added. `docs/PLUGIN_REGISTRY_SUBMISSION.md` documents the checklist for submitting to the [Dispatcharr/Plugins](https://github.com/Dispatcharr/Plugins) registry (not yet submitted — waits on the public flip + v6.2.0 release).
- **LICENSE** copyright line clarified (`brko7 (Ivan Brkic)`).

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

See [docs/INVARIANTS.md](docs/INVARIANTS.md#decisions-deliberately-not-taken)
for the rejected-decisions table. It's a permanent reference, not a per-release
list, and lives with the rest of the design-rule reference.

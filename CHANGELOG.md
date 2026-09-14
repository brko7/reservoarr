# CHANGELOG

All notable changes to `reservoarr.py`. Each version's invariants are earned by a real production failure — read this before changing the script.

## [6.3.5] — 2026-09-14

New default-off tunable ([@PilaScat](https://github.com/PilaScat), [#40](https://github.com/brko7/reservoarr/issues/40) / [PR #42](https://github.com/brko7/reservoarr/pull/42)). **No change for existing installs** — `RESV_REPLAY_SKIP=0` is the default and leaves the output byte-for-byte as in 6.3.4.

- **New `RESV_REPLAY_SKIP=1` tunable that drops what an edge resends after a plain reconnect.** On some CDNs a connection ends with a clean EOF every few minutes and the next one starts 9–25s behind the point the old one reached. The fetcher appended that replay, so viewers saw the same seconds twice; and because `on_reconnect()` re-anchors the PCR chain, the replay was banked as new cushion on every reconnect instead of self-draining. With the tunable on, the fetcher remembers the last PCR taken before the seam and holds the packets of the new connection until the PCR passes it; only then is what came before dropped. Every seam it cannot place passes through whole, as before: a replay further back than `RESV_REPLAY_MAX_S` (60s), a discontinuity flag, a PCR that jumps back or more than 10s forward, no advancing PCR or no TS sync within 1 MB (a repeated PCR is a legal duplicate), more than 96 MB held, a connection that ends before the seam, and any flushing reconnect. This reopens the "EOF-reconnect overlap-replay dedup" row of `docs/INVARIANTS.md`, now as opt-in. Measured on `cdn_sim --front 25 --eof-at 40`:

  |  | `RESV_REPLAY_SKIP=0` | `RESV_REPLAY_SKIP=1` |
  |---|---|---|
  | cushion | 20 → **42s** | 20 → 20 → 21s |
  | ffmpeg `timestamp discontinuity` | 1–3 per run | 0 |
  | log | — | `replay after reconnect: skipped 23.8s (7.1MB) already delivered` |

- **`RESV_GIVEUP_TRIES` also counts bytes arriving, not bytes ingested**, so an attempt that delivered only a replay is not an attempt without data.

- **The stall watchdog (#4) follows bytes arriving from upstream, not bytes ingested.** While the gate holds a replay, `in_total` does not move; an edge that delivered a replay at realtime for longer than `RESV_STALL_S` would have tripped a reconnect in the middle of it. A new `arrived_total` counts every byte read, dropped or kept, and the watchdog watches that. With `RESV_REPLAY_SKIP=0` the two counters advance together.

- `docs/TELEMETRY.md` documents the two `replay after reconnect: …` lines; `docs/TUNABLES.md` the two tunables. New tests in `tests/unit/test_replay_skip.py`: 17 for the gate and 3 for the arrival counter and the watchdog; and an e2e on the forced-EOF path with the gate on (`run_pipeline(replay_skip=True)`).

## [6.3.4] — 2026-09-13

New default-off tunable ([@PilaScat](https://github.com/PilaScat), [PR #39](https://github.com/brko7/reservoarr/pull/39)). **No change for existing installs** — `RESV_GIVEUP_TRIES=0` is the default and preserves today's retry-forever behaviour.

- **New `RESV_GIVEUP_TRIES=N` tunable so a stream that never delivers a byte fails over instead of stranding the channel.** The fetcher retries every upstream error forever, which is correct once data has flowed (the stall watchdog and reconnects keep the cushion alive) but wrong before the first byte: a stream whose edge answers `403` never ends the process, so Dispatcharr never counts a failed attempt, never walks the chain, and never reaches the fallback — the viewer just gets an error after ~50s. On Dispatcharr 0.30.0 the health monitor (`_monitor_health`) only asks for a stream switch after `channel_init_grace_period` (60s) *plus* three unhealthy checks, but the waiting client aborts at that same grace period (Jellyfin at ~50s), so the channel is stopped before the switch is ever requested. The only fast failover path is the process ending. With `RESV_GIVEUP_TRIES=N`, after N upstream attempts that brought no byte at all (errors and connects that close empty alike) the process exits so Dispatcharr retries and fails over; once any byte has arrived it never fires. Measured on the production profile (2026-09-13, provider edges intermittently `403`-ing after a VPN exit change): with `RESV_GIVEUP_TRIES=3`, the stream switch landed 13s after the tune and the client had picture at +16s, versus an error at +50s and no switch at all before. Documented in `docs/TUNABLES.md`; four new tests in `tests/unit/test_giveup.py` run the real `fetcher()` against scripted answers.

- All 69 unit tests pass.

## [6.3.3] — 2026-09-13

Critical regression fix for v6.3.2 ([@PilaScat](https://github.com/PilaScat), [#36](https://github.com/brko7/reservoarr/issues/36) / [PR #37](https://github.com/brko7/reservoarr/pull/37)). **Do not deploy v6.3.2** — its `stderr_watcher` dies on the first read of every tune. **No pacing/controller behaviour changes.**

- **`stderr_watcher` called `ff.stderr.read1(4096)` on a stream that has no `read1`.** ffmpeg is spawned `bufsize=0`, so `ff.stderr` is a raw `io.FileIO` — and `read1` is defined only on `io.BufferedIOBase`, not on raw streams. The `read1` change shipped in v6.3.2's [PR #31](https://github.com/brko7/reservoarr/pull/31) therefore raised `AttributeError: '_io.FileIO' object has no attribute 'read1'` on the *first* read of every tune, killing the watcher thread. Because the watcher is a daemon thread, the tune kept running and nothing failed loudly, but three things went dark:
  - the corrupt-loop detector (invariant #9) never saw a `Packet corrupt (… dts = N)` line, so a wedged edge could no longer be force-reconnected + flushed;
  - no `frame=…speed=` progress line reached Dispatcharr, so the `RESV_FFMPEG_STATS` starvation-failover added in the same v6.3.2 release did nothing;
  - ffmpeg's stderr was no longer drained, so it would block once the pipe filled (~64 KB of warnings).

  Confirmed on Dispatcharr 0.30.0: over a 3-minute tune on stock v6.3.2, `stream_stats` never updated and no `ffmpeg:` line reached `delaybuf.log`; on the fixed build the stats updated ~11s after tune. The fix reads `os.read(ff.stderr.fileno(), 4096)`, which returns whatever is already in the pipe on both raw and buffered streams — the real-time semantics `read1` was reaching for. The v6.3.2 note that `read1` "stays real-time regardless of stderr buffering" was wrong: `read1` isn't present on the raw `FileIO` that v6.3.2 actually hands the watcher.

- **The v6.3.2 blank-picture fix (drop `-fflags +nobuffer`, invariant #12) was unaffected.** It is an `FFMPEG_CMD` change with no dependency on the watcher, so it worked in v6.3.2 and still works — only `RESV_FFMPEG_STATS` and the corrupt-loop detector were dead.

- **Why the suite missed it:** the `test_ffmpeg_stats.py` fake implemented `read1`, so the tests exercised a method production never had; the e2e suite spawns real ffmpeg, but the daemon-thread death is silent, so it stayed green on v6.3.2. The fake now hands `stderr_watcher` a real OS pipe opened the way `Popen(bufsize=0)` opens stderr (`io.FileIO` over the read fd) — without the fix, four of its tests fail with the production `AttributeError`.

- All 65 unit + 7 e2e tests pass.

## [6.3.2] — 2026-09-11

Two contributor-reported fixes ([@PilaScat](https://github.com/PilaScat)), both diagnosed with captured-stream measurements. **No pacing/controller behaviour changes.**

- **Dropped `-fflags +nobuffer` — it cost the whole first GOP of video** ([#32](https://github.com/brko7/reservoarr/issues/32) / [PR #33](https://github.com/brko7/reservoarr/pull/33)). With `+nobuffer`, ffmpeg's demuxer handed packets on before it had the video's parameter sets, so the muxed output opened with audio from the first sample and video only from the next IDR. On a channel with an 8.3s GOP that was a ~7.8s seam of sound with no picture — longer than every default probe window downstream, so `ffprobe` reported `width=0 height=0` and players showed a blank frame. Measured on captured `/proxy/ts/stream/` output, the same input replayed through the exact `FFMPEG_CMD` one flag apart: first video went from 8.38s to 0.68s and the stream resolved `1280x720 High` on the first probe; on the live channel the seam went from 7.87s to 0.06s. The reservoir already absorbs the startup latency the flag was meant to trim, and the low-latency probe window (`-analyzeduration 1M -probesize 500k`) that keeps the tune inside Plex's tuner timeout is unchanged. Codified as **invariant #12**; regression test in `tests/unit/test_ffmpeg_buffering.py`.

- **New `RESV_FFMPEG_STATS` tunable (default off) so Dispatcharr's buffering failover can see a starving feed** ([#30](https://github.com/brko7/reservoarr/issues/30) / [PR #31](https://github.com/brko7/reservoarr/pull/31)). Dispatcharr reads `speed=` only off a stderr line that also carries `frame=`, and `-loglevel warning` (invariant #9) hides ffmpeg's progress line — so a source delivering below content rate was never failed over to the next stream, the exact fault reservoarr is deployed against. `RESV_FFMPEG_STATS=1` appends `-stats` to `FFMPEG_CMD` (loglevel untouched, so invariant #9 holds); `stderr_watcher` now splits on `\r` as well as `\n` (ffmpeg terminates the progress line with `\r`, which `readline()` never yielded) and keeps the progress line out of `delaybuf.log` so it can't rotate the cushion telemetry away. Default-off means existing installs are byte-identical apart from the `\r` split, which cannot fire on warning-level output. Measured recovery on a synthetic starving source: 22.1s from tune to failover, versus no failover at all before. Documented in `docs/TUNABLES.md`.

- **`stderr_watcher` reads with `read1(4096)`**, not `read(4096)`, so the relay stays real-time regardless of stderr buffering — a `BufferedReader.read(4096)` blocks until 4096 bytes or EOF, correct today only because ffmpeg is spawned `bufsize=0`.

- All 65 unit + 7 e2e tests pass.

## [6.3.1] — 2026-07-03

Hardening release from a full-repo review. Three runtime fixes in `reservoarr.py` plus one plugin install-gate fix. **No pacing/controller behaviour changes**; all invariants intact.

- **Monotonic clock for all interval math.** Pacing debt, the #4 stall watchdog, the per-class reconnect debounces, and the arrival-rate window previously measured intervals on `time.time()` (wall clock). A backward NTP step of T seconds became a single `time.sleep(T)` inside the pacing loop — if T exceeded the remaining cushion, the player starved and the tune died; a forward step could false-trip the stall watchdog into a spurious (buffer-kept) reconnect. All interval measurements now use `time.monotonic()`; the wall clock remains only in log-line timestamps. The debounce anchors initialize to `-inf` rather than `0.0` because monotonic time can be under 90s shortly after host boot, which would otherwise rate-limit the first forced reconnect on a freshly booted machine.

- **Clean shutdown when SIGTERM lands mid-pacing-loop.** `on_term` closes ffmpeg's stdin from the signal handler — deliberately, since that unblocks a write wedged on a full pipe. But when the signal lands between `next_slice()` and the write, the write executes against a *closed* file object and raises `ValueError`, which the `except (BrokenPipeError, OSError)` clause did not catch: every unlucky channel stop dumped a Python traceback into Dispatcharr's transcode log and exited nonzero. The window is a large fraction of loop wall-time (it includes the pacing sleep), so this was a routine occurrence, not a rarity. Now caught and logged as a normal shutdown; a `ValueError` raised while *not* shutting down still propagates — real bugs must stay loud.

- **Log rotation runs continuously, not only at spawn.** The 10 MB rotation check ran once at process start, so a single long-lived tune (a 24/7 channel) grew `delaybuf.log` unboundedly until the *next* channel start — contradicting what `docs/TELEMETRY.md` promised. The check now also runs every 512th log line (~2h at stats cadence, ~5min under an ffmpeg-stderr storm; worst-case overshoot past 10 MB is under 100 KB). The open-per-write append pattern keeps it safe across concurrent streams; a process losing the `os.replace` race just swallows the ENOENT. New regression tests in `tests/unit/test_log_rotation.py` (import-time rotation, mid-run rotation, below-threshold no-op).

- **Plugin upgrade gate: unknown installed version now reinstalls.** `plugin.py` treated a missing or unparseable `.installed_version` sentinel as "no upgrade needed". The sentinel write in `_install()` is `suppress(OSError)`-wrapped — if it ever failed once (transient read-only mount, disk full) while `reservoarr.py` existed, **no future version would ever auto-update the script, silently and permanently**: exactly the drift class invariant #11 exists to prevent. The gate now reinstalls whenever the local version is unknown; `_install()` is an idempotent copy, so the repair costs nothing and self-heals the sentinel on the next successful write.

- All 58 unit + 7 e2e tests pass. `docs/TELEMETRY.md` rotation note updated.

## [6.3.0] — 2026-06-22

Observability: CDN overlap-replay detector (log-only).

- **`pcr_back=N` telemetry counter + per-event log marker.** A new ingest-side observation: when the upstream PCR jumps backward by more than 0.5s, increment the counter and emit a `pcr backward jump: -X.Xs (last=A.A cur=B.B)` line. The sample is still rejected by the existing `0 < delta < 10s` validity gate, so pacing behaviour is **unchanged** — this release adds zero new actions, only visibility.

  **Discovered 2026-06-22 morning** on channel `500054979` (Nick Jr, kids' viewing): edge `5.253.85.204` served the same 13–27 seconds of content twice, with corrupted TS packets at the seam. ffmpeg saw the duplicate content as `Packet corrupt` followed by `timestamp discontinuity (stream id=256): -20000000`, rewrote outgoing DTS to stay monotonic, and the smart TV played the duplicated bytes — the viewer perceives this as a ~20s "rewind" mid-cartoon. Three rewinds in 8 minutes on a single tune; the same edge had produced the same shape on 2026-06-17. None of the existing counters (`ccerr`, `pcrrej`, `disc`, `sync`) name the failure mode directly: a backward PCR sample lands in `pcrrej` alongside benign garbage timestamps, so the signal was buried.

  The 0.5s threshold filters sub-frame PCR jitter from real overlap-replays. The detector distinguishes a true rewind (signed raw delta in `(-TS_WRAP_S/2, -0.5s)`) from a PCR wrap (raw delta near `-TS_WRAP_S`, which is a benign 26.5-hour rollover, not a content jump). Wrap aliasing is exercised in `test_pcr_wrap_not_counted_as_backward`.

- **3 new tests in `tests/unit/test_ts_parser.py`**: a 21-second rewind (matching the 07:14:01 incident) counts and is logged; sub-0.5s jitter is not counted; a 26.5h wrap is not counted as a rewind. All 55 unit + 7 e2e tests pass.

- **`docs/TELEMETRY.md`** documents the new field and event marker. **`docs/INVARIANTS.md` deliberately unchanged**: the "rejected" table's `EOF-reconnect overlap-replay dedup` row explicitly asked for *"a log marker + gather evidence first"* before any dedup work. This release is exactly that log marker. Whether to act on the evidence (input-side byte-drop, edge blocklist, or arming `RESV_TS_RECONNECT=1`) is a future decision, gated on telemetry from this release.

  **Deliberately NOT done**: input-side byte deduplication, edge-IP blocklist, ffmpeg flag changes. Each was considered. The dedup remains rejected per `INVARIANTS.md` ("a PCR-splice dedup risks dropping live content on a garbage seam"). Edge blocklisting via 302 inspection is viable but premature without telemetry to characterise edge-failure distributions. ffmpeg flags (`+discardcorrupt`, `-avoid_negative_ts`) act on frame-level corruption, not backward-DTS — documented dead-ends.

## [6.2.3] — 2026-06-21

User-facing packaging release. **No runtime behaviour changes** — `reservoarr.py` is byte-identical to v6.2.1 and v6.2.2 (sha `4f61fe0e…`).

- **Plugin logo** (`plugin/logo.png` + `plugin/logo.svg`). Damped-wave flow mark: jagged on the left (chaotic upstream input), smooth parallel lines on the right (paced output to the player), with a small amber accent dot at the calm side. Designed via Claude Design as a clean SVG (763 bytes source) and rasterised to 512×512 RGBA PNG for the registry tile. The release zip now carries `logo.png` (see `.github/workflows/release.yml` change in [PR #12](https://github.com/brko7/reservoarr/pull/12)); Dispatcharr's plugin loader auto-picks-up `logo.png` from the plugin directory per the upstream `Plugins.md` spec.
- **README walkthrough + troubleshooting** ([PR #11](https://github.com/brko7/reservoarr/pull/11)). Install Option 1 is now an explicit 5-step sequence (Install → open settings → optional default toggle → Generate → refresh). A "First channel tune" section shows a real stats line and what every field should look like when healthy. A "Troubleshooting" section covers the six failure modes a new user actually hits (stream still dies on prime-time gaps, cushion never reaches target, ccerr climbing, AV desync, Plex won't tune, telemetry file missing) — each with a diagnostic path, not a fix recipe. README ships inside the plugin zip, so registry users get the same docs as GitHub viewers.
- **Plugin UI gains a `next_steps` info field** ([PR #11](https://github.com/brko7/reservoarr/pull/11)). Dispatcharr renders plugin info fields above the action buttons; this one walks the user through the post-Generate sequence (where the Stream Profile lands, per-channel vs default, tail the log, what cushion should do in the first ~60s). Previously the in-Dispatcharr text was a single tuning hint.

This is the **first release to ship a logo + walkthrough docs**, and is what the Dispatcharr/Plugins registry will display on its tile from v6.2.3 onward. v6.2.2's tile shows a placeholder; users on auto-update upgrade to v6.2.3 on the registry's next refresh cycle.

## [6.2.2] — 2026-06-21

Packaging-only release ahead of the Dispatcharr/Plugins registry submission. **No runtime behaviour changes** — `reservoarr.py` is byte-identical to v6.2.1.

- **`plugin/plugin.json` version drift fix.** v6.2.1's release zip shipped `plugin.json` saying `version: 6.2.0` because the version-sync guard (see [PR #1](https://github.com/brko7/reservoarr/pull/1)) was merged *after* the v6.2.1 tag. The plugin's upgrade gate (`packaged > local`) would therefore not fire on a v6.2.0 → v6.2.1 in-place install; users would silently keep running pre-alignment-fix code. Discovered during the registry-submission dry-run.
- **First release built under the version-sync CI guard.** `pyproject.toml`, `plugin/plugin.json`, and `plugin/plugin.py` are now enforced equal-or-fail by `tools/check_versions.py`, wired into both `just lint` and `release.yml`. The drift that produced the v6.2.1 zip cannot recur silently.
- **First release built under the deterministic-zip recipe** (`SOURCE_DATE_EPOCH` pinned to the tag commit, `-X`, sorted file list — see [PR #2](https://github.com/brko7/reservoarr/pull/2)). The release `sha256` is now reproducible from the tag.

Repo also flipped public this day. Dispatcharr/Plugins registry submission tracks this release as the canonical published version.

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

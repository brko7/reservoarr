# reservoarr

A delay-buffer **stream profile for [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr)** that absorbs IPTV CDN gaps so Plex Live TV stops dying.

Plex Live TV's tuner gives up after ~15s of input starvation. IPTV CDNs commonly deliver TS in short bursts with prime-time gaps that exceed this, plus corrupt packets with garbage DTS, mid-stream EOFs, and per-connection corrupt-loops. `reservoir.py` sits between the CDN and Plex, eagerly drains the upstream into a RAM reservoir, and releases bytes to ffmpeg at the stream's measured PCR content rate — so playback runs ~30s behind live and gaps shorter than the cushion are invisible to Plex.

```
upstream HTTP  ──►  RAM reservoir  ──►  paced release  ──►  ffmpeg remux  ──►  Dispatcharr  ──►  Plex
(urllib, eager  (≤256MB,             (byte-rate sleeps      (video copy +
 fetch,          ~30s target          @ PCR content          dump_extra,
 auto-reconnect) cushion)             rate)                  audio → AC3)
```

Single-file, **stdlib-only at runtime** (Python ≥3.11). Spawns ffmpeg as a subprocess. Logs JSON-ish telemetry to a configurable log dir.

## Why it exists

Five real production failure modes drove the design — every invariant below is earned by one of them. See `CHANGELOG.md` for the v5/v6/v6.1 history with concrete incidents.

## Hard invariants — DO NOT regress

| # | Invariant | Why |
|---|---|---|
| 1 | **Byte-rate pacing**, NEVER `ffmpeg -re` / `-readrate` | Corrupt packets with garbage DTS make `-re` sleep for >25s with a full reservoir. PCR is a *measurement* input — a garbage sample is dropped, never slept on. |
| 2 | **stdout carries only the TS stream** | Dispatcharr's relay pipe consumes stdout. Logging goes to stderr + the log file. |
| 3 | **Audio re-encoded to AC3** (`-c:a ac3 -b:a 192k -ac 2`) | Upstream-blessed fix for [Dispatcharr #1122](https://github.com/Dispatcharr/Dispatcharr/issues/1122) (Plex MDE failures on AAC streams). `-c:a copy` causes A/V desync. |
| 4 | **`-bsf:v dump_extra=freq=keyframe`** | Re-injects SPS/PPS at every keyframe so mid-stream tune-in (channel switch) doesn't go black. Required in ffmpeg 7.x and 8.x. |
| 5 | **No `-copyts`** | Causes A/V desync on these streams. |
| 6 | **stdlib-only at runtime** | Dispatcharr's container has no pip available for end-users. Dev/test deps live in `pyproject.toml`'s `[dev]` extra. |
| 7 | **Single deployable file** | Spawned per channel-tune as `reservoir.py {streamUrl} {userAgent}`. |
| 8 | **#5 detector defaults to log-only** (`RESV_TS_RECONNECT=0`) | The ingest-side corruption detector ships in observe mode pending more arming evidence (see `CHANGELOG.md`). Setting `=1` arms the forced reconnect. |
| 9 | **ffmpeg at `-loglevel warning`** | The corrupt-loop stderr watcher parses `Packet corrupt (… dts = N)` lines. A speed-watchdog on a quieter setting can't see the corruption. |

## Install into Dispatcharr

`reservoir.py` is deployed as a custom **Stream Profile** in Dispatcharr (Settings → Stream Settings → Profiles).

### Vendored copy

Put `reservoir.py` somewhere inside the Dispatcharr container's mounted `/data` volume (e.g. `/data/scripts/reservoir.py`). Mark it executable. Add a Stream Profile:

| Field | Value |
|---|---|
| **Name** | `delay-buffer` |
| **Command** | `/data/scripts/reservoir.py` |
| **Parameters** | `{streamUrl} {userAgent}` |
| **Active** | yes |

Set it as the instance-wide default in Settings → Stream Settings → Default Stream Profile, or per channel under Channels → Edit → Stream Profile.

Every channel start spawns a fresh process — no container restart needed when you update the script.

### Pinned-release via Ansible (recommended for IaC)

The pattern below — `get_url` of a tagged release with a sha256 checksum, Renovate-tracked — keeps deployments reproducible.

```yaml
# defaults/main.yml
# renovate: datasource=github-releases depName=<owner>/reservoarr
reservoir_version: 6.1.0
reservoir_sha256: <fill from release>
```

```yaml
# tasks/dispatcharr.yml
- name: Deploy delay-buffer reservoir script
  ansible.builtin.get_url:
    url: "https://github.com/<owner>/reservoarr/releases/download/v{{ reservoir_version }}/reservoir.py"
    dest: "{{ appdata_dir }}/dispatcharr/data/scripts/reservoir.py"
    checksum: "sha256:{{ reservoir_sha256 }}"
    mode: "0755"
```

The Stream Profile DB row can be created idempotently via Dispatcharr's `manage.py shell`:

```python
from core.models import StreamProfile, CoreSettings
desired = {"command": "/data/scripts/reservoir.py",
           "parameters": "{streamUrl} {userAgent}", "is_active": True}
p, _ = StreamProfile.objects.get_or_create(name="delay-buffer", defaults=desired)
for k, v in desired.items():
    setattr(p, k, v)
p.save()
CoreSettings._update_group("stream_settings", "Stream Settings",
                          {"default_stream_profile": p.id})
```

## Tunables (environment variables)

All defaults preserve the v6.1 production behavior. **Override only with evidence.**

| Env var | Default | What it does |
|---|---|---|
| `RESV_PREFILL_BYTES` | `1572864` (1.5 MB) | Sniff target before releasing to ffmpeg. **NOT** a reservoir fill — Plex's tuner times out around 15s, so a full prefill caused "won't start, then loads on retry". |
| `RESV_PREFILL_MAX_S` | `3.0` | Sniff timeout. Must stay well under Dispatcharr's `CONNECTION_TIMEOUT` (~10s). |
| `RESV_HEADSTART_S` | `5.0` | Seconds of content released unpaced after prefill. Gives Plex its startup buffer fast; the rest of the CDN's front-load burst stays banked. |
| `RESV_TARGET_S` | `30.0` | Cushion level the controller holds. Tune higher only if your CDN's worst gaps are longer. |
| `RESV_GRACE_S` | `45.0` | First N seconds use floor `1.0` (realtime release); after, floor drops to `0.97`. Prevents starving the player before the bank settles. |
| `RESV_MAX_BYTES` | `268435456` (256 MB) | Reservoir hard cap. Worst-case RAM per concurrent stream. |
| `RESV_STALL_S` | `25.0` | No-ingest watchdog (#4). If no bytes arrive for this long while running, force a reconnect WITHOUT flushing the buffer. `0` disables. |
| `RESV_TS_RECONNECT` | `0` | #5 ingest-corruption detector action mode. `0` = log-only ("would-fire"). `1` = arm the forced reconnect+flush. **See arming guidance in `CHANGELOG.md`.** |
| `RESV_CC_ERR_PER_WIN` | `3` | #5 trigger: CC errors per 15s window to flag. |
| `RESV_SYNC_ERR_PER_WIN` | `2` | #5 trigger: sync losses per 15s window to flag. |
| `RESV_TS_SUSTAIN_WINS` | `2` | #5: consecutive flagged windows before action. |
| `RESV_LOG_DIR` | `/data/scripts/logs` | Where `delaybuf.log` lives. Set to a writable dir if not running under the AIO container. |
| `RESV_FFMPEG_BIN` | `/usr/local/bin/ffmpeg` | ffmpeg path. Override on dev hosts (`/opt/homebrew/bin/ffmpeg`, `/usr/bin/ffmpeg`). |

## Telemetry

`{RESV_LOG_DIR}/delaybuf.log` (self-rotates at 10 MB, keeps one `.1`). One line every 15s per active stream:

```
2026-06-14T10:03:33+0200 [500004175] cushion=27s(pcr) buf=15.5MB out=4.66Mbps in=4.96Mbps crate=4.80Mbps in_total=1843MB reconnects=0 ccerr=0 pcrrej=0 disc=0 sync=0
```

| Field | Meaning |
|---|---|
| `cushion=Ns(pcr)` | Seconds of media buffered ahead of the player. `(pcr)` = measured off the PCR clock (good); `(byte)` = fallback while PCR is unlocked. |
| `buf=…MB` | Reservoir size in bytes. |
| `out=…Mbps` | Output byte rate to ffmpeg. |
| `in=…Mbps` | Arrival byte rate from upstream. |
| `crate=…Mbps` | PCR-derived content rate (the pacing reference). |
| `in_total=…MB` | Lifetime bytes ingested. |
| `reconnects=N` | Upstream reconnects this session. |
| `ccerr/pcrrej/disc/sync` | TS continuity-counter errors / rejected PCR samples (garbage timestamps) / spec-legal discontinuities / sync losses. Healthy = flat zero. |

Lifecycle events also land here (`upstream connected edge=…`, `upstream EOF`, `corrupt-loop detected …`, `would-fire: …`, `flushed reservoir …`).

## Dev pipeline

```bash
just venv       # one-time: create .venv with pytest + ruff + pytest-xdist
just fixture    # generate fixtures/synth.ts (deterministic, ~2s)
just test       # unit tests (35 tests, ~1s)
just e2e        # synthetic end-to-end (5 tests, ~75s wall-clock with xdist -n auto)
just all        # lint + unit + e2e
```

- **Unit tests**: `TsParser` (PCR extraction, wrap-aware delta, CC continuity, sync recovery, content rate vs ffprobe truth on the fixture), pacing controller math, reconnect-precedence state machine, #5 detector rule replay against the real 2026-06-14 incident signature.
- **End-to-end tests**: spawn `tools/cdn_sim.py` as a real HTTP server and `reservoir.py` as a real subprocess, with the system ffmpeg. Asserts cushion build, 12s stall absorbed, >25s stall trips the watchdog without flushing, forced EOF reconnects cleanly (no `AttributeError`), CC corruption logs `would-fire:` with zero false positives.
- **CI**: GitHub Actions runs lint + unit + e2e on every push/PR — provider-independent (synthetic fixture only).

## Tools

| Path | Purpose |
|---|---|
| `tools/cdn_sim.py` | HTTP server that replays a TS file with the IPTorrents-IPTV delivery shape: front-load burst, stall windows, forced EOF, CC-field corruption injection. |
| `tools/make_synth_ts.sh` | Generates the deterministic synthetic MPEG-TS fixture (testsrc2 + sine → H.264 + AC3 with PCR). Used by CI. |
| `tools/make_corrupt_ts.py` | Standalone CC-corruption generator (alternative to cdn_sim's in-band injection). |
| `tools/parsecheck.py` | Feeds a captured TS file through `TsParser` and compares the PCR-derived duration/rate to ffprobe truth. Useful for validating real CDN captures. |
| `tools/smoke_channel.sh` | Manual ops: tune a Dispatcharr channel end-to-end through the proxy and dump the reservoir telemetry. Parameterized via env (`HOST`, `CONTAINER`, `PROXY_BASE`, `LOG_PATH`) — not CI-runnable. |

## License

MIT. See `LICENSE`.

# reservoarr

[![ci](https://github.com/brko7/reservoarr/actions/workflows/ci.yml/badge.svg)](https://github.com/brko7/reservoarr/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/brko7/reservoarr?display_name=tag&sort=semver)](https://github.com/brko7/reservoarr/releases/latest)
[![license: MIT](https://img.shields.io/github/license/brko7/reservoarr)](LICENSE)
[![python: 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

A delay-buffer **stream profile for [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr)** that absorbs IPTV CDN gaps so Plex Live TV stops dying.

```
upstream HTTP  ──►  RAM reservoir  ──►  paced release  ──►  ffmpeg remux  ──►  Dispatcharr  ──►  Plex
(urllib, eager  (≤256MB,             (byte-rate sleeps      (video copy +
 fetch,          ~30s target          @ PCR content          dump_extra,
 auto-reconnect) cushion)             rate)                  audio → AC3)
```

Plex Live TV's tuner gives up after ~15s of input starvation. IPTV CDNs commonly deliver TS in short bursts with prime-time gaps that exceed this, plus corrupt packets with garbage DTS, mid-stream EOFs, and per-connection corrupt-loops. `reservoarr.py` sits between the CDN and Plex, eagerly drains the upstream into a RAM reservoir, and releases bytes to ffmpeg at the stream's measured PCR content rate — so playback runs ~30s behind live and gaps shorter than the cushion are invisible to Plex.

Single-file, **stdlib-only at runtime** (Python ≥3.11). Spawns ffmpeg as a subprocess. Logs telemetry to a configurable log dir.

## Is this for you?

- You run **Dispatcharr** in front of an IPTV provider whose CDN has prime-time gaps, mid-stream EOFs, or occasional corrupt packets.
- You watch through **Plex Live TV** (or any consumer that gives up after ~15s of silence).
- Symptoms: channels die mid-stream, won't tune on first try, A/V desync after a reconnect, or short black-frame stutters.

If your provider streams cleanly and Plex stays happy, you don't need this.

## Install

### Option 1 — Dispatcharr plugin (recommended)

1. **Install** the plugin: Dispatcharr → Plugins → **Find Plugins** → search `reservoarr` → Install.
2. **Open** the plugin settings (Plugins → reservoarr).
3. **(Optional)** tick **"Set as default Stream Profile"** if you want every channel to use it without per-channel assignment. Leave off if you only want it on a subset of channels.
4. Click **Generate Stream Profile**. A profile named `reservoarr` appears in Settings → Stream Settings → Profiles.
5. **Refresh** the Dispatcharr browser tab (the profile picker is cached client-side).

If you didn't set it as default in step 3, assign it per-channel: Channels → Edit → **Stream Profile** → `reservoarr`.

That's it. Now [tune a channel](#first-channel-tune) to confirm it's working.

> Tuning (cushion size, watchdog thresholds, log directory, etc.) is via `RESV_*` environment variables on the Dispatcharr container — not plugin UI fields. Defaults match production-validated behaviour and fit most providers. See [docs/TUNABLES.md](docs/TUNABLES.md) before overriding.

### Option 2 — Vendored copy + manual Stream Profile

1. Put `reservoarr.py` inside the Dispatcharr container's mounted `/data` volume (e.g. `/data/scripts/reservoarr.py`). Make it executable (`chmod +x`).
2. In Dispatcharr → Settings → Stream Settings → Profiles, add:

   | Field | Value |
   |---|---|
   | **Name** | `delay-buffer` |
   | **Command** | `/data/scripts/reservoarr.py` |
   | **Parameters** | `{streamUrl} {userAgent}` |
   | **Active** | yes |

3. Set it as the instance-wide default (Settings → Stream Settings → Default Stream Profile) or per-channel under Channels → Edit → Stream Profile.

Every channel start spawns a fresh process — no container restart needed when you update the script.

### Option 3 — Pinned release via Ansible (for IaC)

The pattern below — `get_url` of a tagged release with a sha256 checksum, Renovate-tracked — keeps deployments reproducible.

```yaml
# defaults/main.yml
# renovate: datasource=github-releases depName=brko7/reservoarr
reservoarr_version: 6.2.3
reservoarr_sha256: <fill from release>
```

```yaml
# tasks/dispatcharr.yml
- name: Deploy delay-buffer reservoarr script
  ansible.builtin.get_url:
    url: "https://github.com/brko7/reservoarr/releases/download/v{{ reservoarr_version }}/reservoarr.py"
    dest: "{{ appdata_dir }}/dispatcharr/data/scripts/reservoarr.py"
    checksum: "sha256:{{ reservoarr_sha256 }}"
    mode: "0755"
```

The Stream Profile DB row can be created idempotently via Dispatcharr's `manage.py shell`:

```python
from core.models import StreamProfile, CoreSettings
desired = {"command": "/data/scripts/reservoarr.py",
           "parameters": "{streamUrl} {userAgent}", "is_active": True}
p, _ = StreamProfile.objects.get_or_create(name="delay-buffer", defaults=desired)
for k, v in desired.items():
    setattr(p, k, v)
p.save()
CoreSettings._update_group("stream_settings", "Stream Settings",
                          {"default_stream_profile": p.id})
```

## First channel tune

After installing, **start a channel in Plex Live TV** (or your client of choice) and tail the telemetry log:

```bash
# Inside the Dispatcharr container (or on the host, against the bind-mount path):
tail -F /data/scripts/logs/delaybuf.log | grep "cushion="
```

You should see one stats line per active stream every 15 seconds. The shape is:

```
2026-06-21T10:03:33+0000 [500004175] cushion=27s(pcr) buf=15.5MB out=4.66Mbps in=4.96Mbps crate=4.80Mbps in_total=1843MB reconnects=0 ccerr=0 pcrrej=0 disc=0 sync=0
```

**Healthy after ~60s:**
- `cushion=` reaches `~25–30s(pcr)` and oscillates within ±6s of that. The `(pcr)` suffix means the cushion is measured off the PCR clock (good); `(byte)` is a degraded fallback.
- `ccerr`, `pcrrej`, `disc`, `sync` flat at zero.
- `reconnects=0` unless your provider is one of the unreliable ones.
- `out=` ≈ `crate=` ± a few percent.

If something looks off, see [Troubleshooting](#troubleshooting) below. Full telemetry schema in [docs/TELEMETRY.md](docs/TELEMETRY.md).

For end-to-end smoke testing of a specific channel from the command line, see `tools/smoke_channel.sh`.

## Troubleshooting

These are the failure modes real users have hit. Each lists what to check first.

### "Stream still dies on prime-time gaps"

The cushion is your only protection against starvation gaps. If it's too small for your CDN's worst gaps, the stream still dies — `reservoarr` only buys time, not infinity.

1. Tail the log while the failure happens. Note what `cushion=` was reading just before the death.
2. If cushion was already low (<10s) when the gap hit: **raise `RESV_TARGET_S`** above your observed worst gap. Each +10s of target ≈ +6–8 MB RAM at typical HD bitrates. Bump `RESV_MAX_BYTES` proportionally.
3. If cushion was healthy (~30s) and you saw a single very long gap (>30s): the CDN is broken or rate-limited. Increase target *and* check provider status — no buffer can hide a sustained content stoppage.

### "Cushion never reaches the target"

`cushion=` plateaus at 5–15s and won't climb. Two common causes:

1. **Source is bitrate-degraded.** Compare `crate=` (PCR content rate) to your channel's nominal rate. If `crate` is well under (e.g. a "5 Mbps" HD channel showing `crate=2 Mbps`), the CDN is throttling and the front-load burst doesn't contain 30s of content. **Nothing to fix in reservoarr; talk to your provider.**
2. **Cushion is stuck on `(byte)` source.** The PCR clock never locked. Check `pcrrej=`; if it's climbing every window, the stream's PCR samples are unparseable. Try `tools/parsecheck.py` on a captured upstream to confirm.

### "ccerr or sync errors keep climbing"

CC (continuity counter) errors flag genuine TS-packet damage. Some sources have a low baseline (1–2 per stats window); a sustained burst (≥3/window for multiple consecutive windows) is real corruption.

1. If `ccerr` ramps up *with* audio/video desync: corruption is hitting decoded content. Watch for `would-fire: TS corruption detected` lines — that's the ingest-side detector picking it up.
2. **The `#5` detector ships in log-only mode by default** (`RESV_TS_RECONNECT=0`). To arm it: set `RESV_TS_RECONNECT=1` on the container. The script will then force a reconnect + buffer flush when corruption sustains; see CHANGELOG v6.1.0 for the arming-test recipe and the open question about source-wide vs. edge-specific corruption.

### "Audio and video drift out of sync"

The 2026-06-16 alignment bug (latent since v5, fixed in v6.2.1) was the cause of every prior AV-desync report in this codebase. If you're on v6.2.1 or later and still see it:

1. Check the log for `Packet corrupt (stream = N, dts = M)` lines (these are ffmpeg-stderr relayed). A loop of *same* `dts` 3× in 120s triggers the corrupt-loop detector — the script will reconnect and flush.
2. If it's *different* dts each time, the source is genuinely emitting damaged frames; the `#5` detector (log-only by default) catches this class and would-fire on it. Arm it as above.
3. If you see neither: please [open an issue](https://github.com/brko7/reservoarr/issues) with the relevant `delaybuf.log` excerpt — pre-v6.2.1 contamination is now ruled out and a new AV-desync class would be worth investigating.

### "Plex says 'no signal' / channel won't tune on first try"

Plex's tuner times out after ~15s of input starvation. The prefill phase exists to push the first bytes within that window.

1. Check the log for `prefill done: NMB in Ns` after a channel start. The `Ns` is wall-clock time to first frame. If it's >5s, your provider is slow to send the first bytes.
2. **Do not** raise `RESV_PREFILL_MAX_S` past 5s — Plex's hard ceiling is ~15s and Dispatcharr's connection timeout sits around 10s.
3. If your provider's first-bytes latency is consistently bad, this is a provider-side problem; no wrapper buffer can fix it.

### Telemetry file isn't appearing

Check `RESV_LOG_DIR` on the container (defaults to `/data/scripts/logs/`). The script `os.makedirs`'s it at startup; if it can't (permission denied, read-only mount), the run continues but logs only to stderr (which Dispatcharr's transcode logger captures separately). Confirm the directory exists and is writable by the container's PUID.

### Still stuck?

Open a [Discussion](https://github.com/brko7/reservoarr/discussions) with:
- `delaybuf.log` excerpt around the event (15 min before/after)
- Your `RESV_*` env settings (or "defaults" if none)
- Whether restarting the channel resolved it

## How it works

Pacing happens in the wrapper, not in ffmpeg. The reservoir fetches upstream bytes eagerly while a controller releases them to ffmpeg at the stream's **PCR content rate** (bytes-per-second of media time), holding a ~30s cushion. The CDN's per-connection front-load burst banks the cushion; gaps shorter than the cushion drain it without the player ever noticing.

Why pace here and not with `ffmpeg -re`: the provider's streams carry occasional corrupt packets with garbage DTS values, and `-re` sleeps on them (observed 25s output freeze with a full reservoir). PCR is a *measurement* input — a garbage sample is rejected by a plausibility window and the chain re-anchors.

Three watchdogs ride alongside:

- **Corrupt-loop** (ffmpeg-stderr-side, default armed): same `dts` reported 3× → reconnect + flush.
- **Stall** (#4, default armed): no ingest progress for `RESV_STALL_S` → reconnect WITHOUT flushing.
- **TS corruption** (#5, default log-only): sustained CC/sync errors while bytes flow → reconnect + flush, when `RESV_TS_RECONNECT=1`.

Deeper reading:

- [docs/INVARIANTS.md](docs/INVARIANTS.md) — the rules anyone editing the script must respect, with the production failure each one is earned by.
- [docs/TUNABLES.md](docs/TUNABLES.md) — env-var reference, when to bump what.
- [docs/TELEMETRY.md](docs/TELEMETRY.md) — log-line schema, lifecycle events, what healthy looks like.
- [CHANGELOG.md](CHANGELOG.md) — incident history per release; the design rationale lives here.

## Dev pipeline

```bash
just venv       # one-time: create .venv with pytest + ruff + pytest-xdist
just fixture    # generate fixtures/synth.ts (deterministic, ~2s)
just test       # unit tests (52 tests, ~1s)
just e2e        # synthetic end-to-end (7 tests, ~90s wall-clock with xdist -n auto)
just all        # lint + unit + e2e
```

- **Unit tests** (52): `TsParser` (PCR extraction, wrap-aware delta, CC continuity, sync recovery, content rate vs ffprobe truth on the fixture), pacing controller math, reconnect-precedence state machine, #5 detector rule replay against the real 2026-06-14 incident signature, and TS-packet alignment regression coverage (v6.2.1 fix).
- **End-to-end tests** (7): spawn `tools/cdn_sim.py` as a real HTTP server and `reservoarr.py` as a real subprocess, with the system ffmpeg. Asserts cushion build, PCR clock lock, 12s stall absorbed, >25s stall trips the watchdog without flushing, forced EOF reconnects cleanly, CC corruption logs `would-fire:` with zero false positives.
- **CI**: GitHub Actions runs lint + unit + e2e on every push/PR — provider-independent (synthetic fixture only).

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR.

## Tools

| Path | Purpose |
|---|---|
| `tools/cdn_sim.py` | HTTP server that replays a TS file with the IPTorrents-IPTV delivery shape: front-load burst, stall windows, forced EOF, CC-field corruption injection. |
| `tools/make_synth_ts.sh` | Generates the deterministic synthetic MPEG-TS fixture (testsrc2 + sine → H.264 + AC3 with PCR). Used by CI. |
| `tools/make_corrupt_ts.py` | Standalone CC-corruption generator (alternative to cdn_sim's in-band injection). |
| `tools/parsecheck.py` | Feeds a captured TS file through `TsParser` and compares the PCR-derived duration/rate to ffprobe truth. Useful for validating real CDN captures. |
| `tools/smoke_channel.sh` | Manual ops: tune a Dispatcharr channel end-to-end through the proxy and dump the reservoir telemetry. Parameterized via env (`HOST`, `CONTAINER`, `PROXY_BASE`, `LOG_PATH`) — not CI-runnable. |

## Support & community

- **Bugs / feature requests** → [Issues](https://github.com/brko7/reservoarr/issues) (use the templates — they ask for the telemetry excerpts that make triage fast).
- **Questions, setup help, tuning** → [Discussions](https://github.com/brko7/reservoarr/discussions).
- **Security or Code-of-Conduct concerns** → [private advisory flow](https://github.com/brko7/reservoarr/security/advisories/new). See [SECURITY.md](SECURITY.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Security

`reservoarr.py` runs inside the Dispatcharr container with whatever privileges that container has. It fetches the upstream URL Dispatcharr hands it, pipes bytes through ffmpeg, and writes a log file — nothing else. No credentials, no database access, no outbound traffic beyond the configured upstream. See [SECURITY.md](SECURITY.md) for the full threat model.

## License

MIT. See [LICENSE](LICENSE).

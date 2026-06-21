# reservoarr

> ⚠️ **Pre-release / not yet published.** This repo is private and the install instructions below reference a `v6.2.0` GitHub Release that does not exist yet. The Dispatcharr Plugins-registry submission is gated on the public flip. Until then, the only way to run this is to clone the repo and follow the Option 2 vendored-copy path manually.

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

### Option 1 — Dispatcharr plugin (recommended, coming with v6.2.0)

Install via Dispatcharr → Plugins → Find Plugins → search "reservoarr" → Install. Click "Generate Stream Profile" in the plugin settings. Done. See [docs/PLUGIN_REGISTRY_SUBMISSION.md](docs/PLUGIN_REGISTRY_SUBMISSION.md) for the registry submission status.

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
reservoarr_version: 6.2.0
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

## Verifying it works

After installing, tune a channel through Plex and watch the telemetry:

```bash
tail -F /data/scripts/logs/delaybuf.log | grep "cushion="
```

Healthy after ~60s: `cushion=` reaches `~25–30s(pcr)` and holds; `ccerr/pcrrej/sync` flat at zero. Full schema in [docs/TELEMETRY.md](docs/TELEMETRY.md).

For end-to-end smoke testing against a running Dispatcharr, see `tools/smoke_channel.sh`.

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

## Security

`reservoarr.py` runs inside the Dispatcharr container with whatever privileges that container has. It fetches the upstream URL Dispatcharr hands it, pipes bytes through ffmpeg, and writes a log file — nothing else. No credentials, no database access, no outbound traffic beyond the configured upstream. See [SECURITY.md](SECURITY.md) for the full threat model and how to report vulnerabilities.

## License

MIT. See [LICENSE](LICENSE).

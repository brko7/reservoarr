# Contributing to reservoarr

reservoarr is a small, audited single-file script with a hard rule that every line is earned by a real production failure. That's not a tone — it's the actual contribution model. Read this first.

## Rules

1. **stdlib-only at runtime.** `reservoarr.py` imports only from the Python ≥3.11 standard library. Dispatcharr's container has no pip available to end-users; a runtime `import requests` will break installs. Dev/test deps (pytest, ruff) live in `pyproject.toml`'s `[dev]` extra.

2. **Single deployable file.** The script must keep running unchanged as `reservoarr.py {streamUrl} {userAgent}`. Splitting into a package would force users to vendor multiple files into `/data/scripts/` and breaks the plugin path.

3. **Respect the hard invariants.** See [docs/INVARIANTS.md](docs/INVARIANTS.md). Every entry is a load-bearing rule with a documented incident behind it. If you think one is wrong, open an issue describing the new evidence — don't patch around it.

4. **Evidence first, actions later.** The #5 detector ships in log-only mode pending an arming-test result. Any new detection rule lands in log-only mode first, gets validated against a real incident, and only then becomes an action. The [CHANGELOG](CHANGELOG.md) entries explain how previous detectors crossed that line.

5. **No new abstractions without a second use site.** The script is intentionally low-altitude. A factory, a strategy class, or a helper module needs to be justified by a concrete second caller in the same PR.

## Dev workflow

```bash
just venv       # one-time: create .venv with pytest + ruff + pytest-xdist
just fixture    # generate fixtures/synth.ts (deterministic, ~2s, needs ffmpeg)
just test       # unit tests (52 tests, ~1s, no ffmpeg)
just e2e        # synthetic end-to-end (7 tests, ~90s wall-clock with xdist -n auto, needs ffmpeg)
just all        # lint + unit + e2e
```

The fixture and e2e tests are provider-independent: they spawn `tools/cdn_sim.py` as a real HTTP server and replay a synthetic TS file with the IPTorrents delivery shape. No live CDN, no captured streams in CI.

On macOS dev hosts, set `RESV_FFMPEG_BIN=/opt/homebrew/bin/ffmpeg` if `which ffmpeg` doesn't return `/usr/local/bin/ffmpeg`.

## What changes look like

Good PRs:

- Fix a bug with a unit or e2e test that captures the failure mode.
- Add a tunable (env var) with a default that preserves existing behaviour.
- Improve a docstring with non-obvious context (a hidden constraint, a workaround for a specific bug, a subtle invariant).

PRs that need an issue first:

- Touching one of the hard invariants.
- Changing the pacing math or controller floor/ceiling.
- Adding a new dependency.
- Restructuring the file or splitting it.

PRs that should bring real-data evidence:

- New detection rule, or changing a threshold (`RESV_CC_ERR_PER_WIN`, `RESV_SYNC_ERR_PER_WIN`, etc.).
- Arming a previously log-only behaviour.

A "real-data evidence" PR includes: the incident timestamp, the relevant `delaybuf.log` lines, what the rule fires on vs misses, and a `tests/unit/test_*` case that replays the signature.

## Reporting a "would-fire didn't fire" (or "fired when it shouldn't")

#5's log-only mode exists exactly to collect these. File an issue with:

1. The `delaybuf.log` lines around the event (15 minutes before/after).
2. Whether ffmpeg's stderr also flagged corruption in the same window.
3. Your `RESV_*` env settings.
4. Whether forcing a manual reconnect (restart the channel) resolved it.

False-positive reports are especially valuable: the rule's calibration depends on them.

## Reporting security issues

See [SECURITY.md](SECURITY.md). Don't open a public issue for vulnerabilities — use GitHub's private security advisory flow.

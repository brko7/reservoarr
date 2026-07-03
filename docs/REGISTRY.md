# Dispatcharr/Plugins registry — maintenance guide

reservoarr is listed in the [Dispatcharr/Plugins](https://github.com/Dispatcharr/Plugins) registry as of 2026-06-21 ([initial submission PR #137](https://github.com/Dispatcharr/Plugins/pull/137); [logo + v6.2.3 PR #139](https://github.com/Dispatcharr/Plugins/pull/139)).

This doc covers ongoing maintenance: how to push new versions to the registry, what the registry's CI checks, and the version-bump rules.

## How the registry works

- The registry stores **metadata only** for external plugins. Each plugin lives under `plugins/<slug>/`. Our entry is at [`plugins/reservoarr/`](https://github.com/Dispatcharr/Plugins/tree/main/plugins/reservoarr).
- The plugin zip itself stays on our GitHub Releases. The registry's `plugin.json` carries a `source_url` with a `{version}` placeholder; on every merged PR, the registry's CI downloads the resolved zip, computes its own SHA256 (it does **not** trust the release body's value), GPG-signs the manifest, and re-publishes the zip onto `Dispatcharr/Plugins`'s own release page.
- The "Find Plugins" UI in every Dispatcharr install reads from the registry's signed manifest, refreshed every ~6h.

## Updating the registry for a new reservoarr release

1. Cut the reservoarr release first (`vX.Y.Z` tag, `release.yml` builds the zip).
2. Fork or update your fork of [Dispatcharr/Plugins](https://github.com/Dispatcharr/Plugins).
3. Edit `plugins/reservoarr/plugin.json` — bump `version` to the new value.
4. Open a PR with title `[reservoarr] Bump to vX.Y.Z` (or similar — bot enforces the title format).
5. The validation bot runs CodeQL + ClamAV + version-bump checks. Wait for green.
6. Wait for maintainer merge.

If you're also updating the logo or README in the registry directory (not just bumping the manifest), follow the same PR flow — but understand the version-bump rule below.

## Version-bump rule

The registry's validation bot **rejects PRs that change files in a plugin dir without a version bump**, except for these specific `plugin.json` fields (which are metadata-only-exempt):

- `description`
- `repo_url`
- `discord_thread`
- `maintainers`
- `min_dispatcharr_version`
- `max_dispatcharr_version`
- `deprecated`
- `unlisted`

**`logo.png`, `README.md`, and adding any new file are NOT exempt.** We learned this the hard way on PR #139: tried to add `logo.png` as a metadata-only change, got rejected, bundled it under v6.2.3 instead.

If you need to ship a user-facing change that doesn't warrant a `reservoarr.py` runtime change, bundle it as a packaging-only release (`reservoarr.py` byte-identical to the previous release, `pyproject.toml`/`plugin.json`/`plugin.py` versions bumped together via the CI sync gate).

## The plugin.json shipped to the registry

```json
{
  "name": "reservoarr",
  "version": "<release version, e.g. 6.2.3>",
  "description": "Delay-buffer stream profile that absorbs IPTV CDN gaps so Plex Live TV stops dying",
  "author": "brko7",
  "maintainers": ["brko7"],
  "license": "MIT",
  "min_dispatcharr_version": "v0.25.0",
  "repo_url": "https://github.com/brko7/reservoarr",
  "source_type": "external",
  "source_url": "https://github.com/brko7/reservoarr/releases/download/v{version}/reservoarr-{version}.zip"
}
```

The `{version}` placeholder is substituted by the registry at fetch time — our release workflow's filename convention (`reservoarr-<version>.zip`) is what makes this work. Don't hardcode the URL.

Note: this is the registry's copy of `plugin.json` (in `Dispatcharr/Plugins`), which is different from the `plugin.json` inside the release zip (in `brko7/reservoarr`'s `plugin/plugin.json`). The two have overlapping but not identical fields — only the in-repo copy is consumed by Dispatcharr at plugin-load time; the registry copy is what the "Find Plugins" UI shows.

## What the registry's CI checks

From the validation bot comments on PRs #137 and #139:

| Check | What it does |
|---|---|
| PR title format | `[<slug>] description` (colon optional) |
| Folder name | Must be lowercase-kebab-case |
| `plugin.json` presence | File must exist |
| JSON syntax | Must be valid JSON |
| Required fields | `name`, `version`, `description`, `author` or `maintainers`, `license` |
| Version format | `MAJOR.MINOR.PATCH` (semver) |
| Version bump | Strictly greater than current published version (see exemption list above) |
| Permission | PR author must be in `author` / `maintainers` |
| License | OSI-approved SPDX identifier |
| `min_dispatcharr_version` / `max_dispatcharr_version` | Semver, if provided |
| `repo_url` / `discord_thread` | Must start with `http://` or `https://` |
| CodeQL | Scans Python in the downloaded zip (blocking) |
| ClamAV | Scans all zip contents for malware (blocking) |
| `source_url` reachability | Downloads the resolved URL; artifact must be present |

The bot posts a structured comment on every PR/push with check status.

## Rolling back a broken release

If a release is broken, **don't try to yank the registry entry** — the registry has no "yank previous" workflow. The only way to make installers skip a bad version is to publish a fixed version above it (e.g. v6.2.X → v6.2.X+1) and bump the registry PR to point at the new one.

If the broken release was published as v6.2.X with `plugin.json.version=6.2.X` and a known-bad runtime, ship a v6.2.X+1 with the fix and update the registry. Old installs on v6.2.X will auto-upgrade on their next 6h registry refresh.

## Watch-outs

- **Don't include `source_type: external` for self-hosted zips** unless the URL is reachable from CI runners (GitHub Releases is fine for public repos).
- **Don't sign the zip yourself** — the registry GPG-signs whatever it pulls.
- **Folder slug** inside the registry MUST be `reservoarr` (lowercase, no separators). This is what becomes the plugin's directory under `/data/plugins/` and the registry key.
- **`plugin.json.version` must match the git tag** (without the `v` prefix). Our `release.yml` enforces this; the registry's `source_url` substitution would otherwise resolve to a non-existent filename.

## References

- Registry repo: https://github.com/Dispatcharr/Plugins
- Registry CONTRIBUTING: https://github.com/Dispatcharr/Plugins/blob/main/CONTRIBUTING.md
- Registry manifest spec: https://github.com/Dispatcharr/Dispatcharr/blob/main/Plugin_repo.md
- Dispatcharr plugin docs: https://github.com/Dispatcharr/Dispatcharr/blob/main/Plugins.md
- Our entry: https://github.com/Dispatcharr/Plugins/tree/main/plugins/reservoarr

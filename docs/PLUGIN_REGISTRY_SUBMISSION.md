# Dispatcharr Plugins registry submission

This is a checklist for opening the PR that adds `reservoarr` to the official
[Dispatcharr/Plugins](https://github.com/Dispatcharr/Plugins) registry.

**Status:** not submitted. Submit after the repo is public and a `v6.2.0` release
exists with attached `reservoarr-6.2.0.zip`.

## Prerequisites

- [ ] `brko7/reservoarr` is **public** on GitHub.
- [ ] Tag `v6.2.0` exists and the release workflow has produced:
  - `reservoarr-6.2.0.zip` (the plugin bundle)
  - SHA256 visible in the release body
- [ ] The zip's top-level folder is `reservoarr/` (set by `.github/workflows/release.yml`).
- [ ] The zip contains: `plugin.py`, `plugin.json`, `reservoarr.py`, `LICENSE`, `README.md`.
- [ ] `plugin.json`'s `version` matches the release tag (without leading `v`).
- [ ] Smoke-tested on a real Dispatcharr instance: install via Plugins UI →
      "Generate Stream Profile" → tune a channel → telemetry healthy.

## How the registry works

Per [Dispatcharr/Plugins CONTRIBUTING](https://github.com/Dispatcharr/Plugins/blob/main/CONTRIBUTING.md):

- The registry stores **only metadata** for external plugins. The plugin zip
  itself stays on our GitHub Releases.
- The registry's CI (CodeQL + ClamAV + version-bump validation) runs on every PR.
- On merge, the registry pulls the zip from our `source_url`, computes its own
  SHA256 (it does **not** trust an upstream-supplied hash), GPG-signs it, and
  re-publishes to `Dispatcharr/Plugins`'s own release.
- The "Find Plugins" UI in every Dispatcharr install reads from the registry's
  signed manifest.

## The PR

Fork [Dispatcharr/Plugins](https://github.com/Dispatcharr/Plugins). Add this file:

**Path:** `plugins/reservoarr/plugin.json`

```json
{
  "name": "reservoarr",
  "version": "6.2.0",
  "description": "Delay-buffer stream profile that absorbs IPTV CDN gaps so Plex Live TV stops dying",
  "author": "brko7",
  "maintainers": ["brko7"],
  "min_dispatcharr_version": "v0.25.0",
  "help_url": "https://github.com/brko7/reservoarr",
  "license": "MIT",
  "repo_url": "https://github.com/brko7/reservoarr",
  "source_type": "external",
  "source_url": "https://github.com/brko7/reservoarr/releases/download/v{version}/reservoarr-{version}.zip"
}
```

The `{version}` placeholder is substituted by the registry at fetch time — our
release workflow's filename convention (`reservoarr-<version>.zip`) is what makes
this work. Don't hardcode the URL.

PR title suggestion: `Add reservoarr plugin v6.2.0`.

PR body: link to this repo, a one-line description, and a screenshot of the
plugin running in Dispatcharr (the registry maintainers ask for one).

## What happens after merge

- The registry's `manifest.json` is regenerated; every Dispatcharr install picks
  it up on the next "Find Plugins" refresh.
- Future versions: bump `plugin.json`'s `version` in the registry (one-line PR)
  whenever we cut a new release here. The `{version}` substitution does the rest.
- The registry validates that the version is monotonically increasing.

## Rolling back

If a release is broken, bump the patch version in **both** this repo's
`plugin.json` AND a follow-up PR to the registry. The registry has no
"yank previous" workflow; the only way to make installers skip a bad version
is to publish a fixed one above it.

## Watch-outs

- **Don't include `source_type: external` for self-hosted zips** unless the URL
  is reachable from CI runners (it is, for public GitHub releases).
- **Don't sign the zip yourself** — the registry GPG-signs whatever it pulls.
- **Folder slug** inside the zip MUST be `reservoarr` (lowercase, no separators).
  This is what becomes the plugin's directory under `/data/plugins/` and the
  registry key.

## References

- Registry repo: https://github.com/Dispatcharr/Plugins
- Registry CONTRIBUTING: https://github.com/Dispatcharr/Plugins/blob/main/CONTRIBUTING.md
- Registry manifest spec: https://github.com/Dispatcharr/Dispatcharr/blob/main/Plugin_repo.md
- Dispatcharr plugin docs: https://github.com/Dispatcharr/Dispatcharr/blob/main/Plugins.md
- Reference precedent (Dispatchwrapparr): https://github.com/jordandalley/dispatchwrapparr

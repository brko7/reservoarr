# Releasing

Maintainer guide for cutting a reservoarr release. The pipeline is automated
from squash-merge to published zip; the registry bump stays manual.

## The flow

1. Branch, make the change, run `just all` locally.
2. Bump the version in **four places** — CI's `tools/check_versions.py` blocks
   the PR if any of them drift:
   - `pyproject.toml` → `version`
   - `plugin/plugin.json` → `version`
   - `plugin/plugin.py` → the `version` class attribute
   - `CHANGELOG.md` → add a `## [X.Y.Z] — YYYY-MM-DD` section (it becomes the
     annotated tag body, and therefore the release notes context)
3. Open a PR whose squash-commit subject is exactly `vX.Y.Z — short subject`.
4. Squash-merge once the required `test` check is green.
5. Automation takes over:
   - **`auto-tag.yml`** sees the `vX.Y.Z` subject, verifies it matches
     `pyproject.toml` and is strictly greater than the latest tag, creates the
     annotated tag with the CHANGELOG section as body, and dispatches
     `release.yml` (an explicit dispatch — a tag pushed by `GITHUB_TOKEN`
     doesn't fire tag-triggered workflows).
   - **`release.yml`** re-checks version sync, builds the reproducible zip via
     `tools/build_zip.sh` (`SOURCE_DATE_EPOCH` pinned to the tag commit's
     timestamp), and publishes the GitHub Release with sha256s for both the
     zip and the standalone `reservoarr.py`.
6. **Manual**: open the Dispatcharr/Plugins PR bumping
   `plugins/reservoarr/plugin.json`'s `version` — see [REGISTRY.md](REGISTRY.md).
   Once merged, the registry re-publishes the zip and every Dispatcharr install
   picks it up on its ~6h registry refresh.

## Reproducibility is load-bearing

Downstream deployments pin the zip by sha256. `tools/build_zip.sh` is the
single source of truth for the zip recipe; CI's reproducibility check builds
it twice (with perturbed mtimes) on any PR touching the recipe or zip contents
and fails on divergence. Don't fork or bypass the recipe, and don't rebuild
published tags casually — a rebuild that produces a different sha invalidates
existing pins.

## What needs a release

Anything that changes zip contents needs a version bump: `reservoarr.py`,
`plugin/plugin.py`, `plugin/plugin.json`, `plugin/logo.png`, `README.md`,
`LICENSE`. Packaging-only releases (byte-identical `reservoarr.py`, versions
bumped together) are normal — v6.2.2 and v6.2.3 were exactly that.

Doc-only changes (`docs/`, `CHANGELOG.md`) and dev tooling (`tests/`,
`tools/` except `build_zip.sh` behaviour, `justfile`) don't need a release.

## Manual fallback

If auto-tag misfires, or you need to rebuild an existing tag:

- Actions → **release** → Run workflow → enter `vX.Y.Z` (the tag must already
  exist), or
- tag locally: `git tag -a vX.Y.Z && git push origin vX.Y.Z` (a human-pushed
  tag triggers `release.yml` directly).

`release.yml` hard-fails if the tag doesn't match `pyproject.toml` at that
commit, so a mistyped tag can't publish a mismatched zip.

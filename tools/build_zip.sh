#!/usr/bin/env bash
# Build the reservoarr plugin zip reproducibly. Single source of truth for
# the release-zip recipe — release.yml calls this on tag push, and the
# reproducibility check in ci.yml calls it twice to verify identical
# inputs produce a byte-identical zip.
#
# Reproducibility matters because superlab's Renovate config pins the
# deployed plugin by sha256. A re-build that produces a different zip
# would invalidate the pin and break the deploy.
#
# Inputs (env):
#   VERSION         — semver string, e.g. "6.3.0" (no leading v). Required.
#   SOURCE_DATE_EPOCH — Unix timestamp pinned for in-zip mtimes. Required.
#                      In release.yml this is the tag commit's timestamp;
#                      in the CI reproducibility check it's an arbitrary
#                      fixed value (identical inputs → identical output).
#
# Outputs:
#   ./reservoarr-${VERSION}.zip in the current working directory.
#   Prints sha256 to stdout (last line).
#
# Reproducibility ingredients (all required, do not remove):
#   - SOURCE_DATE_EPOCH pins all member mtimes (zip 3.0+ honours this).
#   - `-X` strips extra file attributes (ACLs, xattrs) that vary by runner.
#   - Sorted file list pins member ordering.
#   - `touch -d "@$SOURCE_DATE_EPOCH"` aligns staged-file mtimes to the
#     pinned epoch before zipping, otherwise checkout-time mtimes leak in.
#   - `TZ=UTC` removes locale-dependent timezone interpretation.
#   - `LC_ALL=C` pins sort collation.

set -euo pipefail

: "${VERSION:?VERSION must be set (e.g. 6.3.0)}"
: "${SOURCE_DATE_EPOCH:?SOURCE_DATE_EPOCH must be set}"

STAGE="stage/reservoarr"
OUT="reservoarr-${VERSION}.zip"

rm -rf stage "$OUT"
mkdir -p "$STAGE"

# Plugin code + manifest live alongside the runtime script for self-contained install.
cp plugin/plugin.py "$STAGE/plugin.py"
cp plugin/plugin.json "$STAGE/plugin.json"
cp plugin/logo.png "$STAGE/logo.png"
cp reservoarr.py "$STAGE/reservoarr.py"
cp LICENSE "$STAGE/LICENSE"
cp README.md "$STAGE/README.md"
chmod +x "$STAGE/reservoarr.py"

export SOURCE_DATE_EPOCH TZ=UTC
find stage/reservoarr -exec touch -d "@$SOURCE_DATE_EPOCH" {} +
(cd stage && find reservoarr -type f | LC_ALL=C sort | zip -X -@ "../$OUT" >/dev/null)

sha256sum "$OUT" | awk '{print $1}'

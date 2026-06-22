#!/usr/bin/env python3
"""Assert pyproject.toml, plugin/plugin.json, and plugin/plugin.py all carry
the same version string, and that CHANGELOG.md has a matching `## [X.Y.Z]`
section. Run by `just version-check` (which `just lint` and CI both depend on).

Why this exists: the Dispatcharr plugin's upgrade gate in plugin.py only
reinstalls `reservoarr.py` into /data/reservoarr/ when the packaged plugin
version is *strictly greater than* the .installed_version sentinel. If
plugin.py and plugin.json keep saying "6.2.0" while pyproject moves to
"6.2.1", a user installing the v6.2.1 zip keeps running v6.2.0's
reservoarr.py — silently. The 6.2.0/6.2.1 release shipped in exactly this
state before this check existed.

The CHANGELOG check is here because auto-tag.yml uses the matching
`## [X.Y.Z]` section as the annotated tag body. A version bump without a
CHANGELOG entry would merge green, then fail post-merge in auto-tag.
Catching it at PR time turns a post-merge surprise into a pre-merge red.
"""
from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    pp = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    pj = json.loads((ROOT / "plugin/plugin.json").read_text())["version"]
    m = re.search(r'^\s*version\s*=\s*"([^"]+)"', (ROOT / "plugin/plugin.py").read_text(), re.M)
    py = m.group(1) if m else None

    versions = {
        "pyproject.toml": pp,
        "plugin/plugin.json": pj,
        "plugin/plugin.py": py,
    }
    for k, v in versions.items():
        print(f"  {k}: {v}")

    if len(set(versions.values())) != 1 or None in versions.values():
        print("VERSION DRIFT: bump all three to the same value", file=sys.stderr)
        return 1

    changelog = (ROOT / "CHANGELOG.md").read_text()
    if not re.search(rf"^## \[{re.escape(pp)}\]", changelog, re.M):
        print(f"  CHANGELOG.md: missing `## [{pp}]` section", flush=True)
        print(f"CHANGELOG DRIFT: add a `## [{pp}]` section for this release", file=sys.stderr)
        return 1
    print(f"  CHANGELOG.md: [{pp}] section present")

    print("versions in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())

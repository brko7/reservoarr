#!/usr/bin/env python3
"""Bump the release version in lockstep across the three files CI's
`check_versions.py` guards, and scaffold a matching CHANGELOG section.

Usage:
    python tools/bump_version.py X.Y.Z      (or: just bump X.Y.Z)

This is the *doer* to `check_versions.py`'s *guard*. Cutting a release meant
hand-editing `pyproject.toml`, `plugin/plugin.json`, `plugin/plugin.py`, and
`CHANGELOG.md` — four edits that must agree, where a single miss trips CI or,
worse, silently wedges the plugin upgrade gate (invariant #10/#11: the gate
only reinstalls when the *packaged* version is strictly greater than the
installed one, so drift = users never auto-update `reservoarr.py`).

What it does NOT do: write the CHANGELOG prose (that's yours — it becomes the
annotated tag body via auto-tag.yml), commit, tag, or push. Run it, fill in
the scaffolded section, then open the `vX.Y.Z — …` PR by hand.
"""
from __future__ import annotations

import datetime
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
PLUGIN_JSON = ROOT / "plugin/plugin.json"
PLUGIN_PY = ROOT / "plugin/plugin.py"
CHANGELOG = ROOT / "CHANGELOG.md"

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _read_versions() -> dict[str, str]:
    pp = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    pj = json.loads(PLUGIN_JSON.read_text())["version"]
    m = re.search(r'^\s*version\s*=\s*"([^"]+)"', PLUGIN_PY.read_text(), re.M)
    py = m.group(1) if m else None
    return {"pyproject.toml": pp, "plugin/plugin.json": pj, "plugin/plugin.py": py}


def _sub_once(path: Path, pattern: str, new_version: str) -> None:
    """Replace exactly one version occurrence, preserving all other bytes.
    Fails loud if the file's shape drifted so the regex no longer matches."""
    text = path.read_text()
    new_text, n = re.subn(pattern, r"\g<1>" + new_version + r"\g<2>", text)
    if n != 1:
        print(f"ERROR: expected exactly 1 version match in {path.name}, found {n} "
              f"— the file's format changed; update bump_version.py", file=sys.stderr)
        sys.exit(1)
    path.write_text(new_text)


def _scaffold_changelog(new_version: str) -> bool:
    """Insert a `## [X.Y.Z] — <today>` stub before the newest existing entry.
    Returns False (no-op) if the section already exists."""
    text = CHANGELOG.read_text()
    if re.search(rf"^## \[{re.escape(new_version)}\]", text, re.M):
        return False
    today = datetime.date.today().isoformat()
    stub = (
        f"## [{new_version}] — {today}\n\n"
        "_TODO: describe this release. This text becomes the annotated git tag "
        "body (auto-tag.yml) and the GitHub release notes — write it before merging._\n\n"
    )
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("## ["):                      # newest existing entry
            lines.insert(i, stub)
            CHANGELOG.write_text("".join(lines))
            return True
    # No prior entries: append after the file's intro.
    CHANGELOG.write_text(text.rstrip() + "\n\n" + stub)
    return True


def main() -> int:
    if len(sys.argv) != 2 or not SEMVER.match(sys.argv[1]):
        print("usage: bump_version.py X.Y.Z", file=sys.stderr)
        return 2
    new = sys.argv[1]

    current = _read_versions()
    if None in current.values() or len(set(current.values())) != 1:
        for k, v in current.items():
            print(f"  {k}: {v}", file=sys.stderr)
        print("ERROR: the three version files disagree already — fix drift by hand "
              "first, then re-run.", file=sys.stderr)
        return 1
    old = next(iter(current.values()))

    if tuple(map(int, new.split("."))) <= tuple(map(int, old.split("."))):
        print(f"ERROR: {new} is not greater than the current {old}. The plugin "
              f"upgrade gate only reinstalls on a strictly-greater version.", file=sys.stderr)
        return 1

    _sub_once(PYPROJECT, r'(?m)^(version\s*=\s*")' + re.escape(old) + r'(")', new)
    _sub_once(PLUGIN_JSON, r'("version"\s*:\s*")' + re.escape(old) + r'(")', new)
    _sub_once(PLUGIN_PY, r'(?m)^(\s*version\s*=\s*")' + re.escape(old) + r'(")', new)
    changelog_added = _scaffold_changelog(new)

    print(f"bumped {old} -> {new}")
    print("  pyproject.toml, plugin/plugin.json, plugin/plugin.py updated")
    if changelog_added:
        print(f"  CHANGELOG.md: scaffolded a `## [{new}]` section — fill in the prose")
    else:
        print(f"  CHANGELOG.md: `## [{new}]` section already present, left as-is")
    print("\nNext: edit the CHANGELOG prose, then open a PR titled "
          f"`v{new} — <subject>` (the subject must start with v{new} for auto-tag).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
